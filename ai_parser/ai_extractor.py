# ai_extractor.py — CLEANED VERSION

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

    produkt_titel: str = Field(
    description=(
        "Der verkaufsstarke, professionell formulierte und für Social Media (WhatsApp & Telegram) "
        "optimierte Produkttitel. "
        "\n\n"
        "🎯 **KÜRZUNGSREGELN (EXTREM WICHTIG):**\n"
        "- Der Titel DARF MAXIMAL **100 Zeichen** haben.\n"
        "- Wenn der Originaltitel länger ist, MUSST du ihn **intelligent kürzen**.\n"
        "- Kürze NIEMALS mitten im Wort.\n"
        "- Wenn gekürzt wurde: **immer mit '...' enden**.\n"
        "- Erzeuge keine unnatürlichen Abkürzungen.\n"
        "\n"
        "🎯 **PRIORITÄTEN BEIM KÜRZEN:**\n"
        "Behalte IMMER diese Elemente (falls vorhanden):\n"
        "1. **Marke** (z. B. Samsung, Apple, Sony)\n"
        "2. **Produkttyp** (z. B. Smartphone, Wasserkocher, Monitor)\n"
        "3. **kritische Kennzahl oder Modellnummer** (z. B. 256GB, 55 Zoll, 2200W, M1, S23 Ultra)\n"
        "4. **Farbe** (optional, aber bevorzugt, wenn dadurch klarer wird, um welches Produkt es geht)\n"
        "\n"
        "🎯 **STILREGELN:**\n"
        "- Kurz, professionell, leicht lesbar.\n"
        "- Keine überflüssigen Wörter wie 'inkl.', 'Gratis Versand', 'hochwertig', 'super', 'neu', 'Angebot', etc.\n"
        "- Keine Emojis in diesem Feld.\n"
        "\n"
        "📌 **BEISPIELE:**\n"
        "Original: 'Samsung Galaxy S21 Ultra 5G SM-G998B 256GB Phantom Black Dual SIM inkl. Case'\n"
        "→ Ausgabe: 'Samsung Galaxy S21 Ultra 256GB Phantom Black...'\n\n"
        "Original: 'Philips Wasserkocher Edelstahl 1.7L 2200W Schnellkochfunktion, silber'\n"
        "→ Ausgabe: 'Philips Wasserkocher 1.7L 2200W silber'\n\n"
        "Original: 'Nike Air Zoom Pegasus 39 Herren Laufschuhe Schwarz Blau Größe 43 EU'\n"
        "→ Ausgabe: 'Nike Air Zoom Pegasus 39 Herren schwarz blau...'\n\n"
        "Gib **NUR DEN FERTIGEN TITEL** zurück, kein JSON, keine Erklärung."
             )
    )

    marke: str = Field(description="Die Marke oder der Hersteller des Produkts.")
    
    # NEUE LOGIK: Muss den finalen, niedrigsten Preis berechnen!
    # ✅ HIER IST DEIN ANGEPASSTER TEXT MIT VISA-FILTER
    akt_preis: str = Field(
        description=(
            "Der aktuelle Verkaufspreis mit Währung (z.B. 25,45 €). "
            "Dieses Feld MUSS den FINALEN, niedrigsten Preis nach Anwendung des HÖCHSTEN RABATTS (Code oder Aktion) enthalten. "
            "Der Wert muss berechnet und mit Währung angegeben werden. "
            "Ignoriere ALLE Rabatte, die mit 'Amazon Visa', 'Kreditkarte', 'Startgutschrift' oder 'Punkte sammeln' zu tun haben."
        )
    )

    # NEUES FELD: Originalpreis
    original_preis: str = Field(
        description=(
            "Der ursprüngliche, durchgestrichene Preis, der UVP, oder der Preis vor einem Rabatt (z.B. 49,99 €). "
            "**WICHTIGE LOGIK:** Falls kein expliziter UVP/Originalpreis im Text gefunden wird (kein 'durchgestrichener Preis'), "
            "MUSS dieser Wert dem berechneten **'akt_preis'** entsprechen. "
            "Dies stellt sicher, dass dieses Feld niemals leer ist und die Logik konsistent bleibt, wenn kein Rabatt angewendet wird."
        )
    )
    
    # NEUE LOGIK: Muss den Rabatt vom UVP zum NEU berechneten akt_preis berechnen!
    rabatt_prozent: str = Field(
        description=(
            "Der Rabatt in Prozent, z.B. '-35%' oder 'N/A'. MUSS EXAKT VOM 'original_preis' ZUM FINALEN, BERECHNETEN 'akt_preis' AUSGERECHNET WERDEN. "
            "Wenn 'akt_preis' gleich 'original_preis' ist, MUSS dieses Feld **'N/A'** sein. "
            "**WICHTIGE LOGIK:** Wenn das LLM nur den Rabattprozentsatz findet, MUSS es 'original_preis' oder 'akt_preis' berechnen, um die mathematische Logik zu erfüllen. "
            "Das Ergebnis muss in Prozent ('-XX%') angegeben werden."
        )
    )
    marktplatz: str = Field(description="Der Name des Marktplatzes/Shops (z.B. Amazon, Otto, MediaMarkt, oder 'N/A').")
   
    produkt_id: str = Field(description="Die eindeutige Produktkennung wie ASIN, SKU oder Produktnummer. Falls keine gültige Produktkennung gefunden wird, verwende den String: **'produkt titel-der preis'**, wobei **alle Leerzeichen und Kommas** durch Bindestriche (-) ersetzt werden sollen")
    hauptprodukt_bilder: list[str] = Field(
    description=(
        "Eine Liste der relevantesten Produktbild-URLs als Strings. "
        "**Das LLM MUSS diese Prioritäten strikt in dieser Reihenfolge einhalten:**\n\n"
        
        # 1. ABSOLUTE PRIORITÄT (Der Injection-Trick)
        "1. **[HERO-IMAGE] OVERRIDE:** Prüfe zuerst, ob eine URL im Input mit '[HERO-IMAGE]' beginnt. "
        "Dieses Bild MUSS zwingend an die erste Stelle der Liste. "
        "IGNORIERE für dieses eine Bild alle Qualitätsfilter, Format-Regeln oder Logo-Checks – es wird immer akzeptiert!\n"
        
        # 2. Qualität & Filter (Für den Rest der Galerie)
        "2. **Qualität & Eindeutigkeit:** Wähle für die restlichen Bilder nur hochauflösende Fotos (ideal >800px). "
        "Schließe strikt URLs aus, die nach Screenshots, Logos, Icons oder Platzhaltern aussehen "
        "(Wörter wie 'logo', 'icon', 'button' im Namen sind Ausschlusskriterien).\n"
        
        # 3. Konsistenz (Nur für Galerie-Auffüllung)
        "3. **Format- & Serien-Konsistenz:** Die weiteren Bilder sollten idealerweise das gleiche Dateiformat (z.B. jpg/webp) "
        "wie die besten Kandidaten haben. Mische keine Formate, wenn eine klare Serie erkennbar ist (außer das Hero-Image weicht ab).\n"
        
        # 4. Technik
        "4. **Technische Korrektur:** Falls eine URL relativ ist (beginnt mit '/'), wandle sie basierend auf der Produkt-URL "
        "in eine absolute HTTPS-URL um."
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
    # Logik für die Beschreibung beibehalten
    gutschein_details: str = Field(
        description="Die vollständige Beschreibung (Gültigkeit, Bedingungen, Einschränkungen) des Gutscheincodes. WIRD NUR BEFÜLLT, WENN 'gutschein_code' VORHANDEN IST, sonst 'N/A'. WICHTIG: Die Endpreis-Information muss hier zusätzlich genannt werden, z.B. '...der Endpreis beträgt dann XX,XX €', um die Berechnung für den 'akt_preis' zu dokumentieren."
    )   
    # ✅ DEIN RABATT_TEXT MIT VISA FILTER UND EMOJIS
    rabatt_text: str = Field(
        description=(
            "Die KURZE, WERBLICHE ZUSAMMENFASSUNG des Preisvorteils. "
            "Dieses Feld MUSS den **absoluten Rabattbetrag in Euro (z.B. 12,50 €)** nennen, anstatt eines Prozentsatzes. "
            "Es muss beschreiben, wie man den Vorteil erhält (z.B. 'mit Code', 'im Sale'). "
            "Ignoriere ALLE Rabatte, die mit 'Amazon Visa' zu tun haben.\n"
            
            # NEU: Regel zur Rabatt-Stufen-Wahl (Emoji & Ton)
            "**REGEL FÜR ATTENTION:** Jeder generierte Satz MUSS mit einem relevanten Emoji beginnen. Die Wahl des Emojis MUSS von der Höhe des Rabatts abhängen: "
            
            "**PRIORITÄT DER EMOJI-WAHL (BASIEREND AUF RABATT-PROZENT):**"
            "1. **MEGA-DEAL (> 40% Rabatt):** Nutze aggressive Emojis wie **🔥** (Feuer) oder **🚨** (Alarm) und einen dramatischen Text."
            "2. **SOLIDER DEAL (20% - 40% Rabatt):** Nutze neutrale, positive Emojis wie **🎁** (Geschenk), **🔑** (Deal) oder **💸** (Geld)."
            "3. **KLEINER RABATT (< 20% Rabatt):** Nutze funktionale Emojis wie **✅** (Haken), **📧** (E-Mail) oder **📦** (Versand)."
            
            "**WICHTIGSTE NEUE REGEL:** Der **finale Endpreis (akt_preis)** darf **NICHT** in diesem Feld wiederholt werden! "
            "**PRIORITÄT:** Bei kombinierten Rabatten muss das Highlight die Kombination in einem einzigen, kurzen und attraktiven Satz zusammenfassen. "
            "Es dient als Überschrift für Social-Media-Posts und MUSS professionell, prägnant und überzeugend sein. "
            
            "**WICHTIGE BEISPIELE ZUR ORIENTIERUNG (JETZT NUR MIT EURO-RABATT, OHNE ENDPREIS UND IMMER MIT ICON):** "
            "* 🔑 Code-Deal! Mit dem Code **SPAREN20** sparst du **20,00 €**! "
            "* 🔥 Mega-Sale: Sichere dir **45,00 € Sofort-Rabatt**! "
            "* 🎁 3-für-2 Aktion: **25,45 € geschenkt** im Paketpreis. "
            "* 💸 Sichere **50,00 € Sofort-Rabatt**! "
            "* 📧 Newsletter-Vorteil: Mit 10% Code sparst du **3,99 €**! "
            "* 📦 Versandkostenfrei + **15,00 € Rabatt**! "
            "* ✅ **[BASIEREND AUF BERECHNUNG]:** Im Checkout sparst du automatisch **15,00 €**! "
            "* 💸 **[BASIEREND AUF BERECHNUNG]:** Aktiviere den Klick-Coupon und spare **9,67 €**! "
            
            "* Wenn kein Rabatt angewendet wurde ('akt_preis' == 'original_preis'), verwende '🚨 Tiefstpreis-Alarm, Unschlagbar! 💥'."
        )
    )
    hashtags: list[str] = Field(
        description=(
            "Eine strategisch optimierte Liste von Hashtags für maximale Sichtbarkeit und Konversion. "
            "Handele wie ein Social Media Manager, der die aktuelle Saison und Kaufintention versteht.\n\n"
            
            "REGEL 1 (BASIS-TAGS - Wähle 3): "
            "Wähle die 3 passendsten Begriffe für Schnäppchenjäger. "
            "Auswahlpool: ['#angebot', '#rabatt', '#schnäppchen', '#deal', '#bestpreis', '#sale'].\n\n"
            
            "REGEL 2 (SAISON- & EVENT-KONTEXT - Wähle 2): "
            "Analysiere den Kontext des Produkts/Angebots. Ist es für Black Friday, Weihnachten oder Neujahr? "
            "Wähle entsprechende Tags wie: '#blackfriday' (nur im Nov), '#weihnachtsgeschenk', '#geschenkidee', "
            "'#silvesteroutfit', '#wintersale', '#jahresstart'.\n\n"
            
            "REGEL 3 (PRODUKT-NICHE - Wähle 3-5): "
            "Spezifische Tags, die genau beschreiben, WAS es ist und FÜR WEN es ist. "
            "Kombiniere Kategorie und Marke (z.B. '#gamingsetup', '#ps5controller', '#skincare', '#techgadgets').\n\n"
            
            "REGEL 4 (VIRALITÄT): "
            "Vermeide generische Tags wie '#love' oder '#happy'. Nutze Tags, die eine Kaufabsicht signalisieren."
        )
    )
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
        "Produktdetails aus dem gesamten Kontext (**TEXT UND ALLE BILDER**). Halte dich exakt an das JSON-Schema. "
        
        # EXTREM SCHARFE ANWEISUNG ZUR BERECHNUNG DES ENDPREISES
        "**OBERSTE PRIORITÄT: BERECHNE IMMER DEN FINALEN, NIEDRIGSTEN PREIS ('akt_preis')!** "
        "Dazu MUSS du ALLE Arten von **DIREKTEN, SOFORT ANWENDBAREN** Preisvorteilen "
        "aus dem gesamten Kontext erkennen und den Preis **EXAKT** neu berechnen. "
        
        # KRITERIEN FÜR DIE BERECHNUNG DES ENDPREISES ('akt_preis')
        "**DEFINITION 'akt_preis':** Der `akt_preis` MUSS den niedrigsten Kaufpreis darstellen, den ein **UNIVERSALER Kunde** bei Abschluss der Transaktion sofort bezahlt. "
        
        "**PRINZIP DER DIREKTEN REDUKTION:** Nur Preisvorteile, die zu einer **SOFORTIGEN, UNMITTELBAREN Reduktion** des fälligen Betrags im Checkout führen (z.B. Rabattcodes, Sofort-Abzüge, Klick-Coupons, automatische Mengenrabatte, Versandkosten-Ersparnis), dürfen in die Berechnung des `akt_preis` einfließen. Aber NIEMALS Visa-Gutschriften. "
        
        "**AUSNAHME VON DER BERECHNUNG (NACHGELAGERTE VORTEILE):** Vorteile, die eine **hohe Spezifität** oder eine **nachgelagerte Gutschrift** erfordern, sind strikt vom `akt_preis` auszuschließen. Dazu gehören: Gutschriften/Voucher für zukünftige Einkäufe, Cash-Back-Angebote nach dem Kauf, Boni für die Nutzung einer spezifischen (nicht-universellen) Zahlungsart oder Boni, die einen speziellen Kundenstatus voraussetzen. Diese Vorteile MÜSSEN im `rabatt_text` oder `gutschein_details` dokumentiert werden. "
        
        "**PRÄZEDENZ:** Der `akt_preis` muss die Summe **ALLER** direkten Rabatte widerspiegeln. Das Ignorieren eines **direkten** Rabattes gilt als Fehler. "
        
        # BILDER-LOGIK ENTFERNT (Wird jetzt vollständig über das JSON-Schema 'Produktinformation' gesteuert)
        
        "**WICHTIGE REGEL:** Alle URLs, die du für 'hauptprodukt_bilder' findest, **MÜSSEN** "
       "unter Verwendung der 'KANONISCHEN PRODUKT-URL' in absolute Web-Links umgewandelt werden, falls sie relativ sind. "
        "Die 'url_des_produkts' ist die URL, die dir im User-Prompt explizit übergeben wird. "
        "Gib immer gültiges JSON zurück. Wenn keine Daten gefunden werden, nutze 'N/A' oder 0."
    )
    return {"client": client, "config": config, "system_prompt": system_prompt}

def extrahiere_produktsignale(unstrukturierter_text: str, bild_kandidaten_str: str, pack: dict,known_url:str) -> dict:
    """Führt die LLM-basierte Extraktion der Produktsignale aus dem Text und den Bild-Kandidaten durch."""
    LLM_MODEL = "gemini-2.5-pro"
    client = pack["client"]
    config = pack["config"]
    system_prompt = pack["system_prompt"]

    user_prompt = f"""
    Extrahiere die Produktinformationen aus dem folgenden Text.

    🚨 **WICHTIGSTE METADATEN:**
    👉 **KANONISCHE PRODUKT-URL:** {known_url}
    (Nutze diese URL für das Feld 'url_des_produkts' und um relative Bild-Links zu korrigieren!)

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

def extract_and_save_data(llm_input_data: json, output_path: Path):
    """Liest die Input-JSON, führt die LLM-Extraktion durch und speichert das Ergebnis."""
    print(f"\n[SCHRITT 2/2: AI-EXTRAKTOR]")
   
    print(f"  -> Output: {output_path.resolve()}")

    if not llm_input_data:
        raise FileNotFoundError(f"LLM-Input-Datei nicht gefunden")
  
    clean_text = llm_input_data.get("clean_text", "N/A")
    bild_kandidaten = llm_input_data.get("bild_kandidaten", "N/A")
    # HIER: URL aus dem Input-Daten holen
    product_url = llm_input_data.get("product_url", "N/A")
    if clean_text == "N/A" or not clean_text.strip():
        print("WARNUNG: Bereinigter Text ist leer.", file=sys.stderr)
        result = {"Fehler": "Bereinigter Text ist leer."}
    else:
        try:
            pack = baue_pattern_pack()
            result = extrahiere_produktsignale(clean_text, bild_kandidaten, pack,product_url)
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
