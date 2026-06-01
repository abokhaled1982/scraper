// content.js
// ==================================================
// Logik für Amazon-Seiten (Produkt- oder Deals-Seiten).
// - Produktseiten: Versendet HTML, wartet auf SiteStripe-Link, und schließt den Tab bei Erfolg.
// - Deals-Seiten: Startet Auto-Scroll, versendet HTML regelmäßig, aber schließt den Tab NICHT.
// - Opener-Trigger: Erzwingt Senden, wartet auf SiteStripe-Link (falls Produktseite) und schließt den Tab.
// ==================================================

(async () => {
  // --- Konstanten und Konfiguration ---
  const TRIGGER_PARAM = "ext_trigger";
  const TRIGGER_VALUE = "send_html";
  const MIN_RUN_INTERVAL_MS = 60_000; // Max. alle 60s pro URL
  const STRIPE_BUTTON_SEL = "#amzn-ss-get-link-button, .amzn-ss-get-link-button";
  const COPY_BTN_SEL = "#amzn-ss-copy-affiliate-link-btn-announce";

  // --- Zustandsvariablen ---
  let autoScrollInterval = null;
  let lastRunAt = 0;
  let lastRunUrl = "";
  const triggerConsumedForUrl = new Set();
  const clickedOnceForUrl = new Set();
  const linkReadyForUrl = new Map(); // urlKey → affiliateLink-String
  let lastDeliveredLink = null;       // letzter erfolgreich gelieferter Link (zur Stale-Erkennung)
  let lastDeliveredUrlKey = null;     // urlKey, zu dem lastDeliveredLink gehört
  // Sentinel, mit dem das Clipboard vor dem Copy-Klick überschrieben wird,
  // um stale Inhalte sicher zu erkennen.
  const CLIPBOARD_SENTINEL = "__amzn_ss_pending__";

  // --- Dienstprogramme (Utils) ---

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /** Überprüft, ob ein Element sichtbar ist (hat einen Offset-Parent oder Client-Rechtecke). */
  function isVisible(el) {
    if (!el) return false;
    const rects = el.getClientRects?.();
    return !!(el.offsetParent !== null || (rects && rects.length));
  }

  /**
   * Sucht einen Selektor im Hauptdokument und in allen same-origin iFrames.
   * @param {Document} doc - Das zu durchsuchende Dokument.
   * @param {string} selector - Der CSS-Selektor.
   * @returns {Element|null} Das gefundene Element oder null.
   */
  function findElementInDocAndIframes(doc, selector) {
    if (!doc) return null;
    let el = doc.querySelector(selector);
    if (el) return el;

    const iframes = doc.querySelectorAll("iframe");
    for (const f of iframes) {
      try {
        const idoc = f.contentDocument || f.contentWindow?.document;
        if (!idoc) continue;
        el = idoc.querySelector(selector);
        if (el) return el;
      } catch {
        // cross-origin: ignorieren
      }
    }
    return null;
  }

  // --- Amazon-Erkennung ---

  const isAmazonHost = (h = location.hostname) => /^([a-z0-9-]+\.)*amazon\.[a-z.]+$/i.test(h);
  const isAmazonProductPath = (p = location.pathname) => /(\/dp\/[A-Z0-9]{10})(\/|$)/i.test(p) || /(\/gp\/product\/[A-Z0-9]{10})(\/|$)/i.test(p);
  const isAmazonDealsPath = (p = location.pathname) => /^\/(deals|gp\/angebote)/i.test(p);
  const isAmazonTargetPage = () => isAmazonHost() && (isAmazonProductPath() || isAmazonDealsPath());

  function hasOpenerTrigger(href = location.href) {
    try {
      const u = new URL(href, location.origin);
      return u.searchParams.get(TRIGGER_PARAM) === TRIGGER_VALUE;
    } catch {
      return false;
    }
  }

  // --- Auto-Scroll (nur für Deals) ---

  function startAutoScroll() {
     const interval = 30_000 + (200 + Math.random() * 400);
    if (autoScrollInterval) return;
    autoScrollInterval = setInterval(() => {
      // Humanizer: 800px scrollen mit leicht variablem Intervall
     
      window.scrollBy({ top: 800, behavior: "smooth" });
    }, interval); // Intervall ist 8s + jitter
    console.log("[AutoScroll] started (deals)");
  }

  function stopAutoScroll() {
    if (!autoScrollInterval) return;
    clearInterval(autoScrollInterval);
    autoScrollInterval = null;
    console.log("[AutoScroll] stopped");
  }

  // --- SiteStripe-Link-Logik (für Produktseiten) ---

  /** Wartet auf den SiteStripe-Button. */
  async function waitForStripeButton(timeoutMs = 12_000, intervalMs = 400) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const btn = findElementInDocAndIframes(document, STRIPE_BUTTON_SEL);
      if (btn) return btn;
      await sleep(intervalMs);
    }
    return null;
  }

  /**
   * Versucht den Shortlink direkt aus dem SiteStripe-Popover-DOM zu lesen.
   * Sucht in allen <input>/<textarea>/<a>-Elementen nach einem amzn.to- bzw.
   * Amazon-Affiliate-Link. Wesentlich verlässlicher als das Clipboard, weil
   * der Wert immer zum aktuellen Produkt gehört.
   */
  function readShortlinkFromDOM() {
    const candidates = [];
    // Bekannte / wahrscheinliche Container
    const scopes = [
      document,
      ...document.querySelectorAll(
        "#amzn-ss-text-shortlink-textarea, [id^='amzn-ss-text-shortlink'], [id*='shortlink'], [class*='shortlink'], #amzn-ss-text-shortlink-textarea-container, .amzn-ss-text-link-container"
      ),
    ];
    for (const scope of scopes) {
      if (!scope) continue;
      // 1) input/textarea-Werte
      const fields = scope.querySelectorAll
        ? scope.querySelectorAll("input, textarea")
        : [];
      for (const f of fields) {
        const v = (f.value || f.textContent || "").trim();
        if (v) candidates.push(v);
      }
      // 2) Anchor-Hrefs (manche neuen Varianten zeigen den Link als <a>)
      const anchors = scope.querySelectorAll
        ? scope.querySelectorAll("a[href]")
        : [];
      for (const a of anchors) {
        const v = (a.getAttribute("href") || "").trim();
        if (v) candidates.push(v);
      }
      // 3) sichtbarer Text des Scopes selbst
      if (scope !== document) {
        const t = (scope.textContent || "").trim();
        if (t) candidates.push(t);
      }
    }
    // Erstes plausibles Vorkommen extrahieren
    const rx = /https?:\/\/(?:amzn\.to\/[A-Za-z0-9]+|(?:www\.)?amazon\.[a-z.]+\/[^\s"'<>]*(?:tag=|linkCode=)[^\s"'<>]*)/i;
    for (const c of candidates) {
      const m = c.match(rx);
      if (m) return m[0];
    }
    return null;
  }

  /** Validiert einen Affiliate-Link grob. */
  function isPlausibleAffiliateLink(link) {
    if (!link || typeof link !== "string") return false;
    if (!/^https?:\/\//i.test(link)) return false;
    if (link === CLIPBOARD_SENTINEL) return false;
    // amzn.to-Shortlink oder Amazon-URL mit Affiliate-Parametern
    if (/^https?:\/\/amzn\.to\/[A-Za-z0-9]+/i.test(link)) return true;
    if (/amazon\.[a-z.]+\/.*(?:tag=|linkCode=)/i.test(link)) return true;
    return false;
  }

  /**
   * Öffnet das SiteStripe-Popover, klickt "Affiliate-Link kopieren" und
   * liefert den aktuellen Affiliate-Link. Strategie:
   *   1) DOM zuerst (Popover-Input/Anchor) – immer korrekt für das aktuelle Produkt.
   *   2) Clipboard als Fallback, aber vorher mit Sentinel überschrieben und
   *      anschließend gepollt, bis sich der Inhalt ändert.
   *   3) Stale-Detection: identischer Link für eine *andere* URL → verworfen.
   * @returns {Promise<string|null>}
   */
  async function ensureStripeLinkReadyForCurrentProduct() {
    const urlKey = location.href.split("#")[0];
    if (linkReadyForUrl.has(urlKey)) return linkReadyForUrl.get(urlKey);

    // Schritt 1: SiteStripe-Popover öffnen
    if (!clickedOnceForUrl.has(urlKey)) {
      const btn = await waitForStripeButton();
      if (!btn) {
        console.warn("[Stripe] button not found (timeout)");
        return null;
      }
      console.log("[Stripe] click stripe button to open popover");
      btn.click();
      clickedOnceForUrl.add(urlKey);
    }

    // Schritt 2: Versuche zuerst, den Link direkt aus dem geöffneten Popover
    // zu lesen – das ist deterministisch und vermeidet Clipboard-Probleme.
    {
      const domStart = Date.now();
      while (Date.now() - domStart < 8_000) {
        const fromDom = readShortlinkFromDOM();
        if (isPlausibleAffiliateLink(fromDom)) {
          console.log("[Stripe] affiliate link from DOM:", fromDom);
          linkReadyForUrl.set(urlKey, fromDom);
          lastDeliveredLink = fromDom;
          lastDeliveredUrlKey = urlKey;
          return fromDom;
        }
        await sleep(300);
      }
    }

    // Schritt 3: Clipboard vor dem Copy-Klick mit Sentinel überschreiben,
    // damit wir stale Inhalte sicher erkennen können.
    let clipboardWritable = true;
    try {
      await navigator.clipboard.writeText(CLIPBOARD_SENTINEL);
    } catch (e) {
      clipboardWritable = false;
      console.warn("[Stripe] clipboard write (sentinel) failed:", e);
    }

    // Schritt 4: "Affiliate-Link kopieren"-Button im Dialog abwarten und klicken
    const start = Date.now();
    let copyBtn = null;
    while (Date.now() - start < 10_000) {
      copyBtn = document.querySelector(COPY_BTN_SEL);
      if (copyBtn) break;
      await sleep(300);
    }
    if (!copyBtn) {
      console.warn("[Stripe] copy button not found in dialog");
      return null;
    }
    console.log("[Stripe] clicking copy button");
    copyBtn.click();

    // Schritt 5: Clipboard pollen, bis sich der Inhalt vom Sentinel
    // unterscheidet bzw. ein plausibler Link erscheint (max. 10 s).
    const pollStart = Date.now();
    let link = null;
    while (Date.now() - pollStart < 10_000) {
      try {
        const current = (await navigator.clipboard.readText()) || "";
        if (
          isPlausibleAffiliateLink(current) &&
          (clipboardWritable ? current !== CLIPBOARD_SENTINEL : true)
        ) {
          link = current.trim();
          break;
        }
      } catch (e) {
        // readText kann ohne Fokus scheitern – weiter versuchen
      }
      // parallel weiter DOM probieren – falls Amazon den Link nachreicht
      const fromDom = readShortlinkFromDOM();
      if (isPlausibleAffiliateLink(fromDom)) {
        link = fromDom;
        break;
      }
      await sleep(500);
    }

    if (!link) {
      console.warn("[Stripe] no valid affiliate link found (clipboard+DOM)");
      return null;
    }

    // Schritt 6: Stale-Detection – wenn derselbe Link wie für eine andere
    // URL geliefert wird, ist das mit hoher Wahrscheinlichkeit Müll.
    if (
      lastDeliveredLink &&
      link === lastDeliveredLink &&
      lastDeliveredUrlKey &&
      lastDeliveredUrlKey !== urlKey
    ) {
      console.warn(
        "[Stripe] discarding stale link (identical to previous product):",
        link
      );
      return null;
    }

    console.log("[Stripe] affiliate link resolved:", link);
    linkReadyForUrl.set(urlKey, link);
    lastDeliveredLink = link;
    lastDeliveredUrlKey = urlKey;
    return link;
  }

  // --- Pipeline (Module-Ausführung) ---

  async function runPipeline() {
    try {
      // Dynamische Imports für bessere Kapselung und geringere Ladezeit
      // HINWEIS: Ersetze dies durch deine tatsächliche Importlogik
      const loadEnabledModules = async () => []; // Platzhalter
      const makeDetachedDocumentFromPage = () => document; // Platzhalter

      let modules = await loadEnabledModules();
      let doc = makeDetachedDocumentFromPage();
      const ctx = { stats: {}, output: null };

      for (const mod of modules) {
        try {
          const res = await mod.run(doc, ctx);
          doc = res.doc || doc;
        } catch (e) {
          console.warn(`[Module error] ${mod.meta?.id || "unknown"}`, e);
          ctx.stats[`error_${mod.meta?.id || "unknown"}`] = String(e);
        }
      }
      return { stats: ctx.stats, output: ctx.output };
    } catch (e) {
      console.warn("[content] pipeline failed:", e);
      // Den Fehler werfen, um die aufrufende Funktion zu benachrichtigen
      throw e;
    }
  }

  // --- Haupt-Sende-Logik ---

  /**
   * Sendet das aktuelle HTML an den Background-Skript.
   * Schließt den Tab NUR, wenn der Typ "PRODUCT_HTML" ist und die Übertragung erfolgreich war.
   */
  function sendHtml(type, href, html, affiliateLink = null) {
    const payload = { url: href, html };
    if (affiliateLink) payload.affiliateLink = affiliateLink;
    chrome.runtime.sendMessage({ type, payload }, (resp) => {
      // Fehlerbehandlung für Sende-Antworten
      if (chrome.runtime.lastError) {
        console.error(`[send] ${type} failed (runtime error):`, chrome.runtime.lastError.message);
      } else {
        console.log(`[send] ${type} resp:`, resp);

        // Tab nur schließen, wenn es eine Produktseite ist UND der Sendevorgang erfolgreich war
        if (type === "PRODUCT_HTML" && resp?.ok === true && resp?.id) {
          console.log(`[send] PRODUCT_HTML successful, closing tab...`);
          // Sendet Nachricht an Background, um den aktuellen Tab zu schließen
          chrome.runtime.sendMessage({ type: "CLOSE_CURRENT_TAB" });
        }
      }
    });
  }

  /** Hauptfunktion, die die Logik für eine Amazon-Seite ausführt. */
  async function safeRun(reason = "auto") {
    const href = location.href;

    // Nur ausführen, wenn es eine relevante Amazon-Seite ist oder der Trigger gesetzt ist
    if (!(isAmazonTargetPage() || hasOpenerTrigger())) {
      stopAutoScroll();
      return;
    }

    // Auto-Scroll starten/stoppen
    if (isAmazonDealsPath()) startAutoScroll();
    else stopAutoScroll();

    const now = Date.now();

    // Deduplizierung: maximal alle 60s pro URL (außer beim ersten Mal oder bei Trigger)
    if (href === lastRunUrl && now - lastRunAt < MIN_RUN_INTERVAL_MS) return;

    // Kurze Wartezeit, bis initiale Inhalte geladen sind (verbessert die Robustheit)
    await sleep(500);
    if ("requestIdleCallback" in window) {
      await new Promise((r) => requestIdleCallback(r, { timeout: 1500 }));
    }

    console.log("[AutoRun] runPipeline ->", reason, href);

    try {
      // 1. Opener-Trigger-Shortcut (SENDEN OHNE GATE)
      if (hasOpenerTrigger(href)) {
        const key = href.split("#")[0];
        if (!triggerConsumedForUrl.has(key)) {
          // NEU: Nur auf den SiteStripe-Link warten, wenn es eine Amazon Produktseite ist.
          let affiliateLinkOpener = null;
          if (isAmazonProductPath()) {
            affiliateLinkOpener = await ensureStripeLinkReadyForCurrentProduct();
            if (!affiliateLinkOpener) {
              console.warn("[Opener-Trigger] Link not ready, skipping send.");
              return;
            }
          }

          await runPipeline();
          sendHtml("PRODUCT_HTML", href, document.documentElement.outerHTML, affiliateLinkOpener);
          triggerConsumedForUrl.add(key);
          lastRunAt = Date.now();
          lastRunUrl = href;
        }
        return;
      }

      // 2. Produktseiten-Gate (WARTEN AUF SHORTLINK)
      // WARTE NUR auf Shortlink, wenn es eine Produktseite ist und KEIN Opener-Trigger vorliegt.
      let affiliateLink = null;
      if (isAmazonProductPath()) {
        affiliateLink = await ensureStripeLinkReadyForCurrentProduct();
        if (!affiliateLink) {
          // Link nicht bereit -> NICHT senden – nächste Runde abwarten
          return;
        }
      }

      // Deals-Seiten (isAmazonDealsPath()) umgehen die Wartezeit und senden direkt.

      // 3. Modul-Pipeline ausführen (z.B. für Stats/Normalisierung)
      await runPipeline();

      // 4. HTML senden (JETZT SICHER)
      // Wähle den korrekten Nachrichtentyp: Nur Produktseiten erhalten den "Schließen"-Mechanismus.
      const html = document.documentElement.outerHTML;
      const messageType = isAmazonProductPath() ? "PRODUCT_HTML" : "PARSED_HTML";
      sendHtml(messageType, href, html, affiliateLink);

      lastRunAt = Date.now();
      lastRunUrl = href;
    } catch (e) {
      console.warn("[AutoRun] run failed:", e);
    }
  }

  // --- Start-Logik (Events) ---

  // 1) Direkt beim Laden
  if (isAmazonTargetPage() || hasOpenerTrigger()) safeRun("initial");

  // 2) Sanfter Dauerbetrieb (nur wenn Tab sichtbar)
  setInterval(() => {
    if (!document.hidden) safeRun("interval");
  }, Math.max(10_000, Math.floor(MIN_RUN_INTERVAL_MS / 2)));

  // 3) SPA/History-Änderungen (robustes Hooking)
  (function hookHistory() {
    const _ps = history.pushState,
      _rs = history.replaceState;
    history.pushState = function (...a) {
      const r = _ps.apply(this, a);
      queueMicrotask(() => safeRun("pushState"));
      return r;
    };
    history.replaceState = function (...a) {
      const r = _rs.apply(this, a);
      queueMicrotask(() => safeRun("replaceState"));
      return r;
    };
    window.addEventListener("popstate", () => safeRun("popstate"));
  })();

  // 4) DOM-Fallback (MutationObserver)
  const mo = new MutationObserver(() => {
    // Vermeidet unnötige Läufe bei kleinen DOM-Änderungen auf der gleichen URL
    if ((isAmazonTargetPage() || hasOpenerTrigger()) && location.href !== lastRunUrl) safeRun("mutation");
  });
  // Beobachten des gesamten Dokuments auf tiefgreifende Änderungen
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // 5) Bei Tab-Rückkehr erneut prüfen
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) safeRun("visibility");
  });

  // --- Message Listener (Externe Steuerung) ---

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === "START_AUTOSCROLL") {
      startAutoScroll();
      sendResponse({ ok: true });
    } else if (msg?.type === "STOP_AUTOSCROLL") {
      stopAutoScroll();
      sendResponse({ ok: true });
    } else if (msg?.type === "RUN_SANITIZER") {
      // Die Pipeline als "Sanitizer" ausführen und Ergebnis zurücksenden
      runPipeline()
        .then((result) => sendResponse({ ok: true, result }))
        .catch((err) => sendResponse({ ok: false, error: String(err) }));
      return true; // Asynchrone Antwort
    }
  });
})();