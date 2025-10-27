// content.js - Idealo Link Opener und Content-Script-Logik

(function() {
    'use strict';

    // --- Konfigurationskonstanten ---
    const OPEN_DELAY_MS = 10000; // 10 Sekunden Verzögerung zwischen dem Öffnen / Auto-Close Timer

    let isRunning = false;
    
    // --- Hilfsfunktionen ---
    
    // Dummy-Funktion für sleep
    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    /**
     * Sammelt Shop-Links von der Idealo-Seite und filtert Blacklist-Einträge.
     * HINWEIS: Diese Funktion ist ein Platzhalter und benötigt eine echte Implementierung
     * für das Parsen der Idealo-HTML.
     */
    function collectShopLinks() {
        const out = new Set();
        // Dummy-Daten für den Test
        out.add('https://www.idealo.de/preisvergleich/shop.html?offerId=1&name=Shop1');
        out.add('https://www.idealo.de/preisvergleich/shop.html?offerId=2&name=Shop2');
        out.add('https://www.idealo.de/preisvergleich/shop.html?offerId=3&name=Shop3');
        out.add('https://www.facebook.com/irgendwas'); // Wird herausgefiltert
        
        // Optionale Blacklist für Social, Tracking etc.
        const blacklist = ['facebook.com', 'twitter.com', 'instagram.com', 'youtube.com', 'linkedin.com'];
        return Array.from(out).filter((u) => !blacklist.some((b) => u.includes(b)));
    }

    /**
     * Verarbeitet eine Liste von Links, öffnet sie nacheinander und wartet.
     */
    async function processLinksSequentially(links) {
        if (!Array.isArray(links) || links.length === 0) {
            console.log('[Idealo Opener] Keine Links gefunden.');
            return;
        }

        isRunning = true;
        console.log(`[Idealo Opener] Starte mit ${links.length} Links …`);

        for (let i = 0; i < links.length && isRunning; i++) {
            const url = links[i];
            console.log(`[Idealo Opener] (${i + 1}/${links.length}) öffne:`, url);

            // Öffnen im Inkognito; Background schließt nach 10s automatisch
            await new Promise((resolve) => {
                chrome.runtime.sendMessage(
                    { type: 'OPEN_IN_INCOGNITO', url, autoCloseMs: OPEN_DELAY_MS },
                    (res) => {
                        if (chrome.runtime.lastError) {
                            console.warn('[Idealo Opener] sendMessage Fehler:', chrome.runtime.lastError.message);
                        } else if (!res?.ok) {
                            console.warn('[Idealo Opener] Background meldet Fehler:', res?.error);
                        }
                        resolve();
                    }
                );
            });

            // Warte 10 Sekunden, dann nächster Link
            await sleep(OPEN_DELAY_MS);
        }

        console.log('[Idealo Opener] Fertig.');
        isRunning = false;
    }

    /**
     * Startet den sequenziellen Link-Öffner.
     */
    async function startRun() {
        if (isRunning) return;
        const links = collectShopLinks();
        await processLinksSequentially(links);
    }

    /**
     * Logik für das Schließen des aktuellen Tabs (nachdem FINAL_SHOP_LINK gesendet wurde).
     */
    async function closeCurrentTab() {
        console.log('[Content-Script] Sende CLOSE_CURRENT_TAB an Background.');
        await chrome.runtime.sendMessage({ type: "CLOSE_CURRENT_TAB" });
    }

    /**
     * Logik, um den finalen Shop-Link zu senden (simuliert nach Redirect-Erfassung).
     * WICHTIG: Die tatsächliche Implementierung müsste prüfen, ob der aktuelle 
     * Tab auf einer Ziel-Shop-Seite gelandet ist, und den finalen Link erfassen.
     */
    async function logFinalShopLink(link) {
        const res = await chrome.runtime.sendMessage({ 
            type: "FINAL_SHOP_LINK",
            payload: {
                shopLink: link,
                sourceIdealoLink: document.referrer || "Unbekannt"
            }
        });
        
        // Wenn das Protokollieren erfolgreich war, wird der Tab geschlossen.
        if (res?.ok) {
            await closeCurrentTab();
        }
    }

    /**
     * Optional: Auto-Start, wenn auf einer Idealo-Produkt-/Angebotsseite.
     */
    function shouldAutoStart() {
        return /idealo\.de/.test(location.hostname) && /preisvergleich|produkt|angebot/.test(location.href);
    }
    
    // --- Logik für den Tab-Schließmechanismus (Im neu geöffneten Tab) ---
    
    // WICHTIG: Diese Logik würde im Inkognito-Tab ausgeführt. Sie muss prüfen,
    // ob der Idealo-Redirect abgeschlossen ist und den finalen Link protokollieren.
    if (!/idealo\.de/.test(location.hostname)) {
        // Angenommen, wir sind jetzt auf der Ziel-Shop-Seite
        logFinalShopLink(location.href);
    }


    // --- Nachrichten vom Popup/Background (Start/Stop) ---
    chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
        
        // 1. Startet die Sequenz vom Popup (ausgelöst durch run_idealo)
        if (msg?.type === 'RUN_LINK_OPENER') {
            startRun().then(() => sendResponse({ ok: true })).catch((e) => sendResponse({ ok: false, error: String(e) }));
            return true;
        }
        
        // 2. Stoppt die Sequenz vom Popup
        if (msg?.type === 'STOP_LINK_OPENER') {
            isRunning = false;
            sendResponse({ ok: true });
            return;
        }
        
        // 3. Sanitizer-Logik (vom Popup 'download') - Nur Platzhalter
        if (msg?.type === 'RUN_SANITIZER') {
             // Hier müsste die echte Sanitizer-Logik laufen
             sendResponse({ ok: true, result: { output: { html: "<html><body>Gereinigter Inhalt</body></html>" } } });
             return true;
        }
        
        // 4. Scroll-Logik
        if (msg?.type === 'START_AUTOSCROLL') {
            // ... Start-Scroll-Logik ...
            console.log('[Content] Auto-Scroll gestartet.');
            sendResponse({ ok: true });
            return true;
        }
        if (msg?.type === 'STOP_AUTOSCROLL') {
            // ... Stop-Scroll-Logik ...
            console.log('[Content] Auto-Scroll gestoppt.');
            sendResponse({ ok: true });
            return true;
        }

    });


    // Auto-Start (optional) – wenn gewünscht, aktivieren
    if (shouldAutoStart()) {
        // startRun();
    }
    
})();