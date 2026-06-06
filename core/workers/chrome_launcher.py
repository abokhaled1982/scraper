#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chrome_launcher.py — Zentraler Helper, um Chrome je Worker mit
- isoliertem User-Data-Dir (eigenes "Profil"),
- automatisch geladenen Unpacked-Extensions (--load-extension),
zu starten.

Idee:
  Jeder Worker (amazon, facebook, ...) bekommt sein eigenes Profilverzeichnis
  unter ~/.local/share/scraper/chrome_profiles/<name>/.
  Damit muss niemand mehr im Chrome-Profile-Picker klicken und die jeweilige
  Extension wird beim ersten Start mitgeladen — keine manuelle Installation.

Verwendung:
    from core.workers.chrome_launcher import ChromeProfile

    fb = ChromeProfile("facebook", addons=["addons/facebook"])
    fb.open("https://www.facebook.com/")        # 1. Aufruf: startet Chrome
    fb.open("https://www.facebook.com/foo")     # weitere: neuer Tab im selben Profil
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional

from core.logging import get_logger

log = get_logger("chrome_launcher")

# Projekt-Root (…/scraper) — relevant, um Addon-Pfade aufzulösen
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_chrome_bin() -> str:
    """Findet einen brauchbaren Chrome/Chromium-Binary-Pfad."""
    env = os.environ.get("CHROME_BIN")
    if env and Path(env).exists():
        return env
    for candidate in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    ):
        if Path(candidate).exists():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if found:
        return found
    raise FileNotFoundError(
        "Kein Chrome/Chromium gefunden. Setze CHROME_BIN in .env auf den Pfad zum Binary."
    )


def _resolve_addon_paths(addons: Iterable[str | os.PathLike]) -> List[str]:
    """Wandelt relative Addon-Pfade in absolute auf — relativ zum Projekt-Root."""
    abs_paths: List[str] = []
    for a in addons:
        p = Path(a)
        if not p.is_absolute():
            p = (_PROJECT_ROOT / p).resolve()
        if not p.is_dir():
            log.warning(f"[chrome] Addon-Pfad existiert nicht: {p} — wird übersprungen")
            continue
        if not (p / "manifest.json").exists():
            log.warning(f"[chrome] {p} enthält keine manifest.json — wird übersprungen")
            continue
        abs_paths.append(str(p))
    return abs_paths


def _default_profile_root() -> Path:
    """
    Basisverzeichnis aller dedizierten Worker-Profile.
    Default: ~/.local/share/scraper/chrome_profiles/
    Überschreibbar via env CHROME_PROFILES_ROOT.
    """
    env = os.environ.get("CHROME_PROFILES_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "scraper" / "chrome_profiles"


class ChromeProfile:
    """
    Repräsentiert ein isoliertes Chrome-Profil für einen Worker.

    - user_data_dir: eigener Ordner pro Worker → kein Konflikt mit anderen
      Chrome-Instanzen, keine Profilauswahl nötig.
    - addons: werden beim ersten Start via --load-extension geladen.
      Bei späteren Aufrufen ignoriert Chrome diese (Extension bleibt geladen
      so lange das Profil existiert).
    """

    def __init__(
        self,
        name: str,
        addons: Optional[Iterable[str | os.PathLike]] = None,
        extra_args: Optional[Iterable[str]] = None,
    ) -> None:
        self.name = name
        self.user_data_dir = _default_profile_root() / name
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.addons = _resolve_addon_paths(addons or [])
        self.extra_args = list(extra_args or [])
        self._chrome_bin = _resolve_chrome_bin()

    # --------------------------------------------------------------
    def _base_args(self) -> List[str]:
        args = [
            self._chrome_bin,
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=DisableLoadExtensionCommandLineSwitch",
        ]
        if self.addons:
            args.append(f"--load-extension={','.join(self.addons)}")
            # erste Extension auch "gepinnt" anzeigen ist nicht möglich via CLI,
            # aber sie ist aktiv. Nutzer sieht sie in chrome://extensions.
        args.extend(self.extra_args)
        return args

    # --------------------------------------------------------------
    def open(self, url: str, new_tab: bool = True) -> bool:
        """
        Öffnet eine URL.
        - Erster Aufruf startet Chrome (mit Addons geladen).
        - Weitere Aufrufe öffnen einen neuen Tab im laufenden Profil:
          Chrome erkennt das User-Data-Dir und routet die URL an die
          existierende Instanz; der zweite Prozess endet kurz danach.
        """
        if not url:
            return False
        args = self._base_args()
        if new_tab:
            args.append("--new-tab")
        args.append(url)
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.debug(f"[chrome:{self.name}] launch → {url}")
            return True
        except FileNotFoundError:
            log.error(f"[chrome:{self.name}] Binary nicht gefunden: {self._chrome_bin}")
            return False
        except Exception as e:
            log.error(f"[chrome:{self.name}] Start fehlgeschlagen: {e}")
            return False

    # --------------------------------------------------------------
    def launch_if_needed(self, start_url: str = "about:blank") -> bool:
        """
        Startet Chrome falls für dieses Profil noch keine Instanz läuft.
        Erkennung über die Lock-Datei 'SingletonLock' im user_data_dir,
        die Chrome beim Start anlegt und beim sauberen Beenden entfernt.
        """
        lock = self.user_data_dir / "SingletonLock"
        if lock.exists():
            log.info(f"[chrome:{self.name}] Profil läuft bereits — kein Neustart")
            return True
        log.info(f"[chrome:{self.name}] Starte Chrome mit Profil '{self.name}' → {start_url}")
        return self.open(start_url, new_tab=False)
