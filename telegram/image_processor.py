# telegram/image_processor.py
import tempfile
import aiohttp
from PIL import Image
from pathlib import Path
from typing import Optional, Dict, Any, List
from io import BytesIO
import re

# Maximale Dateigröße für Telegram-Bilder (50 MB)
MAX_FILE_SIZE = 50 * 1024 * 1024 

# Formate/Endungen, die oft Probleme machen und konvertiert werden SOLLTEN, 
# da Telethon/Telegram sie als Bildquelle nicht zuverlässig handhabt (z.B. WebP/animierte GIFs).
PROBLEM_EXTENSIONS = (".webp", ".gif")

_URL_RE = re.compile(r"^https?://", re.I)

# NEUE KORREKTUR: Regex für alle Zeichen, die in Windows-Dateinamen illegal sind
# Beinhaltet: <, >, :, ", /, \, |, ?, *, =, &, +
ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*&=+]')

def _is_url(s: Optional[str]) -> bool:
    """Prüft, ob ein String eine URL ist."""
    return bool(s and _URL_RE.match(s))

def get_best_image_url(d: Dict[str, Any]) -> Optional[str]:
    """
    Sucht die beste HTTPS-URL im Angebots-Payload.
    """
    candidates: List[str] = []
    if d.get("main_image"): candidates.append(d["main_image"])
    if isinstance(d.get("images"), list): candidates += [img for img in d["images"] if img]
    if d.get("thumbnail"): candidates.append(d["thumbnail"])

    for img in candidates:
        if _is_url(img):
            return img
    return None

def url_needs_local_processing(url: Optional[str]) -> bool:
    """
    Entscheidet heuristisch anhand der URL-Endung, ob das Bild heruntergeladen 
    und konvertiert werden MUSS (Problemformat).
    """
    if not url:
        return False
    
    # URL von Query-Parametern befreien, um die Endung zu prüfen
    parsed_url = url.split("?")[0].lower()
    
    # Prüfe auf Problemformate (z.B. WebP oder GIF)
    for ext in PROBLEM_EXTENSIONS:
        if parsed_url.endswith(ext):
            return True
    
    return False

async def download_and_convert_to_jpg(url: Optional[str]) -> Optional[Path]:
    """
    Lädt ein Problem-Bild herunter, konvertiert es zu JPG und speichert es temporär.
    Wird nur aufgerufen, wenn url_needs_local_processing True ist.
    
    ULTRA-ROBUSTE KORREKTUR: Die Erstellung des temporären Dateinamens wurde angepasst,
    um alle illegalen URL-Zeichen aus dem Dateinamen zu entfernen.
    """
    if not url:
        return None

    # 1. Nur den Pfad-Teil der URL ohne Query-Parameter und Fragment verwenden.
    # Wir nehmen das letzte Pfad-Segment vor dem ersten '?' oder '#'.
    path_segment = url.split('?')[0].split('#')[0].split('/')[-1]

    # 2. Alle ungültigen Dateisystemzeichen aus diesem Segment entfernen (durch '_').
    base_name = ILLEGAL_FILENAME_CHARS_RE.sub('_', path_segment)
    base_name = base_name.strip('_')
    
    if not base_name:
        # Fallback, falls der Pfad-Segment leer war
        base_name = 'download'

    # Temporäre Datei im System-Temp-Ordner erstellen (mit JPG-Suffix).
    # Die Eindeutigkeit wird primär über den Hash der VOLLSTÄNDIGEN URL sichergestellt.
    temp_file = Path(tempfile.gettempdir()) / f"tg_img_conv_{base_name}_{hash(url)}.jpg"
    
    if temp_file.exists(): temp_file.unlink()

    print(f"📥 Starte asynchronen Download & Konvertierung (erforderlich): {url}")

    try:
        # 1. Bild asynchron herunterladen
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    print(f"⚠️ Bild-Download fehlgeschlagen (Status {response.status}): {url}")
                    return None
                img_data = await response.read()
                
        # 2. Prüfen der Dateigröße
        if len(img_data) > MAX_FILE_SIZE:
             print(f"⚠️ Bild zu groß ({len(img_data)/1024/1024:.1f}MB). Überspringe.")
             return None

        # 3. Mit Pillow öffnen, nach RGB konvertieren und als JPG speichern
        img = Image.open(BytesIO(img_data))
        
        # Konvertiere nach RGB, um Transparenz zu entfernen und JPG-Kompatibilität zu gewährleisten
        if img.mode in ('RGBA', 'P', 'L'):
            img = img.convert('RGB')
        
        # Speichern als JPG mit guter Qualität
        img.save(temp_file, format="JPEG", quality=85)
        
        if temp_file.stat().st_size > MAX_FILE_SIZE:
            temp_file.unlink()
            return None

        print(f"✅ Bild zu JPG konvertiert: {temp_file}")
        return temp_file
        
    except Exception as e:
        print(f"❌ Bildverarbeitung/Konvertierung fehlgeschlagen ({url}): {e}")
        if temp_file.exists(): temp_file.unlink()
        return None