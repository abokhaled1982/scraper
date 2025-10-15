# price_parser.py

import sys
from pathlib import Path

# config aus Parent-Ordner laden (direkter Skriptstart möglich)
sys.path.append(str(Path(__file__).resolve().parent.parent))
# Importiere die benötigten Pfad-Variablen aus config.py
from config import DATA_DIR, OUT_DIR,HTML_SOURCE_FILE,TEMP_LLM_INPUT_FILE

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

def run_pipeline():
    """
    Orchestriert die sequenzielle Ausführung der HTML-Verarbeitung und der AI-Extraktion.
    """
    print("--- STARTE ENDE-ZU-ENDE EXTRAKTIONS-PIPELINE ---")
    
    # Sicherstellen der Verzeichnisstruktur
    create_directories()
    
    # NEU: Der Output-Pfad wird hier direkt als lokale Variable erstellt.
    final_output_file = OUT_DIR / "output.json"

    print(f"HTML Quelle: {HTML_SOURCE_FILE.resolve()}")
    print(f"Finales Ziel: {final_output_file.resolve()}") # Gibt den korrekten Dateipfad aus

    # 1. SCHRITT: HTML-Verarbeitung
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
    print("\n=============================================")
    print("SCHRITT 2: AI-EXTRAKTION")
    print("=============================================")

    try:
        # KORRIGIERT: Übergibt den korrekten Dateipfad (OUT_DIR / "output.json")
        extract_and_save_data(TEMP_LLM_INPUT_FILE, final_output_file)
    except FileNotFoundError as e:
        # Dies sollte nicht passieren, wenn Schritt 1 erfolgreich war.
        print(f"PIPELINE ABGEBROCHEN: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"PIPELINE ABGEBROCHEN: Fehler in der AI-Extraktion: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n--- PIPELINE ERFOLGREICH ABGESCHLOSSEN ---")


if __name__ == "__main__":
    run_pipeline()