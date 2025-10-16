# ai_extractor.py

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

# ai_extractor.py

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

# KORRIGIERT: Dieses Modell wird vereinfacht und nur für die URL verwendet (der Deskriptor wird ignoriert, da die Anforderung nur ein Array von Strings ist).
# Wir behalten es als Pydantic-Objekt, damit das LLM die URLs sauber als Array liefert.
class Produktbild(BaseModel):
    """Repräsentiert die URL eines relevanten Produktbildes."""
    url: str = Field(description="Die absolute URL zur Bilddatei.")
    # Der Deskriptor wird entfernt, da er nicht mehr im finalen Output benötigt wird.
    # groessen_deskriptor: str = Field(...) 


class Produktinformation(BaseModel):
    """Strukturierte Daten, die von der Produktseite extrahiert werden sollen."""
    marke: str = Field(description="Die Marke oder der Hersteller des Produkts.")
    akt_preis: str = Field(description="Der aktuelle Verkaufspreis mit Währung (z.B. 25,45 €).")
    uvp_preis: str = Field(description="Der ursprüngliche Preis (UVP) vor dem Rabatt oder 'N/A'.")
    rabatt_prozent: str = Field(description="Der Rabatt in Prozent, z.B. '-35%' oder 'N/A'.")
    rabatt_text: str = Field(description="Der gefundene werbliche Text/Begriff für einen Rabatt, z.B. 'Sie sparen 5 Euro', '3 für 2 Aktion', 'Sonderpreis' oder 'N/A'.")
    
    # NEU: Feld für die eindeutige Produkt-ID
    produkt_id: str = Field(description="Die eindeutige Produktkennung wie ASIN, SKU oder Produktnummer. Verwende 'N/A', falls nicht gefunden.")
    
    # KORRIGIERT: Die Beschreibung wurde aktualisiert, um die Fokus auf die RELEVANTEN URLs zu legen.
    hauptprodukt_bilder: list[Produktbild] = Field(
        description="Eine Liste der relevantesten URLs des Hauptproduktbildes. Das Array muss leer sein, wenn keine Bilder gefunden werden."
    )
    
    bewertung_wert: float = Field(description="Der numerische Bewertungswert (Stern), z.B. 4.1. Verwende 0.0, falls nicht gefunden.")
    anzahl_reviews: int = Field(description="Die Gesamtzahl der abgegebenen Bewertungen (Reviews). Verwende 0, falls nicht gefunden.")
    anzahl_verkauft: str = Field(description="Die Anzahl der verkauften Produkte (z.B. 'Über 1000 verkauft' oder 'N/A').")
    haendler_verkaeufer: str = Field(description="Der Name des Händlers oder Verkäufers, der das Produkt versendet (z.B. 'Zalando', 'Amazon' oder 'N/A').")
    verfuegbarkeit: str = Field(description="Informationen zur Verfügbarkeit.")
    lieferinformation: str = Field(description="Details zur Lieferung.")
    gutschein_code: str = Field(description="Der Gutscheincode oder Promo-Code, z.B. '20PREMIUM' oder 'N/A'.")
    gutschein_details: str = Field(description="Die vollständige Beschreibung des Gutscheins, z.B. 'Spare -20% auf Premium & Designer Brands.' oder 'N/A'.")


# --- 2. LLM-FUNKTIONEN ---

# ai_extractor.py (Aktualisierte Version)

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

# KORRIGIERT: Das Modell 'Produktbild' wird entfernt, da wir direkt ein Array von Strings (URLs) wollen.
# class Produktbild(BaseModel):
#     """Repräsentiert die URL eines relevanten Produktbildes."""
#     url: str = Field(description="Die absolute URL zur Bilddatei.")

class Produktinformation(BaseModel):
    """Strukturierte Daten, die von der Produktseite extrahiert werden sollen."""
    marke: str = Field(description="Die Marke oder der Hersteller des Produkts.")
    akt_preis: str = Field(description="Der aktuelle Verkaufspreis mit Währung (z.B. 25,45 €).")
    uvp_preis: str = Field(description="Der ursprüngliche Preis (UVP) vor dem Rabatt oder 'N/A'.")
    rabatt_prozent: str = Field(description="Der Rabatt in Prozent, z.B. '-35%' oder 'N/A'.")
    rabatt_text: str = Field(description="Der gefundene werbliche Text/Begriff für einen Rabatt, z.B. 'Sie sparen 5 Euro', '3 für 2 Aktion', 'Sonderpreis' oder 'N/A'.")
    
    produkt_id: str = Field(description="Die eindeutige Produktkennung wie ASIN, SKU oder Produktnummer. Verwende 'N/A', falls nicht gefunden.")
    
    # KORRIGIERT: Der Typ ist jetzt list[str] und die Beschreibung wurde angepasst, um Strings zu extrahieren.
    hauptprodukt_bilder: list[str] = Field(
        description="Eine Liste der relevantesten Produktbild-URLs als Strings. Das Array muss leer sein, wenn keine Bilder gefunden werden."
    )
    produkt_url: str = Field(description="Die kanonische URL des Produkts, extrahiert aus dem Text. Verwende 'N/A', falls nicht gefunden.")
    bewertung_wert: float = Field(description="Der numerische Bewertungswert (Stern), z.B. 4.1. Verwende 0.0, falls nicht gefunden.")
    anzahl_reviews: int = Field(description="Die Gesamtzahl der abgegebenen Bewertungen (Reviews). Verwende 0, falls nicht gefunden.")
    anzahl_verkauft: str = Field(description="Die Anzahl der verkauften Produkte (z.B. 'Über 1000 verkauft' oder 'N/A').")
    haendler_verkaeufer: str = Field(description="Der Name des Händlers oder Verkäufers, der das Produkt versendet (z.B. 'Zalando', 'Amazon' oder 'N/A').")
    verfuegbarkeit: str = Field(description="Informationen zur Verfügbarkeit.")
    lieferinformation: str = Field(description="Details zur Lieferung.")
    gutschein_code: str = Field(description="Der Gutscheincode oder Promo-Code, z.B. '20PREMIUM' oder 'N/A'.")
    gutschein_details: str = Field(description="Die vollständige Beschreibung des Gutscheins, z.B. 'Spare -20% auf Premium & Designer Brands.' oder 'N/A'.")


# --- 2. LLM-FUNKTIONEN ---

def baue_pattern_pack():
    """Initialisiert den LLM-Client und die Konfiguration."""
    # ... (Unverändert) ...
    
    client = genai.Client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Produktinformation,
    )
    
    # KORRIGIERT: System-Prompt angepasst, um die Anweisung für Bilder als String-Array zu vereinfachen.
    system_prompt = (
        "Du bist ein hochpräziser Datenextraktions-Experte. Deine Aufgabe ist es, alle "
        "angeforderten Produktdetails aus dem unstrukturierten Text und der bereitgestellten "
        "Liste von Bild-Kandidaten zu extrahieren. Halte dich streng an das bereitgestellte JSON-Schema. "
        "WICHTIG: Prüfe die Liste der 'BILD-KANDIDATEN' sorgfältig. Wähle die RELEVANTEN "
        "Hauptbild-URLs und **strukturiere diese als Array von URL-Strings** "
        "im Feld 'hauptprodukt_bilder'. Wenn keine Bilder gefunden werden, "
        "muss 'hauptprodukt_bilder' eine leere Liste `[]` sein. **Extrahiere die Produkt-URL in das Feld 'produkt_url'.** Für alle anderen nicht gefundenen Felder "
        "muss exakt 'N/A' oder 0/0.0 befüllt werden. Der Output muss zu 100% valides JSON sein."
    )

    return {
        "client": client,
        "config": config,
        "system_prompt": system_prompt
    }

# ... (Rest der Datei bleibt unverändert)


def extrahiere_produktsignale(unstrukturierter_text: str, bild_kandidaten_str: str, pack: dict) -> dict:
    """Führt die LLM-basierte Extraktion der Produktsignale aus dem Text und den Bild-Kandidaten durch."""
    
    LLM_MODEL = 'gemini-2.5-flash' # Kosteneffizienter für Extraktion
    
    client = pack["client"]
    config = pack["config"]
    system_prompt = pack["system_prompt"]
    
    user_prompt = f"""
Extrahiere die Produktinformationen aus dem folgenden Text.

BILD-KANDIDATEN (Wähle und kategorisiere die relevanten Hauptproduktbilder):
---
{bild_kandidaten_str}
---

PRODUKT-TEXT:
---
{unstrukturierter_text}
---
"""

    print(f"-> Sende Extraktionsanfrage an {LLM_MODEL} (LLM)...")

    response = client.models.generate_content(
        model=LLM_MODEL, 
        contents=[system_prompt, user_prompt],
        config=config,
    )
    
    print("<- Antwort erhalten.")
    
    json_string = response.text.strip()
    produkt_daten = Produktinformation.model_validate_json(json_string)
    
    return produkt_daten.model_dump()


# --- 3. Hauptausführung des Skripts ---

def extract_and_save_data(input_path: Path, output_path: Path):
    """
    Liest die Input-JSON, führt die LLM-Extraktion durch und speichert das Ergebnis.
    """
    print(f"\n[SCHRITT 2/2: AI-EXTRAKTOR]")
    print(f" 	-> LLM-Input-Quelle: {input_path.resolve()}")
    print(f" 	-> Ausgabe-Ziel: {output_path.resolve()}")
    
    if not input_path.exists():
        raise FileNotFoundError(f"LLM-Input-Datei nicht gefunden: {input_path}")
        
    print("\n-> Lese LLM-Input-Daten...")
    with open(input_path, "r", encoding="utf-8") as f:
        llm_input_data = json.load(f)

    clean_text = llm_input_data.get("clean_text", "N/A")
    bild_kandidaten = llm_input_data.get("bild_kandidaten", "N/A")
    
    if clean_text == "N/A" or not clean_text.strip():
        result = {"Fehler": "Bereinigter Text ist leer."}
        print("WARNUNG: Bereinigter Text ist leer. LLM-Extraktion wird übersprungen.", file=sys.stderr)
    else:
        try:
            pack = baue_pattern_pack()
            result = extrahiere_produktsignale(clean_text, bild_kandidaten, pack)
        except EnvironmentError as e:
            print(f"Fehler beim Laden des LLM-Schlüssels: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            error_msg = f"Ein schwerwiegender Fehler bei der LLM-Extraktion ist aufgetreten: {e}"
            print(error_msg, file=sys.stderr)
            result = {"Extraktionsfehler": str(e), "Hinweis": "Prüfen Sie den GOOGLE_API_KEY und das LLM-Schema."}


    # Finales Ergebnis speichern
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

    print(f"\n[ERFOLG] Pipeline abgeschlossen!")
    print(f" 	-> Strukturiertes Ergebnis gespeichert in: {output_path}")

# Die if __name__ == "__main__": Logik wurde entfernt, da der Runner diese Funktion aufruft.