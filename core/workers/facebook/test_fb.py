#!/usr/bin/env python3
"""
test_fb.py — Isolierter Test für den Facebook WebSocket-Server

Startet nur fb_service.py (Port 8080), wartet auf die Extension
und sendet dann einen Test-Post.

Verwendung:
    python3 facebook/test_fb.py
"""

import sys
import time
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.workers.facebook.fb_service as fb_service
from core.workers.facebook.fb_message import create_facebook_message

# --- TEST-DEAL ---
TEST_DEAL = {
    "title":           "Samsung Galaxy S24 Ultra 256GB Titanium Black",
    "price":           {"raw": "899,00 €"},
    "original_price":  {"raw": "1.199,00 €"},
    "discount_percent": "-25%",
    "coupon_code":     "SAVE25",
    "rabatt_text":     "🔥 Mega-Deal! Spare 300 € sofort!",
    "affiliate_url":   "https://amzn.to/test-deal",
    "hashtags":        ["#samsung", "#galaxys24", "#deal", "#schnäppchen", "#techdeals"],
}


def print_banner(text: str):
    print("\n" + "=" * 55)
    print(f"  {text}")
    print("=" * 55)


def main():
    print_banner("FACEBOOK API TEST")

    # 1. Generierten Text anzeigen
    print_banner("GENERIERTER FACEBOOK-TEXT")
    print(create_facebook_message(TEST_DEAL))

    # 2. WebSocket-Server starten
    print_banner("STARTE WEBSOCKET-SERVER (Port 8080)")
    fb_service.init()
    time.sleep(1)
    print("✅ Server läuft auf ws://localhost:8080")

    # 3. Anleitung
    print_banner("EXTENSION VERBINDEN")
    print("👉  1. Öffne Chrome → chrome://extensions")
    print("👉  2. Lade 'addons/facebook' als entpackte Erweiterung")
    print("👉  3. Klicke auf 'Service Worker' → Extension-Konsole öffnet sich")
    print("👉  4. Öffne facebook.com in einem Tab")
    print("👉  5. In der Extension-Konsole sollte erscheinen:")
    print("       '✅ Verbunden mit Node.js Server'")
    print()

    # 4. Warte auf Verbindung (max. 60s)
    print("⏳ Warte auf Extension-Verbindung (max. 60s)...")
    for i in range(30):
        time.sleep(2)
        count = len(fb_service._clients)
        if count > 0:
            print(f"\n✅ Extension verbunden! ({count} Client(s))")
            break
        progress = "█" * (i + 1) + "░" * (29 - i)
        print(f"\r   [{progress}] {(i + 1) * 2}s ", end="", flush=True)
    else:
        print("\n❌ Timeout: Keine Extension verbunden nach 60s.")
        print("   → Extension neu laden und facebook.com öffnen?")
        sys.exit(1)

    # 5. Test-Post senden
    print_banner("SENDE TEST-POST")
    result = asyncio.run(fb_service.send_post(TEST_DEAL))

    if result:
        print("✅ Post erfolgreich an Extension gesendet!")
        print("   → Schau jetzt im Facebook-Tab: der Post-Dialog sollte aufgehen.")
    else:
        print("❌ Senden fehlgeschlagen (kein Client erreichbar).")
        sys.exit(1)

    print_banner("TEST ABGESCHLOSSEN")


if __name__ == "__main__":
    main()
