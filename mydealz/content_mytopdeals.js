/**
 * Content Script für mytopdeals.net:
 * - Implementiert die seiten-spezifische DOM-Logik.
 * - Reagiert auf Nachrichten vom Background-Script (SCAN_DEALS und CLICK_DEAL).
 * * 🚨 WICHTIG: DIE SELEKTOREN MÜSSEN FÜR MYTOPDEALS.NET GEPRÜFT UND ANGEPASST WERDEN! 🚨
 */

// --- SEITEN-SPEZIFISCHE KONFIGURATION ---
const CONFIG = {
    // Container-Selektor: <article id="..." class="box ...">
    containerSelector: "article.box", 
    
    // Die eindeutige ID ist im 'id'-Attribut (z.B. "post-1186104")
    idAttribute: "id", 
    
    // Selektor für den "Zum Deal"-Button
    buttonSelector: "a.deal-button", 
};


// -----------------------------------------------------
// ### 1. DOM-MANIPULATIONS-LOGIK
// -----------------------------------------------------

/**
 * Sucht alle Deals auf der Seite und gibt deren IDs zurück.
 * @returns {Array<string>} Array von Deal-IDs (z.B. ID-Werten).
 */
function scanDeals() {
    const deals = document.querySelectorAll(CONFIG.containerSelector);
    const dealIds = [];

    deals.forEach(dealElement => {
        let id = null;

        if (CONFIG.idAttribute === 'id') {
            id = dealElement.id;
        } else if (CONFIG.idAttribute.startsWith('data-')) {
            // Holt den Wert aus einem data-Attribut (z.B. dealElement.dataset.dealId)
            const idAttributeName = CONFIG.idAttribute.substring(5);
            id = dealElement.dataset[idAttributeName];
        } else {
            // Holt den Wert aus einem anderen Attribut des Containers
            id = dealElement.getAttribute(CONFIG.idAttribute);
        }

        if (id) {
            // Prüfe, ob mindestens ein Button vorhanden ist
            const button = dealElement.querySelector(CONFIG.buttonSelector);
            if (button) {
                dealIds.push(id);
            }
        }
    });

    console.log(`[MyTopDeals Content] ${dealIds.length} Deals gefunden.`);
    return dealIds;
}

/**
 * Führt einen Klick auf den Deal-Button des angegebenen Deals aus.
 * @param {string} dealId Die ID des Deal-Containers.
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
        let dealContainer = null;
        if (CONFIG.idAttribute === 'id') {
             // Suche über Container-Selektor und ID (z.B. article#ID)
            dealContainer = document.querySelector(`${CONFIG.containerSelector}#${dealId}`);
        } else {
            // Suche über Attribut-Selektor (z.B. article[data-deal-id="..."])
            dealContainer = document.querySelector(`${CONFIG.containerSelector}[${CONFIG.idAttribute}="${dealId}"]`);
        }
        
        if (!dealContainer) {
            console.error(`Deal-Container mit ${CONFIG.idAttribute}="${dealId}" nicht gefunden.`);
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
                console.log(`[MyTopDeals Content] Klick auf Deal-Button für ID ${dealId} ausgelöst.`);
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

console.log("[MyTopDeals Content] Script geladen und bereit.");