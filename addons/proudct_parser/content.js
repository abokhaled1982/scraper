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
  // In-Flight-Lock: pro urlKey wird ensureStripeLinkReadyForCurrentProduct()
  // serialisiert. Verhindert, dass parallele Trigger (interval/mutation/visibility)
  // sich gegenseitig das Clipboard zerschießen.
  const inFlightStripeByUrl = new Map(); // urlKey → Promise<string|null>

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
    // amzn.to-Shortlink oder Amazon-URL mit Affiliate-Parametern
    if (/^https?:\/\/amzn\.to\/[A-Za-z0-9]+/i.test(link)) return true;
    if (/amazon\.[a-z.]+\/.*(?:tag=|linkCode=)/i.test(link)) return true;
    return false;
  }

  /**
   * Fordert vom Background-Script an, den aktuellen Tab + sein Fenster in
   * den Vordergrund zu bringen. Das ist der robusteste Weg gegen
   * "Document is not focused" – analog zum Tab-Focus-Guard im Facebook-Addon
   * (chrome.tabs.update + chrome.windows.update).
   *
   * @returns {Promise<boolean>} true, wenn der Background bestätigt hat.
   */
  function requestTabFocus() {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: "FOCUS_TAB" }, (resp) => {
          if (chrome.runtime.lastError) {
            console.warn(
              `[Focus] FOCUS_TAB runtime error: ${chrome.runtime.lastError.message}`
            );
            resolve(false);
            return;
          }
          if (!resp?.ok) {
            console.warn(`[Focus] FOCUS_TAB rejected: ${resp?.error || "unknown"}`);
            resolve(false);
            return;
          }
          resolve(true);
        });
      } catch (e) {
        const msg = e && e.message ? e.message : String(e);
        console.warn(`[Focus] FOCUS_TAB threw: ${msg}`);
        resolve(false);
      }
    });
  }

  /**
   * Versucht, dem Dokument den Fokus zu geben, damit
   * navigator.clipboard.readText() nicht mit "Document is not focused" abbricht.
   * Best-effort: in Hintergrund-Tabs ist das nicht garantiert, aber im aktiven
   * Tab (auch ohne Klick) hilft es zuverlässig.
   */
  function ensureDocumentFocused() {
    try {
      window.focus();
    } catch {
      /* noop */
    }
    try {
      if (document.body && typeof document.body.focus === "function") {
        document.body.focus({ preventScroll: true });
      }
    } catch {
      /* noop */
    }
  }

  /**
   * Fallback-Read über eine versteckte <textarea> + execCommand("paste").
   * Funktioniert in Content-Scripts auch dann, wenn die Async-Clipboard-API
   * mit "Document is not focused" abbricht (z. B. wenn DevTools den Fokus hat).
   *
   * @returns {{ok: boolean, value: string, error: string|null}}
   */
  function readClipboardViaExecCommand() {
    let ta = null;
    try {
      ta = document.createElement("textarea");
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      ta.style.left = "-1000px";
      ta.style.opacity = "0";
      ta.style.pointerEvents = "none";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();

      const ok = document.execCommand("paste");
      const value = (ta.value || "").trim();
      if (!ok && !value) {
        return { ok: false, value: "", error: "execCommand('paste') returned false" };
      }
      return { ok: true, value, error: null };
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      return { ok: false, value: "", error: msg };
    } finally {
      if (ta && ta.parentNode) ta.parentNode.removeChild(ta);
    }
  }

  /**
   * Liest den Clipboard-Inhalt sicher aus. Loggt jeden Versuch inklusive
   * Vorschau, damit man auf jedem Rechner sehen kann, was tatsächlich
   * zurückkommt. Liefert immer ein Objekt – nie eine Exception.
   *
   * Strategie (analog zum Facebook-Addon-Tab-Focus-Guard):
   *   1) Async-API (navigator.clipboard.readText) nach Fokus-Anforderung.
   *   2) Bei "Document is not focused": Background bittet, den Tab + sein
   *      Fenster zu aktivieren (chrome.tabs.update / chrome.windows.update),
   *      danach erneut versuchen.
   *   3) Letzter Fallback: execCommand("paste") in eine versteckte Textarea.
   *
   * @param {number} attempt - 1-basierter Versuchszähler (nur fürs Log).
   * @returns {Promise<{ok: boolean, value: string, error: string|null, source: string}>}
   */
  async function readClipboardSafe(attempt) {
    ensureDocumentFocused();

    // 1) Erster Async-API-Versuch
    try {
      const raw = await navigator.clipboard.readText();
      const value = (raw || "").trim();
      const preview = value.length > 120 ? value.slice(0, 120) + "…" : value;
      console.log(
        `[Clipboard] #${attempt} async ok (focused=${document.hasFocus()}) – len=${value.length}, value="${preview}"`
      );
      return { ok: true, value, error: null, source: "async" };
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      const isFocusError = /not focused|focus/i.test(msg);
      console.warn(
        `[Clipboard] #${attempt} async failed (focused=${document.hasFocus()}): ${msg}`
      );

      // 2) Bei Fokus-Fehler: Tab + Fenster über Background aktiv setzen und retry
      if (isFocusError) {
        const focused = await requestTabFocus();
        console.log(`[Clipboard] #${attempt} requested tab focus -> ${focused}`);
        ensureDocumentFocused();
        // Kurz warten, damit der Fokuswechsel im Renderer ankommt
        await sleep(120);
        try {
          const raw2 = await navigator.clipboard.readText();
          const value2 = (raw2 || "").trim();
          const preview2 = value2.length > 120 ? value2.slice(0, 120) + "…" : value2;
          console.log(
            `[Clipboard] #${attempt} async ok after refocus (focused=${document.hasFocus()}) – len=${value2.length}, value="${preview2}"`
          );
          return { ok: true, value: value2, error: null, source: "async-refocus" };
        } catch (e2) {
          const msg2 = e2 && e2.message ? e2.message : String(e2);
          console.warn(
            `[Clipboard] #${attempt} async still failed after refocus: ${msg2} – trying execCommand fallback`
          );
        }
      }
    }

    // 3) Letzter Fallback: execCommand("paste")
    const fb = readClipboardViaExecCommand();
    if (fb.ok) {
      const preview = fb.value.length > 120 ? fb.value.slice(0, 120) + "…" : fb.value;
      console.log(
        `[Clipboard] #${attempt} execCommand ok – len=${fb.value.length}, value="${preview}"`
      );
      return { ok: true, value: fb.value, error: null, source: "execCommand" };
    }

    console.warn(`[Clipboard] #${attempt} execCommand failed: ${fb.error}`);
    return { ok: false, value: "", error: fb.error, source: "none" };
  }

  /**
   * Pollt in festen Intervallen sowohl Clipboard als auch DOM nach einem
   * plausiblen Affiliate-Link. Bewusst als for-Schleife mit festem
   * Versuchsbudget – einfacher zu debuggen als eine offene while-Schleife.
   *
   * Statt eines Sentinels (der das Clipboard kaputt machen würde, wenn
   * Amazons Copy-Click kein neues writeText auslöst) merken wir uns den
   * Anfangsinhalt und akzeptieren nur Links, die sich davon unterscheiden.
   *
   * @param {object} opts
   * @param {number} opts.attempts - Anzahl der Versuche.
   * @param {number} opts.intervalMs - Pause zwischen den Versuchen.
   * @param {string} opts.baselineClipboard - Inhalt des Clipboards vor dem
   *   Copy-Klick. Wird verworfen, wenn er identisch wiederkommt.
   * @returns {Promise<string|null>}
   */
  async function pollForAffiliateLink({ attempts, intervalMs, baselineClipboard }) {
    for (let i = 1; i <= attempts; i++) {
      // 1) Clipboard prüfen
      const clip = await readClipboardSafe(i);
      if (clip.ok) {
        const isBaseline = clip.value === baselineClipboard;
        if (!isBaseline && isPlausibleAffiliateLink(clip.value)) {
          console.log(`[Poll] link via clipboard on attempt #${i}: ${clip.value}`);
          return clip.value;
        }
      }

      // 2) Parallel DOM probieren – Amazon reicht den Link manchmal nach
      const fromDom = readShortlinkFromDOM();
      if (isPlausibleAffiliateLink(fromDom)) {
        console.log(`[Poll] link via DOM on attempt #${i}: ${fromDom}`);
        return fromDom;
      }

      // 3) Warten vor nächstem Versuch (außer beim letzten Durchlauf)
      if (i < attempts) await sleep(intervalMs);
    }

    console.warn(
      `[Poll] no affiliate link after ${attempts} attempts (${attempts * intervalMs} ms)`
    );
    return null;
  }

  /**
   * Öffnet das SiteStripe-Popover, klickt "Affiliate-Link kopieren" und
   * liefert den aktuellen Affiliate-Link. Strategie:
   *   1) DOM zuerst (Popover-Input/Anchor) – immer korrekt für das aktuelle Produkt.
   *   2) Clipboard-Baseline merken, Copy klicken, auf Änderung warten.
   *   3) Stale-Detection: identischer Link für eine *andere* URL → verworfen.
   *
   * WICHTIG: Pro urlKey wird die Funktion serialisiert (Promise-Lock), damit
   * parallele Trigger (interval/mutation/visibility) sich nicht gegenseitig
   * das Clipboard zerschießen.
   *
   * @returns {Promise<string|null>}
   */
  async function ensureStripeLinkReadyForCurrentProduct() {
    const urlKey = location.href.split("#")[0];
    if (linkReadyForUrl.has(urlKey)) return linkReadyForUrl.get(urlKey);

    // In-Flight-Lock: gibt es bereits einen laufenden Resolve für diese URL,
    // hängen wir uns einfach an dessen Promise. Verhindert Race Conditions
    // beim Clipboard und doppelte Copy-Button-Klicks.
    if (inFlightStripeByUrl.has(urlKey)) {
      console.log("[Stripe] join in-flight resolver for", urlKey);
      return inFlightStripeByUrl.get(urlKey);
    }

    const promise = (async () => {
      try {
        return await _resolveStripeLink(urlKey);
      } finally {
        inFlightStripeByUrl.delete(urlKey);
      }
    })();

    inFlightStripeByUrl.set(urlKey, promise);
    return promise;
  }

  /**
   * Tatsächliche Resolve-Implementierung. Wird nur einmal pro urlKey gleichzeitig
   * ausgeführt (siehe ensureStripeLinkReadyForCurrentProduct-Lock).
   */
  async function _resolveStripeLink(urlKey) {
    // Schritt 0: Tab proaktiv in den Vordergrund holen.
    // Wenn der Opener mehrere Tabs hintereinander öffnet, überlagern sich
    // diese sonst und ein Hintergrund-Tab kann SiteStripe weder zuverlässig
    // öffnen noch die Zwischenablage lesen. Wir fordern daher *vor* jedem
    // Klick, dass der eigene Tab + sein Fenster aktiv werden.
    const focused = await requestTabFocus();
    console.log(`[Stripe] proactive tab focus -> ${focused} (url=${urlKey})`);
    ensureDocumentFocused();
    await sleep(150);

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

    // Schritt 3: Baseline des Clipboards merken (statt Sentinel zu schreiben).
    // Vorteil: wir zerstören keinen evtl. bereits korrekt gesetzten Affiliate-Link
    // und können trotzdem erkennen, ob sich der Inhalt nach dem Copy-Klick ändert.
    let baselineClipboard = "";
    {
      const baseline = await readClipboardSafe(0);
      if (baseline.ok) {
        baselineClipboard = baseline.value;
        // Falls die Baseline bereits ein plausibler Link für DIESES Produkt ist
        // (z. B. weil ein vorheriger Lauf erfolgreich war), direkt verwenden.
        if (
          isPlausibleAffiliateLink(baselineClipboard) &&
          (!lastDeliveredUrlKey || lastDeliveredUrlKey === urlKey)
        ) {
          console.log("[Stripe] baseline clipboard already valid:", baselineClipboard);
          linkReadyForUrl.set(urlKey, baselineClipboard);
          lastDeliveredLink = baselineClipboard;
          lastDeliveredUrlKey = urlKey;
          return baselineClipboard;
        }
      }
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

    // Vor dem Copy-Klick erneut Fokus erzwingen. Sonst kann ein parallel
    // geöffneter Tab zwischenzeitlich den Vordergrund übernommen haben,
    // wodurch navigator.clipboard.writeText() von Amazon ins Leere läuft.
    const focusedBeforeCopy = await requestTabFocus();
    console.log(`[Stripe] refocus before copy -> ${focusedBeforeCopy}`);
    ensureDocumentFocused();
    await sleep(120);

    console.log("[Stripe] clicking copy button");
    copyBtn.click();

    // Schritt 5: Clipboard + DOM pollen, bis ein plausibler Link gefunden wird,
    // der NICHT dem Baseline-Inhalt entspricht (max. ~10 s, feste Intervalle).
    const link = await pollForAffiliateLink({
      attempts: 20,
      intervalMs: 500,
      baselineClipboard,
    });

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