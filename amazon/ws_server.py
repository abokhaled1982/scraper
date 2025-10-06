# ws_server.py — single-save per stream (URL+ID.html naming)
import asyncio
import base64
import json
import pathlib
from datetime import datetime
from typing import Dict, Any
from urllib.parse import urlsplit, urlunsplit
import sys

# add parent directory of amazon/ to sys.path
sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))

# === zentrale Config & Pfade ===
from config import (
    WS_HOST as HOST,
    WS_PORT as PORT,
    INBOX_DIR,
    PRODUCKT_DIR,         # <- Sibling von inbox
    
)

# websockets erst nach config importieren (reine Ordnungssache)
import websockets





# --- toggles ---
HANDLE_PARSED = False   # set True if you want to also save single-shot messages
SEND_ACK_EVERY = 10     # send ack after every N chunks; set 0 to disable

# --- helpers ---

def choose_target_path(url: str, transfer_id: str, doc_type: str | None) -> pathlib.Path:
    """
    - docType == "product"  → speichere in PRODUCKT_DIR
    - sonst                 → speichere in SAVE_DIR (inbox)
    """
    base = safe(canonical_url(url))
    if base.endswith(".html"):
        base = base[:-5]

    if doc_type and str(doc_type).lower() == "product":
        return PRODUCKT_DIR / f"{base}_{transfer_id}.html"
    return INBOX_DIR / f"{base}_{transfer_id}.html"


def canonical_url(u: str) -> str:
    try:
        p = urlsplit(u or "")
        return urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))
    except Exception:
        return u or "page"


def safe(name: str) -> str:
    s = (name or "page").replace("://", "_").replace("/", "_")
    s = s.replace("?", "_").replace("#", "_").replace("&", "_").replace("=", "_")
    return "".join(ch for ch in s if ch.isalnum() or ch in "._-")[:120]


def path_for(url: str, transfer_id: str) -> pathlib.Path:
    base = safe(canonical_url(url))
    if base.endswith(".html"):
        base = base[:-5]
    return SAVE_DIR / f"{base}_{transfer_id}.html"


# --- in-memory state ---
assemblies: Dict[str, Dict[str, Any]] = {}
saved_ids: set[str] = set()


async def handle(ws):
    print(f"[srv] client connected from {ws.remote_address}")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception as e:
                print("[srv] bad json:", e)
                continue

            t = msg.get("type")

            # --- optional one-shot handler ---
            if t == "parsed":
                if not HANDLE_PARSED:
                    await ws.send(json.dumps({"ok": True, "skipped": True, "reason": "parsed_ignored"}))
                    continue
                url = msg.get("url") or "unknown"
                html = msg.get("html", "")
                gen_id = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
                out = path_for(url, gen_id)
                out.write_text(html, encoding="utf-8")
                print(f"[srv] saved → {out} (parsed)")
                await ws.send(json.dumps({"ok": True, "saved": str(out), "id": gen_id}))
                continue

            # --- streaming begin ---
            if t == "begin":
                _id = str(msg["id"])
                total = int(msg.get("total", 0))
                url = msg.get("url", "")
                doc_type = msg.get("docType")
                prev = assemblies.get(_id)
                chunks = prev["chunks"] if prev and "chunks" in prev else {}
                assemblies[_id] = {"total": total, "chunks": chunks, "url": url, "docType": doc_type}
                print(f"[srv] begin id={_id} total={total} url={url} docType={doc_type}")
                await ws.send(json.dumps({"ok": True, "type": "begin_ack", "id": _id}))
                continue

            # --- chunk ---
            if t == "chunk":
                _id = str(msg["id"])
                seq = int(msg["seq"])
                b64 = msg.get("data", "")
                try:
                    data = base64.b64decode(b64)
                except Exception as e:
                    print(f"[srv] decode error id={_id} seq={seq}: {e}")
                    await ws.send(json.dumps({"ok": False, "error": "decode", "id": _id, "seq": seq}))
                    continue
                a = assemblies.setdefault(_id, {"total": msg.get("total", 0), "chunks": {}, "url": msg.get("url", "")})
                a["chunks"][seq] = data
                if SEND_ACK_EVERY and (seq % SEND_ACK_EVERY == 0):
                    await ws.send(json.dumps({"ok": True, "type": "ack", "id": _id, "seq": seq}))
                continue

            # --- end ---
            if t == "end":
                _id = str(msg["id"])
                if _id in saved_ids:
                    print(f"[srv] already saved id={_id}, skip")
                    await ws.send(json.dumps({"ok": True, "skipped": True, "reason": "already_saved", "id": _id}))
                    continue
                a = assemblies.get(_id)
                if not a:
                    print(f"[srv] end without begin id={_id}")
                    await ws.send(json.dumps({"ok": False, "error": "no_begin", "id": _id}))
                    continue
                total = a["total"] or (max(a["chunks"].keys()) + 1 if a["chunks"] else 0)
                missing = sorted(set(range(total)) - set(a["chunks"].keys()))
                if missing:
                    print(f"[srv] missing chunks id={_id}: {len(missing)} → {missing[:10]}…")
                    await ws.send(json.dumps({"ok": False, "error": "missing_chunks", "id": _id, "missing": len(missing)}))
                    continue
                ordered = b"".join(a["chunks"][i] for i in range(total))
                try:
                    html = ordered.decode("utf-8", errors="replace")
                except Exception:
                    html = ordered.decode("latin-1", errors="replace")
                url = a.get("url", "unknown")
                doc_type = a.get("docType")
                out = choose_target_path(url, _id, doc_type)
                out.write_text(html, encoding="utf-8")
                saved_ids.add(_id)
                assemblies.pop(_id, None)
                print(f"[srv] saved → {out} (stream, docType={doc_type})")
                await ws.send(json.dumps({"ok": True, "saved": str(out), "id": _id}))
                continue

            # --- unknown ---
            print("[srv] unknown message:", msg)

    finally:
        print("[srv] client disconnected")


async def main():
    async with websockets.serve(handle, HOST, PORT, max_size=None, ping_interval=30):
        print(f"[srv] listening on ws://{HOST}:{PORT} (single-save mode, url+id naming, product routing)")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
