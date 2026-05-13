#!/usr/bin/env python3
"""
instagram/ig_login.py — Einmaliger interaktiver Instagram-Login

Führt den Challenge-Flow (Email/SMS-Code) durch und speichert
die Session für spätere automatische Nutzung.

Aufruf:
    python -m instagram.ig_login
    # oder
    python instagram/ig_login.py
"""
import os
import sys
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

IG_USERNAME     = os.getenv("IG_USERNAME", "")
IG_PASSWORD     = os.getenv("IG_PASSWORD", "")
IG_SESSION_FILE = pathlib.Path(os.getenv("IG_SESSION_FILE", ".sessions/ig_session.json"))

if not IG_USERNAME or not IG_PASSWORD or IG_PASSWORD == "CHANGE_ME":
    sys.exit("❌ IG_USERNAME und IG_PASSWORD müssen in .env gesetzt sein.")

try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        ChallengeRequired,
        SelectContactPointRecoveryForm,
        RecaptchaChallengeForm,
        BadPassword,
        TwoFactorRequired,
    )
except ImportError:
    sys.exit("❌ instagrapi nicht installiert: pip install instagrapi pillow")


def challenge_code_handler(username: str, choice) -> str:
    """Wird von instagrapi aufgerufen wenn ein Bestätigungscode nötig ist."""
    print(f"\n📱 Instagram-Sicherheitsprüfung für @{username}")
    print("   Instagram hat einen Code per Email oder SMS geschickt.")
    code = input("   Bitte Code eingeben: ").strip()
    return code


def change_password_handler(username: str) -> str:
    """Wird aufgerufen wenn Instagram ein Passwort-Reset anfordert."""
    print(f"\n⚠️  Instagram fordert Passwortänderung für @{username}")
    print("   Bitte ändere dein Passwort manuell in der Instagram-App,")
    print("   dann aktualisiere IG_PASSWORD in der .env-Datei.")
    return IG_PASSWORD


def main():
    print("=" * 50)
    print("   📸 Instagram Einmal-Login")
    print("=" * 50)
    print(f"   Nutzer:  {IG_USERNAME}")
    print(f"   Session: {IG_SESSION_FILE}")
    print()

    IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    cl = Client()
    cl.delay_range = [2, 5]
    cl.challenge_code_handler  = challenge_code_handler
    cl.change_password_handler = change_password_handler

    # Gerätedaten randomisieren (verhindert erneuten Challenge)
    cl.set_locale("de_DE")
    cl.set_timezone_offset(3600)  # UTC+1

    print(f"🔐 Versuche Login als @{IG_USERNAME}...")
    try:
        cl.login(IG_USERNAME, IG_PASSWORD)

    except BadPassword:
        sys.exit("❌ Falsches Passwort. Bitte IG_PASSWORD in .env prüfen.")

    except TwoFactorRequired:
        print("\n🔑 Zwei-Faktor-Authentifizierung aktiv.")
        code = input("   2FA-Code eingeben: ").strip()
        cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)

    except ChallengeRequired:
        print("\n⚠️  Challenge erkannt – versuche automatische Auflösung...")
        try:
            api_path = cl.last_json.get("challenge", {}).get("api_path", "")
            if api_path:
                # Challenge neu laden und Code anfordern
                cl.challenge_resolve(cl.last_json)
                # instagrapi ruft jetzt challenge_code_handler auf
            else:
                sys.exit("❌ Challenge konnte nicht aufgelöst werden (kein api_path).")
        except Exception as e:
            sys.exit(f"❌ Challenge-Auflösung fehlgeschlagen: {e}")

    except SelectContactPointRecoveryForm as e:
        print(f"\n⚠️  Kontaktpunkt-Auswahl nötig: {e}")
        sys.exit("❌ Bitte zuerst in der Instagram-App einloggen und den Account verifizieren.")

    except RecaptchaChallengeForm:
        sys.exit("❌ Instagram fordert reCAPTCHA – bitte zuerst über Browser/App einloggen.")

    except Exception as e:
        print(f"\n❌ Login fehlgeschlagen: {e}")
        print("\nAlternativ: Session-ID aus dem Browser nutzen.")
        print("Öffne Instagram im Browser → F12 → Application → Cookies → sessionid")
        session_id = input("sessionid eingeben (oder Enter zum Abbrechen): ").strip()
        if not session_id:
            sys.exit("Abgebrochen.")
        try:
            cl.login_by_sessionid(session_id)
            # Session vollständig befüllen: User-Infos laden damit user_id gesetzt wird
            print("⏳ Lade Account-Informationen...")
            cl.get_timeline_feed()  # Triggert vollständige Cookie-Initialisierung
            info = cl.account_info()
            print(f"   Username: {info.username}, ID: {info.pk}")
            cl.dump_settings(IG_SESSION_FILE)
            print("✅ Login per sessionid erfolgreich.")
        except Exception as e2:
            sys.exit(f"❌ sessionid-Login fehlgeschlagen: {e2}")

    # Session speichern
    cl.dump_settings(IG_SESSION_FILE)
    print(f"\n✅ Login erfolgreich! Session gespeichert: {IG_SESSION_FILE}")
    print(f"   User-ID:  {cl.user_id}")
    print(f"   Username: {cl.username}")
    print(f"\n✅ Du kannst jetzt 'python -m instagram.ig_watcher' starten.")


def ensure_ig_session() -> bool:
    """
    Prüft ob eine gültige Instagram-Session existiert.
    Falls nicht (oder abgelaufen), führt einen interaktiven Login durch.

    Wird von run_all.py beim Start aufgerufen – genau wie Telegram's
    ensure_both_sessions_sequential(). Blockiert kurz im Terminal wenn
    ein Login-Code nötig ist.

    Returns True wenn Session bereit ist, False bei nicht-behebbarem Fehler.
    """
    try:
        from instagrapi import Client
        from instagrapi.exceptions import (
            ChallengeRequired, SelectContactPointRecoveryForm,
            RecaptchaChallengeForm, BadPassword, TwoFactorRequired,
        )
    except ImportError:
        print("[Instagram] ❌ instagrapi nicht installiert – übersprungen.")
        return False

    print(f"\n[Instagram] Prüfe Session für @{IG_USERNAME}...")
    IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ── Schritt 1: Vorhandene Session aus Cache prüfen ──────────────────────
    if IG_SESSION_FILE.exists():
        cl = Client()
        cl.delay_range = [1, 3]
        cl.set_locale("de_DE")
        cl.set_timezone_offset(3600)
        try:
            cl.load_settings(IG_SESSION_FILE)
            cl.get_timeline_feed()          # stille Validierung
            cl.dump_settings(IG_SESSION_FILE)
            print(f"[Instagram] ✅ Session gültig – @{cl.username} (ID: {cl.user_id})")
            return True
        except Exception as e:
            print(f"[Instagram] ⚠️  Session abgelaufen ({e}) – neuer Login nötig.")

    # ── Schritt 2: Interaktiver Login ────────────────────────────────────────
    print(f"\n{'─'*50}")
    print(f"   📸 Instagram Login – @{IG_USERNAME}")
    print(f"{'─'*50}")

    cl = Client()
    cl.delay_range = [2, 5]
    cl.challenge_code_handler  = challenge_code_handler
    cl.change_password_handler = change_password_handler
    cl.set_locale("de_DE")
    cl.set_timezone_offset(3600)

    print(f"🔐 Login als @{IG_USERNAME}...")
    try:
        cl.login(IG_USERNAME, IG_PASSWORD)

    except BadPassword:
        print("❌ Falsches Passwort. Bitte IG_PASSWORD in .env prüfen.")
        return False

    except TwoFactorRequired:
        print("\n🔑 Zwei-Faktor-Authentifizierung aktiv.")
        code = input("   2FA-Code eingeben: ").strip()
        try:
            cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
        except Exception as e:
            print(f"❌ 2FA-Login fehlgeschlagen: {e}")
            return False

    except ChallengeRequired:
        print("\n⚠️  Challenge erkannt – automatische Auflösung...")
        try:
            cl.challenge_resolve(cl.last_json)
            # instagrapi ruft challenge_code_handler auf (fragt nach Code im Terminal)
        except Exception as e:
            print(f"❌ Challenge-Auflösung fehlgeschlagen: {e}")
            return False

    except (SelectContactPointRecoveryForm, RecaptchaChallengeForm) as e:
        print(f"❌ Instagram-Challenge nicht automatisch lösbar: {e}")
        print("   Bitte einmalig in der Instagram-App/Browser einloggen und erneut versuchen.")
        return False

    except Exception as e:
        print(f"❌ Login fehlgeschlagen: {e}")
        return False

    # Session speichern
    cl.dump_settings(IG_SESSION_FILE)
    print(f"\n✅ Instagram-Login erfolgreich! Session gespeichert: {IG_SESSION_FILE}")
    print(f"   User-ID: {cl.user_id}  |  Username: @{cl.username}")
    return True


if __name__ == "__main__":
    main()
