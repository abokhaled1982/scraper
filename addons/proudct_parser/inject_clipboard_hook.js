// inject_clipboard_hook.js
// Läuft in der PAGE-World (nicht im Content-Script-Isolation-World).
// Fängt alle Clipboard-Schreibvorgänge ab (writeText + execCommand('copy'))
// und meldet den Wert per CustomEvent an das Content-Script.
(() => {
  if (window.__amzn_clip_hook_installed__) return;
  window.__amzn_clip_hook_installed__ = true;

  const AFFILIATE_RX = /(amzn\.to|amazon\.[a-z.]+\/.+(?:tag=|ref=as_li))/i;

  function report(value) {
    try {
      const val = String(value || "").trim();
      if (!val) return;
      window.dispatchEvent(new CustomEvent("__amzn_clipboard_capture__", { detail: { value: val } }));
    } catch (_) {}
  }

  // 1) navigator.clipboard.writeText hooken
  try {
    const clip = navigator.clipboard;
    if (clip && typeof clip.writeText === "function") {
      const origWriteText = clip.writeText.bind(clip);
      clip.writeText = function (text) {
        report(text);
        return origWriteText(text);
      };
    }
  } catch (_) {}

  // 2) document.execCommand('copy') hooken (Fallback für ältere Implementierungen)
  try {
    const origExec = document.execCommand?.bind(document);
    if (origExec) {
      document.execCommand = function (cmd, ...rest) {
        if (String(cmd).toLowerCase() === "copy") {
          try {
            const sel = window.getSelection?.().toString();
            if (sel) report(sel);
            // Auch aktive Textarea/Input prüfen
            const ae = document.activeElement;
            if (ae && (ae.tagName === "TEXTAREA" || ae.tagName === "INPUT")) {
              const v = ae.value?.substring(ae.selectionStart || 0, ae.selectionEnd || ae.value.length);
              if (v) report(v);
            }
          } catch (_) {}
        }
        return origExec(cmd, ...rest);
      };
    }
  } catch (_) {}

  // 3) Copy-Event als zusätzlicher Sniffer
  document.addEventListener(
    "copy",
    (e) => {
      try {
        const data = e.clipboardData?.getData("text/plain") || window.getSelection?.().toString();
        if (data && AFFILIATE_RX.test(data)) report(data);
      } catch (_) {}
    },
    true
  );
})();
