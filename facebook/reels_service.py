# reels/reels_service.py
# Service für Creatomate API zum Rendern von Reels

import os
import pathlib
import time

import requests

API_URL = "https://api.creatomate.com/v2/renders"
API_KEY = os.getenv(
    "CREATOMATE_API_KEY",
    "688232cc736747d08d2c0be29bb54729c84559095069862a8bf917abec15d5bdbc0ba11f777157d7df9196ecf340a18f",
)

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

TEMPLATE_ID = "e7704305-e17f-46d9-a4e6-f26a2888bd14"

def render_template(template_id: str, modifications: dict) -> dict:
    """
    Rendert ein Template mit den gegebenen Modifikationen über die Creatomate API.
    Wartet bis der Render abgeschlossen ist und gibt das Ergebnis zurück.
    """
    data = {
        "template_id": template_id,
        "modifications": modifications
    }
    try:
        print("🚀 Starte Creatomate Render...")
        response = requests.post(API_URL, headers=HEADERS, json=data)
        response.raise_for_status()
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
            status_response = requests.get(status_url, headers=HEADERS)
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
        raise Exception(f"Creatomate API-Fehler: {e}")


def render_reel(modifications: dict, template_id: str | None = None) -> dict:
    """Backward-compatible wrapper for reel rendering."""
    return render_template(template_id or TEMPLATE_ID, modifications)

def download_video(render_result: dict, product_id: str) -> pathlib.Path | None:
    """
    Lädt das gerenderte Video von der Creatomate-URL herunter.
    """
    video_url = render_result.get("url")
    if not video_url:
        print("[VIDEO] ❌ Keine URL im Render-Ergebnis.")
        return None

    HERE = pathlib.Path(__file__).resolve().parent
    VIDEOS_FOLDER = HERE / "videos"
    VIDEOS_FOLDER.mkdir(parents=True, exist_ok=True)
    local_path = VIDEOS_FOLDER / f"{product_id}.mp4"

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