#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Daemon-Worker (Orchestrator), der einzelne HTML-Dateien verarbeitet und die Ergebnisse schreibt.
ROUTING: Unterscheidet zwischen Amazon- und Nicht-Amazon-HTML.
ARCHITEKTUR: Nutzt fachlich getrennte Module (utils, html_parser, data_mapper, ai_extractor).
"""

from __future__ import annotations
import json, time, traceback
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import sys
import uuid 

# Projekt-Config (Annahme: config.py existiert)
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import PRODUCKT_DIR, OUT_DIR, FAILED_DIR, INTERVAL_SECS, REGISTRY_PATH, SUMMARY_PATH

# Importiere die fachlich getrennten Module
from utils import (
    _read_text, _sha1_file, move_to_failed, load_registry, 
    save_registry, write_summary_append, pick_oldest_html, parse_price_string,map_ai_output_to_target_format
)
from html_parser import extract_core_html_data
# Importe für Amazon und AI-Pipeline
from parser import AmazonProductParser, to_b0_schema 
from ai_parser.html_processor import process_html_to_llm_input 
from ai_parser.ai_extractor import extract_and_save_data 


# ----------------------------- ROUTING & AI-PARSING LOGIK --------------------------------------

def is_amazon_html(html_content: str) -> bool:
    """Entscheidet anhand von Amazon-spezifischen Merkmalen, ob es eine Produktseite ist."""
    if any(tag in html_content for tag in [
        'id="productTitle"', 'id="ASIN"', 'data-asin=',
        'class="a-section a-spacing-none"', 'id="twisterDiv"'
        ]):
        return True
    return False

def process_with_ai_parser(fp: Path, out_dir: Path) -> Tuple[bool, str, Dict]:
    """
    Orchestriert die AI-Pipeline: HTML-Extraktion -> LLM-Extraktion -> Mapping.
    """
    
    temp_llm_input_file = fp.with_name(f"{fp.stem}.llm_input.json")
    temp_ai_output_file = fp.with_name(f"{fp.stem}.llm_output.json")
    
    raw_html_content = _read_text(fp) 

    def cleanup_temp_files():
        temp_llm_input_file.unlink(missing_ok=True)
        temp_ai_output_file.unlink(missing_ok=True)
    
    try:
        # 1. DETERMINISTISCHES HTML-PARSING (Hole die URL, Titel, Bilder)
        html_core_data = extract_core_html_data(raw_html_content)
        canonical_url = html_core_data.get('affiliate_url')
        # NEU: Explizite Ausgabe der gefundenen URL
        print(f"[AI-PARSER] Determinierte & normalisierte URL: {canonical_url}") 
        
        # 2. HTML VORBEREITUNG FÜR LLM (Schreibt llm_input.json)
        process_html_to_llm_input(fp, temp_llm_input_file) 
        
        # 3. LLM-EXTRAKTION
        extract_and_save_data(temp_llm_input_file, temp_ai_output_file)
        
        # 4. DATEN-MAPPING (Kombiniert HTML-Core und LLM-Output)
        with open(temp_ai_output_file, 'r', encoding='utf-8') as f:
            ai_output_data = json.load(f)

        data_mapped = map_ai_output_to_target_format(
            ai_output_data, 
            html_core_data, # <-- Die HTML-URL wird mit höchster Priorität hier übergeben
            parse_price_fn=parse_price_string
        )
        
        # ... (rest der Speichelogik und Cleanup bleibt unverändert) ...

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
        
        return True, f"AI OK -> {final_output_file.name}", data_mapped

    except Exception as e:
        cleanup_temp_files()
        raise Exception(f"AI-Pipeline/Mapping Fehler: {e}")

def process_one(fp: Path, reg: Dict) -> Tuple[bool, str]:
    """
    Verarbeitet eine einzelne HTML-Datei: ROUTING: Amazon-Parser oder General-AI-Parser.
    """
    try:
        content = _read_text(fp) # utils
        
        if is_amazon_html(content):
            # --- AMAZON-PFAD ---
            # ... (Unveränderte Amazon-Logik, nutzt utils Funktionen) ...
            parser = AmazonProductParser(content)
            product = parser.parse()
            
            if not product.asin or not product.title:
                raise ValueError("Amazon-Parser lieferte keine Haupt-Produktdaten (ASIN/Titel) oder es ist eine unvollständige Seite.")

            page_hash = _sha1_file(fp) 
            asin = product.asin
            out_path = OUT_DIR / f"{asin}_{page_hash[:10]}.json" 
            data_mapped = to_b0_schema(product)
            
            tmp = out_path.with_suffix(".tmp")
            with tmp.open('w', encoding='utf-8') as f:
                json.dump(data_mapped, f, ensure_ascii=False, indent=2)
            tmp.replace(out_path) 
            
            write_summary_append(data_mapped, SUMMARY_PATH) 
            reg["hashes"][page_hash] = out_path.name
            if asin:
                reg["asins"][asin] = out_path.name
            save_registry(reg, REGISTRY_PATH) 

            #fp.unlink(missing_ok=True)
            return True, f"AMAZON OK -> {out_path.name}"
            
        else:
            # --- AI-PARSER-PFAD ---
            print(f"[ROUTER] {fp.name} -> Nicht-Amazon HTML erkannt. Starte modulare AI-Pipeline.")
            
            ok, msg, data_mapped = process_with_ai_parser(fp, OUT_DIR)
            
            if ok:
                 write_summary_append(data_mapped, SUMMARY_PATH) 
                 fp.unlink(missing_ok=True) 
                 return True, msg
            
            return False, "Fehler in der AI-Pipeline."
            
    except Exception as e:
        tb = traceback.format_exc()
       # move_to_failed(fp, f"ROUTER/VERARBEITUNGSFEHLER: {e}\n\n{tb}", FAILED_DIR)
        return False, f"ROUTER ERR {fp.name}: {e}"


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
            ok, msg = process_one(fp, reg)
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