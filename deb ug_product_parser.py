# debug_parser.py
from pathlib import Path
from core.workers.amazon.product_parser import (
    _extract_ldjson_blocks,
    _find_product_ldjson,
    _extract_image_from_ldjson_product,
    extract_and_normalize_url,
    extract_core_html_data,
    extrahiere_produktbilder_aus_html,
)

for html_file in Path("test").glob("*.html"):
    print(f"\n{'='*60}")
    print(f"DATEI: {html_file.name}")
    print('='*60)

    html = html_file.read_text(encoding="utf-8")

    # LD+JSON
    blocks = _extract_ldjson_blocks(html)
    node   = _find_product_ldjson(blocks)
    print(f"LD+JSON Blöcke:  {len(blocks)}")
    print(f"Product-Node:    {'✓ gefunden' if node else '✗ nicht gefunden'}")
    if node:
        print(f"  url:   {node.get('url', '—')}")
        print(f"  image: {node.get('image', '—')}")

    # Extraktion
    core = extract_core_html_data(html)
    print(f"\nErgebnis:")
    print(f"  title:      {core.get('title', '—')[:80]}")
    print(f"  url:        {core.get('url', '—')}")
    print(f"  bild_ldjson:{core.get('bild_ldjson', '—')[:80]}")

    # Bild-Fallback (DOM)
    bild_dom = extrahiere_produktbilder_aus_html(html)
    print(f"  bild_dom:   {bild_dom[:80]}")