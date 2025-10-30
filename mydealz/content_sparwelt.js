/**
 * Robustes Content Script für sparwelt.de
 * - Shadow DOM fähige Suche
 * - Mehrere Container-/CTA-Fallbacks
 * - Wartet auf dynamisch geladene Inhalte
 * - Kompatibel: CLICK_DEAL -> { clicked: true }
 */

const CONFIG = {
  // Kandidaten für Deal-Container (Reihenfolge = Priorität)
  containerSelectorCandidates: [
    'div[data-content-uuid]',
    'article[data-content-uuid]',
    '[data-content-uuid]',
    '[data-deal-id]',
    '[data-uuid]',
    'article:has(a,button)',          // moderne :has() – in aktueller Chrome/Edge ok
  ],

  // Primärer (alter) Sparwelt-CTA
  primaryButtonSelector: 'div.space-x-2.flex.mt-6 button:last-child',

  // Breite CTA-Fallbacks
  fallbackSelectors: [
    'a[rel][target]',
    'a[href*="go."]',
    'a[href*="out."]',
    'a[href*="/goto/"]',
    'a[href^="http"]',
    'a[href]',
    'button',
    '[role="button"]',
  ],

  // CTA-Texte, die wir erkennen
  ctaTexts: ['zum angebot', 'zum deal', 'zum shop', 'jetzt zum angebot', 'deal anzeigen'],

  // In neuem Tab öffnen?
  openInNewTabByDefault: true,

  // Dynamische Inhalte beobachten
  watchDynamicDeals: true,

  // Scan-Timeout/Retry (ms)
  initialScanRetries: 6,
  retryDelayMs: 400,
};

/* ---------------- Shadow-DOM-fähige Suche ---------------- */

function* walkDeep(node, includeShadow = true) {
  if (!node) return;
  yield node;
  const children = node.children || [];
  for (const c of children) yield* walkDeep(c, includeShadow);
  if (includeShadow && node.shadowRoot) {
    yield* walkDeep(node.shadowRoot, includeShadow);
  }
}

function deepQuerySelectorAll(selector, root = document) {
  const out = [];
  for (const n of walkDeep(root)) {
    if (n.querySelectorAll) {
      try {
        out.push(...n.querySelectorAll(selector));
      } catch { /* some selectors may not be supported in this root */ }
    }
  }
  // Entdoppeln
  return Array.from(new Set(out));
}

/* ---------------- Utils ---------------- */

const norm = s => (s || '').trim().replace(/\s+/g, ' ').toLowerCase();

function looksLikeCta(el) {
  const t = norm(el.textContent);
  if (!t) return false;
  return CONFIG.ctaTexts.some(k => t.includes(k));
}

function openHref(href, newTab = CONFIG.openInNewTabByDefault) {
  try {
    if (!href) return false;
    if (newTab) {
      window.open(href, '_blank', 'noopener,noreferrer');
    } else {
      window.location.assign(href);
    }
    return true;
  } catch (e) {
    console.error('[content] openHref Fehler:', e);
    return false;
  }
}

function safeClick(el) {
  try {
    el.click();
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return true;
  } catch (e) {
    console.error('[content] safeClick Fehler:', e);
    return false;
  }
}

function scrollIntoViewIfNeeded(el) {
  try {
    const r = el.getBoundingClientRect();
    const inView = r.top >= 0 && r.bottom <= (window.innerHeight || document.documentElement.clientHeight);
    if (!inView) el.scrollIntoView({ block: 'center', behavior: 'instant' });
  } catch {}
}

/* ---------------- Container & CTA-Finder ---------------- */

function findDealContainers() {
  for (const sel of CONFIG.containerSelectorCandidates) {
    const list = deepQuerySelectorAll(sel);
    if (list.length) {
      console.log(`[content] Container via "${sel}" gefunden:`, list.length);
      return { containers: list, selectorUsed: sel };
    }
  }
  // Fallback: seitenweit CTAs suchen und nach oben laufen
  const ctas = deepQuerySelectorAll(CONFIG.fallbackSelectors.join(',')).filter(looksLikeCta);
  if (ctas.length) {
    console.warn('[content] Kein definierter Container gefunden – nutze CTA-Fallback (seitweit).');
    // Erzeuge pseudo-Container = nächster Vorfahr, der „card-ähnlich“ ist
    const containers = ctas.map(el => closestCard(el)).filter(Boolean);
    return { containers: Array.from(new Set(containers)), selectorUsed: 'CTA-fallback' };
  }
  return { containers: [], selectorUsed: null };
}

function closestCard(el) {
  return el.closest?.([
    '[data-content-uuid]',
    '[data-deal-id]',
    'article',
    'section',
    'li',
    'div.card',
    'div[role="article"]',
  ].join(',')) || null;
}

function getDealId(container) {
  const candAttrs = ['data-content-uuid', 'data-deal-id', 'data-uuid'];
  for (const a of candAttrs) {
    const v = container.getAttribute?.(a);
    if (v) return v;
  }
  // Kein Attribut? Dann baue eine stabile Kennung
  return container.id || `node:${hashNode(container)}`;
}

function hashNode(node) {
  try {
    const txt = (node.className || '') + '|' + (node.tagName || '') + '|' + (node.innerText || '').slice(0, 128);
    let h = 0;
    for (let i = 0; i < txt.length; i++) h = ((h << 5) - h) + txt.charCodeAt(i) | 0;
    return Math.abs(h);
  } catch { return Math.floor(Math.random()*1e9); }
}

function findDealCta(container) {
  // 1) bekannter Sparwelt-Selektor
  const primary = container.querySelector?.(CONFIG.primaryButtonSelector);
  if (primary) return primary;

  // 2) Kandidaten + Textfilter
  const candidates = container.querySelectorAll
    ? [...container.querySelectorAll(CONFIG.fallbackSelectors.join(','))]
    : [];
  const byText = candidates.filter(looksLikeCta);
  if (byText.length) return byText[0];

  // 3) typische CTA-Cluster
  const cluster = container.querySelector?.('div.space-x-2, div.flex.gap-2, div.flex.space-x-2, div.mt-6');
  if (cluster) {
    const lastCta = cluster.querySelector('a:last-child,button:last-child,[role="button"]:last-child');
    if (lastCta) return lastCta;
  }

  // 4) letzter Link
  const lastAnchor = [...candidates].reverse().find(el => el.tagName === 'A');
  if (lastAnchor) return lastAnchor;

  return null;
}

/* ---------------- Scan & Open ---------------- */

function scanDealsOnce() {
  const { containers, selectorUsed } = findDealContainers();
  const results = [];
  for (const c of containers) {
    const id = getDealId(c);
    const cta = findDealCta(c);
    if (cta) results.push({ id, container: c, cta });
  }
  console.log(`[content] ${results.length} Deals gefunden.${selectorUsed ? ' (via ' + selectorUsed + ')' : ''}`);
  return results;
}

async function scanDealsWithRetry(retries = CONFIG.initialScanRetries, delay = CONFIG.retryDelayMs) {
  for (let i = 0; i <= retries; i++) {
    const res = scanDealsOnce();
    if (res.length) return res;
    if (i < retries) await new Promise(r => setTimeout(r, delay));
  }
  return [];
}

function openDealById(dealId, newTab = CONFIG.openInNewTabByDefault) {
  const all = scanDealsOnce();
  const item = all.find(x => x.id === dealId);
  if (!item) {
    console.warn(`[content] Deal ${dealId} nicht gefunden – versuche Fallback: seitenweiter CTA mit passender Nähe.`);
    // Fallback: seitenweit CTAs nach Text und Nähe
    const ctas = deepQuerySelectorAll(CONFIG.fallbackSelectors.join(',')).filter(looksLikeCta);
    const target = ctas[0];
    if (!target) return false;
    return openOrClick(target, newTab);
  }
  return openOrClick(item.cta, newTab);
}

function openOrClick(cta, newTab) {
  scrollIntoViewIfNeeded(cta);
  if (cta.tagName === 'A' && cta.href) {
    return openHref(cta.href, newTab);
  }
  setTimeout(() => {
    const ok = safeClick(cta);
    console.log(`[content] CTA-Klick ${ok ? 'ausgeführt' : 'fehlgeschlagen'}.`);
  }, 60);
  return true;
}

/* ---------------- Observer & Debounce ---------------- */

let observerStarted = false;
function startObserver() {
  if (observerStarted || !CONFIG.watchDynamicDeals) return;
  observerStarted = true;
  const debounced = debounce(() => {
    console.log('[content] DOM-Änderung erkannt – Deals neu scannen.');
    scanDealsOnce();
  }, 350);
  const mo = new MutationObserver(debounced);
  mo.observe(document.documentElement, { childList: true, subtree: true });
}

function debounce(fn, wait = 250) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), wait); };
}

/* ---------------- Message Handler ---------------- */

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  (async () => {
    try {
      switch (request.type) {
        case 'SCAN_DEALS': {
          const items = await scanDealsWithRetry();
          sendResponse({ deals: items.map(x => x.id) });
          break;
        }
        case 'CLICK_DEAL': {
          const ok = openDealById(request.dealId, /*newTab*/ true);
          sendResponse({ clicked: !!ok });
          break;
        }
        case 'OPEN_DEAL': {
          const newTab = typeof request.newTab === 'boolean' ? request.newTab : CONFIG.openInNewTabByDefault;
          const ok = openDealById(request.dealId, newTab);
          sendResponse({ ok: !!ok });
          break;
        }
        case 'CLICK_ALL': {
          const newTab = typeof request.newTab === 'boolean' ? request.newTab : CONFIG.openInNewTabByDefault;
          const items = await scanDealsWithRetry();
          items.forEach(x => openOrClick(x.cta, newTab));
          sendResponse({ attempted: items.length });
          break;
        }
        default:
          sendResponse({ error: 'Unknown request type' });
      }
    } catch (e) {
      console.error('[content] Message-Handler Fehler:', e);
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true; // async response
});

/* ---------------- Init ---------------- */
(async function init() {
  console.log('[content] Script geladen – robuste CTA-Erkennung aktiv.');
  const items = await scanDealsWithRetry();
  if (!items.length) {
    console.warn('[content] Nach Retries keine Deals gefunden. Diagnostics folgt:');
    // Diagnostics: zeige erste Kartenähnliche Elemente
    const diag = deepQuerySelectorAll('article, section, li, div.card').slice(0, 5);
    console.log('[content] Diagnose Kandidaten:', diag);
  }
  startObserver();
})();
