/**
 * Content Script für dealdoktor.de:
 * - Implementiert die seiten-spezifische DOM-Logik.
 * - Reagiert auf Nachrichten vom Background-Script (SCAN_DEALS und CLICK_DEAL).
 */

// --- SEITEN-SPEZIFISCHE KONFIGURATION ---
const CONFIG = {
    // Container-Selektor basierend auf Ihrem HTML
    containerSelector: "div.box.box-deal[data-uuid]",
    idAttribute: "data-uuid", // Die eindeutige ID ist im 'data-uuid'-Attribut
    // Button-Selektor basierend auf Ihrem HTML
    buttonSelector: "a.btn-deal.btn-shine.btn-primary-gradient",
};


// -----------------------------------------------------
// ### 1. DOM-MANIPULATIONS-LOGIK
// -----------------------------------------------------

/**
 * Sucht alle Deals auf der Seite und gibt deren IDs zurück.
 * @returns {Array<string>} Array von Deal-IDs (z.B. data-uuid-Werten).
 */
function scanDeals() {
    const deals = document.querySelectorAll(CONFIG.containerSelector);
    const dealIds = [];

    deals.forEach(dealElement => {
        let id = null;
        const idAttributeName = CONFIG.idAttribute.startsWith('data-')
            ? CONFIG.idAttribute.substring(5)
            : CONFIG.idAttribute;

        if (CONFIG.idAttribute === 'id') {
            id = dealElement.id;
        } else {
            // Holt den Wert aus einem data-Attribut (z.B. dealElement.dataset.uuid)
            id = dealElement.dataset[idAttributeName];
        }

        if (id) {
            // Prüfe, ob mindestens ein Button vorhanden ist
            const button = dealElement.querySelector(CONFIG.buttonSelector);
            if (button) {
                dealIds.push(id);
            }
        }
    });

    console.log(`[DealDoktor Content] ${dealIds.length} Deals gefunden.`);
    return dealIds;
}

/**
 * Führt einen Klick auf den Deal-Button des angegebenen Deals aus.
 * @param {string} dealId Die ID (data-uuid) des Deal-Containers.
 * @returns {boolean} True, wenn der Klick ausgelöst wurde.
 */
function clickDeal(dealId) {
    // Robuste Klick-Funktion: Versucht native .click() vor Event-Simulation
    function triggerClick(element) {
        if (!element) return false;
        try {
            element.click();
            return true;
        } catch (e) {
            // Fallback: Event-Simulation
            element.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
            element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
            element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
            return true;
        }
    }

    try {
        const idAttributeName = CONFIG.idAttribute;
        // Finde den Artikel-Container über das ID-Attribut und den Wert (z.B. [data-uuid="..."])
        const dealContainer = document.querySelector(`[${idAttributeName}="${dealId}"]`);

        if (!dealContainer) {
            console.error(`Deal-Container mit ${idAttributeName}="${dealId}" nicht gefunden.`);
            return false;
        }

        // Finde den Button innerhalb des Containers
        const button = dealContainer.querySelector(CONFIG.buttonSelector);

        if (!button) {
            console.error(`Button mit Selector "${CONFIG.buttonSelector}" nicht gefunden.`);
            return false;
        }

        // Klick auslösen
        setTimeout(() => {
            try {
                triggerClick(button);
                console.log(`[DealDoktor Content] Klick auf Deal-Button für ID ${dealId} ausgelöst.`);
            } catch (e) {
                console.error("Fehler beim Triggering des Klicks:", e);
            }
        }, 50);

        return true;
    } catch (e) {
        console.error("Unerwarteter Fehler in clickDeal:", e);
        return false;
    }
}


// -----------------------------------------------------
// ### 2. MESSAGE HANDLER
// -----------------------------------------------------

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
    switch (request.type) {
        case "SCAN_DEALS":
            // Das Background-Skript fragt nach den IDs
            const dealIds = scanDeals();
            sendResponse({ deals: dealIds });
            return true; // Asynchrone Antwort

        case "CLICK_DEAL":
            // Das Background-Skript befiehlt einen Klick
            const clicked = clickDeal(request.dealId);
            sendResponse({ clicked: clicked });
            return true; // Asynchrone Antwort

        default:
            return false;
    }
});

console.log("[DealDoktor Content] Script geladen und bereit.");