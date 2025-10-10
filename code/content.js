// content.js
// ==================================================
// Läuft automatisch auf Amazon, unterscheidet Produkt/Deals:
// - Produkt: sendet volles HTML als PRODUCT_HTML
// - Deals: startet Auto-Scroll und sendet volles HTML als PARSED_HTML
// Keine User-Events nötig; wiederholt sich schonend und reagiert auf SPA.
// ==================================================

(async () => {
  let autoScrollInterval = null;

  // --- Amazon-Erkennung ---
  const isAmazonHost = (h = location.hostname) => /^([a-z0-9-]+\.)*amazon\.[a-z.]+$/i.test(h);
  const isAmazonProductPath = (p = location.pathname) =>
    /(\/dp\/[A-Z0-9]{10})(\/|$)/i.test(p) || /(\/gp\/product\/[A-Z0-9]{10})(\/|$)/i.test(p);
  const isAmazonDealsPath = (p = location.pathname) => /^\/deals\b/i.test(p);
  const isAmazonTargetPage = () => isAmazonHost() && (isAmazonProductPath() || isAmazonDealsPath());

  // --- Auto-Scroll nur für Deals ---
  function startAutoScroll() {
    if (autoScrollInterval) return;
    autoScrollInterval = setInterval(() => {
      window.scrollBy({ top: 800, behavior: "smooth" });
      // leichte zufallswarte, damit es natürlicher wirkt
      const jitter = 200 + Math.random() * 400;
    }, 8_000);
    console.log("[AutoScroll] started (deals)");
  }
  function stopAutoScroll() {
    if (!autoScrollInterval) return;
    clearInterval(autoScrollInterval);
    autoScrollInterval = null;
    console.log("[AutoScroll] stopped");
  }

  // --- Deine Pipeline (unverändert aufgerufen) ---
  async function runPipeline() {
    const { loadEnabledModules } = await import(chrome.runtime.getURL("modules/registry.js"));
    const { makeDetachedDocumentFromPage } = await import(chrome.runtime.getURL("utils/dom.js"));

    let modules;
    try {
      modules = await loadEnabledModules();
    } catch (e) {
      console.warn("[content] loadEnabledModules failed:", e);
      throw e;
    }

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
  }

  // --- Auto-Run/Loop: immer volles HTML senden; Deals scrollen ---
  let lastRunAt = 0;
  let lastRunUrl = "";
  const MIN_RUN_INTERVAL_MS = 60_000; // maximal alle 60s pro URL

  async function safeRun(reason = "auto") {
    if (!isAmazonTargetPage()) {
      stopAutoScroll();
      return;
    }

    // Scrollen nur auf Deals-Seiten (dauerhaft)
    if (isAmazonDealsPath()) startAutoScroll(); else stopAutoScroll();

    const now = Date.now();
    const href = location.href;
    if (href === lastRunUrl && now - lastRunAt < MIN_RUN_INTERVAL_MS) return;

    // kurze Warte, bis initiale Inhalte geladen sind
    await new Promise(r => setTimeout(r, 500));
    if ("requestIdleCallback" in window) {
      await new Promise(r => requestIdleCallback(r, { timeout: 1500 }));
    }

    try {
      console.log("[AutoRun] runPipeline ->", reason, href);
      // Optional: läuft deine Module-Pipeline (z.B. für Stats/Normalisierung)
      await runPipeline();

      // *** WICHTIG: immer das ROH-HTML der Seite senden ***
      const html = document.documentElement.outerHTML;
      const payload = { url: href, html };

      if (isAmazonProductPath()) {
        chrome.runtime.sendMessage({ type: "PRODUCT_HTML", payload }, (resp) =>
          console.log("[send] PRODUCT_HTML resp:", resp)
        );
      } else {
        chrome.runtime.sendMessage({ type: "PARSED_HTML", payload }, (resp) =>
          console.log("[send] PARSED_HTML resp:", resp)
        );
      }

      lastRunAt = Date.now();
      lastRunUrl = href;
    } catch (e) {
      console.warn("[AutoRun] run failed:", e);
    }
  }

  // 1) Direkt beim Laden (ohne User-Aktion)
  if (isAmazonTargetPage()) safeRun("initial");

  // 2) Sanfter Dauerbetrieb (nur wenn Tab sichtbar)
  setInterval(() => {
    if (!document.hidden) safeRun("interval");
  }, Math.max(10_000, Math.floor(MIN_RUN_INTERVAL_MS / 2)));

  // 3) SPA/History-Änderungen
  (function hookHistory() {
    const _ps = history.pushState, _rs = history.replaceState;
    history.pushState = function(...a){ const r=_ps.apply(this,a); queueMicrotask(()=>safeRun("pushState")); return r; };
    history.replaceState = function(...a){ const r=_rs.apply(this,a); queueMicrotask(()=>safeRun("replaceState")); return r; };
    window.addEventListener("popstate", () => safeRun("popstate"));
  })();

  // 4) DOM-Fallback (falls URL ohne history wechselt)
  const mo = new MutationObserver(() => {
    if (isAmazonTargetPage() && location.href !== lastRunUrl) safeRun("mutation");
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // 5) Bei Tab-Rückkehr erneut prüfen
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) safeRun("visibility");
  });

  // --- (Optional) Behalte deine Message-APIs bei, sie werden aber nicht benötigt ---
  chrome.runtime.onMessage.addListener((msg, _sender, send) => {
    if (msg?.type === "START_AUTOSCROLL") { startAutoScroll(); }
    if (msg?.type === "STOP_AUTOSCROLL") { stopAutoScroll(); }
    if (msg?.type === "RUN_SANITIZER") {
      runPipeline().then((result)=>send({ ok:true, result })).catch((err)=>send({ ok:false, error:String(err) }));
      return true;
    }
  });
})();
