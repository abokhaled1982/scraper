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
from amazon.utils import (
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

def _extract_ldjson_blocks(html_content: str) -> list[dict]:
    """
    Extrahiert alle gültigen LD+JSON-Blöcke aus dem HTML.
    Gibt eine Liste von parsed JSON-Objekten zurück (niemals None).
    """
    soup = BeautifulSoup(html_content or "", 'lxml')
    blocks = []
    for tag in soup.find_all('script', {'type': 'application/ld+json'}):
        try:
            raw = tag.string or ""
            data = json.loads(raw.strip())
            # Normalisiere zu Liste (manche Seiten geben ein einzelnes Objekt zurück)
            if isinstance(data, list):
                blocks.extend(data)
            elif isinstance(data, dict):
                blocks.append(data)
        except (json.JSONDecodeError, ValueError):
            continue
    return blocks


def _find_product_ldjson(blocks: list[dict]) -> dict | None:
    """
    Sucht in einer Liste von LD+JSON-Blöcken nach dem ersten Produkt-Block.
    Unterstützt @graph-Strukturen und direkte Product-Objekte.
    """
    all_nodes: list[dict] = []
    for block in blocks:
        # @graph auffalten
        if "@graph" in block and isinstance(block["@graph"], list):
            all_nodes.extend(block["@graph"])
        else:
            all_nodes.append(block)

    for node in all_nodes:
        typ = node.get("@type", "")
        # @type kann ein String oder eine Liste sein
        types = [typ] if isinstance(typ, str) else (typ if isinstance(typ, list) else [])
        if any("Product" in t for t in types):
            return node
    return None


def _extract_image_from_ldjson_product(product_node: dict) -> str:
    """
    Extrahiert die beste Bild-URL aus einem LD+JSON Product-Node.
    Unterstützt: string, Liste von strings, ImageObject (einzeln oder Liste).
    Gibt '' zurück, wenn kein Bild gefunden.
    """
    image_field = product_node.get("image")
    if not image_field:
        return ""

    candidates: list[str] = []

    def _resolve(obj) -> str:
        """Löst ein einzelnes Bild-Objekt zu einer URL auf."""
        if isinstance(obj, str):
            return obj.strip()
        if isinstance(obj, dict):
            # ImageObject: bevorzuge 'url', dann 'contentUrl'
            return (obj.get("url") or obj.get("contentUrl") or "").strip()
        return ""

    if isinstance(image_field, list):
        for item in image_field:
            url = _resolve(item)
            if url:
                candidates.append(url)
    else:
        url = _resolve(image_field)
        if url:
            candidates.append(url)

    # Erstes Bild ist laut Schema.org das Hauptbild
    return candidates[0] if candidates else ""


def extrahiere_produktbilder_aus_html(html_content: str) -> str:
    """
    Sucht nach Bild-URLs, extrahiert deren Basis-Pfad und optimiert für eBay/Amazon.
    HÖCHSTE PRIORITÄT: LD+JSON Product-Schema → 100% zuverlässige Bildquelle.
    Fallback: HTML-DOM-Suche mit High-Res-Priorisierung.
    """
    # ── PRIORITÄT 1: LD+JSON (sicherste Quelle) ──────────────────────────────
    ldjson_blocks = _extract_ldjson_blocks(html_content or "")
    product_node = _find_product_ldjson(ldjson_blocks)
    if product_node:
        ldjson_image = _extract_image_from_ldjson_product(product_node)
        if ldjson_image:
            return ldjson_image  # Sofort zurückgeben – kein Fallback nötig

    # ── PRIORITÄT 2: HTML-DOM-Suche (Fallback) ───────────────────────────────
    soup = BeautifulSoup(html_content or "", 'lxml')
    
    # ÄNDERUNG 1: Liste für die Reihenfolge + Set für Duplikat-Check
    basis_url_kandidaten = []
    seen_urls = set()

    # 1. Container-Suche (erweitert für modernere Layouts)
    container_tags = ['div', 'figure', 'section', 'ul', 'li']  
    
    # Relevante Attribute für Bild-Quellen (High-Res zuerst)
    target_attrs = [
        'data-zoom-src',       # eBay Zoom Bild (sehr wichtig!)
        'data-hi-res',         # Generisch High-Res
        'data-large',          # Generisch
        'data-full-image-url', # Manche Shops
        'data-original',       # Lazy Loading Original
        'data-src',            # Lazy Loading Standard
        'src'                  # Fallback
    ]

    for container in soup.find_all(container_tags):
        imgs = container.find_all('img')
        
        # Filter lockern: Manchmal sind Hauptbilder isoliert, aber wir behalten
        # deine Logik bei, Gruppen zu bevorzugen.
        if len(imgs) < 2:
            continue 

        for img in imgs:
            urls = []
            
            # A) Normale Attribute prüfen
            for attr in target_attrs:
                if attr in img.attrs and img[attr]:
                    urls.append(img[attr])

            # B) Srcset parsen (mit Crash-Fix)
            for attr in ['srcset', 'data-srcset']:
                if attr in img.attrs and img[attr]:
                    # Split am Komma, aber leere Einträge filtern!
                    parts = [p.strip() for p in re.split(r',\s*', img[attr]) if p.strip()]
                    for part in parts:
                        url_part = part.split()[0]
                        urls.append(url_part)

            # C) URLs verarbeiten und bereinigen
            for url in urls:
                base = _get_base_url_path(url)
                
                # eBay-Spezial: Versuche URL auf maximale Auflösung (s-l1600) zu zwingen
                if "ebayimg.com" in base:
                    base = re.sub(r's-l\d+\.', 's-l1600.', base)

                if base and not base.lower().endswith(('.svg', '.gif')) and not base.startswith('data:'):
                    # ÄNDERUNG 2: Nur hinzufügen, wenn noch nicht gesehen (behält Reihenfolge)
                    if base not in seen_urls:
                        seen_urls.add(base)
                        basis_url_kandidaten.append(base)

    # ÄNDERUNG 3: 'sorted()' entfernt, Liste direkt joinen
    kandidaten_string = " | ".join(basis_url_kandidaten)
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

def extract_and_normalize_url(html_content: str, product_node: dict | None = None) -> str:
    """
    Extrahiert die reinste Produkt-URL aus dem HTML mithilfe intelligenter
    Suche und normalisiert sie anschließend.

    Prioritäten:
      0. LD+JSON  → product_node["url"]  (sicherste Quelle, wird von außen übergeben
                                           oder intern aus html_content geparst)
      1. Canonical Link
      2. Alternate hreflang="de"
      3. Open Graph og:url
      4. Apple iTunes App Meta
      5. Schema.org Microdata-Links
      6. Regex-Fallback (aggressiv)
    """
    found_url = None

    try:
        # ── PRIORITÄT 0: LD+JSON ─────────────────────────────────────────────
        # Falls kein fertiger Node übergeben wurde, selbst parsen (Fallback-Aufruf)
        if product_node is None:
            _blocks = _extract_ldjson_blocks(html_content)
            product_node = _find_product_ldjson(_blocks)

        if product_node:
            ldjson_url = (product_node.get("url") or "").strip()
            if ldjson_url and ldjson_url.startswith("http"):
                return normalize_url(ldjson_url)

        # ── PRIORITÄT 1–6: HTML-Fallbacks ────────────────────────────────────
        soup = BeautifulSoup(html_content, 'html.parser')

        # 1. Kanonische Links
        canonical_link = soup.find('link', {'rel': 'canonical'})
        if canonical_link and canonical_link.get('href'):
            found_url = canonical_link['href'].strip()

        # 2. Alternate Link (de)
        if not found_url:
            alternate_de_link = soup.find('link', {'rel': 'alternate', 'hreflang': 'de'})
            if alternate_de_link and alternate_de_link.get('href'):
                found_url = alternate_de_link['href'].strip()

        # 3. Open Graph og:url
        if not found_url:
            og_url_meta = soup.find('meta', {'property': 'og:url'})
            if og_url_meta and og_url_meta.get('content'):
                found_url = og_url_meta['content'].strip()

        # 4. Apple iTunes/App Meta-Tag
        if not found_url:
            apple_meta = soup.find('meta', {'name': 'apple-itunes-app'})
            if apple_meta and apple_meta.get('content'):
                content = apple_meta['content']
                match = re.search(r'app-argument=(https?://.+)', content)
                if match:
                    found_url = match.group(1).strip()

        # 5. Schema.org Microdata Product/Offer Links
        if not found_url:
            product_links = soup.select(
                '[itemtype*="schema.org/Product"] a[href], '
                '[itemtype*="schema.org/Offer"] a[href]'
            )
            if product_links:
                found_url = max(
                    [link['href'] for link in product_links if link.get('href')],
                    key=len, default=None
                )

        # 6. Aggressiver Regex-Fallback
        if not found_url:
            urls = re.findall(
                r'https?://(?:www\.)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s"\']*)?',
                html_content
            )
            product_urls = [
                u for u in urls
                if u.count('/') >= 3
                and len(u) > 30
                and not any(ext in u for ext in ['.js', '.css', '.png', '.jpg', '.svg'])
            ]
            if product_urls:
                found_url = max(product_urls, key=len)

        if found_url and found_url.startswith('http'):
            return normalize_url(found_url)

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
    HÖCHSTE PRIORITÄT: LD+JSON für Bild, Titel und URL — HTML-Fallbacks nur wenn nötig.
    """
    # ── SCHRITT 1: LD+JSON auslesen (einmalig, effizient) ────────────────────
    ldjson_blocks = _extract_ldjson_blocks(html_content)
    product_node = _find_product_ldjson(ldjson_blocks)

    # ── SCHRITT 2: Bild — LD+JSON hat absolute Priorität ────────────────────
    bild = ""
    if product_node:
        bild = _extract_image_from_ldjson_product(product_node)

    # ── SCHRITT 3: Titel — LD+JSON, dann HTML-Fallback ───────────────────────
    title = ""
    if product_node:
        title = (product_node.get("name") or "").strip()
    if not title:
        title = extract_title_from_html(html_content)

    # ── SCHRITT 4: URL — LD+JSON Priorität 0, dann HTML-Fallbacks ───────────
    # product_node wird direkt übergeben → kein doppeltes LD+JSON-Parsen
    url = extract_and_normalize_url(html_content, product_node=product_node)

    return {
        "title": title,
        "url": url,
        "bild_ldjson": bild,   # Hauptbild aus LD+JSON (leer = nicht gefunden)
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
        product_url=core_data.get("url","N/A")
        product_title=core_data.get("title","N/A")

        # HÖCHSTE PRIORITÄT: Bild aus LD+JSON (100% zuverlässig)
        bild_ldjson = core_data.get("bild_ldjson", "")
        if bild_ldjson:
            print(f"   -> Bild aus LD+JSON gefunden: {bild_ldjson[:80]}...")
            bild_kandidaten = bild_ldjson
        else:
            # Fallback: HTML-DOM-Suche (nur wenn LD+JSON kein Bild liefert)
            print("   -> Kein LD+JSON-Bild, starte HTML-DOM-Fallback...")
            bild_kandidaten = extrahiere_produktbilder_aus_html(raw_html)
    
   
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
        
        cleanup_temp_files()
        
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
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daemon_loop()