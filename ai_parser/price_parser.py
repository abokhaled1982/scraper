# price_parser.py

import sys
import json
from pathlib import Path

# config aus Parent-Ordner laden (direkter Skriptstart möglich)
sys.path.append(str(Path(__file__).resolve().parent.parent))
# Importiere die benötigten Pfad-Variablen aus config.py
from config import DATA_DIR, OUT_DIR, HTML_SOURCE_FILE, TEMP_LLM_INPUT_FILE

# Importiere die Hauptfunktionen aus den Modulen
try:
    from html_processor import process_html_to_llm_input
    from ai_extractor import extract_and_save_data
except ImportError as e:
    print(f"FEHLER beim Importieren der Module: {e}", file=sys.stderr)
    print("Stellen Sie sicher, dass html_processor.py und ai_extractor.py im selben Verzeichnis liegen.", file=sys.stderr)
    sys.exit(1)


def create_directories():
    """Erstellt alle notwendigen Verzeichnisse, falls sie nicht existieren."""
    # Sicherstellen, dass alle relevanten Verzeichnisse existieren
    HTML_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEMP_LLM_INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Erstellt das Output-Verzeichnis (data/out)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"-> Verzeichnisstruktur in '{DATA_DIR}' gesichert.")

# NEUE FUNKTION: Hilfsfunktion zum Parsen von Preis-Strings
def parse_price_string(price_str: str):
    """Konvertiert einen Preis-String (z.B. '399,99 €') in eine strukturierte Preis-Map."""
    if not price_str or price_str == 'N/A':
        return {"raw": None, "value": None, "currency_hint": None}

    # Bereinige den String: Komma durch Punkt ersetzen, Tausenderpunkte entfernen, Ziffern und Komma/Punkt behalten
    cleaned_str = price_str.replace('.', '').replace(',', '.')

    # Extrahiere Währung und numerischen Teil
    import re
    match = re.search(r'([0-9\.]+)', cleaned_str)
    currency_match = re.search(r'([€$£])', price_str) # Einfache Währungserkennung

    value = float(match.group(1)) if match else None
    currency_hint = currency_match.group(1) if currency_match else None

    return {
        "raw": price_str,
        "value": value,
        "currency_hint": currency_hint
    }

# NEUE FUNKTION: Daten-Transformation und Mapping
def map_ai_output_to_target_format(ai_output: dict, target_template: dict) -> dict:
    """
    Mappt die extrahierten Daten aus dem AI-Output in das Ziel-JSON-Format,
    wobei alle Felder auf Englisch benannt und alle Cleanups durchgeführt werden,
    inklusive der neuen Felder für Verkauf, Händler und Lieferung.
    """
    extracted = ai_output.get("extracted_data", {})
    final_output = target_template.copy()
    clean_text = ai_output.get("clean_text") # Quelle für allgemeine Texte
    
    # --- 1. CORE PRODUCT IDENTIFIER (KORRIGIERT: ASIN/SKU-Logik und Umbenennung zu product_id) ---
    final_output['title'] = ai_output.get('product_title', 'N/A')
    final_output['affiliate_url'] = target_template.get('url_des_produkts')
    
    # NEU: Implementierung der Produkt-ID Logik (ASIN/SKU)
    extracted_product_id = extracted.get('produkt_id') 
    template_asin = target_template.get('asin') # Verwende ASIN aus Template als Fallback

    if extracted_product_id and extracted_product_id != 'N/A':
        final_output['product_id'] = extracted_product_id
    elif template_asin:
        final_output['product_id'] = template_asin
    else:
        final_output['product_id'] = 'N/A'
    
    final_output.pop('asin', None) # Entferne das alte 'asin' Feld
    
    final_output['brand'] = extracted.get('marke', target_template.get('brand', 'N/A'))
    
    # --- 2. PRICE & DISCOUNT MAPPING (Unverändert) ---
    price_info = parse_price_string(extracted.get('akt_preis'))
    original_price_info = parse_price_string(extracted.get('uvp_preis'))
    
    final_output['price'] = price_info
    final_output['original_price'] = original_price_info
    
    current_value = price_info.get('value')
    original_value = original_price_info.get('value')
    if current_value is not None and original_value is not None and original_value > current_value:
        final_output['discount_amount'] = round(original_value - current_value, 2)
    else:
        final_output['discount_amount'] = target_template.get('discount_amount')
        
    final_output['discount_percent'] = extracted.get('rabatt_prozent', target_template.get('discount_percent', 'N/A'))
    
    # --- 3. IMAGES (KORRIGIERT: Mapping direkt auf Array von URLs) ---
    images_from_ai = extracted.get('hauptprodukt_bilder', [])
  
    
  
    # ÄNDERUNG: Mappen auf 'images' anstelle von 'main_product_images'
    final_output['images'] = images_from_ai
    
    # Entferne die alten Felder, falls sie im Template waren
    final_output.pop('main_product_images', None)
    
    # --- 4. RATING MAPPING (KORRIGIERT) ---
    final_output['rating'] = {
        "value": extracted.get('bewertung_wert', target_template.get('rating', 'N/A')),
        "counts": extracted.get('anzahl_reviews', target_template.get('review_count', 'N/A'))
    }
    final_output.pop('review_count', None)
    
    # --- 5. RABAT MAPPING (KORRIGIERT) ---
    gutschein_template = target_template.get('gutschein', {"details": "N/A", "code": "N/A"})
    
    final_output['coboun'] = {
        "code": extracted.get('gutschein_code', gutschein_template.get('code', 'N/A')),
        "code_details": extracted.get('gutschein_details', gutschein_template.get('details', 'N/A')), 
        "more": extracted.get('rabatt_text', target_template.get('rabatt_details', 'N/A'))
    }
    
    final_output.pop('gutschein', None)       
    final_output['rabatt_details'] = None     

    # --- 6. WEITERE PRODUKT- UND LIEFERINFORMATIONEN (NEU) ---
    
    # NEU: Mappen der hinzugefügten Felder auf Englisch
    final_output['units_sold'] = extracted.get('anzahl_verkauft', target_template.get('units_sold', 'N/A'))
    final_output['seller_name'] = extracted.get('haendler_verkaeufer', target_template.get('seller_name', 'N/A'))
    final_output['availability'] = extracted.get('verfuegbarkeit', target_template.get('availability', 'N/A'))
    final_output['shipping_info'] = extracted.get('lieferinformation', target_template.get('shipping_info', 'N/A'))

    # --- 7. STANDARD FELDER (Unverändert) ---
    
    # FEATURES
    ai_features = extracted.get('features')
    if isinstance(ai_features, list) and ai_features:
        final_output['features'] = ai_features
    else:
        final_output['features'] = target_template.get('features', [])
        
    # TEXT FIELDS
    final_output['feature_text'] = extracted.get('feature_text', target_template.get('feature_text'))
    final_output['description'] = extracted.get('beschreibung', target_template.get('description')) 
    
    if final_output['description_text'] is None and clean_text:
        final_output['description_text'] = clean_text
   
  
    return final_output

def main():
    final_output_file = OUT_DIR / "final_mapped_output.json" # Neuen Dateinamen verwenden
    temp_ai_output_file = OUT_DIR / "output.json" # Dateiname vom ai_extractor

    # create_directories()

    # 1. SCHRITT: HTML-Verarbeitung
    # ... (Dieser Teil bleibt unverändert) ...
    print("\n=============================================")
    print("SCHRITT 1: HTML-VERARBEITUNG")
    print("=============================================")
    
    try:
        # Übergibt die fest definierten Pfade an den Prozessor
        process_html_to_llm_input(HTML_SOURCE_FILE, TEMP_LLM_INPUT_FILE)
    except FileNotFoundError as e:
        print(f"PIPELINE ABGEBROCHEN: {e}", file=sys.stderr)
        print("Bitte legen Sie die HTML-Datei unter dem angegebenen Pfad ab.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"PIPELINE ABGEBROCHEN: Fehler in der HTML-Verarbeitung: {e}", file=sys.stderr)
        sys.exit(1)


    # 2. SCHRITT: AI-Extraktion
    # ... (Dieser Teil bleibt unverändert) ...
    print("\n=============================================")
    print("SCHRITT 2: AI-EXTRAKTION")
    print("=============================================")

    try:
        # KORRIGIERT: Übergibt den korrekten Dateipfad
        extract_and_save_data(TEMP_LLM_INPUT_FILE, temp_ai_output_file)
    except FileNotFoundError as e:
        # Dies sollte nicht passieren, wenn Schritt 1 erfolgreich war.
        print(f"PIPELINE ABGEBROCHEN: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"PIPELINE ABGEBROCHEN: Fehler bei der AI-Extraktion: {e}", file=sys.stderr)
        sys.exit(1)

    # 3. SCHRITT: Daten-Mapping und Transformation
    print("\n=============================================")
    print("SCHRITT 3: DATEN-MAPPING ZUM ZIELFORMAT")
    print("=============================================")
    
    try:
        # 3a. Lade das Ergebnis des AI-Extrakters
        print(f"-> Lade AI-Output von: {temp_ai_output_file.name}")
        with open(temp_ai_output_file, 'r', encoding='utf-8') as f:
            ai_output_data = json.load(f)

              
        # KORRIGIERT: Template angepasst auf die neuen Zielfelder
        target_template_content = {
            "title": "roborock Qrevo Serie Saugroboter mit Wischfunktion, 8000Pa Saugkraft(verbessert von Qrevo S), Anti-Verfilzungs-Seitenbürste, Hindernisvermeidung, LiDAR-Navigation, All-in-One Dock,Schwarz(QV 35A Set)",
            "affiliate_url": "https://www.amazon.de/roborock-Anti-Verfilzungs-Seitenb%C3%BCrste-Hindernisvermeidung-LiDAR-Navigation-35A/dp/B0DSLBN5FS",
            "brand": "roborock",
            "product_id": "N/A", # NEU: Generisches Produkt-ID Feld
            "asin": "B0DSLBN5FS", # Beibehalten als Fallback/Quelle
            "price": {"raw": None, "value": None, "currency_hint": None},
            "original_price": {"raw": None, "value": None, "currency_hint": None},
            "discount_amount": None,
            "discount_percent": "N/A",
            "rating": "N/A",
            "review_count": "N/A",
            "gutschein": {"details": "N/A", "code": "N/A"},
            "coupon_text": None,
            "coupon_value": {"percent": None, "amount": None, "currency_hint": None},
            "rabatt_details": "N/A",
            "images": [], # KORRIGIERT: Umbenennung auf 'images'
            "main_product_images": [], # Beibehalten, falls es später entfernt wird
            "features": [],
            "feature_text": None,
            "description": None,
            "description_text": None
        }

        # 3c. Mappe die Daten
        mapped_data = map_ai_output_to_target_format(ai_output_data, target_template_content)
        
        # HINWEIS: Das 'asin' Feld wird im Mapping entfernt.
        mapped_data.pop('asin', None)
        mapped_data.pop('main_product_images', None)

        # 3d. Speichere das Endergebnis
        print(f"-> Speichere gemapptes Ergebnis unter: {final_output_file.name}")
        with open(final_output_file, 'w', encoding='utf-8') as f:
            json.dump(mapped_data, f, indent=4, ensure_ascii=False)
        print("<- PIPELINE ERFOLGREICH ABGESCHLOSSEN.")

    except FileNotFoundError:
        print(f"FEHLER: Die Datei {temp_ai_output_file.name} wurde nicht gefunden. Schritt 2 fehlgeschlagen.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"PIPELINE ABGEBROCHEN: Fehler im Daten-Mapping: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()