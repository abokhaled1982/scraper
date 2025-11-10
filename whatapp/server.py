# server.py
import asyncio
import json
import signal
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
import base64 

from websockets import serve, WebSocketServerProtocol

HOST = "127.0.0.1"     # nur lokal
PORT = 8765

BASE_DIR = Path(__file__).resolve().parent
OFFER_PATH = BASE_DIR / "angebot.json"   # erwartet: liegt neben server.py

clients: set[WebSocketServerProtocol] = set()

# -------------------- WebSocket Basics --------------------

async def register(ws: WebSocketServerProtocol):
    clients.add(ws)
    print(f"[+] Client verbunden: {ws.remote_address} (gesamt: {len(clients)})")

async def unregister(ws: WebSocketServerProtocol):
    clients.discard(ws)
    print(f"[-] Client getrennt: {ws.remote_address} (gesamt: {len(clients)})")

async def handler(ws: WebSocketServerProtocol):
    await register(ws)
    try:
        async for msg in ws:
            print(f"[<] {ws.remote_address}: {msg}")
            # kleines ACK
            await ws.send(json.dumps({"type": "ack", "ok": True}, ensure_ascii=False))
    except Exception as e:
        print(f"[!] Verbindung-Fehler: {e}")
    finally:
        await unregister(ws)

async def broadcast(obj: Dict[str, Any]):
    """Sende ein JSON-Objekt an alle verbundenen Clients."""
    if not clients:
        print("[i] Keine verbundenen Clients.")
        return
    txt = json.dumps(obj, ensure_ascii=False)
    await asyncio.gather(*(c.send(txt) for c in list(clients)), return_exceptions=True)
    print(f"[>] Broadcast gesendet an {len(clients)} Client(s).")

# -------------------- Angebot laden & formatieren --------------------

def _fmt_price(p: Optional[Dict[str, Any]]) -> Optional[str]:
    if not p:
        return None
    raw = p.get("raw")
    val = p.get("value")
    return raw or (f"{val:.2f} €" if isinstance(val, (int, float)) else None)

def format_offer_text(offer: Dict[str, Any]) -> str:
    """
    Baut den Text, der als Bildunterschrift verwendet wird (OHNE Links).
    """
    title       = offer.get("title") or "Angebot"
    brand       = offer.get("brand")
    market      = offer.get("market") or offer.get("seller_name")
    price       = _fmt_price(offer.get("price"))
    orig        = _fmt_price(offer.get("original_price"))
    discount_p  = offer.get("discount_percent")
    coupon      = (offer.get("coupon") or {}).get("code")
    coupon_more = (offer.get("coupon") or {}).get("more")
    avail       = offer.get("availability")
    shipping    = offer.get("shipping_info")
    rating      = offer.get("rating") or {}
    rating_val  = rating.get("value")
    rating_ct   = rating.get("counts")

    lines: list[str] = []
    lines.append(f"🎁 *{title}*")
    if brand:
        lines.append(f"🏷️ Marke: {brand}")

    if price and orig and orig != "None":
        lines.append(f"💶 Preis: {price}  (statt ~{orig}~{f', {discount_p}' if discount_p else ''})")
    elif price:
        lines.append(f"💶 Preis: {price}{f' ({discount_p})' if discount_p else ''}")
    elif discount_p:
        lines.append(f"💶 Rabatt: {discount_p}")

    if coupon:
        lines.append(f"🏷️ Gutschein: {coupon}")
        if coupon_more:
            lines.append(f"ℹ️ {coupon_more}")

    if market:
        lines.append(f"🛍️ Marktplatz: {market}")
    if avail:
        lines.append(f"✅ Status: {avail}")
    if shipping:
        lines.append(f"🚚 Versand: {shipping}")

    if isinstance(rating_val, (int, float)) and rating_val > 0:
        stars = "⭐" * max(1, min(5, int(round(rating_val))))
        lines.append(f"📊 Bewertung: {rating_val:.1f} {stars} ({rating_ct} Bewertungen)")

    lines.append("")
    lines.append("🟢 Deal live – viel Spaß beim Schnäppchen! 🚀")
    return "\n".join(lines).strip()

def extract_image_url(offer: Dict[str, Any]) -> Optional[str]:
    images = offer.get("images", [])
    if images and isinstance(images, list) and images[0]:
        return images[0]
    
    return (
        offer.get("image_url")
        or offer.get("image")
        or None
    )

def normalize_offer_data(data: Any) -> Dict[str, Any]:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            raise ValueError(f"angebot.json enthält String, der nicht erneut JSON ist: {e}")

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                data = item
                break
        else:
            raise ValueError("angebot.json ist eine Liste ohne Dict-Elemente.")

    if isinstance(data, dict) and "offer" in data and isinstance(data["offer"], dict):
        data = data["offer"]

    if not isinstance(data, dict):
        raise ValueError(f"angebot.json hat unerwarteten Typ: {type(data).__name__} (erwartet dict)")

    return data

def load_offer(path: Path) -> Dict[str, Any]:
    print(f"[i] Lade Angebot aus: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = raw
    return normalize_offer_data(data)

def load_image_as_base64(image_url: str) -> Optional[str]:
    """Lädt ein Bild von einer URL und kodiert es als Base64-String."""
    try:
        # Lade das Bild binär von der URL
        with urllib.request.urlopen(image_url) as response:
            image_data = response.read()
        
        base64_encoded = base64.b64encode(image_data).decode('utf-8')
        
        mime_type = response.info().get_content_type()
        print(f"[i] Bild geladen ({len(image_data)} Bytes, {mime_type}) und als Base64 kodiert.")
        return f"data:{mime_type};base64,{base64_encoded}"
    except Exception as e:
        print(f"[!] Konnte Bild nicht als Base64 laden: {e}")
        return None

# HINZUFÜGEN: Temporäre Speicherung des Bildes
def download_image_to_temp(image_url: str) -> Optional[Path]:
    """Lädt ein Bild von einer URL und speichert es temporär."""
    try:
        # Lade das Bild binär von der URL
        with urllib.request.urlopen(image_url) as response:
            image_data = response.read()
        
        # Erstelle eine temporäre Datei
        # `tempfile.NamedTemporaryFile` speichert das Bild auf der Festplatte
        # und gibt den Pfad zurück. delete=False, damit die Datei nach dem Schließen
        # des Context-Managers nicht sofort gelöscht wird, sondern erst später.
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(image_data)
            temp_path = Path(tmp_file.name)
        
        # Versuche, die ursprüngliche Dateierweiterung hinzuzufügen, falls möglich
        mime_type = response.info().get_content_type()
        ext = ".jpg" # Default-Erweiterung
        if "png" in mime_type:
             ext = ".png"
        elif "gif" in mime_type:
             ext = ".gif"
        # Die temporäre Datei umbenennen, um die korrekte Endung zu haben
        new_path = temp_path.with_suffix(ext)
        temp_path.rename(new_path)
        
        print(f"[i] Bild temporär gespeichert unter: {new_path}")
        return new_path
    except Exception as e:
        print(f"[!] Konnte Bild nicht temporär speichern: {e}")
        return None

async def send_offer_from_file():
    if not OFFER_PATH.exists():
        print(f"[!] Angebotsdatei fehlt: {OFFER_PATH.name} (erwartet unter {OFFER_PATH})")
        return

    temp_image_path: Optional[Path] = None
    try:
        offer = load_offer(OFFER_PATH)
        text_body = format_offer_text(offer)
        
        img_url = extract_image_url(offer)
        affiliate_url = offer.get("affiliate_url") or offer.get("url")
        
        # Text-Body + Link werden zur Bildunterschrift
        final_caption = text_body
        if affiliate_url:
            # Der Link wird in die Bildunterschrift (Caption) eingefügt
            final_caption += f"\n\n🔗 *Direkt zum Angebot:*\n{affiliate_url}"
        
        
        # NEUE LOGIK: Bild temporär speichern, um den lokalen Pfad zu erhalten
        if img_url:
            temp_image_path = download_image_to_temp(img_url)
        
        
        # Payload für den Versand vorbereiten
        if temp_image_path:
            # Sende als "openMediaPicker" mit dem lokalen Pfad
            # Der Client wird diesen Pfad öffnen und das Bild hochladen/senden
            payload: Dict[str, Any] = {
                "type": "openMediaPicker", 
                "path": str(temp_image_path), # Hier ist der Pfad als String
                "caption": final_caption # Der gesamte Text wird die Bildunterschrift
            }
        else:
            # Fallback auf einfachen Textversand
            payload: Dict[str, Any] = {"type": "send", "text": final_caption}

        await broadcast(payload)

    except Exception as e:
        print(f"[!] Fehler beim Lesen/Formatieren: {e}")
    
    finally:
        # AUFRÄUMEN: Temporäre Datei löschen
        if temp_image_path and temp_image_path.exists():
            try:
                # temp_image_path.unlink()
                print(f"[i] Temporäre Bilddatei gelöscht: {temp_image_path}")
            except Exception as e:
                print(f"[!] Konnte temporäre Datei nicht löschen: {e}")
# -------------------- Extras und Main --------------------

def example_party_message() -> Dict[str, Any]:
    return {
        "type": "send",
        "text": (
            "🎉 HEY FRIENDS! 🎉\n"
            "Es ist wieder soweit – Party-Time steht an! 🥳\n\n"
            "📅 Datum: Samstag, 9. November\n"
            "🕒 Uhrzeit: ab 20:00 Uhr\n"
            "📍 Ort: Bei mir zuhause (Adresse auf Anfrage 🏠)\n\n"
            "🎵 Coole Musik\n"
            "🍹 Drinks & Snacks\n"
            "💃 Gute Laune garantiert!\n\n"
            "Bring deine besten Vibes & gerne 1–2 Freunde mit.\n"
            "Sag mir kurz Bescheid, ob du kommst ✅\n\n"
            "✨ Dresscode: „Glow & Fun“ – etwas, das leuchtet oder funkelt 😎\n\n"
            "Let’s make it a night to remember! 🌙\n"
            "#PartyModeOn 💫"
        )
    }

async def stdin_loop():
    print("Kommando:")
    print("  'o'  → Angebot aus angebot.json senden (versucht Bild-Upload)")
    print("  'p'  → Party-Nachricht senden")
    print("  'b <Text>' → freien Text senden")
    print("  'm <image>' → offne image dialog")
    print("  'q'  → quit")
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.05)
            continue
        line = line.strip()
        if not line:
            continue

        if line.lower() == "q":
            print("[i] Beenden…")
            for c in list(clients):
                await c.close()
            return

        if line.lower() == "o":
            await send_offer_from_file()
            continue

        if line.lower() == "p":
            await broadcast(example_party_message())
            continue

        if line.startswith("b "):
            await broadcast({"type": "send", "text": line[2:]})
            continue
        if line.lower() == "m":           # m = media picker öffnen
            #await broadcast({"type": "openMediaPicker"})
            await send_offer_from_file()
            continue

        # Standard: ganze Zeile senden
        await broadcast({"type": "send", "text": line})

async def main():
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(_shutdown(loop)))

    async with serve(handler, HOST, PORT):
        print(f"[i] WS Server läuft auf ws://{HOST}:{PORT}/  (CTRL+C = quit)")
        print(f"[i] Erwartete Angebotsdatei: {OFFER_PATH}")
        await stdin_loop()

async def _shutdown(loop):
    print("\n[i] Server-Shutdown eingeleitet...")
    for c in list(clients):
        await c.close()
    loop.stop()
    print("[i] Server gestoppt.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass