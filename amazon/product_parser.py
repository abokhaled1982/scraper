#!/usr/bin/env python3
# -*- coding: utf-8 -*-


from __future__ import annotations
import json, time
import re
from pathlib import Path
from typing import  Tuple, Dict, Any
import sys
import uuid
from bs4 import BeautifulSoup, Comment 
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode 


# Projekt-Config (Annahme: config.py existiert)
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import PRODUCKT_DIR, OUT_DIR, FAILED_DIR, INTERVAL_SECS, REGISTRY_PATH, SUMMARY_PATH

# Importiere die fachlich getrennten Module
from utils import (
    _read_text,
    is_amazon_html,
    load_registry, 
    pick_oldest_html, map_ai_output_to_target_format
)

# Importe für Amazon und AI-Pipeline

from ai_parser.ai_extractor import extract_and_save_data 

from amazon.amazon_parser import AmazonProductParser


load_dotenv() 



# ----------------------------- URL Normalisierung & Extraktion --------------------------------------


def _get_base_url_path(url: str) -> str:
    """Extrahiert die Basis-URL und den Pfad ohne Query-Parameter (imwidth) und ohne Deskriptoren (300w)."""
    # Entferne w/x-Deskriptoren falls vorhanden (z.B. " 300w")
    url_without_desc = re.sub(r'\s+\d+[wx]$', '', url).strip()
    
    try:
        parsed_url = urlparse(url_without_desc)
        # Bilde die URL neu aus Schema, Netloc und Pfad, ohne Query/Fragment
        return urlunparse(parsed_url._replace(query='', params='', fragment=''))
    except ValueError:
        return url_without_desc # Fehlerfall

def extrahiere_produktbilder_aus_html(html_content: str) -> str:
    """
    Sucht nach Bild-URLs innerhalb von Containern mit mehreren Bildern,
    extrahiert deren Basis-Pfad (ohne Auflösungsparameter) und gibt
    eine bereinigte, eindeutige Liste zurück.
    Einzelbilder außerhalb solcher Container werden ignoriert.
    """
    soup = BeautifulSoup(html_content or "", 'lxml')
    basis_url_kandidaten = set()

    # 1. Suche nach Containern mit mehreren <img>
    container_tags = ['div', 'figure', 'section', 'ul']  # typische Container
    for container in soup.find_all(container_tags):
        imgs = container.find_all('img')
        if len(imgs) < 2:
            continue  # Einzelbilder ignorieren
        for img in imgs:
            urls = []
            for attr in ['src', 'data-src', 'data-original', 'data-full-image-url']:
                if attr in img.attrs and img[attr]:
                    urls.append(img[attr])
            for attr in ['srcset', 'data-srcset']:
                if attr in img.attrs and img[attr]:
                    parts = re.split(r',\s*', img[attr])
                    for part in parts:
                        url_only = part.split()[0]
                        urls.append(url_only)
            for url in urls:
                base = _get_base_url_path(url)
                if base and not base.lower().endswith(('.svg', '.gif')) and not base.startswith('data:'):
                    basis_url_kandidaten.add(base)

    kandidaten_string = " | ".join(sorted(list(basis_url_kandidaten)))
    return kandidaten_string if basis_url_kandidaten else "N/A"


def normalize_url(url: str) -> str:
    """
    Normalisiert eine URL: entfernt Fragmente, sortiert/entfernt bestimmte Query-Parameter und entfernt nachgestellte Schrägstriche.
    (Diese Hilfsfunktion muss definiert sein, um den Code lauffähig zu machen.)
    """
    if not url or not url.startswith('http'):
        return url
    
    parsed = urlparse(url)
    # Entferne Fragment-Bezeichner (#...)
    path = parsed.path
    query = parsed.query
    
    # Optional: Entferne nachgestellten Schrägstrich, außer wenn der Pfad nur '/' ist
    if path.endswith('/') and len(path) > 1:
        path = path.rstrip('/')
        
    # Optional: Logik zur Bereinigung von Query-Parametern könnte hier eingefügt werden
    
    # Erstelle die bereinigte URL neu (ohne Fragment)
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        query, # Behalte die Query-Parameter
        ''     # Fragment ist leer
    ))
    return normalized

def extract_and_normalize_url(html_content: str) -> str:
    """
    Extrahiert die reinste Produkt-URL aus dem HTML mithilfe intelligenter
    Suche und normalisiert sie anschließend.
    """
    found_url = None
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 1. HÖCHSTE PRIORITÄT: Kanonische Links
        canonical_link = soup.find('link', {'rel': 'canonical'})
        if canonical_link and canonical_link.get('href'):
            found_url = canonical_link['href'].strip()
        
        # 2. ZWEITE PRIORITÄT: Alternate Link (de)
        # Sucht nach <link rel="alternate" href="..." hreflang="de">
        if not found_url:
            alternate_de_link = soup.find('link', {'rel': 'alternate', 'hreflang': 'de'})
            if alternate_de_link and alternate_de_link.get('href'):
                found_url = alternate_de_link['href'].strip()
        
        # 3. DRITTE PRIORITÄT: Open Graph Tags
        if not found_url:
            og_url_meta = soup.find('meta', {'property': 'og:url'})
            if og_url_meta and og_url_meta.get('content'):
                found_url = og_url_meta['content'].strip()

        # 4. VIERTE PRIORITÄT: Apple iTunes/App Meta-Tag (z.B. für App-Stores)
        if not found_url:
            apple_meta = soup.find('meta', {'name': 'apple-itunes-app'})
            if apple_meta and apple_meta.get('content'):
                content = apple_meta['content']
                match = re.search(r'app-argument=(https?://.+)', content)
                if match:
                    found_url = match.group(1).strip()
        
        # 5. FÜNFTE PRIORITÄT: Schema.org Product/Offers URL
        if not found_url:
            product_links = soup.select('[itemtype*="schema.org/Product"] a[href], [itemtype*="schema.org/Offer"] a[href]')
            if product_links:
                found_url = max([link['href'] for link in product_links if link.get('href')], key=len, default=None)

        # 6. LETZTER FALLBACK: AGGRESSIVE REGEX-SUCHE im gesamten HTML-Text
        if not found_url:
            # Sucht nach http/https URLs, die keine statischen Ressourcen (Bilder, JS, CSS) sind
            # Wir suchen URLs mit mindestens 3 Pfadsegmenten (wahrscheinlich spezifische Produkt-URL)
            urls = re.findall(r'https?://(?:www\.)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s"\']*)?', html_content)
            
            # Filtert nach URLs, die spezifischer erscheinen (mind. 3 Pfadsegmente und keine zu kurzen URLs)
            product_urls = [u for u in urls if u.count('/') >= 3 and len(u) > 30 and not any(ext in u for ext in ['.js', '.css', '.png', '.jpg', '.svg'])] 
            
            if product_urls:
                # Wählt die längste gefundene URL, da sie am spezifischsten ist
                found_url = max(product_urls, key=len)

        # 7. Normalisierung der URL
        if found_url and found_url.startswith('http'):
            return found_url

        return "" # Gibt leeren String zurück, wenn nichts gefunden wurde
        
    except Exception as e:
        # Hier sollte eine geeignete Fehlerbehandlung stattfinden (z.B. Logging)
        print(f"Fehler beim Parsen: {e}")
        return ""
def extract_title_from_html(html_content: str) -> str:
    """Extrahiert den bereinigten Titel."""
    soup = BeautifulSoup(html_content, 'lxml')
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        title = re.sub(r' \| .*| - .*| – .*| :: .*', '', title)
        return title
    
    if h1 := soup.find('h1'):
        return h1.get_text().strip()
    
    if og_title := soup.find('meta', {'property': 'og:title'}):
        if og_title.get('content'):
            return og_title['content'].strip()
            
    return "N/A"


def extract_core_html_data(html_content: str) -> Dict[str, Any]:
    """
    Führt alle deterministischen HTML-Extraktionen durch und konsolidiert sie in einem Dictionary.
    """
    title = extract_title_from_html(html_content)
    url = extract_and_normalize_url(html_content) # <- Ihre Power-Funktion wird hier verwendet
  
    
    return {
        "title": title,
        "url": url      
    }


def clean_html_to_core_text(html_content: str) -> str:
    """
    Parst den HTML-Inhalt, entfernt alle nicht-relevanten Boilerplate-Elemente, 
    und extrahiert den maximalen reinen Produkt-Kern-Text.
    """
    soup = BeautifulSoup(html_content, 'lxml')

    ignore_tags = [
        'script', 'style', 'header', 'footer', 'nav', 
         'iframe', 'noscript', 'button',
        'link', 'meta', 'svg', 'img', 'picture', 'source'
    ]
    
    boilerplate_selectors = [
        '.cookie-banner', '#cookie-consent', '.gdpr-popup',
        '#site-footer', '#site-header', '.site-nav', '.related-products',
        '.upsell', '.cross-sell', '.newsletter-signup', '.social-links',
        'dialog', 'modal', 'popup', 'menu', 'search',
        'toolbar', 'banner' 
    ]
    
    for tag in soup(ignore_tags):
        tag.decompose()

    for selector in boilerplate_selectors:
        for element in soup.select(selector):
            element.decompose()

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
        
    text = soup.get_text()

    text = re.sub(r'[\t\r\n]+', '\n', text)
    text = re.sub(r' +', ' ', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))
    text = re.sub(r'\n\s*\n', '\n', text).strip()
    
    return text


def process_html_to_llm_input(html_path: Path, output_path: Path):
    """
    Hauptfunktion, die HTML verarbeitet und die LLM-Input-JSON-Datei speichert.
    """
    print(f"\n[SCHRITT 1/2: HTML-PROZESSOR]")
   
    isAmazon:bool=False
    product_url=""
    product_title=""
    if not html_path.exists():
        raise FileNotFoundError(f"HTML-Quelldatei nicht gefunden: {html_path}")    
      
    raw_html = _read_text(html_path)

    if is_amazon_html(raw_html):      
        parser = AmazonProductParser(raw_html)
        product = parser.parse()
        isAmazon=True

    print("-> Starte Extraktion der Bild-Kandidaten...")
    if(isAmazon):
         bild_kandidaten = product.images
         product_url=product.product_info["shortlink"]
    else:
        core_data=extract_core_html_data(raw_html)
        bild_kandidaten = extrahiere_produktbilder_aus_html(raw_html)
        product_url=core_data.get("url","N/A")
        product_title=core_data.get("title","N/A")
    
   
    #print(f" 	-> Gefundene Bild-Kandidaten: {len(bild_kandidaten.split(' | ')) if bild_kandidaten != 'N/A' else 0} URLs/Deskriptoren.")
  
    print("-> Starte HTML-Bereinigung...")
    clean_text = clean_html_to_core_text(raw_html)
    print("<- HTML-Bereinigung abgeschlossen.")

    if not clean_text.strip():
        print("WARNUNG: Der bereinigte Text ist leer.", file=sys.stderr)
        clean_text = "N/A"

    llm_input_data = {
        "source_file": str(html_path),        
        "clean_text": clean_text,
        "isAmazon":isAmazon,
        "bild_kandidaten": bild_kandidaten,
        "product_url":product_url,
        "product_title":product_title
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(llm_input_data, f, ensure_ascii=False, indent=2)

    print(f"\n[ERFOLG] LLM-Input-Datei gespeichert in: {output_path}")
    
    return llm_input_data

    


# ----------------------------- ROUTING & AI-PARSING LOGIK --------------------------------------
def process_one(fp: Path, out_dir: Path) -> Tuple[bool, str, Dict]:
    """
    Orchestriert die AI-Pipeline: HTML-Extraktion -> LLM-Extraktion -> Mapping.
    """      
    temp_llm_input_file = fp.with_name(f"{fp.stem}.llm_input.json")
    temp_ai_output_file = fp.with_name(f"{fp.stem}.llm_output.json")
       
    def cleanup_temp_files():
        temp_llm_input_file.unlink(missing_ok=True)
        temp_ai_output_file.unlink(missing_ok=True)
        fp.unlink(missing_ok=True) 
    
    try: 
        # 1. HTML VORBEREITUNG FÜR LLM (Schreibt llm_input.json)
        ai_inputput_data=process_html_to_llm_input(fp, temp_llm_input_file)
        # 2. LLM-EXTRAKTION
        extract_and_save_data(ai_inputput_data, temp_ai_output_file)        
        
        # 3. DATEN-MAPPING (Kombiniert HTML-Core und LLM-Output)
        with open(temp_ai_output_file, 'r', encoding='utf-8') as f:
            ai_output_data = json.load(f)

        # NEUE PRÜFUNG 1: Stoppt, wenn der LLM-Output einen Extraktionsfehler enthält (z.B. Overload)
        if "Extraktionsfehler" in ai_output_data.get("extracted_data", {}):
            error_message = ai_output_data["extracted_data"]["Extraktionsfehler"]
            # Wirft eine Exception, um die Speicherung des finalen Output-Files zu verhindern.
            raise ValueError(f"LLM-Extraktionsfehler (z.B. Overload): {error_message}")


        data_mapped = map_ai_output_to_target_format(
            ai_output_data,
            ai_inputput_data            
        ) 
        
        # NEUE PRÜFUNG 2: Stoppt, wenn der berechnete Preis N/A ist
        if data_mapped.get('akt_preis') == 'N/A':
            # Wirft eine Exception, um die Speicherung des finalen Output-Files zu verhindern.
            raise ValueError("Produktpreis 'akt_preis' ist 'N/A'. Überspringe Speicherung.")


        # Speichere das Endergebnis
        product_identifier = data_mapped.get('product_id', 'N/A')
        if product_identifier in ('N/A', None):
            random_id = str(uuid.uuid4()).replace('-', '') 
            product_identifier = f"random_{random_id[:12]}" 
            
        final_output_file = out_dir / f"{product_identifier}.json" 

        tmp = final_output_file.with_suffix(".tmp")
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(data_mapped, f, indent=4, ensure_ascii=False)
        tmp.replace(final_output_file)
        
        #cleanup_temp_files()
        
        return True, f"AI OK -> {final_output_file.name}"

    except Exception as e:
        cleanup_temp_files()
        raise Exception(f"AI-Pipeline/Mapping Fehler: {e}")


# ----------------------------- DAEMON LOOP --------------------------------------

def daemon_loop(interval: int = INTERVAL_SECS) -> None:
    """
    Watch-Loop: zieht regelmäßig die älteste HTML-Datei und verarbeitet sie.
    """
    print(f"[product-parser] watching {PRODUCKT_DIR} every {interval}s -> {OUT_DIR}")
    reg = load_registry(REGISTRY_PATH) 
    while True:
        try:
            fp = pick_oldest_html(PRODUCKT_DIR) 
            if not fp:
                time.sleep(interval)
                continue
            ok, msg = process_one(fp, OUT_DIR)
            print(f"[product-parser] {msg}")
            time.sleep(1) 
        except Exception as e:
            print(f"[product-parser] SCHWERWIEGENDER FEHLER IM DAEMON: {e}", file=sys.stderr)
            time.sleep(interval)


if __name__ == '__main__':
    PRODUCKT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    
    daemon_loop()