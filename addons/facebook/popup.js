// popup.js — Live Status Dashboard
const TARGET_URL = "https://www.facebook.com/profile.php?id=61584368422265";

const STATE_LABELS = {
  disconnected: "Getrennt",
  connecting:   "Verbindet...",
  handshake:    "Handshake...",
  ready:        "Bereit",
  busy:         "Aktiv",
  error:        "Fehler",
};

const ACTIVITY_ICONS = {
  disconnected: "🔌",
  connecting:   "🔄",
  handshake:    "🤝",
  ready:        "✅",
  busy:         "⚙️",
  error:        "❌",
};

function renderStatus(status) {
  if (!status) return;

  // Badge
  const badge    = document.getElementById("stateBadge");
  const stateText = document.getElementById("stateText");
  badge.className = `badge ${status.state}`;
  stateText.textContent = STATE_LABELS[status.state] || status.state;

  // Server URL
  if (status.wsUrl) document.getElementById("wsUrl").textContent = status.wsUrl;

  // Pending
  const p = status.pending || 0;
  document.getElementById("pendingCount").textContent = p === 0 ? "–" : `${p} gepuffert`;

  // Activity
  const icon = document.getElementById("activityIcon");
  const text = document.getElementById("activityText");
  icon.textContent = ACTIVITY_ICONS[status.state] || "ℹ️";
  text.textContent = status.activity || STATE_LABELS[status.state] || "";
  text.className   = `activity-text ${status.state === "busy" ? "active" : ""}`;

  // Logs
  renderLogs(status.logs || []);
}

function renderLogs(logs) {
  const panel = document.getElementById("logPanel");
  const wasAtBottom = panel.scrollHeight - panel.clientHeight - panel.scrollTop < 30;

  panel.innerHTML = logs.map(entry => {
    const ts  = entry.ts ? entry.ts.slice(11, 19) : "";
    const lvl = entry.level || "info";
    const msg = entry.msg || "";
    return `<div class="log-entry">
      <span class="log-ts">${ts}</span>
      <span class="log-msg ${lvl}">${escHtml(msg)}</span>
    </div>`;
  }).join("");

  if (wasAtBottom) panel.scrollTop = panel.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ── Poll status from background every second ──────────────────────────────
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

document.getElementById("clearLog").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "clear_log" }, pollStatus);
});

// ── Live updates via onMessage ────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "status_update") renderStatus(msg);
});

// ── Init ──────────────────────────────────────────────────────────────────
pollStatus();
setInterval(pollStatus, 2000);
