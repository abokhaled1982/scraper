# server.py
import asyncio
import json
import signal
import sys
from websockets import serve, WebSocketServerProtocol

HOST = "127.0.0.1"   # nur lokal
PORT = 8765

clients = set()

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
            # Optional: Echo-ACK
            await ws.send(json.dumps({"type": "ack", "ok": True}))
    except Exception as e:
        print(f"[!] Verbindung-Fehler: {e}")
    finally:
        await unregister(ws)

async def broadcast(obj):
    if not clients:
        print("[i] Keine verbundenen Clients.")
        return
    txt = json.dumps(obj)
    await asyncio.gather(*(c.send(txt) for c in list(clients)), return_exceptions=True)
    print(f"[>] Broadcast gesendet an {len(clients)} Client(s).")

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
    print("Kommando: 'p' = Party senden · 'b <Text>' = eigenen Text senden · 'q' = quit")
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.05); continue
        line = line.strip()
        if not line: continue
        if line.lower() == "q":
            print("[i] Beenden…")
            for c in list(clients):
                await c.close()
            return
        if line.lower() == "p":
            await broadcast(example_party_message()); continue
        if line.startswith("b "):
            await broadcast({"type": "send", "text": line[2:]}); continue
        # Standard: ganze Zeile senden
        await broadcast({"type": "send", "text": line})

async def main():
    async with serve(handler, HOST, PORT):
        print(f"[i] WS Server läuft auf ws://{HOST}:{PORT}/  (CTRL+C = quit)")
        await stdin_loop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(loop.shutdown_asyncgens()))
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
        print("[i] Server gestoppt.")
