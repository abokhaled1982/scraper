# ai_extractor.py — VERTEX AI VERSION

import json
import sys
import time
from pathlib import Path
from pydantic import BaseModel, Field
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.api_core import exceptions as google_exceptions
from dotenv import load_dotenv

load_dotenv()

# --- Vertex AI Konfiguration ---
VERTEX_PROJECT_ID = "crack-photon-495413-r1"
VERTEX_LOCATION   = "us-central1"
LLM_MODEL         = "gemini-2.5-pro"

# --- 1. LLM-DATENMODELLE ---

class Produktinformation(BaseModel):
    """Strukturierte Daten, die von der Produktseite extrahiert werden sollen."""

    produkt_titel: str = Field(
        description=(
            "Der verkaufsstarke, professionell formulierte und für Social Media (WhatsApp & Telegram) "
            "optimierte Produkttitel. "
            "\n\n"
            "KÜRZUNGSREGELN (EXTREM WICHTIG):\n"
            "- Der Titel DARF MAXIMAL 100 Zeichen haben.\n"
            "- Wenn der Originaltitel länger ist, MUSST du ihn intelligent kürzen.\n"
            "- Kürze NIEMALS mitten im Wort.\n"
            "- Wenn gekürzt wurde: immer mit '...' enden.\n"
            "- Erzeuge keine unnatürlichen Abkürzungen.\n"
            "\n"
            "PRIORITÄTEN BEIM KÜRZEN:\n"
            "Behalte IMMER: 1. Marke  2. Produkttyp  3. kritische Kennzahl/Modellnummer  4. Farbe (optional)\n"
            "\n"
            "STILREGELN:\n"
            "- Kurz, professionell, leicht lesbar.\n"
            "- Keine überflüssigen Wörter wie 'inkl.', 'Gratis Versand', 'hochwertig', 'super', 'neu', 'Angebot'.\n"
            "- Keine Emojis in diesem Feld.\n"
            "\n"
            "Gib NUR DEN FERTIGEN TITEL zurück, kein JSON, keine Erklärung."
        )
    )

    marke: str = Field(description="Die Marke oder der Hersteller des Produkts.")

    akt_preis: str = Field(
        description=(
            "Der aktuelle Verkaufspreis mit Währung (z.B. 25,45 €). "
            "Dieses Feld MUSS den FINALEN, niedrigsten Preis nach Anwendung des HÖCHSTEN RABATTS (Code oder Aktion) enthalten. "
            "Ignoriere ALLE Rabatte, die mit 'Amazon Visa', 'Kreditkarte', 'Startgutschrift' oder 'Punkte sammeln' zu tun haben."
        )
    )

    original_preis: str = Field(
        description=(
            "Der ursprüngliche, durchgestrichene Preis, der UVP, oder der Preis vor einem Rabatt (z.B. 49,99 €). "
            "Falls kein expliziter UVP/Originalpreis gefunden wird, MUSS dieser Wert dem 'akt_preis' entsprechen."
        )
    )

    rabatt_prozent: str = Field(
        description=(
            "Der Rabatt in Prozent, z.B. '-35%' oder 'N/A'. "
            "MUSS EXAKT VOM 'original_preis' ZUM FINALEN 'akt_preis' BERECHNET WERDEN. "
            "Wenn 'akt_preis' gleich 'original_preis' ist, MUSS dieses Feld 'N/A' sein."
        )
    )

    marktplatz: str = Field(description="Der Name des Marktplatzes/Shops (z.B. Amazon, Otto, MediaMarkt, oder 'N/A').")

    produkt_id: str = Field(
        description=(
            "Die eindeutige Produktkennung wie ASIN, SKU oder Produktnummer. "
            "Falls keine gültige Produktkennung gefunden wird, verwende 'produkt-titel-der-preis' "
            "(alle Leerzeichen und Kommas durch Bindestriche ersetzen)."
        )
    )

    hauptprodukt_bilder: list[str] = Field(
        description=(
            "Eine Liste der relevantesten Produktbild-URLs als Strings. "
            "1. Hohe Auflösung (Breite > 800px bevorzugt). "
            "2. Nur echte Produktfotos, keine Logos/Icons/Screenshots/Banner. "
            "3. Format-Konsistenz: nur das dominante Format (JPG oder WebP). "
            "4. Relative URLs in absolute URLs umwandeln (Basis: kanonische Produkt-URL). "
            "5. Wenn keine passenden Bilder: leere Liste []."
        )
    )

    url_des_produkts: str = Field(description="Die kanonische URL des Produkts. Verwende 'N/A', falls nicht gefunden.")
    bewertung_wert: float = Field(description="Der numerische Bewertungswert (Stern), z.B. 4.1.")
    anzahl_reviews: int = Field(description="Die Gesamtzahl der Bewertungen.")
    anzahl_verkauft: str = Field(description="Die Anzahl verkaufter Produkte (z.B. 'Über 1000 verkauft' oder 'N/A').")
    haendler_verkaeufer: str = Field(description="Der Händler oder Verkäufername.")
    verfuegbarkeit: str = Field(description="Informationen zur Verfügbarkeit.")
    lieferinformation: str = Field(description="Details zur Lieferung.")
    gutschein_code: str = Field(description="Der Gutscheincode oder 'N/A'.")

    gutschein_details: str = Field(
        description=(
            "Die vollständige Beschreibung (Gültigkeit, Bedingungen, Einschränkungen) des Gutscheincodes. "
            "NUR BEFÜLLEN, WENN 'gutschein_code' vorhanden ist, sonst 'N/A'. "
            "Endpreis-Information nennen: '...der Endpreis beträgt dann XX,XX €'."
        )
    )

    rabatt_text: str = Field(
        description=(
            "Die KURZE, WERBLICHE ZUSAMMENFASSUNG des Preisvorteils mit absolutem Rabattbetrag in Euro. "
            "Beginne immer mit einem passenden Emoji: "
            "MEGA-DEAL (>40%): 🔥 oder 🚨 | SOLIDER DEAL (20-40%): 🎁 🔑 💸 | KLEINER RABATT (<20%): ✅ 📧 📦. "
            "Den 'akt_preis' NICHT wiederholen. "
            "Kein Rabatt: '🚨 Tiefstpreis-Alarm, Unschlagbar! 💥'."
        )
    )

    reel_titel: str = Field(
        description=(
            "Kurzer Produkttitel AUSSCHLIESSLICH für das Reel-Video-Template (erscheint groß im Video). "
            "SPRACHE: IMMER AUF DEUTSCH. "
            "MAXIMAL 22 ZEICHEN (inkl. Leerzeichen). Nur: Marke + Produkttyp + EINE kritische Kennzahl. "
            "Beispiele: 'Anker USB-C 240W', 'Samsung QLED 65\"', 'Nike Air Max 90'. "
            "KEINE Farbe, KEINE Füllwörter, KEINE Punkte am Ende, KEINE Emojis. "
            "Kürze intelligent an einer Wortgrenze. "
            "Gib NUR den fertigen Kurztitel zurück."
        )
    )

    reel_beschreibung: str = Field(
        description=(
            "Kurze Produkt-Beschreibung AUSSCHLIESSLICH für das Reel-Video-Template. "
            "SPRACHE: IMMER AUF DEUTSCH. "
            "MAXIMAL 4 WÖRTER. Beschreibt den Hauptnutzen oder das Highlight des Produkts. "
            "Beispiele: 'Schnellste USB-C Ladung', '4K HDR Gaming Display', 'Leichtes Laufschuh-Design'. "
            "KEINE Emojis, KEIN Preis, KEINE Rabattinfos, KEINE Markennamen wiederholen. "
            "Gib NUR die fertigen max. 4 Wörter zurück."
        )
    )

    reel_caption: str = Field(
        description=(
            "GEKÜRZTER Rabatt-Text AUSSCHLIESSLICH für das kleine Caption-Label im Reel-Video. "
            "ABLEITUNG: Nimm den 'rabatt_text' und kürze ihn auf MAXIMAL 25 ZEICHEN. "
            "REGELN:\n"
            "- Behalte das/die GLEICHEN Emojis wie in 'rabatt_text' (NIEMALS weglassen oder ersetzen). "
            "- Kürze den Text intelligent: Emoji + Kernaussage (Betrag oder %). "
            "- Kürze NIEMALS mitten im Wort. "
            "- Kein Zeilenumbruch, keine zusätzlichen Infos. "
            "BEISPIELE (rabatt_text -> reel_caption):\n"
            "  '🔥 MEGA-DEAL! Ganze 105,80 € gespart!' -> '🔥 105,80 € gespart!'\n"
            "  '🎁 Satte 110,02 € gespart!' -> '🎁 110,02 € gespart!'\n"
            "  '✅ Kleiner Rabatt, guter Preis!' -> '✅ Guter Preis!'\n"
            "  '🚨 Tiefstpreis-Alarm, Unschlagbar! 💥' -> '🚨 Tiefstpreis! 💥'\n"
            "Gib NUR den fertigen Caption-Text zurück, keine Erklärung."
        )
    )

    hashtags: list[str] = Field(
        description=(
            "Strategisch optimierte Hashtag-Liste. "
            "3 Basis-Tags aus: #angebot #rabatt #schnäppchen #deal #bestpreis #sale. "
            "2 Saison/Event-Tags (z.B. #weihnachtsgeschenk #wintersale). "
            "3-5 Produkt-Nische-Tags (Kategorie + Marke, z.B. #gamingsetup #ps5controller). "
            "Keine generischen Tags wie #love oder #happy."
        )
    )


# --- 2. LLM-FUNKTIONEN ---

SYSTEM_PROMPT = (
    "Du bist ein hochpräziser Datenextraktions-Experte für den deutschen Markt. "
    "Extrahiere alle angeforderten Produktdetails aus dem gesamten Kontext. "
    "Halte dich exakt an das JSON-Schema. "
    "SPRACHE: Alle Textfelder (insbesondere reel_titel, reel_beschreibung, reel_caption, "
    "rabatt_text, produkt_titel) IMMER AUF DEUTSCH ausgeben — auch wenn der Originaltitel "
    "auf Englisch ist. Produktnamen und Markennamen dürfen englisch bleiben, "
    "aber Beschreibungen, Captions und sonstige Texte sind IMMER DEUTSCH. "

    "OBERSTE PRIORITÄT: BERECHNE IMMER DEN FINALEN, NIEDRIGSTEN PREIS (akt_preis)! "
    "Erkenne ALLE DIREKTEN, SOFORT ANWENDBAREN Preisvorteile: "
    "Rabattcodes, Sofort-Abzüge, Klick-Coupons, Mengenrabatte, Versandkosten-Ersparnis. "
    "NIEMALS Visa-Gutschriften oder nachgelagerte Cash-Back-Angebote einrechnen. "

    "akt_preis = niedrigster Kaufpreis, den ein universaler Kunde sofort zahlt. "

    "Bildregeln: "
    "- Nur echte Produktbilder (keine Logos, Icons, Screenshots, Banner). "
    "- Format-Konsistenz (nur JPG oder nur WebP). "
    "- Relative URLs in absolute umwandeln (Basis: kanonische Produkt-URL). "
    "- Keine Bilder: leere Liste []. "

    "Gib immer gültiges JSON zurück. Fehlende Werte: 'N/A' oder 0."
)


def baue_pattern_pack() -> dict:
    """Initialisiert Vertex AI und gibt Model + Config zurück."""
    vertexai.init(project=VERTEX_PROJECT_ID, location=VERTEX_LOCATION)
    model = GenerativeModel(
        model_name=LLM_MODEL,
        system_instruction=SYSTEM_PROMPT,
    )
    # Schema als dict übergeben, da Vertex AI Pydantic-Klassen nicht direkt unterstützt
    schema_dict = Produktinformation.model_json_schema()
    config = GenerationConfig(
        response_mime_type="application/json",
        response_schema=schema_dict,
    )
    return {"model": model, "config": config}


def extrahiere_produktsignale(
    unstrukturierter_text: str,
    bild_kandidaten_str: str,
    pack: dict,
) -> dict:
    """Führt die LLM-basierte Extraktion durch."""
    model: GenerativeModel = pack["model"]
    config: GenerationConfig = pack["config"]

    user_prompt = (
        "Extrahiere die Produktinformationen aus dem folgenden Text.\n\n"
        "BILD-KANDIDATEN:\n"
        "---\n"
        f"{bild_kandidaten_str}\n"
        "---\n\n"
        "PRODUKT-TEXT:\n"
        "---\n"
        f"{unstrukturierter_text}\n"
        "---"
    )

    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"-> Sende Extraktionsanfrage an {LLM_MODEL} (Versuch {attempt}/{MAX_RETRIES}) ...")
            response = model.generate_content(
                contents=user_prompt,
                generation_config=config,
            )
            print("<- Antwort erhalten.")
            produkt_daten = Produktinformation.model_validate_json(response.text.strip())
            return produkt_daten.model_dump()
        except (google_exceptions.ResourceExhausted, google_exceptions.ServiceUnavailable) as e:
            wait = 2 ** attempt
            print(f"[WARN] API Overload (Versuch {attempt}): {e}. Warte {wait}s ...", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(wait)
        except Exception:
            raise


# --- 3. HAUPTFUNKTION ---

def extract_and_save_data(llm_input_data: dict, output_path: Path):
    """Liest die Input-JSON, führt die LLM-Extraktion durch und speichert das Ergebnis."""
    print(f"\n[SCHRITT 2/2: AI-EXTRAKTOR]")
    print(f"  -> Output: {output_path.resolve()}")

    if not llm_input_data:
        raise ValueError("LLM-Input-Datei nicht gefunden oder leer.")

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
