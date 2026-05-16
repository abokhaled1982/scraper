# reels/reels_service.py
# Service für Creatomate API zum Rendern von Reels

import os
import pathlib
import time

import requests

API_URL = "https://api.creatomate.com/v2/renders"
_DEFAULT_API_KEY = os.getenv("CREATOMATE_API_KEY", "")


def _get_api_key(template_id: str | None = None) -> str:
    """Liest api_key aus dem Template-Registry, falls vorhanden. Fallback: Umgebungsvariable."""
    if template_id:
        import json as _json
        import pathlib as _pl
        templates_dir = _pl.Path(__file__).resolve().parent / "templates"
        for fp in templates_dir.glob("*.json"):
            try:
                data = _json.loads(fp.read_text(encoding="utf-8"))
                if data.get("template_id") == template_id:
                    key = str(data.get("api_key") or "").strip()
                    if key:
                        return key
            except Exception:
                pass
    return _DEFAULT_API_KEY


def _make_headers(template_id: str | None = None) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_api_key(template_id)}",
    }

_RETRY_DELAYS = [30, 60, 120]  # Sekunden Wartezeit bei 429 (3 Versuche)

def render_template(template_id: str, modifications: dict) -> dict:
    """
    Rendert ein Template mit den gegebenen Modifikationen über die Creatomate API.
    Wartet bis der Render abgeschlossen ist und gibt das Ergebnis zurück.
    Bei 429 Too Many Requests wird automatisch mit Wartezeit wiederholt.
    """
    data = {
        "template_id": template_id,
        "modifications": modifications,
    }
    try:
        print("🚀 Starte Creatomate Render...")
        headers = _make_headers(template_id)
        response = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS, start=1):
            if delay:
                print(f"   ⏳ Rate-Limit – warte {delay}s vor Versuch {attempt}...")
                time.sleep(delay)
            response = requests.post(API_URL, headers=headers, json=data)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", delay or 30))
                print(f"   ⚠️  429 Too Many Requests (Versuch {attempt}/{len(_RETRY_DELAYS)+1}) – warte {retry_after}s...")
                if attempt <= len(_RETRY_DELAYS):
                    time.sleep(retry_after)
                    continue
                response.raise_for_status()
            else:
                response.raise_for_status()
                break
        renders = response.json()

        # API gibt eine Liste zurück
        if isinstance(renders, list):
            render_data = renders[0]
        else:
            render_data = renders

        render_id = render_data.get("id")
        if not render_id:
            raise ValueError(f"Keine Render-ID in der Antwort: {render_data}")

        print(f"⏳ Render gestartet (ID: {render_id}). Warte auf Fertigstellung...")

        # Polling bis Status "succeeded" oder "failed"
        status_url = f"{API_URL}/{render_id}"
        while True:
            status_response = requests.get(status_url, headers=headers)
            status_response.raise_for_status()
            status_data = status_response.json()
            status = status_data.get("status")
            print(f"   Status: {status}")
            if status == "succeeded":
                print(f"✅ Render fertig. URL: {status_data.get('url')}")
                return status_data
            elif status in ("failed", "error"):
                raise ValueError(f"Render fehlgeschlagen: {status_data.get('error', status_data)}")
            time.sleep(5)
    except requests.RequestException as e:
        # Bei HTTP-Fehlern: Body mit ausgeben, damit Creatomate-Validierungsfehler sichtbar sind
        body = ""
        try:
            if response is not None:
                body = response.text[:800]
        except Exception:
            pass
        raise Exception(f"Creatomate API-Fehler: {e}" + (f" | Body: {body}" if body else ""))


def render_reel(modifications: dict, template_id: str | None = None) -> dict:
    """Backward-compatible wrapper for reel rendering."""
    if not template_id:
        raise ValueError("template_id ist erforderlich – bitte in der Template-JSON-Datei eintragen.")
    return render_template(template_id, modifications)

def download_video(render_result: dict, product_id: str) -> pathlib.Path | None:
    """
    Lädt das gerenderte Video von der Creatomate-URL herunter.
    Speichert in data/media/videos/queue/ (noch nicht gepostet).
    """
    video_url = render_result.get("url")
    if not video_url:
        print("[VIDEO] ❌ Keine URL im Render-Ergebnis.")
        return None

    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from config import VIDEOS_QUEUE_DIR
    VIDEOS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    local_path = VIDEOS_QUEUE_DIR / f"{product_id}.mp4"

    try:
        print(f"⬇️  Lade Video herunter: {video_url}")
        resp = requests.get(video_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        print(f"✅ Video gespeichert: {local_path} ({local_path.stat().st_size / 1024 / 1024:.1f} MB)")
        return local_path
    except Exception as e:
        print(f"[VIDEO] ❌ Download fehlgeschlagen: {e}")
        return None