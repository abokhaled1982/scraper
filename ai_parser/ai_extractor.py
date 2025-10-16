# ai_extractor.py
"""
Definiert die Pydantic-Datenmodelle und die Logik zur LLM-gestützten Extraktion.

AKTUALISIERUNGEN: 
1. Feld 'url_des_produkts' im LLM-Schema hinzugefügt (wird später zu affiliate_url).
"""

import os
import json
import sys
from pathlib import Path
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# --- 1. LLM-DATENMODELLE ---

class Produktbild(BaseModel):
    """Repräsentiert eine URL des Hauptproduktbildes mit ihrem Größendeskriptor."""
    url: str = Field(description="Die absolute URL zur Bilddatei.")
    groessen_deskriptor: str = Field(
        description="Der Breiten- oder Dichte-Deskriptor, z.B. '480w' (Mobile), '1200w' (Desktop) oder '2x' (Retina). Wähle den besten Deskriptor, der zur URL passt, oder schätze diesen ('1x' als Standard)."
    )

class Gutschein(BaseModel):
    """Repräsentiert spezifische Gutschein-Informationen."""
    details: str = Field(description="Der gefundene Gutscheintext, z.B. '5% Rabatt mit Code X' ODER 'N/A'.")
    code: str = Field(description="Der Gutscheincode oder 'N/A', falls kein Code gefunden wurde.")

class Produktinformation(BaseModel):
    """Strukturierte Daten, die von der Produktseite extrahiert werden sollen."""
    titel: str = Field(description="Der vollständige, aussagekräftige Produkt-Titel aus dem Text.")
    sku_asin: str = Field(description="Die eindeutige Produktnummer (ASIN, SKU, EAN, MPN oder eine ähnliche Produktkennung) aus dem Text. Gib 'N/A' an, wenn keine gefunden wird.")
    
    # NEU: Das Feld für die Produkt-URL
    url_des_produkts: str = Field(description="Die vollständige und bereinigte URL der Produktseite, gefunden in canonical tags oder anderen Links im HTML-Inhalt. Gib 'N/A' an, wenn keine URL gefunden wird.")

    marke: str = Field(description="Die Marke oder der Hersteller des Produkts.")
    akt_preis: str = Field(description="Der aktuelle Verkaufspreis mit Währung (z.B. 25,45 €).")
    uvp_preis: str = Field(description="Der ursprüngliche Preis (UVP) vor dem Rabatt oder 'N/A'.")
    rabatt_prozent: str = Field(description="Der Rabatt in Prozent, z.B. '-35%' oder 'N/A'.")
    rabatt_text: str = Field(description="Der gefundene werbliche Text/Begriff für einen Rabatt, z.B. 'Sie sparen 5 Euro', '3 für 2 Aktion', 'Sonderpreis', 'Befristetes Angebot' oder 'N/A'.")

    bewertung: str = Field(description="Die durchschnittliche Kundenbewertung, z.B. '4,5 von 5 Sternen' oder 'N/A'.")
    anzahl_bewertungen: str = Field(description="Die Gesamtzahl der abgegebenen Kundenbewertungen, z.B. '1.234' oder 'N/A'.")

    gutschein: Gutschein = Field(description="Informationen über Gutscheine.")
    verfuegbarkeit: str = Field(description="Die Verfügbarkeit des Produkts, z.B. 'Auf Lager' oder 'Nicht auf Lager'.")
    produkt_highlights: list[str] = Field(description="Eine Liste der wichtigsten Produktmerkmale/Highlights (Bullet-Points).")
    images: list[Produktbild] = Field(
        description="Eine Liste der relevantesten URLs des Hauptproduktbildes, wobei nur die beste/größte URL pro Bild mit ihrem Größendeskriptor angegeben wird."
    )


# --- 2. LLM-LOGIK UND IMPLEMENTIERUNG ---

def baue_pattern_pack():
    """Erstellt das Pattern Pack für die LLM-Extraktion."""
    schema_definition = Produktinformation.model_json_schema()
    return {"output_schema": schema_definition}

def extrahiere_produktsignale(clean_text: str, bild_kandidaten_list: list[str], pack: dict) -> dict:
    """Führt die LLM-Extraktion durch."""
    try:
        client = genai.Client()
    except Exception:
        raise EnvironmentError("GOOGLE_API_KEY ist nicht gesetzt oder ungültig.")
    
    # Prompt-Anpassung, um die URL-Extraktion zu betonen
    prompt = f"""
    Extrahiere die folgenden Produktinformationen aus dem bereitgestellten Text und den Bild-Kandidaten.
    1. Fülle 'titel' mit dem vollständigsten und besten Produktnamen.
    2. Fülle 'sku_asin' mit der eindeutigen Produktnummer (ASIN, SKU, EAN o.ä.) aus dem Text.
    3. Fülle 'url_des_produkts' mit der vollständigen und bereinigten URL der Produktseite (canonical oder Haupt-Link).
    4. Fülle 'rabatt_text' mit dem allgemeinen Rabatt- oder Angebotstext (wird später zu gutschein.details).
    5. Fülle 'gutschein.code' NUR mit dem tatsächlichen Gutscheincode.
    Gib 'N/A' an, wenn die Information fehlt.

    --- BILD-KANDIDATEN ---
    {json.dumps(bild_kandidaten_list, indent=2, ensure_ascii=False)}
    
    --- BEREINIGTER TEXT ---
    {clean_text[:4000]}
    """
    
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=pack["output_schema"],
    )

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=config,
        )
        return json.loads(response.text)
    
    except Exception as e:
        print(f"LLM-Aufruf fehlgeschlagen: {e}", file=sys.stderr)
        return {"Extraktionsfehler": f"LLM-Fehler: {e}"}


# --- 3. HAUPTLOGIK (extract_and_save_data) ---

def extract_and_save_data(llm_input_path: Path, output_path: Path):
    """Führt die LLM-Extraktion durch und speichert das Roh-Ergebnis."""
    try:
        with open(llm_input_path, 'r', encoding='utf-8') as f:
            llm_input_data = json.load(f)
    except Exception as e:
        raise FileNotFoundError(f"Fehler beim Laden der LLM-Eingabedatei: {e}")

    clean_text = llm_input_data.get("clean_text", "")
    bild_kandidaten_str = llm_input_data.get("bild_kandidaten", "") 
    
    print("\t-> Starte LLM-Extraktion...")
    
    result = {}
    if not clean_text.strip():
        result = {"Fehler": "Bereinigter Text ist leer."}
        print("WARNUNG: Bereinigter Text ist leer. LLM-Extraktion wird übersprungen.", file=sys.stderr)
    else:
        try:
            pack = baue_pattern_pack()
            bild_kandidaten_list = bild_kandidaten_str.split(' | ') if bild_kandidaten_str else []
            result = extrahiere_produktsignale(clean_text, bild_kandidaten_list, pack)
        except EnvironmentError as e:
            print(f"Fehler beim Laden des LLM-Schlüssels: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            error_msg = f"Ein schwerwiegender Fehler bei der LLM-Extraktion ist aufgetreten: {e}"
            print(error_msg, file=sys.stderr)
            result = {"Extraktionsfehler": str(e), "Hinweis": "Prüfen Sie den GOOGLE_API_KEY und das LLM-Schema."}

    # Finales Ergebnis speichern (Roh-Output)
    final_output = {
        "source_file": llm_input_data.get("source_file", "N/A"),
        "product_title": llm_input_data.get("product_title", "N/A"), 
        "asin": llm_input_data.get("asin", "N/A"), 
        "raw_bild_kandidaten": bild_kandidaten_str,
        "clean_text": clean_text,
        "extracted_data": result,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(final_output, f, indent=4, ensure_ascii=False)
    
    print(f"\t-> LLM-Roh-Output gespeichert unter: {output_path.resolve()}")