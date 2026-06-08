// inject_trigger_capture.js
// =====================================================================
// Läuft bei `document_start` auf JEDER Seite. Aufgabe: die ursprüngliche
// URL festhalten, BEVOR ein Seitenskript (z. B. Affiliate-Tracker wie Awin
// auf sportspar.de) sie via `history.replaceState()` säubert und dabei
// unseren `?ext_trigger=send_html`-Marker entfernt.
//
// Inhalte werden auf `window.__OPENER_TRIGGER_URL__` / `__OPENER_TRIGGER__`
// abgelegt – diese Werte liegen im isolierten Content-Script-World pro
// Origin und sind damit für das spätere `content.js` (das bei
// `document_idle` läuft) verfügbar.
// =====================================================================

(() => {
  try {
    const initialHref = location.href;
    window.__OPENER_TRIGGER_URL__ = initialHref;
    const u = new URL(initialHref);
    window.__OPENER_TRIGGER__ =
      u.searchParams.get("ext_trigger") === "send_html";
    if (window.__OPENER_TRIGGER__) {
      console.log("[trigger-capture] opener-trigger detected at start:", initialHref);
    }
  } catch (e) {
    // niemals Seite brechen
    console.warn("[trigger-capture] failed:", e && e.message ? e.message : e);
  }
})();
