import os
import time
import shutil
import subprocess, sys
from pathlib import Path




HERE = Path(__file__).parent.resolve()
LOADER = HERE / "browser/loader.py"


URL = "https://www.amazon.de/deals?ref_=nav_cs_gb"
URL2="https://www.geldhub.de/de"
EDGE_PROFILE = "Default"      # z.B. "Default", "Profile 1", "Profile 2"
CHROME_PROFILE = "Profile 1"  # z.B. "Default", "Profile 1"


def run_loader_blocking():
    if not LOADER.is_file():
        print(f"[supervisor] loader.py not found at {LOADER}; skipping.")
        return
    print("[supervisor] starting loader.py before all scripts …")
    rc = subprocess.call([sys.executable, str(LOADER)])
    print(f"[supervisor] loader.py finished (exit code={rc})")

def candidates_for(app_name, subpath_64, subpath_86):
    # typische Installationspfade + PATH
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cands = [
        app_name,  # falls im PATH
        str(Path(pf) / subpath_64),
        str(Path(pf86) / subpath_86),
    ]
    return cands

def find_executable(candidates):
    for c in candidates:
        # erst PATH prüfen
        w = shutil.which(c)
        if w:
            return w
        # dann absoluter Pfad?
        p = Path(c)
        if p.is_file():
            return str(p)
    return None

edge_cands = candidates_for(
    "msedge.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"Microsoft\Edge\Application\msedge.exe",
)
chrome_cands = candidates_for(
    "chrome.exe",
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome\Application\chrome.exe",
)

edge_path = find_executable(edge_cands)
chrome_path = find_executable(chrome_cands)

if not edge_path:
    raise FileNotFoundError(
        "Microsoft Edge (msedge.exe) wurde nicht gefunden.\n"
        "Bitte prüfe die Installation oder passe den Pfad im Skript an.\n"
        f"Getestete Pfade:\n- " + "\n- ".join(edge_cands)
    )

print("Starte Microsoft Edge...")
subprocess.Popen([edge_path, f"--profile-directory={EDGE_PROFILE}", URL])

# kurze Pause, damit Edge sicher hochkommt
time.sleep(5)

if not chrome_path:
    raise FileNotFoundError(
        "Google Chrome (chrome.exe) wurde nicht gefunden.\n"
        "Bitte prüfe die Installation oder passe den Pfad im Skript an.\n"
        f"Getestete Pfade:\n- " + "\n- ".join(chrome_cands)
    )

print("Starte Google Chrome...")
subprocess.Popen([chrome_path, f"--profile-directory={CHROME_PROFILE}", URL2])

print("Warte 30 Sekunden...")
time.sleep(30)
print("Fertig ✅")
