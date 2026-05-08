# test_reels.py
# Test-Skript für Reels mit Dummy-Daten

import asyncio
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from facebook.reels_processor import process_single_deal
from facebook import fb_service

async def test_with_dummy_data():
    # Erstelle Dummy-Deal-Datei
    dummy_data = {
        "type": "reel",
        "title": "Test Deal",
        "affiliate_url": "https://example.com",
        "discount_percent": 25,
        "images": [
            "https://via.placeholder.com/300x300.png?text=Image1",
            "https://via.placeholder.com/300x300.png?text=Image2",
            "https://via.placeholder.com/300x300.png?text=Image3"
        ]
    }
    
    test_file = HERE / "data" / "out" / "dummy_test.json"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(json.dumps(dummy_data), encoding="utf-8")
    
    sent_ids = set()
    
    print("🧪 Teste Reels-Processing mit Dummy-Daten...")
    print("⚠️  API-Aufruf ist AUSKOMMENTIERT, um Kredit zu sparen!")

    print("📡 Starte Facebook-WebSocket-Server für Addon...")
    fb_service.init()
    print("⏳ Warte auf Addon-Verbindung...")
    connected = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fb_service.ensure_connected(timeout=60),
    )

    if not connected:
        print("❌ Keine Facebook-Extension verbunden. Bitte 'addons/facebook' neu laden und facebook.com öffnen.")
        print("ℹ️ Test bricht ab, weil das Addon nicht verbunden ist.")
    else:
        await process_single_deal(test_file, sent_ids)

    print("✅ Test abgeschlossen. Entferne Dummy-Datei.")
    test_file.unlink(missing_ok=True)

if __name__ == "__main__":
    asyncio.run(test_with_dummy_data())