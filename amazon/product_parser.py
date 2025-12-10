#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import json, time
import re
from pathlib import Path
from typing import Tuple, Dict, Any
import sys
import uuid
from bs4 import BeautifulSoup, Comment 
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode 
# Oben bei den Imports hinzufügen:
import metadata_parser
# --- NEU: Extruct Integration ---
import extruct
from w3lib.html import get_base_url

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
    if not url: return ""
    # Entferne w/x-Deskriptoren falls vorhanden (z.B. " 300w")
    url_without_desc = re.sub(r'\s+\d+[wx]$', '', url).strip()
    
    try:
        parsed_url = urlparse(url_without_desc)
        # Bilde die URL neu aus Schema, Netloc und Pfad, ohne Query/Fragment
        return urlunparse(parsed_url._replace(query='', params='', fragment=''))
    except ValueError:
        return url_without_desc # Fehlerfall

def is_valid_image_url(url: str) -> bool:
    """Hilfsfunktion: Filtert Müll-URLs (Logos, Icons, etc.)"""
    if not url: return False
    url_lower = url.lower()
    
    # Blockliste (Müll vermeiden)
    junk_terms = [
        'logo', 'icon', 'button', 'payment', 'trusted', 'rating', 'star', 
        'placeholder', 'pixel', 'blank', 'avatar', 'user', 'confetti', 
        'map', 'weather', 'sprite', 'loader', 'spinner'
    ]
    if any(term in url_lower for term in junk_terms):
        return False
        
    # Base64 ignorieren wir meistens bei großen Galerien (zu lang für LLM Input)
    if 'data:image' in url_lower: return False 

    return True
# Oben bei den Imports hinzufügen:
import metadata_parser

def extrahiere_produktbilder_aus_html(html_content: str) -> str:
    """
    Professioneller Bild-Parser (Best-of-Breed Ansatz).
    1. Metadata-Parser: Holt robust das OpenGraph/Twitter 'Hero'-Bild.
    2. Extruct: Holt strukturierte Daten (Schema.org/JSON-LD).
    3. Custom JSON Parser: Holt versteckte Galerien (L'TUR, TUI).
    4. Fallback: Scannt img-Tags.
    """
    if not html_content:
        return "N/A"

    soup = BeautifulSoup(html_content, 'lxml')
    candidates = []
    seen = set()
    base_url = get_base_url(html_content, 'http://localhost')

    # -------------------------------------------------------------------------
    # STRATEGIE 1: OpenGraph via 'metadata-parser' (Die Profi-Lösung)
    # -------------------------------------------------------------------------
    try:
        # search_head_only=False sucht im ganzen Body, falls Meta-Tags verrutscht sind
        page = metadata_parser.MetadataParser(html=html_content, search_head_only=False)
        
        # 'image' sucht automatisch nach og:image, twitter:image, link_image_src etc.
        og_img = page.get_metadata_link('image')
        
        if og_img:
            # metadata-parser fixiert oft schon relative URLs, aber sicher ist sicher:
            if og_img.startswith('//'): og_img = 'https:' + og_img
            
            base = _get_base_url_path(og_img)
            if base and is_valid_image_url(base):
                seen.add(base)
                candidates.append(base)
    except Exception as e:
        print(f"[Parser] Warnung: Metadata-Parser Fehler: {e}")

    # -------------------------------------------------------------------------
    # STRATEGIE 2: Extruct (Schema.org / JSON-LD)
    # -------------------------------------------------------------------------
    try:
        data = extruct.extract(html_content, base_url=base_url)
        for item in data.get('json-ld', []):
            item_type = item.get('@type')
            if isinstance(item_type, list): item_type = item_type[0] if item_type else ""
            
            if item_type in ['Product', 'Hotel', 'LodgingBusiness', 'ImageObject']:
                imgs = item.get('image')
                if imgs:
                    if isinstance(imgs, str): imgs = [imgs]
                    elif isinstance(imgs, dict): imgs = [imgs.get('url')]
                    elif isinstance(imgs, list):
                        imgs = [i.get('url') if isinstance(i, dict) else i for i in imgs]
                    
                    for url in imgs:
                        if url:
                            base = _get_base_url_path(url)
                            if base and base not in seen and is_valid_image_url(base):
                                seen.add(base)
                                candidates.append(base)
    except Exception:
        pass # Extruct Fehler ignorieren

    # -------------------------------------------------------------------------
    # STRATEGIE 3: JSON-Attribute (L'TUR, TUI, Modern SPAs)
    # -------------------------------------------------------------------------
    if len(candidates) < 5:
        potential_gallery_tags = soup.find_all(lambda tag: tag.name.endswith('gallery') or tag.name.endswith('ui') or 'gallery' in str(tag.get('class', [])))
        for tag in potential_gallery_tags:
            for attr_name, attr_value in tag.attrs.items():
                if 'data' in attr_name or 'json' in attr_name:
                    if isinstance(attr_value, str) and (attr_value.strip().startswith('{') or attr_value.strip().startswith('[')):
                        try:
                            json_data = json.loads(attr_value)
                            # ... (deine existierende extract_urls Funktion hier einfügen oder nutzen) ...
                            def extract_urls_recursive(obj):
                                found = []
                                if isinstance(obj, dict):
                                    for k, v in obj.items():
                                        if k in ['url', 'full', 'src', 'large'] and isinstance(v, str) and v:
                                            found.append(v)
                                        else: found.extend(extract_urls_recursive(v))
                                elif isinstance(obj, list):
                                    for item in obj: found.extend(extract_urls_recursive(item))
                                return found

                            for url in extract_urls_recursive(json_data):
                                if url.startswith('//'): url = 'https:' + url
                                base = _get_base_url_path(url)
                                if base and base not in seen and is_valid_image_url(base):
                                    seen.add(base)
                                    candidates.append(base)
                        except: continue

    # -------------------------------------------------------------------------
    # STRATEGIE 4: Klassischer Fallback (img Tags)
    # -------------------------------------------------------------------------
    container_tags = ['div', 'figure', 'section', 'ul', 'li', 'slider-view']
    target_attrs = ['data-zoom-src', 'data-hi-res', 'data-large', 'data-full-image-url', 'data-src', 'src']
    
    for container in soup.find_all(container_tags):
        imgs = container.find_all('img')
        if not imgs: continue
        if len(imgs) < 2 and candidates: continue 

        for img in imgs:
            urls = []
            for attr in target_attrs:
                if img.has_attr(attr): urls.append(img[attr])
            
            if img.has_attr('srcset'):
                parts = [p.strip().split()[0] for p in img['srcset'].split(',') if p.strip()]
                urls.extend(parts)
            if img.has_attr('data-srcset'):
                parts = [p.strip().split()[0] for p in img['data-srcset'].split(',') if p.strip()]
                urls.extend(parts)

            for url in urls:
                if url.startswith('//'): url = 'https:' + url
                base = _get_base_url_path(url)
                if "ebayimg.com" in base: base = re.sub(r's-l\d+\.', 's-l1600.', base)

                if base and base not in seen and is_valid_image_url(base):
                    seen.add(base)
                    candidates.append(base)

    return " | ".join(candidates) if candidates else "N/A"

def normalize_url(url: str) -> str:
    """
    Normalisiert eine URL: entfernt Fragmente, sortiert/entfernt bestimmte Query-Parameter und entfernt nachgestellte Schrägstriche.
    """
    if not url or not url.startswith('http'):
        return url
    
    parsed = urlparse(url)
    path = parsed.path
    query = parsed.query
    
    if path.endswith('/') and len(path) > 1:
        path = path.rstrip('/')
        
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        query, 
        ''     
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
        if not found_url:
            alternate_de_link = soup.find('link', {'rel': 'alternate', 'hreflang': 'de'})
            if alternate_de_link and alternate_de_link.get('href'):
                found_url = alternate_de_link['href'].strip()
        
        # 3. DRITTE PRIORITÄT: Open Graph Tags
        if not found_url:
            og_url_meta = soup.find('meta', {'property': 'og:url'})
            if og_url_meta and og_url_meta.get('content'):
                found_url = og_url_meta['content'].strip()

        # 4. VIERTE PRIORITÄT: Apple iTunes/App Meta-Tag
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

        # 6. LETZTER FALLBACK: AGGRESSIVE REGEX-SUCHE
        if not found_url:
            urls = re.findall(r'https?://(?:www\.)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s"\']*)?', html_content)
            product_urls = [u for u in urls if u.count('/') >= 3 and len(u) > 30 and not any(ext in u for ext in ['.js', '.css', '.png', '.jpg', '.svg'])] 
            if product_urls:
                found_url = max(product_urls, key=len)

        if found_url and found_url.startswith('http'):
            return found_url

        return "" 
        
    except Exception as e:
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
    url = extract_and_normalize_url(html_content) 
    
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
        'link', 'svg', 'img', 'picture', 'source'
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

        # NEUE PRÜFUNG 1: Stoppt, wenn der LLM-Output einen Extraktionsfehler enthält
        if "Extraktionsfehler" in ai_output_data.get("extracted_data", {}):
            error_message = ai_output_data["extracted_data"]["Extraktionsfehler"]
            raise ValueError(f"LLM-Extraktionsfehler (z.B. Overload): {error_message}")

        data_mapped = map_ai_output_to_target_format(
            ai_output_data,
            ai_inputput_data            
        ) 
        
        # NEUE PRÜFUNG 2: Stoppt, wenn der berechnete Preis N/A ist
        if data_mapped.get('akt_preis') == 'N/A':
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
        #cleanup_temp_files()
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