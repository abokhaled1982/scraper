# html_processor.py
"""
Bereinigt das HTML-Dokument und bereitet die Eingabedaten für das LLM vor.

AKTUALISIERUNGEN:
1. Eine Funktion zur Extraktion der kanonischen Produkt-URL wurde hinzugefügt.
2. Die kanonische URL wird dem 'clean_text' als klar gekennzeichneter Block hinzugefügt, 
   damit das LLM sie zuverlässig für das Feld 'url_des_produkts' verwenden kann.
"""

import os
import sys
import re
import json
from pathlib import Path
from bs4 import BeautifulSoup, Comment 
from dotenv import load_dotenv

load_dotenv() 

# --- HILFSFUNKTIONEN ---

def clean_html_to_core_text(html_content: str) -> str:
    """Entfernt irrelevante Tags und gibt den reinen Text zurück."""
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'aside', 'form', 'iframe', 'noscript', 'button', 'link', 'meta', 'svg', 'img', 'picture', 'source']):
            tag.decompose()
        # Gibt den bereinigten Text zurück
        return soup.get_text(separator=' ', strip=True) 
    except Exception:
        return "" 

def extrahiere_produktbilder_aus_html(html_content: str) -> str:
    """Extrahiert die URLs der Produktbilder, getrennt durch ' | '."""
    # Platzhalter für komplexe Logik, die LLM-seitig verarbeitet wird.
    return "" 

def extrahiere_url_aus_html(html_content: str) -> str:
    """
    Extrahiert die kanonische URL aus dem HTML-Inhalt mittels BeautifulSoup.
    PRIORITÄT: canonical Link, Microdata URL, Open Graph URL.
    """
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        
        # 1. Suche nach dem kanonischen Link-Tag (höchste Priorität)
        canonical_link = soup.find('link', rel='canonical')
        if canonical_link and canonical_link.get('href'):
            return canonical_link.get('href').strip()
        
        # 2. Fallback: Suche in Open Graph URL Tag
        og_url = soup.find('meta', property='og:url')
        if og_url and og_url.get('content'):
            return og_url.get('content').strip()
            
        # 3. Fallback: Suche in Microdata (z.B. schema.org/Product 'url' property)
        itemprop_url = soup.find(itemprop='url')
        if itemprop_url and itemprop_url.get('href'):
            return itemprop_url.get('href').strip()
            
    except Exception as e:
        print(f"Fehler bei der URL-Extraktion: {e}", file=sys.stderr)
        
    return "N/A" # Default, wenn nichts gefunden wird

def create_fallback_id(text: str) -> str:
    """Erstellt eine einfache, saubere ID aus einem Textstring."""
    if not text or text == "Titel unbekannt":
        return "produkt_id_unbekannt"
        
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s_]', '', text)
    text = re.sub(r'\s+', '_', text)
    text = re.sub(r'_{2,}', '_', text)
    return text.strip('_')

# --- HAUPTLOGIK: HTML-Verarbeitung ---

def process_html_to_llm_input(html_path: Path, output_path: Path):
    """
    Liest rohes HTML, bereinigt es und bereitet die Eingabedaten für das LLM vor.
    """
    print(f"-> Verarbeite HTML-Datei: {html_path.name}")
    
    if not html_path.exists():
        raise FileNotFoundError(f"HTML-Quelldatei nicht gefunden: {html_path}")

    print("-> Lese rohes HTML...")
    with open(html_path, "r", encoding="utf-8") as f:
        raw_html = f.read()

    product_title_placeholder = "Titel muss vom LLM extrahiert werden."
    
    # Ermittle ASIN oder Fallback-ID
    asin_match = re.search(r'([A-Z0-9]{10})\.', html_path.name)
    asin = asin_match.group(1) if asin_match else None
    
    product_id = asin if asin else create_fallback_id(html_path.stem)
    print(f" \t-> Ermittelte ASIN/ID: '{product_id}'")

    # NEU: URL-Extraktion durch den Parser
    canonical_url = extrahiere_url_aus_html(raw_html)
    print(f" \t-> Kanonische URL (vom Parser): '{canonical_url}'")
    
    print("-> Starte Extraktion der Bild-Kandidaten...")
    bild_kandidaten = extrahiere_produktbilder_aus_html(raw_html) 
    bild_kandidaten_list = bild_kandidaten.split(' | ') if bild_kandidaten else []
    print(f" \t-> Gefundene Bild-Kandidaten: {len(bild_kandidaten_list)} URLs/Deskriptoren.")
    
    print("-> Starte HTML-Bereinigung...")
    clean_text = clean_html_to_core_text(raw_html)
    
    if not clean_text.strip():
        print("WARNUNG: Der bereinigte Text ist leer.", file=sys.stderr)
        clean_text = "N/A"

    # WICHTIG: Füge die kanonische URL dem bereinigten Text hinzu, damit das LLM sie findet
    if canonical_url != "N/A":
        canonical_url_block = f"\n\n--- KANONISCHE URL VOM PARSER ---\n{canonical_url}\n--- ENDE URL BLOCK ---\n\n"
        clean_text = canonical_url_block + clean_text
        print(" \t-> Kanonische URL wurde dem LLM-Text hinzugefügt.")
    
    print("<- HTML-Bereinigung abgeschlossen.")


    llm_input_data = {
        "source_file": str(html_path),
        "product_title": product_title_placeholder,
        "asin": product_id,
        "bild_kandidaten": bild_kandidaten,
        "clean_text": clean_text,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(llm_input_data, f, indent=4, ensure_ascii=False)
        
    print(f"-> LLM-Input gespeichert unter: {output_path.resolve()}")