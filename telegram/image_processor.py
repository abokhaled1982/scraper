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

# Erweitere Regex um Pfad-Segmente, die auf dynamische Größenänderung hindeuten, 
# z.B. /max/1400xauto/, /300x300/, /w_500/.
FILTER_QUERY_RE = re.compile(
    r'[/\\?](filters|fit-in|format|quality|resize|crop|width|height|w=|h=|auto|optmize|scale|smart|url=|[0-9]+x(auto|[0-9]+))', 
    re.I
)
# Der neue Teil [0-9]+x(auto|[0-9]+) fängt Muster wie "1400xauto" oder "300x300" ab.
# Formate/Endungen, die oft Probleme machen und konvertiert werden SOLLTEN, 
# da Telethon/Telegram sie als Bildquelle nicht zuverlässig handhabt (z.B. WebP/animierte GIFs).
PROBLEM_EXTENSIONS = (".webp", ".gif")

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
    """
    if not url:
        return False
    
    url_lower = url.lower()
    parsed_path = url_lower.split("?")[0]
    
    # 1. Prüfung auf Problemformate (WebP, GIF etc.)
    for ext in PROBLEM_EXTENSIONS:
        if parsed_path.endswith(ext):
            # print(f"ℹ️ Lokale Konvertierung erforderlich: Problemformat '{ext}' erkannt in {url}")
            return True
    
    # 2. Prüfung auf komplexe URL-Filter/Query-Parameter (wie /filters:..., ?w=...)
    if FILTER_QUERY_RE.search(url):
        # print(f"ℹ️ Lokale Konvertierung erforderlich: Komplexe Filter/Dynamik-URL erkannt in {url}")
        
        # NEUE INTELLIGENTE PRÜFUNG: Wenn die URL dynamisch ist UND keine Standard-Bildendung hat,
        # ist die Wahrscheinlichkeit eines Telegram-Fehlers sehr hoch.
        is_known_extension = parsed_path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))
        
        # Wenn komplexe Filter gefunden wurden, aber keine Standard-Erweiterung,
        # dann MUSS lokal verarbeitet werden (z.B. der coolblue-Fall).
        if not is_known_extension:
             # print(f"⚠️ Lokale Konvertierung erzwungen: Dynamik ohne Endung.")
             return True
        
        # Wenn Filter da sind, aber eine Standard-Endung (.jpg) existiert, könnte Telegram es packen.
        # Trotzdem: Bei Filtern ist Konvertierung sicherer, um Dateinamen-Fehler zu vermeiden.
        return True 

    # 3. Standard-URL ohne erkannte Probleme
    return False


# ... (Ihre Funktion url_needs_local_processing bleibt unverändert)

async def download_and_convert_to_jpg(url: Optional[str]) -> Optional[Path]:
    """
    Lädt ein Problem-Bild herunter, konvertiert es zu JPG und speichert es temporär.
    Wird nur aufgerufen, wenn url_needs_local_processing True ist.
    
    KORREKTUR: Jetzt mit User-Agent-Header, um 403-Fehler zu vermeiden.
    """
    if not url:
        return None

    # 1. Nur den Pfad-Teil der URL ohne Query-Parameter und Fragment verwenden.
    path_segment = url.split('?')[0].split('#')[0].split('/')[-1]

    # 2. Alle ungültigen Dateisystemzeichen aus diesem Segment entfernen (durch '_').
    base_name = ILLEGAL_FILENAME_CHARS_RE.sub('_', path_segment)
    base_name = base_name.strip('_')
    
    if not base_name:
        base_name = 'download'

    temp_file = Path(tempfile.gettempdir()) / f"tg_img_conv_{base_name}_{hash(url)}.jpg"
    
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