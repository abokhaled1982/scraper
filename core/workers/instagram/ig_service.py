# instagram/ig_service.py
# instagrapi-Wrapper: Login, Session-Verwaltung, Photo/Reel-Upload

import json
import logging
import os
import pathlib
import struct
import time

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("ig_service")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [IG] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

IG_USERNAME     = os.getenv("IG_USERNAME", "")
IG_PASSWORD     = os.getenv("IG_PASSWORD", "")
IG_SESSION_FILE = pathlib.Path(os.getenv("IG_SESSION_FILE", ".sessions/ig_session.json"))

if not IG_USERNAME or not IG_PASSWORD or IG_PASSWORD == "CHANGE_ME":
    raise SystemExit(
        "❌ IG_USERNAME und IG_PASSWORD müssen in .env gesetzt sein.\n"
        "   Bitte IG_PASSWORD=<dein_passwort> in .env eintragen."
    )

_client = None  # Singleton


# ── Monkey-Patch: instagrapi Upload-Parameter für bessere Qualität ────────
def _patch_clip_upload_quality():
    """
    Patcht instagrapi's clip_upload() damit bessere Upload-Metadaten
    an Instagram gesendet werden. Ohne Patch sendet instagrapi:
      video_bit_rate_bps=0, quality="", transcoding_required=True, network="ig_dummy"
    Das führt dazu, dass Instagram das Video aggressiv re-encoded.
    """
    try:
        import instagrapi.mixins.clip as clip_mod
        _orig_clip_upload = clip_mod.UploadClipMixin.clip_upload

        import functools

        @functools.wraps(_orig_clip_upload)
        def _patched_clip_upload(self, path, caption, **kwargs):
            # Vor dem Upload: private.post monkey-patchen um upload_settings zu fixen
            _orig_post = self.private.post

            def _patched_post(url, *args, **kw):
                # Nur upload_settings-Request patchen
                if "upload_settings" in str(url) and "data" in kw:
                    try:
                        data = kw["data"]
                        if isinstance(data, str):
                            settings = json.loads(data)
                            props = settings.get("upload_setting_properties", {})
                            video = props.get("video", {})
                            ctx = props.get("context", {})
                            creative = props.get("creative_tools", {})
                            network = props.get("network", {})

                            # Bitrate aus Dateigröße und Dauer berechnen
                            file_size = video.get("video_original_file_size", 0)
                            duration_ms = video.get("video_duration_milliseconds", 0)
                            if file_size and duration_ms:
                                bitrate = int((file_size * 8) / (duration_ms / 1000))
                                video["video_bit_rate_bps"] = bitrate
                                video["audio_bit_rate_bps"] = 128000  # 128 kbps AAC

                            # Codec-Info setzen
                            video["source_video_codec"] = "h264"
                            video["audio_codec_type"] = "aac"

                            # Qualität auf HD setzen
                            ctx["quality"] = "hd"

                            # Transmuxing statt Transcoding (wenn möglich)
                            creative["transmuxing_eligible"] = True
                            creative["transcoding_required"] = False

                            # Netzwerk als gut melden
                            network["download_latency_connection_quality"] = "EXCELLENT"
                            network["network_connection_name"] = "WIFI"
                            network["download_bandwidth_connection_quality"] = "EXCELLENT"

                            props["video"] = video
                            props["context"] = ctx
                            props["creative_tools"] = creative
                            props["network"] = network
                            settings["upload_setting_properties"] = props

                            new_data = json.dumps(settings)
                            kw["data"] = new_data
                            # Content-Length Header updaten
                            if "headers" in kw:
                                new_len = str(len(new_data.encode("utf-8")))
                                kw["headers"]["Content-Length"] = new_len
                                kw["headers"]["X-Entity-Length"] = new_len
                            logger.info("✅ Upload-Metadaten gepatcht (HD-Qualität)")
                    except Exception as e:
                        logger.warning(f"⚠️ Upload-Settings Patch fehlgeschlagen: {e}")
                return _orig_post(url, *args, **kw)

            self.private.post = _patched_post
            try:
                return _orig_clip_upload(self, path, caption, **kwargs)
            finally:
                self.private.post = _orig_post  # Original wiederherstellen

        clip_mod.UploadClipMixin.clip_upload = _patched_clip_upload
        logger.info("✅ instagrapi clip_upload HD-Qualitäts-Patch aktiv")
    except Exception as e:
        logger.warning(f"⚠️ Konnte clip_upload nicht patchen: {e}")


_patch_clip_upload_quality()
# ─────────────────────────────────────────────────────────────────────────


def _get_client():
    """Gibt einen eingeloggten instagrapi-Client zurück (Singleton mit Session-Cache)."""
    global _client
    if _client is not None:
        return _client

    try:
        from instagrapi import Client
    except ImportError:
        raise SystemExit("❌ instagrapi nicht installiert. Bitte: pip install instagrapi pillow")

    cl = Client()
    cl.delay_range = [2, 5]
    cl.set_locale("de_DE")
    cl.set_timezone_offset(3600)

    IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Session-Cache laden (nach einmaligem Login via ig_login.py)
    if IG_SESSION_FILE.exists():
        try:
            cl.load_settings(IG_SESSION_FILE)
            # Nur Session-Revalidierung – kein vollständiger Re-Login (vermeidet unnötige Challenges)
            cl.get_timeline_feed()
            cl.dump_settings(IG_SESSION_FILE)  # Session-Token aktualisieren
            logger.info("✅ Instagram-Session aus Cache geladen.")
            _client = cl
            return _client
        except Exception as e:
            logger.warning(f"⚠️ Session-Cache ungültig ({e}), versuche Re-Login...")
            try:
                cl2 = Client()
                cl2.delay_range = [2, 5]
                cl2.set_locale("de_DE")
                cl2.set_timezone_offset(3600)
                cl2.login(IG_USERNAME, IG_PASSWORD)
                cl2.dump_settings(IG_SESSION_FILE)
                logger.info("✅ Re-Login erfolgreich, Session erneuert.")
                _client = cl2
                return _client
            except Exception as e2:
                raise SystemExit(
                    f"❌ Instagram Re-Login fehlgeschlagen: {e2}\n"
                    "   Bitte einmalig ausführen: python -m instagram.ig_login"
                )

    # Kein Cache vorhanden → Anweisung ausgeben
    raise SystemExit(
        "❌ Keine gültige Instagram-Session gefunden.\n"
        "   Bitte einmalig ausführen:\n"
        "   python -m instagram.ig_login\n"
        "   Der Login-Assistent führt dich durch den Verifizierungsschritt."
    )


def post_photo(image_path: pathlib.Path, caption: str) -> bool:
    """Postet ein Foto auf Instagram. Gibt True bei Erfolg zurück."""
    try:
        cl = _get_client()
        media = cl.photo_upload(str(image_path), caption=caption)
        logger.info(f"✅ Foto gepostet: media_id={media.id}")
        return True
    except Exception as e:
        logger.error(f"❌ Foto-Upload fehlgeschlagen: {e}")
        return False


def post_reel(video_path: pathlib.Path, caption: str, thumbnail_path: pathlib.Path | None = None) -> str | None:
    """Postet ein Reel (Video) auf Instagram. Gibt media_id bei Erfolg zurück, sonst None."""
    try:
        cl = _get_client()
        extra = {}
        if thumbnail_path and thumbnail_path.exists():
            extra["thumbnail"] = str(thumbnail_path)
        media = cl.clip_upload(str(video_path), caption=caption, **extra)
        logger.info(f"✅ Reel gepostet: media_id={media.id}")
        return str(media.id)
    except Exception as e:
        logger.error(f"❌ Reel-Upload fehlgeschlagen: {e}")
        return None


def update_bio_link(url: str) -> bool:
    """Aktualisiert den Website-Link im Instagram-Profil (Link in Bio)."""
    try:
        cl = _get_client()
        cl.account_edit(external_url=url)
        logger.info(f"✅ Bio-Link aktualisiert: {url}")
        return True
    except Exception as e:
        logger.error(f"❌ Bio-Link Update fehlgeschlagen: {e}")
        return False


def post_comment(media_id: str, text: str) -> bool:
    """Kommentiert einen Post (z.B. Affiliate-Link)."""
    try:
        cl = _get_client()
        cl.media_comment(media_id, text)
        logger.info(f"✅ Kommentar gepostet auf {media_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Kommentar fehlgeschlagen: {e}")
        return False
