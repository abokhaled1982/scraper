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

from core.logging import get_logger  # noqa: E402
log = get_logger("product_parser")  # noqa: E402
from core.paths import PRODUCKT_DIR, INTERVAL_SECS
from core.db import deals_repo

# Importiere die fachlich getrennten Module
from core.workers.amazon.utils import (
    _read_text,
    is_amazon_html,
    pick_oldest_html, map_ai_output_to_target_format
)

# Importe für Amazon und AI-Pipeline

from core.workers.ai_parser.ai_extractor import extract_and_save_data 

from core.workers.amazon.amazon_parser import AmazonProductParser


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


def _score_img_tag(img, soup) -> int:
    """
    Bewertet ein <img>-Tag danach, wie wahrscheinlich es das Hauptproduktbild ist.
    Höherer Score = wahrscheinlicher das Hauptbild.
    """
    score = 0
    tag_str = str(img).lower()
    classes = " ".join(img.get("class") or []).lower()
    parent_classes = " ".join(
        c for p in img.parents
        for c in (p.get("class") or [])
    ).lower()
    alt = (img.get("alt") or "").lower()
    img_id = (img.get("id") or "").lower()

    # Positive Signale: Klassen/IDs die auf Hauptbild hindeuten
    PRODUCT_SIGNALS = [
        "product", "pdp", "main-image", "primary", "hero",
        "gallery-main", "featured", "zoom", "detail", "article",
        "hauptbild", "produktbild",
    ]
    for signal in PRODUCT_SIGNALS:
        if signal in classes or signal in img_id:
            score += 10
        if signal in parent_classes:
            score += 5

    # Negative Signale: Thumbnails, Navigation, Logos, Dekoration
    NOISE_SIGNALS = [
        "thumb", "thumbnail", "mini", "small", "icon", "logo",
        "banner", "badge", "sprite", "avatar", "review",
        "related", "upsell", "cross", "swatch", "nav", "menu",
    ]
    for signal in NOISE_SIGNALS:
        if signal in classes or signal in img_id or signal in parent_classes:
            score -= 15

    # Größe aus Attributen (größere Bilder = Hauptbild)
    try:
        w = int(img.get("width") or 0)
        h = int(img.get("height") or 0)
        if w >= 400 or h >= 400:
            score += 8
        elif w >= 200 or h >= 200:
            score += 3
        elif 0 < w < 80 or 0 < h < 80:
            score -= 10  # eindeutig Thumbnail
    except (ValueError, TypeError):
        pass

    # Zoom/High-Res Attribute vorhanden = sehr starkes Signal
    HIRES_ATTRS = ["data-zoom-src", "data-hi-res", "data-large",
                   "data-full-image-url", "data-zoom"]
    for attr in HIRES_ATTRS:
        if img.has_attr(attr):
            score += 12

    # Alt-Text enthält Produktbezug
    if alt and len(alt) > 5 and not any(
        n in alt for n in ["logo", "icon", "banner", "sprite"]
    ):
        score += 3

    # Lazy-Loading data-src ohne src = wahrscheinlich echtes Inhaltsbild
    if img.has_attr("data-src") and not img.get("src", "").startswith("http"):
        score += 4

    return score


def _resolve_img_url(img) -> str:
    """
    Gibt die beste verfügbare URL eines <img>-Tags zurück.
    Reihenfolge: High-Res-Attribute → data-src → src → srcset (größte).
    """
    HIRES_ATTRS = [
        "data-zoom-src", "data-hi-res", "data-large",
        "data-full-image-url", "data-original",
    ]
    for attr in HIRES_ATTRS:
        val = (img.get(attr) or "").strip()
        if val and val.startswith("http"):
            return val

    for attr in ["data-src", "src"]:
        val = (img.get(attr) or "").strip()
        if val and val.startswith("http"):
            return val

    # srcset: nimm den Kandidaten mit dem größten Deskriptor
    for attr in ["srcset", "data-srcset"]:
        val = img.get(attr) or ""
        if not val:
            continue
        best_url, best_w = "", 0
        for part in re.split(r",\s*", val):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            u = tokens[0]
            if not u.startswith("http"):
                continue
            w = 0
            if len(tokens) > 1:
                m = re.match(r"(\d+)[wx]", tokens[1])
                w = int(m.group(1)) if m else 0
            if w > best_w:
                best_w, best_url = w, u
        if best_url:
            return best_url

    return ""


# Affiliate/Tracking-Parameter die aus Produkt-URLs entfernt werden sollen
_AFFILIATE_PARAMS = {
    "ref", "tag", "linkCode", "linkId", "th",          # Amazon
    "utm_source", "utm_medium", "utm_campaign",         # UTM
    "utm_term", "utm_content", "utm_id",
    "aff_id", "aff", "affiliate", "partner_id",        # Generisch
    "clickid", "click_id", "subid", "sub_id",
    "gclid", "fbclid", "msclkid", "ttclid",            # Ad-Tracker
    "epik", "_ga",
}

def _clean_product_url(url: str) -> str:
    """
    Entfernt Affiliate- und Tracking-Parameter aus einer URL,
    behält aber produktrelevante Query-Parameter (z.B. Varianten-IDs).
    """
    if not url or not url.startswith("http"):
        return url
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=False)
        cleaned_qs = {k: v for k, v in qs.items() if k.lower() not in _AFFILIATE_PARAMS}
        new_query = urlencode(cleaned_qs, doseq=True)
        path = parsed.path.rstrip("/") if len(parsed.path) > 1 else parsed.path
        return urlunparse((
            parsed.scheme, parsed.netloc, path,
            parsed.params, new_query, ""
        ))
    except Exception:
        return url


def extrahiere_produktbilder_aus_html(
    html_content: str,
    product_node: dict | None = None,
) -> str:
    """
    Extrahiert das Hauptproduktbild aus HTML.

    Prioritäten:
      1. LD+JSON  (product_node übergeben oder intern geparst)
      2. og:image Meta-Tag
      3. Scoring-basierte <img>-Suche im DOM
         – bewertet Klassen, Größe, High-Res-Attribute, Eltern-Kontext
      4. Größtes <img> im gesamten Body (letzter Fallback)
    """
    html_content = html_content or ""

    # ── PRIORITÄT 1: LD+JSON ─────────────────────────────────────────────────
    if product_node is None:
        product_node = _find_product_ldjson(_extract_ldjson_blocks(html_content))
    if product_node:
        img = _extract_image_from_ldjson_product(product_node)
        if img:
            return img

    soup = BeautifulSoup(html_content, "lxml")

    # ── PRIORITÄT 2: og:image ────────────────────────────────────────────────
    og_img = soup.find("meta", {"property": "og:image"})
    if og_img:
        val = (og_img.get("content") or "").strip()
        if val and val.startswith("http"):
            return val

    # ── PRIORITÄT 3: Score-basierte IMG-Suche ────────────────────────────────
    SKIP_EXTENSIONS = (".svg", ".gif", ".webp")   # webp oft Thumbnail auf manchen Shops

    best_img, best_score = None, -999
    for img in soup.find_all("img"):
        url = _resolve_img_url(img)
        if not url:
            continue
        url_lower = url.lower()
        if any(url_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        if url_lower.startswith("data:"):
            continue

        sc = _score_img_tag(img, soup)
        if sc > best_score:
            best_score, best_img = sc, url

    # Nur akzeptieren wenn Score positiv (echtes Produktbild-Signal vorhanden)
    if best_img and best_score > 0:
        # eBay-Spezial: maximale Auflösung erzwingen
        if "ebayimg.com" in best_img:
            best_img = re.sub(r"s-l\d+\.", "s-l1600.", best_img)
        return _get_base_url_path(best_img)

    # ── PRIORITÄT 4: Größtes <img> im Body (letzter Notfall-Fallback) ────────
    largest_url, largest_area = "", 0
    for img in soup.find_all("img"):
        url = _resolve_img_url(img)
        if not url or url.startswith("data:"):
            continue
        try:
            w = int(img.get("width") or 0)
            h = int(img.get("height") or 0)
            area = w * h
        except (ValueError, TypeError):
            area = 0
        if area > largest_area:
            largest_area, largest_url = area, url

    return _get_base_url_path(largest_url) if largest_url else "N/A"
def normalize_url(url: str) -> str:
    """
    Normalisiert eine Produkt-URL:
    - Entfernt Affiliate- und Tracking-Parameter
    - Entfernt Fragment (#...)
    - Entfernt nachgestellten Schrägstrich
    """
    return _clean_product_url(url)

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
        log.error(f"Fehler beim Parsen: {e}")
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
    LD+JSON wird einmalig geparst und an alle Unterfunktionen weitergereicht.

    Rückgabe-Keys:
      title       – Produkttitel
      url         – bereinigte Produkt-URL (ohne Affiliate-Parameter)
      bild_ldjson – Hauptbild (LD+JSON → og:image → Score-DOM → größtes img)
    """
    # ── SCHRITT 1: LD+JSON einmalig parsen ───────────────────────────────────
    ldjson_blocks = _extract_ldjson_blocks(html_content)
    product_node  = _find_product_ldjson(ldjson_blocks)

    # ── SCHRITT 2: Bild — vollständige Fallback-Kette ────────────────────────
    # product_node weitergeben → kein doppeltes Parsen in der Funktion
    bild = extrahiere_produktbilder_aus_html(html_content, product_node=product_node)

    # ── SCHRITT 3: Titel — LD+JSON, dann HTML-Fallback ───────────────────────
    title = (product_node.get("name") or "").strip() if product_node else ""
    if not title:
        title = extract_title_from_html(html_content)

    # ── SCHRITT 4: URL — LD+JSON Priorität 0, dann HTML-Fallbacks ────────────
    url = extract_and_normalize_url(html_content, product_node=product_node)

    return {
        "title":      title,
        "url":        url,
        "bild_ldjson": bild,
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
    log.info(f"\n[SCHRITT 1/2: HTML-PROZESSOR]")
   
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

    log.info("-> Starte Extraktion der Bild-Kandidaten...")
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
            log.info(f"   -> Bild aus LD+JSON gefunden: {bild_ldjson[:80]}...")
            bild_kandidaten = bild_ldjson
        else:
            # Fallback: HTML-DOM-Suche (nur wenn LD+JSON kein Bild liefert)
            log.info("   -> Kein LD+JSON-Bild, starte HTML-DOM-Fallback...")
            bild_kandidaten = extrahiere_produktbilder_aus_html(raw_html)
    
   
    #print(f" 	-> Gefundene Bild-Kandidaten: {len(bild_kandidaten.split(' | ')) if bild_kandidaten != 'N/A' else 0} URLs/Deskriptoren.")
  
    log.info("-> Starte HTML-Bereinigung...")
    clean_text = clean_html_to_core_text(raw_html)
    log.info("<- HTML-Bereinigung abgeschlossen.")

    if not clean_text.strip():
        log.warning("WARNUNG: Der bereinigte Text ist leer.")
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

    log.info(f"\n[ERFOLG] LLM-Input-Datei gespeichert in: {output_path}")
    
    return llm_input_data

    


# ----------------------------- ROUTING & AI-PARSING LOGIK --------------------------------------
def process_one(fp: Path, out_dir: Path | None = None) -> Tuple[bool, str, Dict]:
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


        # Speichere das Endergebnis in DB (statt JSON in OUT_DIR)
        product_identifier = data_mapped.get('product_id', 'N/A')
        if product_identifier in ('N/A', None):
            random_id = str(uuid.uuid4()).replace('-', '')
            product_identifier = f"random_{random_id[:12]}"

        deal_id = deals_repo.enqueue(str(product_identifier), data_mapped)

        cleanup_temp_files()

        return True, f"AI OK -> deal#{deal_id} ({product_identifier})"

    except Exception as e:
        cleanup_temp_files()
        raise Exception(f"AI-Pipeline/Mapping Fehler: {e}")


# ----------------------------- DAEMON LOOP --------------------------------------

def daemon_loop(interval: int = INTERVAL_SECS) -> None:
    """
    Watch-Loop: zieht regelmäßig die älteste HTML-Datei und verarbeitet sie.
    """
    from core.db import workers_repo
    _WORKER = "amazon_parser"
    workers_repo.register(_WORKER)
    log.info(f"[product-parser] watching {PRODUCKT_DIR} every {interval}s -> DB(deals)")
    while True:
        try:
            fp = pick_oldest_html(PRODUCKT_DIR) 
            if not fp:
                workers_repo.set_idle(_WORKER)
                time.sleep(interval)
                continue
            workers_repo.set_task(_WORKER, f"parsing {fp.name}")
            ok, msg = process_one(fp, None)
            log.info(f"[product-parser] {msg}")
            time.sleep(1) 
        except Exception as e:
            workers_repo.set_error(_WORKER, str(e)[:200])
            log.error(f"[product-parser] SCHWERWIEGENDER FEHLER IM DAEMON: {e}")
            time.sleep(interval)


if __name__ == '__main__':
    # OUT_DIR entfällt – Deals werden in die DB geschrieben.
    daemon_loop()