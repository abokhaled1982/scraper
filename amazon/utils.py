# data_mapper.py

from __future__ import annotations
import hashlib, json, time, shutil
from pathlib import Path
from typing import Optional, Dict, Any
import os 
import re 
import sys

# ----------------------------- ALLGEMEINE HILFSFUNKTIONEN (DATEI/REGISTRY) --------------------------------------

def _read_text(fp: Path) -> str:
    """Liest den Dateiinhalt als Text."""
    try:
        return fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return fp.read_bytes().decode("utf-8", errors="ignore")

def _sha1_bytes(b: bytes) -> str:
    """Berechnet den SHA1-Hash der übergebenen Bytes."""
    return hashlib.sha1(b).hexdigest() 

def _sha1_file(fp: Path) -> str:
    """Berechnet den SHA1-Hash einer Datei."""
    try:
        return _sha1_bytes(fp.read_bytes())
    except Exception:
        return _sha1_bytes(_read_text(fp).encode("utf-8", errors="ignore"))
    
def move_to_failed(fp: Path, reason: str, FAILED_DIR: Path) -> None:
    """Verschiebt die Quelldatei und den Fehlergrund in den Fehler-Ordner."""
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    target = FAILED_DIR / f"{fp.name}.txt"
    try:
        with target.open("w", encoding="utf-8") as f:
            f.write(f"Source: {fp.name}\nTimestamp: {time.time()}\nReason:\n{reason}")
        shutil.move(str(fp), str(FAILED_DIR / fp.name))
    except Exception as e:
        print(f"WARNUNG: Konnte Datei {fp.name} nicht nach FAILED_DIR verschieben: {e}", file=sys.stderr)

def load_registry(REGISTRY_PATH: Path) -> Dict:
    """Lädt das Verarbeitungs-Registry."""
    try:
        if REGISTRY_PATH.exists():
            with REGISTRY_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"hashes": {}, "asins": {}}

def save_registry(reg: Dict, REGISTRY_PATH: Path) -> None:
    """Speichert das Verarbeitungs-Registry (atomar)."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)

def write_summary_append(data: Dict, SUMMARY_PATH: Path) -> None:
    """Fügt ein Ergebnis dem Summary-Log hinzu."""
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SUMMARY_PATH.open("a", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.write("\n")

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
    if price_str_cleaned.count(',') > 1 and '.' not in price_str_cleaned:
        # Beispiel: 1.234,56 -> 1234.56
        cleaned_str = price_str_cleaned.replace('.', '').replace(',', '.')
    elif price_str_cleaned.count(',') == 1 and price_str_cleaned.count('.') == 0:
         # Beispiel: 399,99 € -> 399.99
         cleaned_str = price_str_cleaned.replace(',', '.')
    else:
         # Standardfall (z.B. 399.99 € oder 1,234.56)
         cleaned_str = price_str_cleaned.replace(',', '') 

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
    "coupon": {"code": "N/A", "code_details": "N/A", "more": "N/A"},
    "images": [], "features": [], 
    "feature_text": None, "description": None,
    "units_sold": "N/A", "seller_name": "N/A", "availability": "N/A", "shipping_info": "N/A",
}


def map_ai_output_to_target_format(
    ai_output: Dict[str, Any], 
    html_core_data: Dict[str, Any], 
    parse_price_fn: callable
) -> Dict[str, Any]:
    """
    Mappt die extrahierten Daten aus dem AI-Output und den HTML-Kerndaten 
    in das Ziel-JSON-Format (Englische Felder, bereinigt).
    
    Args:
        ai_output: Das rohe Output-Dictionary vom LLM/AI-Extractor.
        html_core_data: Das Resultat von html_parser.extract_core_html_data.
        parse_price_fn: Die utils.parse_price_string Funktion.
    """
    extracted = ai_output.get("extracted_data", {})
    final_output = TARGET_SCHEMA_TEMPLATE.copy()
    
    # ----------------------------- 1. CORE PRODUCT IDENTIFIER (Kombination HTML + AI) --------------------------------------
    
    # TITEL: Priorität: HTML-geparster Titel > AI-geparster Titel > Input-Titel
    html_title = html_core_data.get('title_html', 'N/A')
    extracted_title = extracted.get('produkt_titel')
    input_title = ai_output.get('product_title', 'N/A') 
    
    
    if extracted_title != 'N/A':
        final_output['title'] = extracted_title
    elif html_title:
        final_output['title'] = html_title
    else:
        final_output['title'] = input_title

    # AFFILIATE URL: Priorität: HTML-URL (sauber und normalisiert) > AI-URL > Fallback
    html_url = html_core_data.get('affiliate_url', 'N/A')
    extracted_url = extracted.get('url_des_produkts') 

    final_output['affiliate_url'] = html_url
    if final_output['affiliate_url'] == 'N/A' and extracted_url and extracted_url != 'N/A':
         final_output['affiliate_url'] = extracted_url
         
    # PRODUKT-ID: AI-ID > Fallback
    extracted_product_id = extracted.get('produkt_id') 

    if extracted_product_id and extracted_product_id != 'N/A':
        final_output['product_id'] = extracted_product_id
    else:
        final_output['product_id'] = 'N/A'
    
   
    
    # ----------------------------- 2. PRICE & DISCOUNT MAPPING --------------------------------------
    price_info = parse_price_fn(extracted.get('akt_preis'))
    original_price_info = parse_price_fn(extracted.get('uvp_preis'))
    
    final_output['price'] = price_info
    final_output['brand'] = extracted.get('marke', 'N/A')
    final_output['original_price'] = original_price_info
    
    current_value = price_info.get('value')
    original_value = original_price_info.get('value')
    final_output['market'] = extracted.get('marktplatz', 'N/A')
    final_output['discount_amount'] = None
    if current_value is not None and original_value is not None and original_value > current_value:
        final_output['discount_amount'] = round(original_value - current_value, 2)
        
    final_output['discount_percent'] = extracted.get('rabatt_prozent', 'N/A')
    
    # ----------------------------- 3. IMAGES (Kombination HTML + AI) --------------------------------------
    # Priorität: HTML-Bilder (deterministisch) > AI-Bilder > Fallback
   
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
    
    final_output['coupon'] = {
        "code": extracted.get('gutschein_code', 'N/A'),
        "code_details": extracted.get('gutschein_details', 'N/A'), 
        "more": extracted.get('rabatt_text', 'N/A')
    }
    
    final_output['units_sold'] = extracted.get('anzahl_verkauft', 'N/A')
    final_output['seller_name'] = extracted.get('haendler_verkaeufer', 'N/A')
    final_output['availability'] = extracted.get('verfuegbarkeit', 'N/A')
    final_output['shipping_info'] = extracted.get('lieferinformation', 'N/A')

    # TEXT FELDER
    ai_features = extracted.get('features')
    final_output['features'] = ai_features if isinstance(ai_features, list) else []
        
    final_output['feature_text'] = extracted.get('feature_text')
    final_output['description'] = extracted.get('beschreibung') 
    
    return final_output