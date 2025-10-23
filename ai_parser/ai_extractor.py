# ai_extractor.py — CLEANED VERSION

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

# --- 1. LLM-DATENMODELLE ---

class Produktinformation(BaseModel):
    """Strukturierte Daten, die von der Produktseite extrahiert werden sollen."""
    produkt_titel: str = Field(description="Der vollständige und präzise Titel des Produkts.")
    marke: str = Field(description="Die Marke oder der Hersteller des Produkts.")
    
    # NEUE LOGIK: Muss den finalen, niedrigsten Preis berechnen!
    akt_preis: str = Field(
        description="Der aktuelle Verkaufspreis mit Währung (z.B. 25,45 €). Dieses Feld MUSS den FINALEN, niedrigsten Preis nach Anwendung des HÖCHSTEN RABATTS (Code oder Aktion) enthalten. Der Wert muss berechnet und mit Währung angegeben werden."
    )
    uvp_preis: str = Field(description="Der ursprüngliche Preis (UVP) vor dem Rabatt oder 'N/A'.")
    
    # NEUE LOGIK: Muss den Rabatt vom UVP zum NEU berechneten akt_preis berechnen!
    rabatt_prozent: str = Field(
        description="Der Rabatt in Prozent, z.B. '-35%' oder 'N/A'. MUSS EXAKT VOM UVP ZUM FINALEN, BERECHNETEN 'akt_preis' AUSGERECHNET WERDEN. Das Ergebnis muss in Prozent ('-XX%') angegeben werden."
    )
    marktplatz: str = Field(description="Der Name des Marktplatzes/Shops (z.B. Amazon, Otto, MediaMarkt, oder 'N/A').")
   
    produkt_id: str = Field(description="Die eindeutige Produktkennung wie ASIN, SKU oder Produktnummer. Falls keine gültige Produktkennung gefunden wird, verwende den String: **'produkt titel-der preis'**, wobei **alle Leerzeichen und Kommas** durch Bindestriche (-) ersetzt werden sollen")
    hauptprodukt_bilder: list[str] = Field(description="Eine Liste der relevantesten Produktbild-URLs als Strings.")
    url_des_produkts: str = Field(description="Die kanonische URL des Produkts. Verwende 'N/A', falls nicht gefunden.")
    bewertung_wert: float = Field(description="Der numerische Bewertungswert (Stern), z.B. 4.1.")
    anzahl_reviews: int = Field(description="Die Gesamtzahl der Bewertungen.")
    anzahl_verkauft: str = Field(description="Die Anzahl verkaufter Produkte (z.B. 'Über 1000 verkauft' oder 'N/A').")
    haendler_verkaeufer: str = Field(description="Der Händler oder Verkäufername.")
    verfuegbarkeit: str = Field(description="Informationen zur Verfügbarkeit.")
    lieferinformation: str = Field(description="Details zur Lieferung.")
    
    gutschein_code: str = Field(description="Der Gutscheincode oder 'N/A'.")    
    # Logik für die Beschreibung beibehalten
    gutschein_details: str = Field(
        description="Die vollständige Beschreibung (Gültigkeit, Bedingungen, Einschränkungen) des Gutscheincodes. WIRD NUR BEFÜLLT, WENN 'gutschein_code' VORHANDEN IST, sonst 'N/A'. WICHTIG: Die Endpreis-Information muss hier zusätzlich genannt werden, z.B. '...der Endpreis beträgt dann XX,XX €', um die Berechnung für den 'akt_preis' zu dokumentieren."
    )   
    rabatt_text: str = Field(description="Der gefundene werbliche Text/Begriff für EINEN ANDEREN Rabatt, der KEINEN Code erfordert (z.B. 'Sie sparen 5 Euro', '3 für 2 Aktion', '10% bei Newsletter-Anmeldung', 'Sonderpreis') oder 'N/A'.")
# --- 2. LLM-FUNKTIONEN ---

def baue_pattern_pack():
    """Initialisiert den LLM-Client und die Konfiguration."""
    client = genai.Client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Produktinformation,
    )
    system_prompt = (
        "Du bist ein hochpräziser Datenextraktions-Experte. Extrahiere alle angeforderten "
        "Produktdetails aus Text und Bild-URL-Kandidaten. Halte dich exakt an das JSON-Schema. "
        "Gib immer gültiges JSON zurück. Wenn keine Daten gefunden werden, nutze 'N/A' oder 0."
    )
    return {"client": client, "config": config, "system_prompt": system_prompt}


def extrahiere_produktsignale(unstrukturierter_text: str, bild_kandidaten_str: str, pack: dict) -> dict:
    """Führt die LLM-basierte Extraktion der Produktsignale aus dem Text und den Bild-Kandidaten durch."""
    LLM_MODEL = "gemini-2.5-flash"
    client = pack["client"]
    config = pack["config"]
    system_prompt = pack["system_prompt"]

    user_prompt = f"""
Extrahiere die Produktinformationen aus dem folgenden Text.

BILD-KANDIDATEN:
---
{bild_kandidaten_str}
---

PRODUKT-TEXT:
---
{unstrukturierter_text}
---
"""

    print(f"-> Sende Extraktionsanfrage an {LLM_MODEL} ...")
    response = client.models.generate_content(
        model=LLM_MODEL,
        contents=[system_prompt, user_prompt],
        config=config,
    )
    print("<- Antwort erhalten.")

    produkt_daten = Produktinformation.model_validate_json(response.text.strip())
    return produkt_daten.model_dump()


# --- 3. HAUPTFUNKTION ---

def extract_and_save_data(input_path: Path, output_path: Path):
    """Liest die Input-JSON, führt die LLM-Extraktion durch und speichert das Ergebnis."""
    print(f"\n[SCHRITT 2/2: AI-EXTRAKTOR]")
    print(f"  -> Input: {input_path.resolve()}")
    print(f"  -> Output: {output_path.resolve()}")

    if not input_path.exists():
        raise FileNotFoundError(f"LLM-Input-Datei nicht gefunden: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        llm_input_data = json.load(f)

    clean_text = llm_input_data.get("clean_text", "N/A")
    bild_kandidaten = llm_input_data.get("bild_kandidaten", "N/A")

    if clean_text == "N/A" or not clean_text.strip():
        print("WARNUNG: Bereinigter Text ist leer.", file=sys.stderr)
        result = {"Fehler": "Bereinigter Text ist leer."}
    else:
        try:
            pack = baue_pattern_pack()
            result = extrahiere_produktsignale(clean_text, bild_kandidaten, pack)
        except Exception as e:
            print(f"Fehler bei der Extraktion: {e}", file=sys.stderr)
            result = {"Extraktionsfehler": str(e)}

    final_output = {
        "source_file": llm_input_data.get("source_file", "N/A"),
        "product_title": llm_input_data.get("product_title", "N/A"),
        "raw_bild_kandidaten": bild_kandidaten,
        "clean_text": clean_text,
        "extracted_data": result,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_output, f, ensure_ascii=False, indent=2)

    print(f"[ERFOLG] Ergebnis gespeichert: {output_path}")
