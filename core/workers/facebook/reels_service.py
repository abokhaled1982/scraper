# reels/reels_service.py
# Service für Creatomate API zum Rendern von Reels

import os
import pathlib
import time

import requests

from core.logging import get_logger  # noqa: E402
log = get_logger("reels_service")  # noqa: E402

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
        log.info("🚀 Starte Creatomate Render...")
        headers = _make_headers(template_id)
        response = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS, start=1):
            if delay:
                log.info(f"   ⏳ Rate-Limit – warte {delay}s vor Versuch {attempt}...")
                time.sleep(delay)
            response = requests.post(API_URL, headers=headers, json=data)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", delay or 30))
                log.warning(f"   ⚠️  429 Too Many Requests (Versuch {attempt}/{len(_RETRY_DELAYS)+1}) – warte {retry_after}s...")
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

        log.info(f"⏳ Render gestartet (ID: {render_id}). Warte auf Fertigstellung...")

        # Polling bis Status "succeeded" oder "failed"
        status_url = f"{API_URL}/{render_id}"
        while True:
            status_response = requests.get(status_url, headers=headers)
            status_response.raise_for_status()
            status_data = status_response.json()
            status = status_data.get("status")
            log.info(f"   Status: {status}")
            if status == "succeeded":
                log.info(f"✅ Render fertig. URL: {status_data.get('url')}")
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

# ─── typ3_audio Konstanten (Legacy-Fallbacks; primaer wird die Registry verwendet) ────
TYP3_AUDIO_TEMPLATE_ID = "d8285bc5-5ee4-4394-bb95-9d3333742d9d"


def build_typ3_audio_modifications(data: dict) -> dict:
    """
    Erstellt das Modifications-Dict für das typ3_audio-Template aus einem Deal-Dict.
    Exakt dieselbe Struktur wie in creatomate.py (BASE_MODIFICATIONS + Voiceover-SHX.source).
    """
    images: list = data.get("images") or []
    product_image_url: str = (
        (images[0] if images else "")
        or str(data.get("image_url") or "").strip()
    )

    normal_price     = _fmt_price(data.get("original_price"))
    discounted_price = _fmt_price(data.get("price"))

    caption = (
        data.get("reel_caption")
        or data.get("rabatt_text")
        or "🔥 Discount Alert"
    )

    product_name        = str(data.get("title") or "N/A").strip() or "N/A"
    product_description = str(data.get("reel_beschreibung") or data.get("description") or "N/A").strip() or "N/A"
    website             = str(data.get("affiliate_url") or data.get("url") or "www.dealsboss.de").strip()

    voiceover = str(data.get("voiceover_text") or "").strip()
    if not voiceover:
        title = product_name if product_name != "N/A" else "Dieses Produkt"
        discount = str(data.get("discount_percent") or "").replace("N/A", "").strip()
        if discount:
            voiceover = (
                f"Krasses Angebot heute! {title} jetzt {discount} günstiger – "
                f"nur {discounted_price}. Jetzt schnell zuschlagen, Link in der Bio!"
            )
        else:
            voiceover = (
                f"Schnell sein lohnt sich! {title} jetzt für nur {discounted_price}. "
                f"Link in der Bio!"
            )

    # EXAKT dieselbe Reihenfolge/Keys wie creatomate.py
    return {
        "Product-Image.source":     product_image_url,
        "Product-Name.text":        product_name,
        "Product-Description.text": product_description,
        "Normal-Price.text":        normal_price,
        "Discounted-Price.text":    discounted_price,
        "Caption.text":             caption,
        "CTA.text":                 "Folgt uns für mehr Rabatte!",
        "Website.text":             website,
        "Voiceover-C9N.source":     voiceover,
    }


def render_typ3_audio(data: dict, template_id: str | None = None) -> dict:
    """
    Rendert das typ3_audio-Template – exakt wie creatomate.py.

    Verwendet die hardcoded TEMPLATE_ID + API_KEY (gleiche Werte wie creatomate.py),
    damit sich Verhalten in der App und im Test-Script identisch verhalten.
    Der `template_id`-Parameter wird nur genutzt, wenn er explizit überschrieben wird.
    """
    resolved_id = (template_id or "").strip() or TYP3_AUDIO_TEMPLATE_ID

    modifications = build_typ3_audio_modifications(data)

    # API-Key dynamisch aus der Registry (typ3_audio.json -> api_key)
    api_key = _get_api_key(resolved_id) or _DEFAULT_API_KEY
    if not api_key:
        raise ValueError("Kein Creatomate API-Key gefunden (Registry leer und CREATOMATE_API_KEY nicht gesetzt).")

    payload = {
        "template_id": resolved_id,
        "modifications": modifications,
    }
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    log.info(f"[typ3_audio] 🚀 Starte Creatomate Render (template_id={resolved_id})")
    log.info(f"[typ3_audio]    Modifications: {list(modifications.keys())}")

    # POST kann bei Voiceover-Templates >30s dauern (Creatomate validiert/preprocesst
    # ElevenLabs-Audio vor dem Annehmen). Großzügiger Timeout + Retry gegen transiente
    # Read-/Connection-Timeouts.
    _POST_TIMEOUT = 120
    _POST_RETRIES = [10, 30]  # Backoff-Sekunden vor Versuch 2 und 3
    response = None
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + _POST_RETRIES, start=1):
        if delay:
            log.warning(
                f"[typ3_audio]    ⏳ Retry POST (Versuch {attempt}/{len(_POST_RETRIES)+1}) "
                f"nach {delay}s – letzter Fehler: {last_exc}"
            )
            time.sleep(delay)
        try:
            response = requests.post(API_URL, headers=headers, json=payload, timeout=_POST_TIMEOUT)
            break
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt > len(_POST_RETRIES):
                raise Exception(f"Creatomate POST-Timeout nach {attempt} Versuchen: {e}") from e
    assert response is not None  # mypy/safety
    if response.status_code >= 400:
        raise Exception(
            f"Creatomate API-Fehler ({response.status_code}): {response.text[:600]}"
        )
    renders = response.json()
    render_data = renders[0] if isinstance(renders, list) else renders
    render_id = render_data.get("id")
    if not render_id:
        raise ValueError(f"Keine Render-ID in Antwort: {render_data}")

    log.info(f"[typ3_audio] ⏳ Render gestartet (ID: {render_id}). Warte auf Fertigstellung...")

    status_url = f"{API_URL}/{render_id}"
    start = time.time()
    while True:
        if time.time() - start > 300:
            raise TimeoutError(f"Render-Timeout nach 300s (ID: {render_id})")
        status_response = requests.get(status_url, headers=headers, timeout=15)
        status_response.raise_for_status()
        status_data = status_response.json()
        status = status_data.get("status")
        log.info(f"[typ3_audio]    Status: {status}")
        if status == "succeeded":
            log.info(f"[typ3_audio] ✅ Render fertig. URL: {status_data.get('url')}")
            return status_data
        if status in ("failed", "error"):
            raise ValueError(f"Render fehlgeschlagen: {status_data.get('error', status_data)}")
        time.sleep(5)


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
        log.error("[VIDEO] ❌ Keine URL im Render-Ergebnis.")
        return None

    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

    from core.paths import VIDEOS_QUEUE_DIR
    VIDEOS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    local_path = VIDEOS_QUEUE_DIR / f"{product_id}.mp4"

    try:
        log.info(f"⬇️  Lade Video herunter: {video_url}")
        resp = requests.get(video_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        log.info(f"✅ Video gespeichert: {local_path} ({local_path.stat().st_size / 1024 / 1024:.1f} MB)")
        return local_path
    except Exception as e:
        log.error(f"[VIDEO] ❌ Download fehlgeschlagen: {e}")
        return None