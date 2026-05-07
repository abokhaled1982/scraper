# reels/reels_service.py
# Service für Creatomate API zum Rendern von Reels

import requests
import time
import json
import pathlib

API_URL = "https://api.creatomate.com/v2/renders"
API_KEY = "688232cc736747d08d2c0be29bb54729c84559095069862a8bf917abec15d5bdbc0ba11f777157d7df9196ecf340a18f"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

TEMPLATE_ID = "e7704305-e17f-46d9-a4e6-f26a2888bd14"

def render_reel(modifications: dict) -> dict:
    """
    Rendert ein Reel mit den gegebenen Modifikationen.
    Gibt das Render-Ergebnis zurück.
    """
    print("🧪 MOCK: Simuliere API-Aufruf (kein echter Render)")
    # Simuliere erfolgreichen Render
    return {
        "id": "mock_render_id",
        "status": "succeeded",
        "url": "https://example.com/mock_video.mp4"
    }
    
    # Echter Code (auskommentiert für Test)
    # data = {
    #     "template_id": TEMPLATE_ID,
    #     "modifications": modifications
    # }
    # try:
    #     response = requests.post(API_URL, headers=HEADERS, json=data)
    #     response.raise_for_status()
    #     render_data = response.json()
    #     render_id = render_data.get("id")
    #     if not render_id:
    #         raise ValueError("Keine Render-ID in der Antwort")
    #     
    #     # Warte auf Fertigstellung
    #     while True:
    #         status_response = requests.get(f"{API_URL}/{render_id}", headers=HEADERS)
    #         status_response.raise_for_status()
    #         status_data = status_response.json()
    #         status = status_data.get("status")
    #         if status == "succeeded":
    #             return status_data
    #         elif status == "failed":
    #             raise ValueError(f"Render fehlgeschlagen: {status_data.get('error')}")
    #         time.sleep(5)  # Warte 5 Sekunden
    # except requests.RequestException as e:
    #     raise Exception(f"API-Fehler: {e}")

def download_video(render_result: dict, product_id: str) -> pathlib.Path | None:
    """
    Lädt das gerenderte Video herunter. Bei Test nutzt es die existierende dummy.mp4.
    """
    print("🧪 MOCK: Simuliere Video-Download")
    HERE = pathlib.Path(__file__).resolve().parent
    VIDEOS_FOLDER = HERE / "videos"
    VIDEOS_FOLDER.mkdir(parents=True, exist_ok=True)
    
    # Immer dummy.mp4 für Tests benutzen, wenn sie existiert
    dummy_mp4 = VIDEOS_FOLDER / "dummy.mp4"
    if dummy_mp4.exists():
        print(f"✅ Benutze existierende dummy.mp4 ({dummy_mp4.stat().st_size} bytes)")
        return dummy_mp4
    
    # Fallback: Erstelle eine Minimal-MP4 (wenn dummy.mp4 fehlt)
    local_path = VIDEOS_FOLDER / f"{product_id}.mp4"
    print(f"⚠️  dummy.mp4 nicht gefunden, erstelle Fallback-MP4: {local_path}")
    
    # Minimal-MP4 Header 
    mp4_bytes = bytes([
        0x00, 0x00, 0x00, 0x20, 0x66, 0x74, 0x79, 0x70,
        0x69, 0x73, 0x6f, 0x6d, 0x00, 0x00, 0x00, 0x00,
        0x69, 0x73, 0x6f, 0x6d, 0x69, 0x73, 0x6f, 0x32,
        0x6d, 0x70, 0x34, 0x31, 0x69, 0x73, 0x6f, 0x6d
    ])
    local_path.write_bytes(mp4_bytes)
    return local_path
    
    # Echter Code (auskommentiert)
    # video_url = render_result.get("url")
    # if not video_url:
    #     return None
    # import pathlib
    # HERE = pathlib.Path(__file__).resolve().parent
    # VIDEOS_FOLDER = HERE / "videos"
    # VIDEOS_FOLDER.mkdir(parents=True, exist_ok=True)
    # local_path = VIDEOS_FOLDER / f"{product_id}.mp4"
    # try:
    #     resp = requests.get(video_url, stream=True)
    #     resp.raise_for_status()
    #     with open(local_path, "wb") as f:
    #         for chunk in resp.iter_content(chunk_size=65536):
    #             f.write(chunk)
    #     return local_path
    # except Exception as e:
    #     print(f"[VIDEO] Download fehlgeschlagen: {e}")
    #     return None

# Beispiel-Modifikationen
# modifications = {
#     "Product Image 3.source": "https://...",
#     "Product Offer 3.text": "-30%",
#     "Product Image 2.source": "https://...",
#     "Product Offer 2.text": "-25%",
#     "Product Image 1.source": "https://...",
#     "Product Offer 1.text": "-20%",
#     "Call to Action.text": "See you at\nwww.mybrand.com"
# }