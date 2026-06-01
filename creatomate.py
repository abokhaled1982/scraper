#!/usr/bin/env python3
"""
Test-Script: Creatomate + ElevenLabs Voiceover
Testet verschiedene Varianten des Voiceover-Layer-Namens
um herauszufinden welches Format korrekt funktioniert.
"""

import json
import time
import requests

# ── Konfiguration ─────────────────────────────────────────────────────────────
API_KEY     = "cca743b75b9c46e0bf5e2325a31cbc2ba1081a03bd7863bb50aa678c1b8671559c1e333a97b6d4f6a49d4089252dc49f"
TEMPLATE_ID = "65d0c5db-a2a1-40b6-8240-0d1b68c0a706"
API_URL     = "https://api.creatomate.com/v2/renders"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}

# ── Test-Daten ─────────────────────────────────────────────────────────────────
BASE_MODIFICATIONS = {
    "Product-Image.source":     "https://creatomate.com/files/assets/fe61553c-4274-4586-affe-54cffe99ccdc",
    "Product-Name.text":        "Nike RN Flyknit",
    "Product-Description.text": "High-Performance Running Shoes",
    "Normal-Price.text":        "109,99 €",
    "Discounted-Price.text":    "89,99 €",
    "Caption.text":             "🔥 Discount Alert – 42% Rabatt!",
    "CTA.text":                 "Folgt uns für mehr Rabatte!",
    "Website.text":             "www.dealsboss.de",
}

VOICEOVER_TEXT = (
    "Krasses Angebot heute! Nike RN Flyknit jetzt 42 Prozent günstiger – "
    "nur 89 Euro 99. Jetzt schnell zuschlagen, Link in der Bio!"
)

# Verschiedene Varianten die wir testen
VARIANTS = [
    {
        "name":    'Voiceover-SHX.source (korrekter Key für dieses Template)',
        "key":     "Voiceover-SHX.source",
        "value":   VOICEOVER_TEXT,
    },
]


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def start_render(modifications: dict) -> dict:
    """Startet einen Render und gibt die initiale Antwort zurück."""
    payload = {
        "template_id": TEMPLATE_ID,
        "modifications": modifications,
    }
    resp = requests.post(API_URL, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) else data


def poll_render(render_id: str, timeout: int = 180) -> dict:
    """Pollt den Render-Status bis succeeded/failed oder Timeout."""
    status_url = f"{API_URL}/{render_id}"
    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        if elapsed > timeout:
            return {"status": "timeout", "id": render_id}
        resp = requests.get(status_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "unknown")
        print(f"      [{elapsed:>3}s] Status: {status}")
        if status == "succeeded":
            return data
        if status in ("failed", "error"):
            return data
        time.sleep(5)


def run_variant(variant: dict) -> dict:
    """Führt einen einzelnen Test-Render durch und gibt das Ergebnis zurück."""
    mods = {**BASE_MODIFICATIONS, variant["key"]: variant["value"]}

    print(f"\n{'─'*60}")
    print(f"  🧪 {variant['name']}")
    print(f"     Layer-Key : {variant['key']}")
    print(f"     Text      : {variant['value'][:60]}...")
    print()

    try:
        render = start_render(mods)
        render_id = render.get("id")
        if not render_id:
            print(f"  ❌ Keine Render-ID in Antwort: {render}")
            return {"variant": variant["name"], "status": "no_id", "render": render}

        print(f"     Render-ID : {render_id}")
        print(f"     Polling   ...")
        result = poll_render(render_id)
        status = result.get("status")

        if status == "succeeded":
            url = result.get("url", "N/A")
            print(f"  ✅ ERFOLG! Video-URL: {url}")
        elif status == "timeout":
            print(f"  ⏱️  TIMEOUT nach 180s")
        else:
            error = result.get("error") or result.get("message") or status
            print(f"  ❌ FEHLGESCHLAGEN: {error}")

        return {
            "variant": variant["name"],
            "key":     variant["key"],
            "status":  status,
            "url":     result.get("url"),
            "error":   result.get("error"),
            "id":      render_id,
        }

    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:400]
        except Exception:
            pass
        print(f"  ❌ HTTP-Fehler: {e} | Body: {body}")
        return {"variant": variant["name"], "key": variant["key"], "status": "http_error", "error": str(e), "body": body}

    except Exception as e:
        print(f"  ❌ Unbekannter Fehler: {e}")
        return {"variant": variant["name"], "key": variant["key"], "status": "exception", "error": str(e)}


# ── Hauptprogramm ──────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  CREATOMATE + ELEVENLABS VOICEOVER TEST")
    print("=" * 60)
    print(f"  Template-ID : {TEMPLATE_ID}")
    print(f"  Varianten   : {len(VARIANTS)}")
    print(f"  Voiceover   : {VOICEOVER_TEXT[:55]}...")

    results = []

    for variant in VARIANTS:
        result = run_variant(variant)
        results.append(result)
        time.sleep(2)  # kurze Pause zwischen Renders

    # ── Zusammenfassung ──
    print(f"\n{'='*60}")
    print("  ERGEBNIS-ZUSAMMENFASSUNG")
    print(f"{'='*60}")
    for r in results:
        icon = "✅" if r["status"] == "succeeded" else "❌"
        print(f"  {icon}  {r['variant']}")
        if r["status"] == "succeeded":
            print(f"      URL   : {r.get('url')}")
        else:
            print(f"      Fehler: {r.get('error') or r.get('body') or r['status']}")

    print()

    # JSON-Output für Debugging
    print("── JSON-Output ──────────────────────────────────────────")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()