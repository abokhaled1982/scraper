# instagram/ig_service.py
# instagrapi-Wrapper: Login, Session-Verwaltung, Photo/Reel-Upload

import json
import logging
import os
import pathlib
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


def post_reel(video_path: pathlib.Path, caption: str, thumbnail_path: pathlib.Path | None = None) -> bool:
    """Postet ein Reel (Video) auf Instagram."""
    try:
        cl = _get_client()
        extra = {}
        if thumbnail_path and thumbnail_path.exists():
            extra["thumbnail"] = str(thumbnail_path)
        media = cl.clip_upload(str(video_path), caption=caption, **extra)
        logger.info(f"✅ Reel gepostet: media_id={media.id}")
        return True
    except Exception as e:
        logger.error(f"❌ Reel-Upload fehlgeschlagen: {e}")
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
