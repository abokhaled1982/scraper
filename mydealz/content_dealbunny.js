/**
 * Content Script für dealbunny.de:
 * - Implementiert die seiten-spezifische DOM-Logik.
 * - Reagiert auf Nachrichten vom Background-Script (SCAN_DEALS und CLICK_DEAL).
 */

// --- SEITEN-SPEZIFISCHE KONFIGURATION ---
const CONFIG = {
    // Äußerer Container für einen einzelnen Deal (basierend auf dem HTML-Snippet)
    containerSelector: "div.cha-8atqhb", 
    // Der Deal-Button, der die Deal-ID enthält
    buttonSelector: "button[deal_id]", 
    // Die eindeutige ID ist im 'deal_id'-Attribut des Buttons
    idAttribute: "deal_id", 
};


// -----------------------------------------------------
// ### 1. DOM-MANIPULATIONS-LOGIK
// -----------------------------------------------------

/**
 * Sucht alle Deals auf der Seite und gibt deren IDs zurück.
 * @returns {Array<string>} Array von Deal-IDs (z.B. deal_id-Werten).
 */
function scanDeals() {
    const deals = document.querySelectorAll(CONFIG.containerSelector);
    const dealIds = [];

    deals.forEach(dealElement => {
        // Die ID befindet sich im Button, nicht im Container, 
        // daher suchen wir zuerst den Button.
        const button = dealElement.querySelector(CONFIG.buttonSelector);
        if (button) {
            let id = null;

            // Holt den Wert des 'deal_id'-Attributs vom Button
            id = button.getAttribute(CONFIG.idAttribute);
            
            if (id) {
                dealIds.push(id);
            }
        }
    });

    console.log(`[Dealbunny Content] ${dealIds.length} Deals gefunden.`);
    return dealIds;
}

/**
 * Führt einen Klick auf den Deal-Button des angegebenen Deals aus.
 * @param {string} dealId Die ID (deal_id) des Buttons.
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
        // Finde den Button direkt über das deal_id-Attribut
        const button = document.querySelector(`${CONFIG.buttonSelector}[${CONFIG.idAttribute}="${dealId}"]`);

        if (!button) {
            console.error(`Button mit ${CONFIG.idAttribute}="${dealId}" nicht gefunden.`);
            return false;
        }

        // Klick auslösen
        setTimeout(() => {
            try {
                triggerClick(button);
                console.log(`[Dealbunny Content] Klick auf Deal-Button für ID ${dealId} ausgelöst.`);
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

console.log("[Dealbunny Content] Script geladen und bereit.");