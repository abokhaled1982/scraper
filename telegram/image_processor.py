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

# Erweitere Regex um Pfad-Segmente, die auf dynamische Größenänderung hindeuten.
# KORREKTUR: '&' als Trennzeichen HINZUGEFÜGT, um Parameter überall in der Query zu finden.
FILTER_QUERY_RE = re.compile(
    r'[/\\?&](filters|fit-in|format|quality|resize|crop|width|height|w=|h=|x=|y=|auto|optmize|scale|smart|url=|strip|trim|ex=|ey=|align|resizesource|unsharp|[0-9]+x(auto|[0-9]+))', 
    re.I
)
# Formate/Endungen, die oft Probleme machen und konvertiert werden SOLLTEN
PROBLEM_EXTENSIONS = (".webp", ".gif")
# NEU: Alle Standard-Erweiterungen für die "No-Extension"-Prüfung
STANDARD_EXTENSIONS = PROBLEM_EXTENSIONS + (".jpg", ".jpeg", ".png")

_URL_RE = re.compile(r"^https?://", re.I)

# NEUE KORREKTUR: Regex für alle Zeichen, die in Windows-Dateinamen illegal sind
# Beinhaltet: <, >, :, ", /, \, |, ?, *, =, &, +
ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*&=+]')

DOWNLOAD_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'image/*, text/html',
}


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
    Entscheidet heuristisch, ob das Bild heruntergeladen und konvertiert werden MUSS.
    
    Die Konvertierung ist notwendig, wenn:
    1. Die URL Query-Parameter enthält, die auf dynamische Filterung/Skalierung hindeuten.
    2. Die URL ein Format verwendet, das Telegram/Telethon Probleme bereiten kann (WebP, GIF).
    3. Die URL keine Standard-Erweiterung (z.B. .jpg) enthält, da sie dann typischerweise dynamisch ist.
    """
    if not url:
        return False
    
    url_lower = url.lower()
    parsed_path = url_lower.split("?")[0]
    
    # 1. Prüfung auf Problemformate (WebP, GIF etc.)
    for ext in PROBLEM_EXTENSIONS:
        if parsed_path.endswith(ext):
            return True

    # 2. Prüfung auf komplexe URL-Filter/Query-Parameter
    if FILTER_QUERY_RE.search(url):
        return True 

    # 3. NEUE PRÜFUNG: Dynamische URLs ohne Standard-Erweiterung (fängt Ihre 3dmensionals-URL)
    is_any_standard_extension_present = any(parsed_path.endswith(ext) for ext in STANDARD_EXTENSIONS)
    
    if not is_any_standard_extension_present:
        # Die URL endet nicht auf .jpg, .png, .webp oder .gif. Dies ist ein Zeichen für eine 
        # dynamisch generierte URL, die Telegram oft nicht verarbeiten kann.
        return True

    # 4. Standard-URL (z.B. https://host.de/bild.jpg ohne Queries)
    return False

async def download_and_convert_to_jpg(url: Optional[str]) -> Optional[Path]:
    """
    Lädt ein Problem-Bild herunter, konvertiert es zu JPG und speichert es temporär.
    Wird nur aufgerufen, wenn url_needs_local_processing True ist.
    """
    if not url:
        return None

    # 1. KORREKTUR: Die langen Pfad-Segmente werden NICHT mehr für den Dateinamen verwendet.
    #    Ein zu langer Dateiname (über 259 Zeichen im gesamten Pfad) ist die Ursache für 
    #    den "No such file or directory" (Errno 2) Fehler auf Windows-Systemen.
    #    Wir verwenden einen kurzen, statischen Basisnamen. Die Eindeutigkeit wird
    #    durch den Hash der URL am Ende gewährleistet.
    base_name = 'conv_img'
    
    # 2. Erstellung des temporären Pfades mit dem kurzen Basisnamen und dem Hash
    temp_file = Path(tempfile.gettempdir()) / f"tg_img_{base_name}_{hash(url)}.jpg"
    
    if temp_file.exists(): temp_file.unlink()  
    
    # ... der Rest der Funktion bleibt unverändert ...
    if temp_file.exists(): temp_file.unlink()

    print(f"📥 Starte asynchronen Download & Konvertierung (erforderlich): {url}")

    try:
        # 1. Bild asynchron herunterladen
        # !!! WICHTIG: User-Agent-Header in die Session integrieren
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers=DOWNLOAD_HEADERS # <-- NEU: Header hinzufügen
        ) as session:
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
        
        if img.mode in ('RGBA', 'P', 'L'):
            img = img.convert('RGB')
        
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