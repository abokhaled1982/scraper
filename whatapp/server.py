# server.py
import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from websockets import serve, WebSocketServerProtocol

HOST = "127.0.0.1"   # nur lokal
PORT = 8765

BASE_DIR = Path(__file__).resolve().parent
OFFER_PATH = BASE_DIR / "angebot.json"   # <--- robust: neben server.py

clients = set()

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
            await ws.send(json.dumps({"type": "ack", "ok": True}))
    except Exception as e:
        print(f"[!] Verbindung-Fehler: {e}")
    finally:
        await unregister(ws)

async def broadcast(obj: Dict[str, Any]):
    if not clients:
        print("[i] Keine verbundenen Clients.")
        return
    txt = json.dumps(obj, ensure_ascii=False)
    await asyncio.gather(*(c.send(txt) for c in list(clients)), return_exceptions=True)
    print(f"[>] Broadcast gesendet an {len(clients)} Client(s).")

# -------------------- Deal-Formatter --------------------

def _fmt_price(p: Optional[Dict[str, Any]]) -> Optional[str]:
    if not p:
        return None
    raw = p.get("raw")
    val = p.get("value")
    return raw or (f"{val:.2f} €" if isinstance(val, (int, float)) else None)

def format_offer_text(offer: Dict[str, Any]) -> str:
    title      = offer.get("title") or "Angebot"
    brand      = offer.get("brand")
    market     = offer.get("market") or offer.get("seller_name")
    price      = _fmt_price(offer.get("price"))
    orig       = _fmt_price(offer.get("original_price"))
    discount_p = offer.get("discount_percent")
    coupon     = (offer.get("coupon") or {}).get("code")
    coupon_more= (offer.get("coupon") or {}).get("more")
    avail      = offer.get("availability")
    shipping   = offer.get("shipping_info")
    rating     = offer.get("rating") or {}
    rating_val = rating.get("value")
    rating_ct  = rating.get("counts")
    url        = offer.get("affiliate_url") or offer.get("url")

    lines = []
    lines.append(f"🎁 *{title}*")
    if brand:  lines.append(f"🏷️ Marke: {brand}")

    if price and orig and orig != "None":
        lines.append(f"💶 Preis: {price}  (statt ~{orig}~{f', {discount_p}' if discount_p else ''})")
    elif price:
        lines.append(f"💶 Preis: {price}{f' ({discount_p})' if discount_p else ''}")
    elif discount_p:
        lines.append(f"💶 Rabatt: {discount_p}")

    if coupon:
        lines.append(f"🏷️ Gutschein: {coupon}")
        if coupon_more: lines.append(f"ℹ️ {coupon_more}")

    if market:  lines.append(f"🛍️ Marktplatz: {market}")
    if avail:   lines.append(f"✅ Status: {avail}")
    if shipping:lines.append(f"🚚 Versand: {shipping}")

    if isinstance(rating_val, (int, float)) and rating_val > 0:
        star = "⭐" * max(1, min(5, int(round(rating_val))))
        lines.append(f"📊 Bewertung: {rating_val:.1f} {star} ({rating_ct} Bewertungen)")

    if url:
        lines.append("")
        lines.append(f"🔗 *Direkt zum Angebot:* {url}")

    lines.append("")
    lines.append("🟢 Deal live – viel Spaß beim Schnäppchen! 🚀")
    return "\n".join(lines).strip()

def load_offer(path: Path) -> Dict[str, Any]:
    print(f"[i] Lade Angebot aus: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("angebot.json muss ein JSON-Objekt sein")
    return data

async def send_offer_from_file():
    if not OFFER_PATH.exists():
        print(f"[!] Angebotsdatei fehlt: {OFFER_PATH.name} (erwartet unter {OFFER_PATH})")
        return
    try:
        offer = load_offer(OFFER_PATH)
        text = format_offer_text(offer)
        await broadcast({"type": "send", "text": text})
    except Exception as e:
        print(f"[!] Fehler beim Lesen/Formatieren: {e}")

# -------------------- Extras --------------------

def example_party_message():
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
    print("  'o'  → Angebot aus angebot.json senden")
    print("  'p'  → Party-Nachricht senden")
    print("  'b <Text>' → freien Text senden")
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

        await broadcast({"type": "send", "text": line})

async def main():
    async with serve(handler, HOST, PORT):
        print(f"[i] WS Server läuft auf ws://{HOST}:{PORT}/  (CTRL+C = quit)")
        print(f"[i] Erwartete Angebotsdatei: {OFFER_PATH}")
        await stdin_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[i] Server gestoppt.")
