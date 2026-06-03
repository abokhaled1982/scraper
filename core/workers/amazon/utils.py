# data_mapper.py

from __future__ import annotations
import hashlib, json, time, shutil
from pathlib import Path
from typing import Optional, Dict, Any
import os 
import re 

# ----------------------------- ALLGEMEINE HILFSFUNKTIONEN (DATEI/REGISTRY) --------------------------------------

from core.logging import get_logger  # noqa: E402
log = get_logger("utils")  # noqa: E402
def is_amazon_html(html_content: str) -> bool:
    """Entscheidet anhand von Amazon-spezifischen Merkmalen, ob es eine Produktseite ist."""
    if any(tag in html_content for tag in [
        'id="productTitle"', 'id="ASIN"', 'data-asin=',
        'class="a-section a-spacing-none"', 'id="twisterDiv"'
        ]):
        return True
    return False

def _read_text(fp: Path) -> str:
    """Liest den Dateiinhalt als Text."""
    log.info(f" 	-> Read HTML-File: {fp.resolve()}")
    try:
        return fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return fp.read_bytes().decode("utf-8", errors="ignore")

def _sha1_bytes(b: bytes) -> str:
    """Berechnet den SHA1-Hash der übergebenen Bytes."""
    return hashlib.sha1(b).hexdigest() 

   
def move_to_failed(fp: Path, reason: str, FAILED_DIR: Path) -> None:
    """Verschiebt die Quelldatei und den Fehlergrund in den Fehler-Ordner."""
    target = FAILED_DIR / f"{fp.name}.txt"
    try:
        with target.open("w", encoding="utf-8") as f:
            f.write(f"Source: {fp.name}\nTimestamp: {time.time()}\nReason:\n{reason}")
        shutil.move(str(fp), str(FAILED_DIR / fp.name))
    except Exception as e:
        log.error(f"WARNUNG: Konnte Datei {fp.name} nicht nach FAILED_DIR verschieben: {e}")

def pick_oldest_html(PRODUCKT_DIR: Path) -> Optional[Path]:
    """Wählt die älteste HTML-Datei aus dem PRODUCKT_DIR zur Verarbeitung."""
    files = sorted(PRODUCKT_DIR.glob('*.html'), key=os.path.getmtime)
    if files:
        return files[0]
    return None

# ----------------------------- FACHLICHE HILFSFUNKTIONEN (Preis) --------------------------------------

def parse_price_string(price_str: str) -> Dict[str, Any]:
    """
    Konvertiert einen Preis-String (z.B. '399,99 €') in eine strukturierte Preis-Map.
    """
    if not price_str or price_str in ('N/A', '0', '0.0'):
        return {"raw": None, "value": None, "currency_hint": None}

    price_str_cleaned = re.sub(r'[^\d,\.€$£]', '', price_str).strip()
    
    # Intelligente Behandlung von Dezimal- und Tausenderzeichen
    has_comma = ',' in price_str_cleaned
    has_dot   = '.' in price_str_cleaned
    if has_comma and has_dot:
        # Mischformat: das LETZTE Trennzeichen ist das Dezimalzeichen.
        last_comma = price_str_cleaned.rfind(',')
        last_dot   = price_str_cleaned.rfind('.')
        if last_comma > last_dot:
            # Europäisch: '1.234,56' -> '1234.56'
            cleaned_str = price_str_cleaned.replace('.', '').replace(',', '.')
        else:
            # US: '1,234.56' -> '1234.56'
            cleaned_str = price_str_cleaned.replace(',', '')
    elif has_comma:
        # Beispiel: '399,99 €' -> '399.99'
        cleaned_str = price_str_cleaned.replace(',', '.')
    else:
        # Nur Punkte oder gar nichts (z.B. '99.99' oder '1234')
        cleaned_str = price_str_cleaned

    # Extrahiere Währung und numerischen Teil
    match = re.search(r'([0-9\.]+)', cleaned_str)
    currency_match = re.search(r'([€$£])', price_str) 

    try:
        value = float(match.group(1)) if match else None
    except ValueError:
        value = None 
        
    currency_hint = currency_match.group(1) if currency_match else None

    return {
        "raw": price_str,
        "value": value,
        "currency_hint": currency_hint
    }
# --- ZIEL-SCHEMA TEMPLATE ---


TARGET_SCHEMA_TEMPLATE = {
    "title": "N/A", "affiliate_url": "N/A", "brand": "N/A", "product_id": "N/A",
    "market": "N/A", # <-- NEU: Marktplatz
    "price": {"raw": None, "value": None, "currency_hint": None},
    "original_price": {"raw": None, "value": None, "currency_hint": None},
    "discount_amount": None, "discount_percent": "N/A", 
    "rating": {"value": 0.0, "counts": 0},
    "rabatt_text":"N/A",
    "coupon": {"code": "N/A", "code_details": "N/A", "more": "N/A"},
    "images": [], "features": [], 
    "feature_text": None, "description": None,
    "units_sold": "N/A", "seller_name": "N/A", "availability": "N/A", "shipping_info": "N/A",
    "hashtags": [],  # <--- HIER HINZUFÜGEN
    "reel_titel": "N/A", "reel_beschreibung": "N/A", "reel_caption": "N/A",
    "voiceover_text": "N/A",
    "produkt_kategorie": "N/A",
    "template_type": "",
    "type": "post",   # "reel" wenn Rabatt >= 30%, sonst "post"
}

def map_ai_output_to_target_format(
    ai_output: Dict[str, Any],
    ai_input: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Mappt die extrahierten Daten aus dem AI-Output und den HTML-Kerndaten 
    in das Ziel-JSON-Format (Englische Felder, bereinigt).
    """
    extracted = ai_output.get("extracted_data", {})
    final_output = TARGET_SCHEMA_TEMPLATE.copy()
    
    isAmazon = ai_input.get('isAmazon', False)
    
    # URL Mapping
    if isAmazon:
        final_output['affiliate_url'] = ai_input.get("product_url", "N/A")
    elif extracted.get('url_des_produkts') != "N/A":
         final_output['affiliate_url'] = extracted.get('url_des_produkts')
    else:
        final_output['affiliate_url'] = ai_input.get("product_url", "N/A")

    # Titel Mapping
    if extracted.get('produkt_titel') != "N/A":    
        final_output['title'] = extracted.get('produkt_titel')   
    else:
        final_output['title'] = ai_input.get('product_title', 'N/A')
        
    # PRODUKT-ID: AI-ID > Fallback
    extracted_product_id = extracted.get('produkt_id') 
    if extracted_product_id and extracted_product_id != 'N/A':
        final_output['product_id'] = extracted_product_id
    else:
        final_output['product_id'] = 'N/A'   
    
    # ----------------------------- 2. PRICE & DISCOUNT MAPPING --------------------------------------
    final_output['price'] = extracted.get('akt_preis', 'N/A')
    final_output['brand'] = extracted.get('marke', 'N/A')
    final_output['original_price'] = extracted.get('original_preis', 'N/A')  
    final_output['discount_amount'] = extracted.get('discount_amount', 'N/A')
    final_output['discount_percent'] = extracted.get('rabatt_prozent', 'N/A')    
    
    # ----------------------------- 3. IMAGES (Kombination HTML + AI) --------------------------------------
    final_output['market'] = extracted.get('marktplatz', 'N/A')
    html_images = extracted.get('hauptprodukt_bilder', [])    
    if html_images:
        final_output['images'] = html_images   
    else:
        final_output['images'] = []    
        
    # ----------------------------- 4. RATING & COUPON & WEITERE FELDER (AI) --------------------------------------
    final_output['rating'] = {
        "value": extracted.get('bewertung_wert', 0.0),
        "counts": extracted.get('anzahl_reviews', 0)
    } 

    final_output['rabatt_text'] = extracted.get('rabatt_text', 'N/A')

    final_output['coupon'] = {
        "code": extracted.get('gutschein_code', 'N/A'),
        "code_details": extracted.get('gutschein_details', 'N/A'), 
        "more": extracted.get('rabatt_text', 'N/A')
    }    
    
    final_output['units_sold'] = extracted.get('anzahl_verkauft', 'N/A')
    final_output['seller_name'] = extracted.get('haendler_verkaeufer', 'N/A')
    final_output['availability'] = extracted.get('verfuegbarkeit', 'N/A')
    final_output['shipping_info'] = extracted.get('lieferinformation', 'N/A')

    # REEL-FELDER (für Template)
    final_output['reel_titel'] = extracted.get('reel_titel', 'N/A')
    final_output['reel_beschreibung'] = extracted.get('reel_beschreibung', 'N/A')
    final_output['reel_caption'] = extracted.get('reel_caption', 'N/A')
    final_output['voiceover_text'] = extracted.get('voiceover_text', 'N/A')

    # TEMPLATE-AUSWAHL (datengetrieben aus AI): produkt_kategorie + template_type
    # werden von template_interface.resolve_template_selection() ausgewertet.
    final_output['produkt_kategorie'] = extracted.get('produkt_kategorie', 'N/A')
    final_output['template_type'] = extracted.get('template_type', '')
    
    # TEXT FELDER
    ai_features = extracted.get('features')
    final_output['features'] = ai_features if isinstance(ai_features, list) else []        
    final_output['feature_text'] = extracted.get('feature_text')
    final_output['description'] = extracted.get('beschreibung') 

    # --- HIER IST DER KORRIGIERTE HASHTAG BLOCK ---
    # Wir prüfen 'extracted', nicht 'raw_extracted'
    # Wir schreiben in 'final_output', nicht 'data_mapped'
    if "hashtags" in extracted and isinstance(extracted["hashtags"], list):
        final_output["hashtags"] = extracted["hashtags"]
    else:
        # Fallback, falls LLM failt oder Feld leer ist
        final_output["hashtags"] = ["#angebot", "#rabatt", "#schnäppchen"]

    # --- TYPE: reel wenn Rabatt >= 30%, sonst post ---
    discount_raw = str(final_output.get("discount_percent") or "").replace("-", "").replace("%", "").replace(",", ".").strip()
    try:
        discount_val = float(discount_raw)
    except ValueError:
        discount_val = 0.0
    final_output["type"] = "reel" if discount_val >= 30.0 else "post"

    return final_output