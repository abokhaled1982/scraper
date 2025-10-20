// content.js
// ==================================================
// Läuft automatisch auf Amazon, unterscheidet Produkt/Deals:
// - Produkt: sendet volles HTML als PRODUCT_HTML
// - Deals: startet Auto-Scroll und sendet volles HTML als PARSED_HTML
// + Erweiterung: Auf Produktseiten den SiteStripe-Button
//   ".amzn-ss-get-link-button" einmalig klicken und erst senden,
//   WENN der Shortlink wirklich im DOM sichtbar ist (sonst kein Send).
// ==================================================

(async () => {
  let autoScrollInterval = null;

  // --- Amazon-Erkennung ---
  const isAmazonHost = (h = location.hostname) => /^([a-z0-9-]+\.)*amazon\.[a-z.]+$/i.test(h);
  const isAmazonProductPath = (p = location.pathname) => /(\/dp\/[A-Z0-9]{10})(\/|$)/i.test(p) || /(\/gp\/product\/[A-Z0-9]{10})(\/|$)/i.test(p);
  const isAmazonDealsPath = (p = location.pathname) => /^\/deals\b/i.test(p);
  const isAmazonTargetPage = () => isAmazonHost() && (isAmazonProductPath() || isAmazonDealsPath());
  // --- Opener-Trigger (Query) ---
  const TRIGGER_PARAM = "ext_trigger";
  const TRIGGER_VALUE = "send_html";
  let triggerConsumedForUrl = new Set();
  function hasOpenerTrigger(href = location.href) {
    try {
      const u = new URL(href, location.origin);
      return u.searchParams.get(TRIGGER_PARAM) === TRIGGER_VALUE;
    } catch {
      return false;
    }
  }


  // --- Auto-Scroll nur für Deals ---
  function startAutoScroll() {
    if (autoScrollInterval) return;
    autoScrollInterval = setInterval(() => {
      window.scrollBy({ top: 800, behavior: "smooth" });
      const _jitter = 200 + Math.random() * 400; // noop, nur für Humanizer
    }, 8_000);
    console.log("[AutoScroll] started (deals)");
  }
  function stopAutoScroll() {
    if (!autoScrollInterval) return;
    clearInterval(autoScrollInterval);
    autoScrollInterval = null;
    console.log("[AutoScroll] stopped");
  }

  // --- Utils ---
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clickedOnceForUrl = new Set();
  const linkReadyForUrl = new Set();

  function isVisible(el) {
    if (!el) return false;
    const rects = el.getClientRects?.();
    return !!(el.offsetParent !== null || (rects && rects.length));
  }

  // Button suchen (auch in same-origin iframes)
  function findStripeButtonInDoc(doc) {
    if (!doc) return null;
    let btn = doc.querySelector("#amzn-ss-get-link-button, .amzn-ss-get-link-button");
    if (btn) return btn;

    const iframes = doc.querySelectorAll("iframe");
    for (const f of iframes) {
      try {
        const idoc = f.contentDocument || f.contentWindow?.document;
        if (!idoc) continue;
        btn = idoc.querySelector("#amzn-ss-get-link-button, .amzn-ss-get-link-button");
        if (btn) return btn;
      } catch {
        // cross-origin: ignorieren
      }
    }
    return null;
  }

  async function waitForStripeButton(timeoutMs = 12_000, intervalMs = 400) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const btn = findStripeButtonInDoc(document);
      if (btn) return btn;
      await sleep(intervalMs);
    }
    return null;
  }

  // Shortlink-Element finden (auch in same-origin iframes)
  function findAffiliateLinkInDoc(doc) {
    if (!doc) return null;
    const SEL = "#amzn-ss-text-shortlink-textarea, .amzn-ss-get-shortlink-text";
    let el = doc.querySelector(SEL);
    if (el) return el;

    const iframes = doc.querySelectorAll("iframe");
    for (const f of iframes) {
      try {
        const idoc = f.contentDocument || f.contentWindow?.document;
        if (!idoc) continue;
        el = idoc.querySelector(SEL);
        if (el) return el;
      } catch {
        // cross-origin: ignorieren
      }
    }
    return null;
  }

  function extractAffiliateValue(el) {
    return (el?.value || el?.textContent || "").trim();
  }

  async function waitForAffiliateLink(timeoutMs = 20_000, intervalMs = 250) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const el = findAffiliateLinkInDoc(document);
      if (el && isVisible(el)) {
        const val = extractAffiliateValue(el);
        if (/(https?:\/\/|amzn\.to|tag=)/i.test(val)) {
          return { el, val };
        }
      }
      await sleep(intervalMs);
    }
    return null;
  }

  // Klick + Link-Wartekette, setzt linkReadyForUrl wenn erfolgreich
  async function ensureStripeLinkReadyForCurrentProduct() {
    const urlKey = location.href.split("#")[0];

    if (linkReadyForUrl.has(urlKey)) return true;

    // Falls Button vorhanden, ggf. einmalig klicken
    if (!clickedOnceForUrl.has(urlKey)) {
      const btn = await waitForStripeButton(12_000, 400);
      if (btn) {
        console.log("[Stripe] click button");
        btn.click();
        clickedOnceForUrl.add(urlKey);
      } else {
        console.warn("[Stripe] button not found (timeout)");
      }
    }

    // Egal ob geklickt oder bereits offen: warte auf befüllten, sichtbaren Link
    const link = await waitForAffiliateLink(25_000, 250);
    if (link) {
      console.log("[Stripe] link ready:", link.val);
      linkReadyForUrl.add(urlKey);
      return true;
    }

    console.warn("[Stripe] link not ready -> skip sending this round");
    return false; // wichtig: NICHT senden
  }

  // --- Module-Pipeline (unverändert, ohne Warte-Logik fürs Senden) ---
  async function runPipeline() {
    try {
      const { loadEnabledModules } = await import(chrome.runtime.getURL("modules/registry.js"));
      const { makeDetachedDocumentFromPage } = await import(chrome.runtime.getURL("utils/dom.js"));

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
      throw e;
    }
  }

  // --- Auto-Run/Loop: jetzt mit harter Gate vor dem Senden ---
  let lastRunAt = 0;
  let lastRunUrl = "";
  const MIN_RUN_INTERVAL_MS = 60_000; // maximal alle 60s pro URL

  async function safeRun(reason = "auto") {
    if (!(isAmazonTargetPage() || hasOpenerTrigger())) {
      stopAutoScroll();
      return;
    }

    // Scrollen nur auf Deals-Seiten (dauerhaft)
    if (isAmazonDealsPath()) startAutoScroll();
    else stopAutoScroll();

    const now = Date.now();
    const href = location.href;
    if (href === lastRunUrl && now - lastRunAt < MIN_RUN_INTERVAL_MS) return;

    // kurze Warte, bis initiale Inhalte geladen sind
    await new Promise((r) => setTimeout(r, 500));
    if ("requestIdleCallback" in window) {
      await new Promise((r) => requestIdleCallback(r, { timeout: 1500 }));
    }

    try {
      console.log("[AutoRun] runPipeline ->", reason, href);

      // **NEU**: Bei Produktseiten Senden nur, wenn Shortlink ready ist.      // >>> Trigger-Shortcut: Wenn per Opener aufgerufen (ext_trigger=send_html),
      // dann verhalte dich wie Produktseite und sende SOFORT (ohne SiteStripe-Gate).
      if (hasOpenerTrigger(href)) {
        const key = href.split("#")[0];
        if (!triggerConsumedForUrl.has(key)) {
          await runPipeline();
          const html = document.documentElement.outerHTML;
          const payload = { url: href, html };
          chrome.runtime.sendMessage({ type: "PRODUCT_HTML", payload }, (resp) => console.log("[send] PRODUCT_HTML (trigger) resp:", resp));
          triggerConsumedForUrl.add(key);
          lastRunAt = Date.now();
          lastRunUrl = href;
        }
        return;
      }


      if (isAmazonProductPath()) {
        const ok = await ensureStripeLinkReadyForCurrentProduct();
        if (!ok) {
          // NICHT senden – nächste Runde abwarten (Interval/Mutation/History)
          return;
        }
      }

      // Optional: Module laufen lassen (Stats/Normalisierung)
      await runPipeline();

      // *** WICHTIG: immer das ROH-HTML der Seite senden (aber erst NACH Gate) ***
      const html = document.documentElement.outerHTML;
      const payload = { url: href, html };

      if (isAmazonProductPath()) {
        chrome.runtime.sendMessage({ type: "PRODUCT_HTML", payload }, (resp) => console.log("[send] PRODUCT_HTML resp:", resp));
      } else {
        chrome.runtime.sendMessage({ type: "PARSED_HTML", payload }, (resp) => console.log("[send] PARSED_HTML resp:", resp));
      }

      lastRunAt = Date.now();
      lastRunUrl = href;
    } catch (e) {
      console.warn("[AutoRun] run failed:", e);
    }
  }

  // 1) Direkt beim Laden
  if (isAmazonTargetPage() || hasOpenerTrigger()) safeRun("initial");

  // 2) Sanfter Dauerbetrieb (nur wenn Tab sichtbar)
  setInterval(() => {
    if (!document.hidden) safeRun("interval");
  }, Math.max(10_000, Math.floor(MIN_RUN_INTERVAL_MS / 2)));

  // 3) SPA/History-Änderungen
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

  // 4) DOM-Fallback
  const mo = new MutationObserver(() => {
    if ((isAmazonTargetPage() || hasOpenerTrigger()) && location.href !== lastRunUrl) safeRun("mutation");
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // 5) Bei Tab-Rückkehr erneut prüfen
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) safeRun("visibility");
  });

  // --- Legacy-Message-APIs ---
  chrome.runtime.onMessage.addListener((msg, _sender, send) => {
    if (msg?.type === "START_AUTOSCROLL") startAutoScroll();
    if (msg?.type === "STOP_AUTOSCROLL") stopAutoScroll();
    if (msg?.type === "RUN_SANITIZER") {
      runPipeline()
        .then((result) => send({ ok: true, result }))
        .catch((err) => send({ ok: false, error: String(err) }));
      return true;
    }
  });
})();
