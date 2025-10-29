const $ = (sel) => document.querySelector(sel);
const listEl = $("#links");
const countEl = $("#count");
const toggleEl = $("#toggle-enabled");

async function sendMessageToBg(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, resolve);
  });
}

const btnAuto = document.getElementById("auto-open");

async function getState() {
  return new Promise((r) => chrome.runtime.sendMessage({ type: "GET_STATE" }, r));
}
async function toggleAuto() {
  const state = await getState();
  const next = !state.autoOpen;
  chrome.runtime.sendMessage({ type: "TOGGLE_AUTO_OPEN", enabled: next });
  updateButton(next);
}
function updateButton(enabled) {
  btnAuto.textContent = "Auto-Open: " + (enabled ? "An" : "Aus");
}

btnAuto.addEventListener("click", toggleAuto);
getState().then(s => updateButton(s.autoOpen));


function render(links = []) {
  listEl.innerHTML = "";
  if (!links.length) {
    listEl.innerHTML = `<li class="muted">Keine Links erkannt.</li>`;
  } else {
    const frag = document.createDocumentFragment();
    links.forEach((u, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<a href="${u}" target="_blank" rel="noreferrer noopener">${
        i + 1
      }. ${u}</a>`;
      frag.appendChild(li);
    });
    listEl.appendChild(frag);
  }
  countEl.textContent = `${links.length} Link${links.length === 1 ? "" : "s"}`;
}

async function loadState() {
  const res = await sendMessageToBg({ type: "GET_LINKS" });
  render(res?.links || []);
  toggleEl.checked = !!res?.enabled;
}

async function setEnabled(enabled) {
  await sendMessageToBg({ type: "SET_ENABLED", enabled });
}

$("#refresh").addEventListener("click", async () => {
  // Frische Daten anfordern: wir triggern den Content im aktiven Tab, damit er sofort sendet
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: false },
        func: () => {
          try {
            if (globalThis.__deal_utils__?.extract) {
              const links = globalThis.__deal_utils__.extract(document);
              chrome.runtime.sendMessage({
                type: "DEAL_LINKS",
                payload: links,
              });
            }
          } catch (e) {}
        },
      });
    } catch (e) {
      console.warn("Konnte im Tab nicht ausführen:", e);
    }
  }
  await loadState();
});

$("#copy-all").addEventListener("click", async () => {
  const res = await sendMessageToBg({ type: "GET_LINKS" });
  const text = (res?.links || []).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    $("#copy-all").textContent = "Kopiert!";
    setTimeout(() => ($("#copy-all").textContent = "Alle kopieren"), 1200);
  } catch {
    alert("Kopieren fehlgeschlagen.");
  }
});

toggleEl.addEventListener("change", async (e) => {
  await setEnabled(e.target.checked);
  await loadState();
});

// initial
loadState();
