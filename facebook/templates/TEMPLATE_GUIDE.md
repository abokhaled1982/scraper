# Neues Template integrieren

## 1. JSON-Datei anlegen

Datei in `facebook/templates/` kopieren (z. B. `offer_type3.json`):

```json
{
  "template_type": "offer_type3",
  "template_id": "DEINE-NEUE-TEMPLATE-ID",
  "kind": "offer",
  "image_fit": "contain",
  "description": "Kurze Beschreibung des Templates."
}
```

> **Nur zwei Felder ändern:** `template_type` (eindeutiger Name) und `template_id` (aus Creatomate).  
> `kind` bleibt `"offer"` (oder `"reel"` für Reel-Templates). Alles andere bleibt gleich.

---

## 2. Template in einem Deal verwenden

Im Deal-JSON das Feld `template_type` setzen:

```json
{
  "template_type": "offer_type3",
  "title": "Produktname",
  "price": "€ 49,99",
  "normal_price": "€ 89,99",
  "website_text": "www.meineshop.de",
  "reel_caption": "Sale Alert 🔥"
}
```

### Felder für Caption & Website

| Deal-JSON-Feld | Creatomate-Layer |
|---|---|
| `reel_caption` | `Caption.text` |
| `website_text` / `website` / `domain` | `Website.text` |
| `cta_text` / `cta` | `CTA.text` |

---

## 3. Spontane Überschreibung (optional)

Einzelne Layer im Deal-JSON direkt überschreiben:

```json
{
  "template_modifications": {
    "Caption.text": "Mein eigener Caption-Text 🔥",
    "Website.text": "www.andereshop.de"
  }
}
```

Diese Werte haben **immer Vorrang** vor den automatisch berechneten Werten.

---

## Verfügbare Templates

| `template_type` | `template_id` | Typ |
|---|---|---|
| `offer_type1` | `a39e2efb-e7b6-43ca-ad09-524ef8ba91ac` | offer |
| `offer_type2` | `65d0c5db-a2a1-40b6-8240-0d1b68c0a706` | offer |
