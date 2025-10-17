#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Daemon-Worker, der einzelne HTML-Dateien verarbeitet und die Ergebnisse schreibt.
ROUTING: Unterscheidet zwischen Amazon- und Nicht-Amazon-HTML und nutzt 
den dedizierten Amazon-Parser ODER die AI-Pipeline (html_processor + ai_extractor).

Die Logik zur Preisbereinigung und das detaillierte AI-Output-Mapping 
wurde aus price_parser.py integriert.

INPUT: PRODUCKT_DIR (HTML-Dateien)
OUTPUT: OUT_DIR (JSON-Dateien)
"""

from __future__ import annotations
import hashlib, json, time, traceback, shutil
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List
import sys
import uuid # Für die Generierung zufälliger Produkt-IDs für AI-geparste Inhalte
import os # Für os.path.getmtime in _pick_oldest_html
import re # Für parse_price_string

# Projekt-Config
# Sicherstellen, dass 'config.py' im Parent-Verzeichnis verfügbar ist.
sys.path.append(str(Path(__file__).resolve().parent.parent))
# Annahme: config.py enthält PRODUCKT_DIR, OUT_DIR, FAILED_DIR, INTERVAL_SECS, REGISTRY_PATH, SUMMARY_PATH
from config import PRODUCKT_DIR, OUT_DIR, FAILED_DIR, INTERVAL_SECS, REGISTRY_PATH, SUMMARY_PATH

# Importiere die benötigten Parser-Module
# parser.py (für Amazon)
from parser import AmazonProductParser, to_b0_schema 
# html_processor.py (für AI-Pipeline: HTML-Vorbereitung)
from ai_parser.html_processor import process_html_to_llm_input 
# ai_extractor.py (für AI-Pipeline: LLM-Extraktion)
from ai_parser.ai_extractor import extract_and_save_data 


# ----------------------------- HILFSFUNKTIONEN (Allgemein) --------------------------------------

def _read_text(fp: Path) -> str:
    # Lese Dateiinhalt
    try:
        return fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return fp.read_bytes().decode("utf-8", errors="ignore")

def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest() 

def _sha1_file(fp: Path) -> str:
    try:
        return _sha1_bytes(fp.read_bytes())
    except Exception:
        return _sha1_bytes(_read_text(fp).encode("utf-8", errors="ignore"))

def _move_to_failed(fp: Path, reason: str) -> None:
    # Verschiebt die Quelldatei in den Fehler-Ordner
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    target = FAILED_DIR / f"{fp.name}.txt"
    try:
        with target.open("w", encoding="utf-8") as f:
            f.write(f"Source: {fp.name}\nTimestamp: {time.time()}\nReason:\n{reason}")
        shutil.move(str(fp), str(FAILED_DIR / fp.name))
    except Exception as e:
        print(f"WARNUNG: Konnte Datei {fp.name} nicht nach FAILED_DIR verschieben: {e}", file=sys.stderr)

def _load_registry() -> Dict:
    try:
        if REGISTRY_PATH.exists():
            with REGISTRY_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"hashes": {}, "asins": {}}

def _save_registry(reg: Dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)

def _write_summary_append(data: Dict) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SUMMARY_PATH.open("a", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.write("\n")

def _pick_oldest_html() -> Optional[Path]:
    # Wählt die älteste HTML-Datei aus dem PRODUCKT_DIR
    files = sorted(PRODUCKT_DIR.glob('*.html'), key=os.path.getmtime)
    if files:
        return files[0]
    return None

# ----------------------------- HILFSFUNKTIONEN (AI-Parsing/Mapping von price_parser.py) --------------------------------------

def parse_price_string(price_str: str) -> Dict[str, Any]:
    """Konvertiert einen Preis-String (z.B. '399,99 €') in eine strukturierte Preis-Map."""
    if not price_str or price_str in ('N/A', '0', '0.0'):
        return {"raw": None, "value": None, "currency_hint": None}

    # Bereinige den String: Komma durch Punkt ersetzen, Tausenderpunkte entfernen, Ziffern und Komma/Punkt behalten
    # Regex: Entferne alles, was keine Ziffer, Komma, Punkt, Leerzeichen oder Währungszeichen ist.
    price_str_cleaned = re.sub(r'[^\d,\.€$£]', '', price_str).strip()
    
    # Versuche, Komma als Dezimaltrennzeichen zu erkennen und zu korrigieren
    # Wenn mehr als ein Komma vorhanden ist, behandle es als Tausenderseparator, ansonsten als Dezimaltrennzeichen
    if price_str_cleaned.count(',') > 1 and '.' not in price_str_cleaned:
        # Beispiel: 1.234,56 -> 1234.56
        cleaned_str = price_str_cleaned.replace('.', '').replace(',', '.')
    elif price_str_cleaned.count(',') == 1 and price_str_cleaned.count('.') == 0:
         # Beispiel: 399,99 € -> 399.99
         cleaned_str = price_str_cleaned.replace(',', '.')
    else:
         # Standardfall (z.B. 399.99 € oder bereits sauber)
         cleaned_str = price_str_cleaned.replace(',', '') # Nur Tausender-Komma entfernen, falls vorhanden

    # Extrahiere Währung und numerischen Teil
    match = re.search(r'([0-9\.]+)', cleaned_str)
    currency_match = re.search(r'([€$£])', price_str) # Einfache Währungserkennung

    try:
        value = float(match.group(1)) if match else None
    except ValueError:
        value = None # Kann passieren, wenn z.B. nur "." übrig bleibt
        
    currency_hint = currency_match.group(1) if currency_match else None

    return {
        "raw": price_str,
        "value": value,
        "currency_hint": currency_hint
    }


def map_ai_output_to_target_format(ai_output: dict, target_template: dict) -> dict:
    """
    Mappt die extrahierten Daten aus dem AI-Output in das Ziel-JSON-Format,
    wobei alle Felder auf Englisch benannt und alle Cleanups durchgeführt werden.
    """
    extracted = ai_output.get("extracted_data", {})
    final_output = target_template.copy()
    
    # --- 1. CORE PRODUCT IDENTIFIER ---
    
    # Mapping des Titels: LLM-Titel > Template-Titel > Input-Titel
    extracted_title = extracted.get('produkt_titel')
    input_title = ai_output.get('product_title', 'N/A')
    
    final_output['title'] = extracted_title if extracted_title and extracted_title != 'N/A' else input_title

    # Mapping der Affiliate URL: LLM-URL > Template-URL
    extracted_url = extracted.get('url_des_produkts')
    final_output['affiliate_url'] = extracted_url if extracted_url and extracted_url != 'N/A' else 'N/A'
    
    # NEU: Implementierung der Produkt-ID Logik (ASIN/SKU)
    extracted_product_id = extracted.get('produkt_id') 
    template_asin = target_template.get('asin') # Verwende ASIN aus Template als Fallback (wird später entfernt)

    if extracted_product_id and extracted_product_id != 'N/A':
        final_output['product_id'] = extracted_product_id
    elif template_asin:
        final_output['product_id'] = template_asin
    else:
        final_output['product_id'] = 'N/A'
    
    final_output['brand'] = extracted.get('marke', 'N/A')
    
    # --- 2. PRICE & DISCOUNT MAPPING ---
    price_info = parse_price_string(extracted.get('akt_preis'))
    original_price_info = parse_price_string(extracted.get('uvp_preis'))
    
    final_output['price'] = price_info
    final_output['original_price'] = original_price_info
    
    current_value = price_info.get('value')
    original_value = original_price_info.get('value')
    
    final_output['discount_amount'] = None
    if current_value is not None and original_value is not None and original_value > current_value:
        final_output['discount_amount'] = round(original_value - current_value, 2)
        
    final_output['discount_percent'] = extracted.get('rabatt_prozent', 'N/A')
    
    # --- 3. IMAGES ---
    # hauptprodukt_bilder sollte bereits eine Liste von Strings sein (siehe ai_extractor.py Korrektur)
    images_from_ai = extracted.get('hauptprodukt_bilder', [])
    final_output['images'] = images_from_ai if isinstance(images_from_ai, list) else [] 
    
    # --- 4. RATING MAPPING ---
    final_output['rating'] = {
        "value": extracted.get('bewertung_wert', 0.0),
        "counts": extracted.get('anzahl_reviews', 0)
    }
    
    # --- 5. COUPON/RABATT MAPPING ---
    final_output['coupon'] = {
        "code": extracted.get('gutschein_code', 'N/A'),
        "code_details": extracted.get('gutschein_details', 'N/A'), 
        "more": extracted.get('rabatt_text', 'N/A')
    }
    
    # --- 6. WEITERE PRODUKT- UND LIEFERINFORMATIONEN ---
    final_output['units_sold'] = extracted.get('anzahl_verkauft', 'N/A')
    final_output['seller_name'] = extracted.get('haendler_verkaeufer', 'N/A')
    final_output['availability'] = extracted.get('verfuegbarkeit', 'N/A')
    final_output['shipping_info'] = extracted.get('lieferinformation', 'N/A')

    # --- 7. TEXT FELDER ---
    # FEATURES
    ai_features = extracted.get('features')
    final_output['features'] = ai_features if isinstance(ai_features, list) else []
        
    final_output['feature_text'] = extracted.get('feature_text')
    final_output['description'] = extracted.get('beschreibung') 
    
    # Entferne veraltete/redundante Felder
    final_output.pop('review_count', None)
    final_output.pop('description_text', None)
    final_output.pop('coupon_text', None)
    final_output.pop('coupon_value', None)
    final_output.pop('gutschein', None)
    final_output.pop('rabatt_details', None)
    
    return final_output

# ----------------------------- ROUTING & AI-PARSING LOGIK (KORRIGIERT) --------------------------------------

def is_amazon_html(html_content: str) -> bool:
    """Entscheidet anhand von Amazon-spezifischen Merkmalen, ob es eine Produktseite ist."""
    # Häufige Amazon-spezifische IDs/Attribute.
    if any(tag in html_content for tag in [
        'id="productTitle"', 
        'id="ASIN"', 
        'data-asin=',
        'class="a-section a-spacing-none"',
        'id="twisterDiv"'
        ]):
        return True
    return False

def process_with_ai_parser(fp: Path, out_dir: Path) -> Tuple[bool, str, Dict]:
    """
    Orchestriert die AI-Pipeline für eine Nicht-Amazon HTML-Datei.
    Führt die HTML-Vorbereitung, LLM-Extraktion und das Mappen zum Zielformat durch.
    """
    
    temp_llm_input_file = fp.with_name(f"{fp.stem}.llm_input.json")
    temp_ai_output_file = fp.with_name(f"{fp.stem}.llm_output.json")

    # Temporäre Dateien MÜSSEN im Fehlerfall gelöscht werden
    def cleanup_temp_files():
        temp_llm_input_file.unlink(missing_ok=True)
        temp_ai_output_file.unlink(missing_ok=True)
    
    try:
        # 1. HTML parsen und für das LLM vorbereiten (html_processor.py)
        print(f"[AI-PARSER] Starte HTML-Vorbereitung für {fp.name}...")
        process_html_to_llm_input(fp, temp_llm_input_file) 
        
        # 2. LLM-Extraktion starten (ai_extractor.py)
        print(f"[AI-PARSER] Starte LLM-Extraktion...")
        extract_and_save_data(temp_llm_input_file, temp_ai_output_file)
        
        # 3. DATEN-MAPPING ZUM ZIELFORMAT (Logik aus price_parser.py main/map_ai_output_to_target_format)
        print(f"[AI-PARSER] Starte Mapping und Speichern...")

        # 3a. Lade das Ergebnis des AI-Extrakters
        with open(temp_ai_output_file, 'r', encoding='utf-8') as f:
            ai_output_data = json.load(f)

        # 3b. Definiere ein Minimal-Template für das Mapping
        target_template_content = {
            "title": "N/A", "affiliate_url": "N/A", "brand": "N/A", "product_id": "N/A",
            "asin": "AI_FALLBACK", # Platzhalter für den Dateinamen-Fallback
            "price": {"raw": None, "value": None, "currency_hint": None},
            "original_price": {"raw": None, "value": None, "currency_hint": None},
            "discount_amount": None, "discount_percent": "N/A", "rating": {"value": 0.0, "counts": 0},
            "coupon": {"code": "N/A", "code_details": "N/A", "more": "N/A"},
            "images": [], "features": [], "feature_text": None, "description": None,
            "units_sold": "N/A", "seller_name": "N/A", "availability": "N/A", "shipping_info": "N/A",
        }
        
        # 3c. Mappe die Daten (Hier wird 'product_id' gesetzt)
        data_mapped = map_ai_output_to_target_format(ai_output_data, target_template_content)
        
        # Füge Daemon-spezifische Meta-Informationen hinzu
        data_mapped["source_file"] = str(fp.name)
        data_mapped["page_hash"] = _sha1_file(fp)

        # 3d. BESTIMME DEN NAMEN DER AUSGABEDATEI (vom Mapping)
        product_identifier = data_mapped.get('product_id', data_mapped.get('asin', 'N/A'))
        if product_identifier in ('N/A', None, 'AI_FALLBACK'):
            random_id = str(uuid.uuid4()).replace('-', '') 
            product_identifier = f"random_{random_id[:12]}" 
            
        final_output_file = out_dir / f"{product_identifier}.json"

        # VOR dem Speichern: Entferne den temporären ASIN-Fallback
        data_mapped.pop('asin', None)
        
        # 3e. Speichere das Endergebnis (atomar)
        tmp = final_output_file.with_suffix(".tmp")
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(data_mapped, f, indent=4, ensure_ascii=False)
        tmp.replace(final_output_file)

        # 4. Cleanup und Rückmeldung
        cleanup_temp_files()
        
        return True, f"AI OK -> {final_output_file.name}", data_mapped

    except Exception as e:
        # Cleanup der temporären Dateien im Fehlerfall
        cleanup_temp_files()
        # Fehler wird von process_one abgefangen und die Datei verschoben
        raise Exception(f"AI-Pipeline/Mapping Fehler: {e}")


# ----------------------------- HAUPTFUNKTION (PROCESS_ONE) --------------------------------------

def process_one(fp: Path, reg: Dict) -> Tuple[bool, str]:
    """
    Verarbeitet eine einzelne HTML-Datei: 
    ROUTING: Amazon-Parser oder General-AI-Parser.
    """
    try:
        # 1. Lese Dateiinhalt
        content = _read_text(fp)
        
        # 2. ROUTING-Entscheidung (Amazon vs. AI)
        if is_amazon_html(content):
            # --- AMAZON-PFAD ---
            print(f"[ROUTER] {fp.name} -> Amazon HTML erkannt. Starte Amazon-Parser.")
            
            parser = AmazonProductParser(content)
            product = parser.parse()
            
            if not product.asin or not product.title:
                raise ValueError("Amazon-Parser lieferte keine Haupt-Produktdaten (ASIN/Titel) oder es ist eine unvollständige Seite.")

            # Speichern im BO-Schema
            page_hash = _sha1_bytes(content.encode('utf-8'))
            asin = product.asin
            out_path = OUT_DIR / f"{asin}_{page_hash[:10]}.json" 

            data_mapped = to_b0_schema(product)
            
            tmp = out_path.with_suffix(".tmp")
            with tmp.open('w', encoding='utf-8') as f:
                json.dump(data_mapped, f, ensure_ascii=False, indent=2)
            tmp.replace(out_path) 
            
            _write_summary_append(data_mapped)

            # Registry aktualisieren
            reg["hashes"][page_hash] = out_path.name
            if asin:
                reg["asins"][asin] = out_path.name
            _save_registry(reg)

            # Quelle löschen (verarbeitet)
            fp.unlink(missing_ok=True)
            return True, f"AMAZON OK -> {out_path.name}"
            
        else:
            # --- AI-PARSER-PFAD ---
            print(f"[ROUTER] {fp.name} -> Nicht-Amazon HTML erkannt. Starte AI-Pipeline.")
            # Nutzt die korrigierte Funktion mit integriertem Mapping
            ok, msg, data_mapped = process_with_ai_parser(fp, OUT_DIR)
            
            if ok:
                 _write_summary_append(data_mapped)
                 fp.unlink(missing_ok=True) # Quelle löschen (verarbeitet)
                 return True, msg
            
            return False, msg # Sollte nicht erreicht werden, da Fehler geworfen werden
            
    except Exception as e:
        tb = traceback.format_exc()
        # Verschiebe die Quelldatei und den Fehlergrund in den FAILED_DIR
        _move_to_failed(fp, f"ROUTER/VERARBEITUNGSFEHLER: {e}\n\n{tb}")
        return False, f"ROUTER ERR {fp.name}: {e}"


# ----------------------------- DAEMON LOOP --------------------------------------

def daemon_loop(interval: int = INTERVAL_SECS) -> None:
    """
    Watch-Loop: zieht regelmäßig die älteste HTML-Datei und verarbeitet sie.
    """
    print(f"[product-parser] watching {PRODUCKT_DIR} every {interval}s -> {OUT_DIR}")
    reg = _load_registry()
    while True:
        try:
            fp = _pick_oldest_html()
            if not fp:
                time.sleep(interval)
                continue
            ok, msg = process_one(fp, reg)
            print(f"[product-parser] {msg}")
            time.sleep(1) # Kurze Pause nach erfolgreicher Verarbeitung
        except Exception as e:
            print(f"[product-parser] SCHWERWIEGENDER FEHLER IM DAEMON: {e}", file=sys.stderr)
            time.sleep(interval)


if __name__ == '__main__':
    # Sicherstellen, dass die Verzeichnisse existieren
    PRODUCKT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    
    daemon_loop()