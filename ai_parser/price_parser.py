# price_parser.py
"""
Haupt-Pipeline-Datei. 
Koordiniert die HTML-Verarbeitung (Schritt 1), 
die AI-Extraktion (Schritt 2) und die finale 
Umstrukturierung und Preis-Normalisierung (Schritt 3).

AKTUALISIERUNGEN:
1. Das finale JSON enthält jetzt das Feld "affiliate_url".
2. Die ASIN/SKU (vom LLM extrahiert) dient als Dateiname.
"""

import sys
from pathlib import Path
import json
import re 
# Importiere die Hauptfunktionen aus den Modulen
try:
    from html_processor import process_html_to_llm_input
    from ai_extractor import extract_and_save_data
except ImportError as e:
    print(f"FEHLER beim Importieren der Module: {e}", file=sys.stderr)
    sys.exit(1)


# Pfad-Import und Fallback-Pfade (wie zuvor definiert)
sys.path.append(str(Path(__file__).resolve().parent.parent))
try:
    from config import DATA_DIR, OUT_DIR, HTML_SOURCE_FILE, TEMP_LLM_INPUT_FILE
except ImportError:
    DATA_DIR = Path("data")
    OUT_DIR = DATA_DIR / "out"
    HTML_SOURCE_FILE = DATA_DIR / "input" / "raw_product.html"
    TEMP_LLM_INPUT_FILE = DATA_DIR / "temp" / "llm_input.json"
    print("WARNUNG: Konnte 'config.py' nicht laden. Verwende Standard-Pfade.")


def create_directories():
    """Erstellt alle notwendigen Verzeichnisse, falls sie nicht existieren."""
    HTML_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEMP_LLM_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"-> Verzeichnisstruktur in '{DATA_DIR}' gesichert.")

# --- FUNKTIONEN FÜR PREIS-PARSING UND TRANSFORMATION ---

def parse_price(price_str: str) -> dict:
    """Konvertiert einen Preis-String in das standardisierte Preis-Objekt."""
    if not price_str or price_str.upper() in ('N/A', 'NONE'):
        return {"raw": None, "value": None, "currency_hint": None}
    
    match = re.search(r'([\d\.,]+)\s*([€$a-zA-Z]{1,3})', price_str.strip())
    
    if match:
        raw = price_str.strip()
        value_str = match.group(1).replace('.', '').replace(',', '.') 
        currency_hint = match.group(2).replace('€', 'EUR')
        try:
            value = float(value_str)
        except ValueError:
            value = None
        return {"raw": raw, "value": value, "currency_hint": currency_hint}
    
    try:
        return {"raw": price_str.strip(), "value": float(price_str.replace(',', '.')), "currency_hint": None}
    except ValueError:
        return {"raw": price_str.strip(), "value": None, "currency_hint": None}


def transform_data_to_final_schema(ai_data: dict, metadata: dict) -> dict:
    """
    Strukturiert die extrahierten AI-Daten in das gewünschte Ziel-JSON-Format um.
    """
    
    extracted = ai_data.get('extracted_data', {})
    price_obj = parse_price(extracted.get('akt_preis', 'N/A'))
    original_price_obj = parse_price(extracted.get('uvp_preis', 'N/A'))
    
    # Rabatt-Berechnung
    discount_amount = None
    discount_percent = extracted.get('rabatt_prozent', None)
    if price_obj['value'] is not None and original_price_obj['value'] is not None:
        discount_amount = round(original_price_obj['value'] - price_obj['value'], 2)
        if original_price_obj['value'] and original_price_obj['value'] > 0:
             discount_percent_val = (discount_amount / original_price_obj['value']) * 100
             discount_percent = discount_percent if discount_percent and isinstance(discount_percent, str) and '%' in discount_percent else f"{round(discount_percent_val)}%"

    gutschein_info = extracted.get('gutschein', {})
    
    # 3. Erstellung des finalen JSON-Objekts (Ziel-Struktur)
    final_json = {
        "title": extracted.get('titel', metadata.get("product_title", "N/A")), 
        
        # NEU: Das extrahierte URL-Feld
        "affiliate_url": extracted.get('url_des_produkts', None),

        "brand": extracted.get('marke', 'N/A'),
        "asin": extracted.get('sku_asin', metadata.get("asin", "N/A")), 
        
        "price": price_obj,
        "original_price": original_price_obj,
        "discount_amount": discount_amount,
        "discount_percent": discount_percent,
        
        # Bewertung
        "rating": extracted.get('bewertung', None),
        "review_count": extracted.get('anzahl_bewertungen', None),

        # Konsolidierung Gutschein
        "gutschein": { 
            "details": extracted.get('rabatt_text', None), 
            "code": gutschein_info.get('code', None)
        },
        
        # Redundante Felder
        "coupon_text": None,
        "coupon_value": { "percent": None, "amount": None, "currency_hint": None },
        "rabatt_text": None, 

        "final_price_after_coupon": None,
        "availability": extracted.get('verfuegbarkeit', 'N/A'),
        "shipping_cost_text": None,
        "bullets": extracted.get('produkt_highlights', []), 
        
        "images": extracted.get('images', []), 
    }
    
    return final_json


# --- 4. PIPELINE-STEUERUNG (main) ---

def main():
    """Steuert den gesamten Extraktions- und Transformationsprozess."""
    create_directories()

    ai_raw_output_file = OUT_DIR / "ai_raw_output.json" 

    # 1. SCHRITT: HTML-VERARBEITUNG
    print("\n=============================================")
    print("SCHRITT 1: HTML-VERARBEITUNG")
    print("=============================================")
    
    try:
        process_html_to_llm_input(HTML_SOURCE_FILE, TEMP_LLM_INPUT_FILE)
    except Exception as e:
        print(f"PIPELINE ABGEBROCHEN: Fehler in der HTML-Verarbeitung: {e}", file=sys.stderr)
        sys.exit(1)

    # Lade die LLM-Input-Daten für Metadaten
    try:
        with open(TEMP_LLM_INPUT_FILE, 'r', encoding='utf-8') as f:
            llm_input_data = json.load(f)
    except Exception:
        print("PIPELINE ABGEBROCHEN: Konnte LLM-Input-Daten nicht laden.", file=sys.stderr)
        sys.exit(1)
        
    initial_asin = llm_input_data.get("asin", "unbekannt")

    # 2. SCHRITT: AI-Extraktion
    print("\n=============================================")
    print("SCHRITT 2: AI-EXTRAKTION (Roh-Daten)")
    print("=============================================")

    try:
        extract_and_save_data(TEMP_LLM_INPUT_FILE, ai_raw_output_file)
    except Exception as e:
        print(f"PIPELINE ABGEBROCHEN: Fehler in der AI-Extraktion: {e}", file=sys.stderr)
        sys.exit(1)

    # 3. SCHRITT: DATEN-TRANSFORMATION
    print("\n=============================================")
    print("SCHRITT 3: DATEN-TRANSFORMATION")
    print("=============================================")
    
    try:
        with open(ai_raw_output_file, 'r', encoding='utf-8') as f:
            ai_data = json.load(f)
    except Exception:
        print("PIPELINE ABGEBROCHEN: Konnte AI-Roh-Output nicht laden.", file=sys.stderr)
        sys.exit(1)
    
    # Ermittle die definitive ID aus dem LLM-Output
    llm_extracted_id = ai_data.get('extracted_data', {}).get('sku_asin')
    
    # Die finale ID ist die LLM-ID, ansonsten der Fallback aus Schritt 1
    final_id = llm_extracted_id if llm_extracted_id and llm_extracted_id.upper() != 'N/A' else initial_asin
    
    metadata = {
        "asin": initial_asin, 
        "product_title": llm_input_data.get("product_title", "N/A"),
    }
    
    transformed_data = transform_data_to_final_schema(ai_data, metadata)
    
    # Der finale Dateipfad verwendet die definitive ID
    final_output_file = OUT_DIR / f"{final_id}.json"
    
    with open(final_output_file, 'w', encoding='utf-8') as f:
        json.dump(transformed_data, f, indent=4, ensure_ascii=False)
        
    print("<- Transformation abgeschlossen.")
    print(f"-> FINALES JSON gespeichert unter: {final_output_file.resolve()}")


if __name__ == '__main__':
    main()