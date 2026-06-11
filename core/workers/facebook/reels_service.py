# reels/reels_service.py
# Service fuer Creatomate API zum Rendern von Reels

import json
import os
import pathlib
import time
from dataclasses import dataclass

import requests

from core.logging import get_logger  # noqa: E402
from core.workers.facebook.creatomate_accounts import (  # noqa: E402
    CreatomateAccount,
    build_fallback_chain,
    is_no_credit_error,
    kind_for_template_type,
    mark_key_exhausted,
    persist_active_account,
)

log = get_logger("reels_service")  # noqa: E402

API_URL = "https://api.creatomate.com/v2/renders"
_DEFAULT_API_KEY = os.getenv("CREATOMATE_API_KEY", "")

_TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent / "templates"

# Rate-Limit-Retry-Backoffs fuer 429 (gilt pro Account-Versuch).
_RATE_LIMIT_DELAYS = [30, 60, 120]
# Transient-POST-Retry-Backoffs (Connection/Timeout).
_POST_RETRY_DELAYS = [10, 30]

_POST_TIMEOUT_S      = 120
_POLL_INTERVAL_S     = 5
_POLL_DEADLINE_S     = 300
_STATUS_TIMEOUT_S    = 15


# ── Registry-Helfer (Live-Lookup, damit Updates aus persist_active_account sofort wirken) ─
def _read_template_cfg_by_id(template_id: str) -> dict | None:
    """Liest die templates/*.json, die diese template_id traegt."""
    if not template_id or not _TEMPLATES_DIR.is_dir():
        return None
    for fp in _TEMPLATES_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("template_id") or "").strip() == template_id:
            return data
    return None


def _read_template_cfg_by_type(template_type: str) -> dict | None:
    if not template_type or not _TEMPLATES_DIR.is_dir():
        return None
    for fp in _TEMPLATES_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("template_type") or "").strip() == template_type:
            return data
    return None


def _get_api_key(template_id: str | None = None) -> str:
    """Liest den derzeit aktiven api_key fuer eine template_id aus der Registry."""
    if template_id:
        cfg = _read_template_cfg_by_id(template_id)
        if cfg:
            key = str(cfg.get("api_key") or "").strip()
            if key:
                return key
    return _DEFAULT_API_KEY


def _mask(key: str, head: int = 6, tail: int = 4) -> str:
    if len(key) <= head + tail:
        return "***"
    return f"{key[:head]}…{key[-tail:]}"


# ── Render-Outcome ────────────────────────────────────────────────────────────
@dataclass
class _RenderOutcome:
    """Strukturiertes Ergebnis eines einzelnen Render-Versuchs."""
    ok: bool
    data: dict | None = None       # Render-Antwort bei ok=True
    status_code: int | None = None # HTTP-Status des letzten Calls
    error: str | None = None       # menschenlesbarer Fehlertext
    no_credit: bool = False        # True wenn "kein Credit/Plan abgelaufen"


def _perform_render(
    api_key: str,
    template_id: str,
    modifications: dict,
    *,
    log_prefix: str = "",
) -> _RenderOutcome:
    """
    Fuehrt EINEN Render-Versuch (POST + Polling) mit dem gegebenen api_key
    + template_id aus und liefert ein strukturiertes Outcome zurueck.

    * Behandelt 429 (Rate-Limit) mit Retry.
    * Behandelt Connection/Timeout-Fehler beim POST mit Retry.
    * Erkennt "kein Credit"-Fehler und setzt ``no_credit=True``.
    * Wirft KEINE Exception bei HTTP-Fehlern – die ruft der Wrapper aus.
    """
    if not api_key:
        return _RenderOutcome(
            ok=False, error="Kein api_key vorhanden (Registry leer / CREATOMATE_API_KEY nicht gesetzt).",
        )

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "template_id":   template_id,
        "modifications": modifications,
    }

    log.info(
        f"{log_prefix}🚀 Render-Versuch (template_id={template_id}, key={_mask(api_key)})"
    )

    # ── POST mit 429- + Transient-Retry ───────────────────────────────────
    response: requests.Response | None = None
    last_exc: Exception | None = None
    combined_delays = [0] + _POST_RETRY_DELAYS
    for post_attempt, post_delay in enumerate(combined_delays, start=1):
        if post_delay:
            log.warning(
                f"{log_prefix}   ⏳ Retry POST (Versuch {post_attempt}) "
                f"nach {post_delay}s – letzter Fehler: {last_exc}"
            )
            time.sleep(post_delay)
        try:
            response = requests.post(
                API_URL, headers=headers, json=payload, timeout=_POST_TIMEOUT_S,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            response = None
            continue

        # 429 -> Rate-Limit-Backoff im selben POST-Slot.
        if response.status_code == 429:
            retry_after_hdr = response.headers.get("Retry-After")
            for rl_idx, rl_delay in enumerate(_RATE_LIMIT_DELAYS, start=1):
                wait_s = int(retry_after_hdr) if retry_after_hdr else rl_delay
                log.warning(
                    f"{log_prefix}   ⚠️  429 Too Many Requests "
                    f"(Versuch {rl_idx}/{len(_RATE_LIMIT_DELAYS)}) – warte {wait_s}s…"
                )
                time.sleep(wait_s)
                response = requests.post(
                    API_URL, headers=headers, json=payload, timeout=_POST_TIMEOUT_S,
                )
                if response.status_code != 429:
                    break

        break  # POST komplett (ggf. inkl. 429-Backoff)

    if response is None:
        return _RenderOutcome(
            ok=False,
            error=f"POST nach {len(combined_delays)} Versuchen fehlgeschlagen: {last_exc}",
        )

    # ── HTTP-Fehler auswerten ─────────────────────────────────────────────
    body_text = response.text or ""
    if response.status_code >= 400:
        no_credit = is_no_credit_error(response.status_code, body_text)
        return _RenderOutcome(
            ok=False,
            status_code=response.status_code,
            error=f"HTTP {response.status_code}: {body_text[:600]}",
            no_credit=no_credit,
        )

    # ── Render-ID extrahieren ─────────────────────────────────────────────
    try:
        renders = response.json()
    except ValueError:
        return _RenderOutcome(
            ok=False, status_code=response.status_code,
            error="Antwort war kein gueltiges JSON.",
        )
    render = renders[0] if isinstance(renders, list) and renders else renders
    render_id = render.get("id") if isinstance(render, dict) else None
    if not render_id:
        return _RenderOutcome(
            ok=False, status_code=response.status_code,
            error=f"Keine Render-ID in der Antwort: {str(render)[:300]}",
        )

    log.info(
        f"{log_prefix}⏳ Render gestartet (ID: {render_id}). Warte auf Fertigstellung…"
    )

    # ── Polling ───────────────────────────────────────────────────────────
    status_url = f"{API_URL}/{render_id}"
    deadline = time.time() + _POLL_DEADLINE_S
    last_status = ""
    while time.time() < deadline:
        try:
            sr = requests.get(status_url, headers=headers, timeout=_STATUS_TIMEOUT_S)
        except requests.RequestException as exc:
            return _RenderOutcome(
                ok=False, error=f"Polling-Fehler: {exc}",
            )
        if sr.status_code >= 400:
            return _RenderOutcome(
                ok=False, status_code=sr.status_code,
                error=f"Polling HTTP {sr.status_code}: {sr.text[:300]}",
            )
        sd = sr.json()
        last_status = str(sd.get("status") or "")
        log.info(f"{log_prefix}   Status: {last_status}")
        if last_status == "succeeded":
            log.info(f"{log_prefix}✅ Render fertig. URL: {sd.get('url')}")
            return _RenderOutcome(ok=True, data=sd)
        if last_status in ("failed", "error"):
            return _RenderOutcome(
                ok=False, error=f"Render fehlgeschlagen: {sd.get('error', sd)}",
            )
        time.sleep(_POLL_INTERVAL_S)

    return _RenderOutcome(
        ok=False, error=f"Polling-Timeout nach {_POLL_DEADLINE_S}s (zuletzt: {last_status!r})",
    )


# ── Wrapper mit Account-Fallback ──────────────────────────────────────────────
def _render_with_account_fallback(
    template_id: str,
    modifications: dict,
    *,
    log_prefix: str = "",
) -> dict:
    """
    Versucht den Render zuerst mit dem aktuell in der Registry hinterlegten
    Account. Bei "kein Credit"-Fehler iteriert durch den passenden Pool
    (``creatomate/accounts/*_(all|fashon).txt``) und persistiert den
    Sieger-Account zurueck in die Template-JSON.

    Bei jedem anderen Fehler wird sofort abgebrochen (damit wir nicht Credits
    bei mehreren Accounts fuer dasselbe kaputte Payload verbrennen).
    """
    # Template-Type + Account-Kind aus der aktuellen Registry herleiten.
    current_cfg  = _read_template_cfg_by_id(template_id) or {}
    template_type = str(current_cfg.get("template_type") or "").strip()
    kind          = kind_for_template_type(template_type, current_cfg)

    current_api_key = _get_api_key(template_id)

    # ── 1) Erster Versuch mit dem aktuell aktiven Account ─────────────────
    outcome = _perform_render(
        current_api_key, template_id, modifications, log_prefix=log_prefix,
    )
    if outcome.ok:
        return outcome.data or {}

    if not outcome.no_credit:
        raise Exception(
            f"Creatomate API-Fehler (kein Credit-Problem, kein Fallback): {outcome.error}"
        )

    log.warning(
        f"{log_prefix}💸 Aktiver Account hat keine Credits "
        f"(HTTP {outcome.status_code}). Starte Fallback-Kette (kind={kind})."
    )
    mark_key_exhausted(current_api_key, reason=f"HTTP {outcome.status_code}")

    # ── 2) Fallback-Kette ─────────────────────────────────────────────────
    if not template_type:
        raise Exception(
            "Fallback nicht moeglich: aktuelle template_id "
            f"({template_id}) ist nicht in templates/*.json registriert."
        )

    chain = build_fallback_chain(kind, exclude_keys={current_api_key})
    if not chain:
        raise Exception(
            f"Keine Fallback-Accounts verfuegbar (kind={kind}). "
            f"Bitte in creatomate/accounts/ neuen Account hinterlegen."
        )

    last_error = outcome.error or "unknown"
    for idx, acc in enumerate(chain, start=1):
        log.info(
            f"{log_prefix}↪️  Fallback {idx}/{len(chain)}: account={acc.name!r} "
            f"template_id={acc.template_id} key={_mask(acc.api_key)}"
        )
        sub = _perform_render(
            acc.api_key, acc.template_id, modifications,
            log_prefix=log_prefix + f"   [fallback#{idx}] ",
        )
        if sub.ok:
            persist_active_account(template_type, acc)
            log.info(
                f"{log_prefix}🎯 Fallback erfolgreich via {acc.name!r}; "
                f"Template-Registry aktualisiert."
            )
            return sub.data or {}

        if sub.no_credit:
            mark_key_exhausted(acc.api_key, reason=f"HTTP {sub.status_code}")
            last_error = sub.error or last_error
            continue

        # Anderer Fehler -> abbrechen, sonst verbrennen wir nur Credits.
        raise Exception(
            f"Creatomate Fallback abgebrochen (kein Credit-Problem) "
            f"bei account={acc.name!r}: {sub.error}"
        )

    raise Exception(
        f"Alle {len(chain)} Fallback-Accounts (kind={kind}) sind ohne Credits. "
        f"Letzter Fehler: {last_error}"
    )


# ── Public API ────────────────────────────────────────────────────────────────
def render_template(template_id: str, modifications: dict) -> dict:
    """
    Rendert ein Template ueber die Creatomate API.

    Wartet bis der Render abgeschlossen ist und gibt die Render-Antwort zurueck.
    Bei "kein Credit"-Fehler wird automatisch der naechste Account aus dem Pool
    unter ``creatomate/accounts/`` probiert und der funktionierende Account
    persistiert in ``core/workers/facebook/templates/<template>.json``.
    """
    return _render_with_account_fallback(template_id, modifications)


def render_reel(modifications: dict, template_id: str | None = None) -> dict:
    """Backward-compatible Wrapper fuer Reel-Rendering."""
    if not template_id:
        raise ValueError(
            "template_id ist erforderlich – bitte in der Template-JSON-Datei eintragen."
        )
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
    Rendert das typ3_audio-Template ueber die Creatomate API.

    * Aufloesung der template_id:
        1) expliziter Parameter ``template_id`` (falls gesetzt)
        2) aktuelle ``template_id`` aus ``templates/typ3_audio.json``
        3) hartkodierter Legacy-Fallback ``TYP3_AUDIO_TEMPLATE_ID``
    * Account-Fallback (kein Credit) ist automatisch aktiv – siehe
      ``_render_with_account_fallback``.
    """
    resolved_id = (template_id or "").strip()
    if not resolved_id:
        cfg = _read_template_cfg_by_type("typ3_audio")
        if cfg:
            resolved_id = str(cfg.get("template_id") or "").strip()
    if not resolved_id:
        resolved_id = TYP3_AUDIO_TEMPLATE_ID

    modifications = build_typ3_audio_modifications(data)

    log.info(f"[typ3_audio] 🎬 Render-Auftrag (template_id={resolved_id})")
    log.info(f"[typ3_audio]    Modifications: {list(modifications.keys())}")

    return _render_with_account_fallback(
        resolved_id, modifications, log_prefix="[typ3_audio] ",
    )


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