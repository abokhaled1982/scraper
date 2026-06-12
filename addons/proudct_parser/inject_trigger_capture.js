// inject_trigger_capture.js
// =====================================================================
// Läuft bei `document_start` auf JEDER Seite – also BEVOR irgendein
// Page-Skript (z. B. Affiliate-Tracker wie Awin auf sportspar.de) per
// `history.replaceState()` die URL säubern kann. Wir merken uns hier, ob
// die ursprüngliche URL den Opener-Marker `#__opener__` (Fragment) trug.
//
// Warum Fragment statt Query-Parameter?
//   - Fragmente werden NIE an den Server geschickt; Affiliate-Tracker
//     sehen sie nicht und können sie nicht in 301/302-Redirects umbauen.
//   - Fragmente landen nicht in serverseitigen Logs.
//   - Der Wert ist trotzdem in `location.href` lesbar, solange wir früh
//     genug schauen – und document_start ist garantiert vor allen
//     Page-Skripten.
//
// Werte werden auf `window.__OPENER_TRIGGER_URL__` / `__OPENER_TRIGGER__`
// im isolierten Content-Script-World abgelegt und sind damit für das
// spätere `content.js` (document_idle) verfügbar – auch wenn die Seite
// zwischenzeitlich URL und Fragment via replaceState() entfernt hat.
// =====================================================================

(() => {
  try {
    const initialHref = location.href;
    window.__OPENER_TRIGGER_URL__ = initialHref;
    const u = new URL(initialHref);
    window.__OPENER_TRIGGER__ = u.hash === "#__opener__";
    if (window.__OPENER_TRIGGER__) {
      console.log("[trigger-capture] opener-trigger detected at start:", initialHref);
    }
  } catch (e) {
    // niemals Seite brechen
    console.warn("[trigger-capture] failed:", e && e.message ? e.message : e);
  }
})();
