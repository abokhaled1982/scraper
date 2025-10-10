#!/usr/bin/env python3
"""
open_chrome_profile2.py

Öffnet Google Chrome mit Profil "Profile 2".
Usage:
    python open_chrome_profile2.py                 -> öffnet https://www.google.com
    python open_chrome_profile2.py https://example.com
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

DEFAULT_URL = "https://www.google.com"
PROFILE_DIR_NAME = "Profile 2"  # Profilname wie in Chrome-Profilverzeichnis

def find_chrome_executable():
    # Gängige Windows-Pfade prüfen
    local_app = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")

    candidates = []
    if local_app:
        candidates.append(Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe")
    if program_files:
        candidates.append(Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe")
    if program_files_x86:
        candidates.append(Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for p in candidates:
        if p.exists():
            return str(p)

    # Fallback: im PATH suchen
    chrome_path = shutil.which("chrome.exe") or shutil.which("chrome")
    if chrome_path:
        return chrome_path

    return None

def main():
    # URL aus Argumenten lesen
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

    chrome_exe = find_chrome_executable()
    if not chrome_exe:
        print("Fehler: Chrome wurde nicht gefunden. Bitte prüfen, ob Chrome installiert ist.")
        sys.exit(1)

    # Standard User Data Verzeichnis unter Windows
    local_app = os.environ.get("LOCALAPPDATA", "")
    if not local_app:
        print("Warnung: LOCALAPPDATA nicht gesetzt — versuche, chrome trotzdem zu starten.")
        user_data_dir = ""
    else:
        user_data_dir = str(Path(local_app) / "Google" / "Chrome" / "User Data")

    profile_dir = PROFILE_DIR_NAME

    cmd = [
        chrome_exe,
        "--new-window",
    ]

    if user_data_dir:
        cmd.append(f'--user-data-dir={user_data_dir}')
    cmd.append(f'--profile-directory={profile_dir}')
    cmd.append(url)

    print("Starte Chrome mit:")
    print("  chrome_exe: ", chrome_exe)
    if user_data_dir:
        print("  user_data_dir:", user_data_dir)
    print("  profile_directory:", profile_dir)
    print("  url:", url)
    print()

    try:
        # Startet Chrome ohne das Konsolenfenster zu blockieren
        subprocess.Popen(cmd, shell=False)
    except Exception as e:
        print("Fehler beim Starten von Chrome:", e)
        sys.exit(2)

if __name__ == "__main__":
    main()
