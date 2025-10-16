# html_processor.py
"""
Bereinigt das HTML-Dokument und bereitet die Eingabedaten für das LLM vor.
Der Titel wird nun nicht mehr aus dem <title>-Tag extrahiert, sondern dem LLM
zur Extraktion überlassen, um die beste Qualität zu erzielen.
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
    # ... (Ihr vorhandener Code hier: Bereinigt HTML und gibt String zurück) ...
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'aside', 'form', 'iframe', 'noscript', 'button', 'link', 'meta', 'svg', 'img', 'picture', 'source']):
            tag.decompose()
        # Gibt den bereinigten Text zurück
        return soup.get_text(separator=' ', strip=True) 
    except Exception:
        return "" 

def extrahiere_produktbilder_aus_html(html_content: str) -> str:
    # ... (Ihr vorhandener Code hier: Extrahiert Bilder und gibt String zurück) ...
    return "" # Fallback: leeren String

def create_fallback_id(text: str) -> str:
    # ... (Ihr vorhandener Code hier: Erstellt ID aus Text) ...
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
    print(f"-> LLM-Input Ziel: {output_path.resolve()}")

    if not html_path.exists():
        raise FileNotFoundError(f"HTML-Quelldatei nicht gefunden: {html_path}")

    print("-> Lese rohes HTML...")
    with open(html_path, "r", encoding="utf-8") as f:
        raw_html = f.read()

    # *NEU:* Der Titel wird hier nicht extrahiert, sondern als Platzhalter übergeben.
    # Der LLM-Extractor muss nun den besten Titel finden.
    product_title_placeholder = "Titel muss vom LLM extrahiert werden."
    print(f" \t-> Produkt-Titel: '{product_title_placeholder}' (LLM-Aufgabe)")
    
    # Ermittle ASIN oder Fallback-ID
    asin_match = re.search(r'([A-Z0-9]{10})\.', html_path.name)
    asin = asin_match.group(1) if asin_match else None
    
    if asin:
        product_id = asin
        print(f" \t-> Ermittelte ASIN: '{product_id}'")
    else:
        # Fallback-ID basierend auf Dateiname oder Platzhalter (für Dateinamen-Logik)
        product_id = create_fallback_id(html_path.stem)
        print(f" \t-> KEINE ASIN gefunden. Erstelle Fallback-ID: '{product_id}'")


    print("-> Starte Extraktion der Bild-Kandidaten...")
    bild_kandidaten = extrahiere_produktbilder_aus_html(raw_html) 
    bild_kandidaten_list = bild_kandidaten.split(' | ') if bild_kandidaten else []
    print(f" \t-> Gefundene Bild-Kandidaten: {len(bild_kandidaten_list)} URLs/Deskriptoren.")
    
    print("-> Starte HTML-Bereinigung...")
    clean_text = clean_html_to_core_text(raw_html)
    print("<- HTML-Bereinigung abgeschlossen.")

    if not clean_text.strip():
        print("WARNUNG: Der bereinigte Text ist leer.", file=sys.stderr)
        clean_text = "N/A"

    llm_input_data = {
        "source_file": str(html_path),
        "product_title": product_title_placeholder, # Platzhalter für die Metadaten
        "asin": product_id,
        "bild_kandidaten": bild_kandidaten,
        "clean_text": clean_text,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(llm_input_data, f, indent=4, ensure_ascii=False)
        
    print(f"-> LLM-Input gespeichert unter: {output_path.resolve()}")

# if __name__ == '__main__': ... (Ihr vorhandener Code hier)