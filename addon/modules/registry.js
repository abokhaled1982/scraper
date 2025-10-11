// modules/registry.js
export const ALL_MODULES = [
  { id: "stripScriptsOnly", path: "modules/stripScriptsOnly.js", label: "Remove <script> tags", enabledByDefault: true },
  { id: "serialize", path: "modules/serialize.js", label: "Serialize cleaned HTML", enabledByDefault: true }
];

const STORAGE_KEY = "module_prefs";

export async function getEnabledMap() {
  const { [STORAGE_KEY]: prefs } = await chrome.storage.local.get(STORAGE_KEY);
  const map = {};
  for (const m of ALL_MODULES) map[m.id] = prefs?.[m.id] ?? m.enabledByDefault;
  return map;
}
export async function setEnabled(id, enabled) {
  const { [STORAGE_KEY]: prefs } = await chrome.storage.local.get(STORAGE_KEY);
  await chrome.storage.local.set({ [STORAGE_KEY]: { ...(prefs || {}), [id]: enabled } });
}
export async function loadEnabledModules() {
  const enabled = await getEnabledMap();
  const active = ALL_MODULES.filter(m => enabled[m.id]);
  const out = [];
  for (const mod of active) {
    const url = chrome.runtime.getURL(mod.path);
    const m = await import(url);
    out.push({ meta: mod, run: m.run });
  }
  return out;
}
