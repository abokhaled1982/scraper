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


def _fmt_price(field_val) -> str:
    """Hilfsfunktion: Normalisiert einen Preiswert (dict oder String) zu einem lesbaren String."""
    if isinstance(field_val, dict):
        raw = field_val.get("raw") or ""
        if raw and raw not in ("N/A", "0", "0.0"):
            return raw
        val = field_val.get("value")
        hint = field_val.get("currency_hint") or "€"
        return f"{val} {hint}" if val else "N/A"
    return str(field_val) if field_val else "N/A"

def build_typ3_audio_modifications(data: dict) -> dict:
    """
    Erstellt das Modifications-Dict für das typ3_audio-Template aus einem Deal-Dict.
    """
    images: list = data.get("images") or []
    product_image_url: str = images[0] if images else ""

    normal_price     = _fmt_price(data.get("original_price"))
    discounted_price = _fmt_price(data.get("price"))

    caption = data.get("reel_caption") or data.get("rabatt_text") or "N/A"

    voiceover = data.get("voiceover_text") or "N/A"
    if voiceover == "N/A":
        title = data.get("title", "Dieses Produkt") or "Dieses Produkt"
        price = data.get("price", "N/A")
        price_str = _fmt_price(price) if isinstance(price, dict) else str(price)
        discount = str(data.get("discount_percent") or "").replace("N/A", "").strip()
        if discount:
            voiceover = (
                f"Krasses Angebot heute! {title} jetzt {discount} günstiger — "
                f"nur {price_str}. Jetzt zuschlagen!"
            )
        else:
            voiceover = (
                f"Schnell sein lohnt sich! {title} jetzt für nur {price_str}. "
                f"Link in der Bio!"
            )

    return {
        "Product-Image.source":     product_image_url,
        "Product-Name.text":        data.get("title", "N/A"),
        "Product-Description.text": data.get("reel_beschreibung", "N/A"),
        "Normal-Price.text":        normal_price,
        "Discounted-Price.text":    discounted_price,
        "Caption.text":             caption,
        "CTA.text":                 "Folgt uns für mehr Rabatte!",
        "Website.text":             data.get("affiliate_url", "N/A"),
        
        # HIER IST DER FIX: .text Suffix hinzugefügt!
        "Voiceover-WZ7.text":       voiceover,
    }


def render_typ3_audio(data: dict, template_id: str | None = None) -> dict:
    """
    Rendert das typ3_audio-Template für ein gegebenes Deal-Dict.

    Liest template_id bevorzugt aus:
      1. dem übergebenen `template_id`-Parameter
      2. dem Deal-Dict (data["template_id"])
      3. der Template-Registry (template_type "typ3_audio")

    Gibt das fertige Creatomate-Render-Result-Dict zurück.
    """
    # Priorität: expliziter Parameter → deal-dict → registry
    resolved_id = (
        template_id
        or str(data.get("template_id") or "").strip()
        or _get_api_key_from_registry("typ3_audio")   # gibt hier template_id, nicht key
    )

    if not resolved_id:
        # Letzter Fallback: direkt aus Registry lesen
        import json as _json
        templates_dir = pathlib.Path(__file__).resolve().parent / "templates"
        for fp in templates_dir.glob("*.json"):
            try:
                cfg = _json.loads(fp.read_text(encoding="utf-8"))
                if cfg.get("template_type") == "typ3_audio":
                    resolved_id = str(cfg.get("template_id") or "").strip()
                    break
            except Exception:
                pass

    if not resolved_id:
        raise ValueError(
            "Kein template_id für typ3_audio gefunden. "
            "Bitte in facebook/templates/typ3_audio.json eintragen."
        )

    modifications = build_typ3_audio_modifications(data)
    print(f"[typ3_audio] Starte Render mit template_id={resolved_id} ...")
    return render_template(resolved_id, modifications)


def _get_api_key_from_registry(template_type: str) -> str:
    """Gibt die template_id (nicht den api_key) für einen template_type aus der Registry zurück."""
    import json as _json
    templates_dir = pathlib.Path(__file__).resolve().parent / "templates"
    if not templates_dir.exists():
        return ""
    for fp in templates_dir.glob("*.json"):
        try:
            cfg = _json.loads(fp.read_text(encoding="utf-8"))
            if cfg.get("template_type") == template_type:
                return str(cfg.get("template_id") or "").strip()
        except Exception:
            pass
    return ""

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