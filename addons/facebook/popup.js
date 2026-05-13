// popup.js — Live Status Dashboard v3.3
const TARGET_URL = "https://www.facebook.com/profile.php?id=61584368422265";

const STATE_LABELS = {
  disconnected: "Getrennt",
  connecting:   "Verbindet...",
  handshake:    "Handshake...",
  ready:        "Bereit",
  busy:         "Aktiv",
  error:        "Fehler",
};

// ── Step definitions (matched against activity strings) ──────────────────
const POST_STEPS = [
  { id: "open",    label: "Dialog öffnen",    keywords: ["dialog", "öffne", "vorbereitet", "vorbereitung"] },
  { id: "image",   label: "Bild hochladen",   keywords: ["bild", "image", "foto"] },
  { id: "text",    label: "Text eingeben",    keywords: ["text", "schreibe", "eingabe", "tippe"] },
  { id: "post",    label: "Posten",           keywords: ["posten", "sende", "button", "klick", "weiter"] },
  { id: "comment", label: "Kommentieren",     keywords: ["komment", "link", "affiliate"] },
  { id: "done",    label: "Fertig",           keywords: ["fertig", "abgeschlossen", "erfolgreich"] },
];

const REEL_STEPS = [
  { id: "open",    label: "Reel starten",     keywords: ["reel", "upload", "gestartet"] },
  { id: "upload",  label: "Video hochladen",  keywords: ["video", "upload", "lade hoch"] },
  { id: "text",    label: "Text eingeben",    keywords: ["text", "schreibe", "caption"] },
  { id: "post",    label: "Veröffentlichen",  keywords: ["teilen", "posten", "veröffent"] },
  { id: "comment", label: "Kommentieren",     keywords: ["komment", "link", "affiliate"] },
  { id: "done",    label: "Fertig",           keywords: ["fertig", "abgeschlossen", "erfolgreich"] },
];

// ── Local state ───────────────────────────────────────────────────────────
let lastKnownState   = "disconnected";
let currentSteps     = null;
let activeStepIndex  = -1;
let lastResult       = null; // { success, text, time }

function detectStepIndex(activity, steps) {
  if (!activity) return -1;
  const lower = activity.toLowerCase();
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].keywords.some(k => lower.includes(k))) return i;
  }
  return -1;
}

function inferSteps(activity) {
  if (!activity) return POST_STEPS;
  const lower = activity.toLowerCase();
  return (lower.includes("reel") || lower.includes("video")) ? REEL_STEPS : POST_STEPS;
}

// ── Render ────────────────────────────────────────────────────────────────
function renderStatus(status) {
  if (!status) return;
  const state    = status.state || "disconnected";
  const activity = status.activity || "";
  lastKnownState = state;

  // Badge
  const badge = document.getElementById("stateBadge");
  badge.className = `badge ${state}`;
  document.getElementById("stateText").textContent = STATE_LABELS[state] || state;

  // Activity text
  const actEl = document.getElementById("activityText");
  actEl.textContent  = activity || STATE_LABELS[state] || "";
  actEl.className    = `state-activity${state === "busy" ? " active" : ""}`;

  // Info rows
  if (status.wsUrl) document.getElementById("wsUrl").textContent = status.wsUrl;
  const p = status.pending || 0;
  const pendEl = document.getElementById("pendingCount");
  pendEl.textContent = p === 0 ? "–" : `${p} gepuffert`;
  pendEl.className   = `info-value${p > 0 ? " highlight" : ""}`;

  // Progress bar + Steps
  updateProgress(state, activity);

  // Last result (only update when transitioning OUT of busy with a result)
  if (state === "ready" && lastKnownState === "busy") {
    // Will be set by explicit task_result handling
  }

  // Logs
  renderLogs(status.logs || []);
}

function updateProgress(state, activity) {
  const bar    = document.getElementById("progressBar");
  const panel  = document.getElementById("stepsPanel");
  const list   = document.getElementById("stepsList");

  if (state === "busy") {
    // Show steps panel
    if (!currentSteps) {
      currentSteps = inferSteps(activity);
      activeStepIndex = -1;
    }
    const idx = detectStepIndex(activity, currentSteps);
    if (idx > activeStepIndex) activeStepIndex = idx;

    // Progress bar indeterminate
    bar.className = "progress-bar indeterminate";

    // Render steps
    panel.classList.add("visible");
    list.innerHTML = currentSteps.map((s, i) => {
      let cls  = "";
      let icon = "○";
      if (i < activeStepIndex) { cls = "done";   icon = "✓"; }
      else if (i === activeStepIndex) { cls = "active"; icon = "▶"; }
      return `<div class="step ${cls}"><span class="step-icon">${icon}</span>${escHtml(s.label)}</div>`;
    }).join("");

  } else if (state === "ready") {
    if (lastResult) {
      bar.className = lastResult.success ? "progress-bar done" : "progress-bar failed";
      setTimeout(() => { bar.className = "progress-bar hidden"; }, 3000);
    } else {
      bar.className = "progress-bar hidden";
    }
    // Keep steps visible briefly, then hide
    setTimeout(() => {
      panel.classList.remove("visible");
      currentSteps    = null;
      activeStepIndex = -1;
    }, 3500);

  } else if (state === "error") {
    bar.className = "progress-bar failed";
    setTimeout(() => { bar.className = "progress-bar hidden"; }, 4000);
    panel.classList.remove("visible");
    currentSteps    = null;
    activeStepIndex = -1;

  } else {
    bar.className = "progress-bar hidden";
    panel.classList.remove("visible");
    currentSteps    = null;
    activeStepIndex = -1;
  }
}

function showLastResult(success, errorMsg) {
  lastResult = { success, time: new Date() };
  const el   = document.getElementById("lastResult");
  const icon = document.getElementById("lastResultIcon");
  const text = document.getElementById("lastResultText");
  const time = document.getElementById("lastResultTime");

  el.className = `last-result ${success ? "success" : "failure"}`;
  icon.textContent = success ? "✅" : "❌";
  text.textContent = success
    ? "Post erfolgreich veröffentlicht"
    : `Fehler: ${errorMsg || "unbekannt"}`;
  time.textContent = lastResult.time.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderLogs(logs) {
  const panel = document.getElementById("logPanel");
  const wasAtBottom = panel.scrollHeight - panel.clientHeight - panel.scrollTop < 30;
  panel.innerHTML = logs.map(entry => {
    const ts  = entry.ts ? entry.ts.slice(11, 19) : "";
    const lvl = entry.level || "info";
    const msg = entry.msg  || "";
    return `<div class="log-entry"><span class="log-ts">${ts}</span><span class="log-msg ${lvl}">${escHtml(msg)}</span></div>`;
  }).join("");
  if (wasAtBottom) panel.scrollTop = panel.scrollHeight;
}

function escHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ── Poll ─────────────────────────────────────────────────────────────────
function pollStatus() {
  chrome.runtime.sendMessage({ type: "get_status" }, (resp) => {
    if (chrome.runtime.lastError) return;
    if (resp) renderStatus(resp);
  });
}

// ── Buttons ───────────────────────────────────────────────────────────────
document.getElementById("goToPageBtn").addEventListener("click", () => {
  chrome.tabs.query({ url: ["*://*.facebook.com/*"] }, (tabs) => {
    if (tabs.length > 0) {
      chrome.tabs.update(tabs[0].id, { url: TARGET_URL, active: true });
    } else {
      chrome.tabs.create({ url: TARGET_URL });
    }
  });
});

document.getElementById("refreshBtn").addEventListener("click", pollStatus);

document.getElementById("clearLogBtn").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "clear_log" }, pollStatus);
});

// Keep old clearLog id working (log-header button)
const oldClear = document.getElementById("clearLog");
if (oldClear) oldClear.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "clear_log" }, pollStatus);
});

// ── Live updates via onMessage ────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "status_update") {
    renderStatus(msg);
  }
  // When background reports a completed task result
  if (msg.type === "task_done") {
    showLastResult(msg.success, msg.error);
  }
});

// ── Init ──────────────────────────────────────────────────────────────────
pollStatus();
setInterval(pollStatus, 2000);
