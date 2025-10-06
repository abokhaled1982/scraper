#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, re
import urllib.parse
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup

DEFAULT_BASE = "https://www.amazon.de"

def norm(s): return re.sub(r"\s+", " ", (s or "")).strip()

def clean_price(text: str):
    if not text: return None
    t = text.strip()
    m = re.search(r"(?:€\s*)?(\d[\d.\s]*[.,]\d{2})(?:\s*€)?", t)
    if not m: return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try: val = float(num)
    except Exception: val = None
    return {"raw": t, "value": val}

def detect_base_url(soup):
    can = soup.select_one("link[rel='canonical']")
    if can and can.get("href", "").startswith("http"): return can["href"]
    og = soup.select_one('meta[property="og:url"]')
    if og and og.get("content", "").startswith("http"): return og["content"]
    return DEFAULT_BASE

def absolutize(href, base_url):
    if not href: return None
    if href.startswith("http://") or href.startswith("https://"): return href
    if href.startswith("file:"):
        m = re.search(r"(/(?:dp|gp|deal)/[A-Za-z0-9/._\-?=&#%]+)", href)
        return urljoin(DEFAULT_BASE, m.group(1)) if m else None
    return urljoin(base_url or DEFAULT_BASE, href)

# ------- extraction helpers tailored to your deals HTML -------

CARD_SEL = [
    "div[class*='ProductCard-module__card'][data-testid='product-card']",
    "div[data-testid='grid-deal-card']",
]

import re, urllib.parse  # make sure both are imported

ASIN_ID_RE = re.compile(r"^title-([A-Z0-9]{10})$", re.I)
ASIN_URL_RES = [
    re.compile(r"/dp/([A-Z0-9]{10})(?:[/?#]|$)", re.I),
    re.compile(r"/gp/product/([A-Z0-9]{10})(?:[/?#]|$)", re.I),
]
QS_ASIN_KEYS = {"asin", "ASIN"}

def extract_asin_from_card(card) -> str | None:
    # 1) direct attribute on the card
    val = (card.get("data-asin") or "").strip()
    if re.fullmatch(r"[A-Z0-9]{10}", val, re.I):
        return val.upper()

    # 2) the title node id="title-<ASIN>"
    title = card.select_one("p[id^='title-']") or card.select_one("[id^='title-']")
    if title:
        m = ASIN_ID_RE.match(title.get("id") or "")
        if m:
            return m.group(1).upper()

    # 3) sometimes an inner anchor carries data-asin
    a = card.select_one("a[data-asin]") or card.select_one("a[asin]")
    if a:
        aval = (a.get("data-asin") or a.get("asin") or "").strip()
        if re.fullmatch(r"[A-Z0-9]{10}", aval, re.I):
            return aval.upper()

    return None

def extract_asin_from_url(url: str | None) -> str | None:
    if not url:
        return None
    for rx in ASIN_URL_RES:
        m = rx.search(url)
        if m:
            return m.group(1).upper()
    # query-string fallback (?asin=...)
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for k in QS_ASIN_KEYS:
            if k in qs and qs[k]:
                v = qs[k][0]
                if re.fullmatch(r"[A-Z0-9]{10}", v, re.I):
                    return v.upper()
    except Exception:
        pass
    return None


def extract_link(card, base_url):
    a = card.select_one("a[data-testid='product-card-link']") or card.select_one("a[href]")
    return absolutize(a.get("href"), base_url) if a else None

def extract_title(card):
    p = card.select_one("p[id^='title-'].ProductCard-module__title_awabIOxk6xfKvxKcdKDH")
    if p:
        txt = norm(p.get_text(" ", strip=True))
        if txt: return txt
    off = [el.get_text(strip=True) for el in card.select(".a-offscreen")]
    def looks_price(s): return bool(re.search(r"(€\s*)?\d[\d.\s]*[.,]\d{2}(€)?$", s))
    cand = [t for t in off if t and not looks_price(t)]
    if cand: return max(cand, key=len)
    return norm(card.get_text(" ", strip=True))[:200] or None

def extract_discount(card):
    m = re.search(r"(\d{1,3})\s?%(\s*Rabatt)?", card.get_text(" ", strip=True))
    return int(m.group(1)) if m else None

def extract_prices(card):
    prices = [clean_price(el.get_text()) for el in card.select("span.a-price .a-offscreen")]
    prices = [p for p in prices if p and p["value"] is not None]
    if not prices: return None, None
    vals = sorted(set([p["value"] for p in prices]))
    if len(vals) >= 2:
        deal_val, orig_val = vals[0], vals[-1]
        deal_raw = next((p["raw"] for p in prices if p["value"] == deal_val), f"{deal_val:.2f}€")
        return {"raw": deal_raw, "value": deal_val}, {"raw": f"{orig_val:.2f}€", "value": orig_val}
    else:
        v = vals[0]
        deal_raw = next((p["raw"] for p in prices if p["value"] == v), f"{v:.2f}€")
        return {"raw": deal_raw, "value": v}, None

def parse_deals_from_html(html: str):
    soup = BeautifulSoup(html, "lxml")
    base_url = detect_base_url(soup)

    cards = []
    for sel in CARD_SEL: cards.extend(soup.select(sel))
    if not cards:
        for a in soup.find_all("a", href=True):
            if re.search(r"/(dp|deal)/", a["href"]): cards.append(a)

    results, seen = [], set()
    for card in cards:
        url = extract_link(card, base_url) if hasattr(card, "select_one") else absolutize(card.get("href"), base_url)
        asin = None
        if hasattr(card, "get"):
            asin = extract_asin_from_card(card)
        if not asin:
            asin = extract_asin_from_url(url)

        dedup_key = asin or url
        if not dedup_key or dedup_key in seen:
            continue
        seen.add(dedup_key)

        name = extract_title(card) if hasattr(card, "select_one") else (card.get("aria-label") or norm(card.get_text(strip=True)))
        deal_price, orig_price = (extract_prices(card) if hasattr(card, "select_one") else (None, None))
        disc = extract_discount(card) if hasattr(card, "get_text") else None

        if disc is None and deal_price and orig_price and deal_price["value"] and orig_price["value"]:
            try:
                if orig_price["value"] > 0:
                    disc = int(round((orig_price["value"] - deal_price["value"]) / orig_price["value"] * 100))
            except Exception:
                pass

        results.append({
            "asin": asin,   # NEW FIELD
            "product_name": name or None,
            "product_url": url,
            "price": deal_price,
            "discount_percent": disc
        })
    return results

def main():
    ap = argparse.ArgumentParser(description="Parse Amazon Angebotslisten (offline) aus ./inbox.")
    ap.add_argument("--inbox", default="inbox", help="Input-Ordner mit HTML")
    ap.add_argument("--out",   default="out",   help="Output-Ordner")
    args = ap.parse_args()

    inbox = Path(args.inbox).resolve()
    out   = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)

    files = sorted([p for p in inbox.rglob("*") if p.suffix.lower() in {".html", ".htm"}])
    if not files:
        print(f"[WARN] Keine HTML-Dateien in {inbox}")
        return

    all_rows = []
    for fp in files:
        try:
            raw = fp.read_text(encoding="utf-8", errors="ignore")
            rows = parse_deals_from_html(raw)
            for r in rows: r["_source_file"] = str(fp)
            all_rows.extend(rows)
            print(f"[OK] {fp.name}: {len(rows)} Deals")
        except Exception as e:
            print(f"[ERR] {fp}: {e}")

    (out / "deals.json").write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "deals.jsonl").open("w", encoding="utf-8") as f:
        for r in all_rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n[SUMMARY] total deals: {len(all_rows)} -> {out/'deals.json'}")

if __name__ == "__main__":
    main()
