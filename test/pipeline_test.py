#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test/pipeline_test.py — Tiefen-Tests für ALLE Kernkomponenten der Pipeline.

Ziel: Sicherstellen, dass
  * KEIN Post mit "N/A"-Preis durchgeht,
  * KEIN Post ohne valide Bild-URL durchgeht,
  * Preis-/Discount-Parser korrekt arbeiten,
  * Bilder-Filter (WebP / dynamische URLs) richtig greifen,
  * Amazon-URL-Kanonisierung & De-Duplikation funktionieren,
  * Telegram-/Facebook-Texterzeugung korrekt ist,
  * Datenbank-Lifecycle (queue → processing → sent / failed) klappt,
  * WS-Server Streaming-Chunks korrekt zusammensetzt,
  * AI-Extractor mit gemocktem Vertex AI das Schema erfüllt,
  * (--live) echter Chrome-Start funktioniert,
  * (--external) echter Vertex-AI-Call funktioniert.

Aufruf:
    source .venv/bin/activate
    python test/pipeline_test.py                 # alles in-process, ~130 Tests
    python test/pipeline_test.py --live          # + echter Chrome-Start (öffnet Tab!)
    python test/pipeline_test.py --external      # + 1 echter Vertex-AI-Call
    python test/pipeline_test.py --verbose       # Tracebacks komplett
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

# ─── ISOLATION: Test-DB VOR jedem core-Import setzen ─────────────────────
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="scraper_pipeline_"))
_TEST_DB = _TMP_DIR / "pipeline_test.db"
os.environ["CORE_DB_URL"] = f"sqlite:///{_TEST_DB}"
os.environ.setdefault("CORE_DASHBOARD_PORT", "9932")
os.environ.setdefault("CORE_LOG_PORT", "9922")
# Standardmäßig Chrome NICHT wirklich starten
os.environ.setdefault("DRY_RUN", "1")

# Erst nach env-Setup importieren
from core.db import deals_repo, state_repo, ENGINE  # noqa: E402
from core.db.models import (  # noqa: E402
    DEAL_STATUS_QUEUE, DEAL_STATUS_PROCESSING,
    DEAL_STATUS_SENT, DEAL_STATUS_FAILED,
    Base,
)

# Komponenten unter Test
from core.workers.facebook import fb_processor  # noqa: E402
from core.workers.facebook import reels_processor  # noqa: E402
from core.workers.facebook import fb_message  # noqa: E402
from core.workers.telegram import image_processor  # noqa: E402
from core.workers.telegram import offer_message  # noqa: E402
from core.workers.amazon import product_opener  # noqa: E402
from core.workers.amazon import parser_worker  # noqa: E402
from core.workers.amazon import amzon_dealsList_parser as deals_parser  # noqa: E402
from core.workers.amazon import utils as amzn_utils  # noqa: E402
from core.workers.amazon import ws_server  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────
# Test-Framework
# ─────────────────────────────────────────────────────────────────────────
VERBOSE = False


class Result:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[str] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  ✅ {name}")

    def fail(self, name: str, err: str) -> None:
        self.failed.append((name, err))
        head = err.strip().splitlines()[0] if err.strip() else "(no message)"
        print(f"  ❌ {name}\n       {head}")
        if VERBOSE:
            for line in err.strip().splitlines()[1:]:
                print(f"       {line}")

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append(name)
        print(f"  ⏭  {name}  ({reason})")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print("\n" + "=" * 64)
        print(
            f"Pipeline Ergebnis: {len(self.passed)}/{total} bestanden"
            + (f", {len(self.skipped)} übersprungen" if self.skipped else "")
        )
        if self.failed:
            print("\nFehlgeschlagen:")
            for name, err in self.failed:
                print(f"  • {name}")
                for line in err.strip().splitlines()[:6]:
                    print(f"      {line}")
            return 1
        print("Alle Pipeline-Prüfungen erfolgreich.")
        return 0


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def _case(r: Result, name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
        r.ok(name)
    except AssertionError as e:
        r.fail(name, str(e) or repr(e))
    except Exception:
        r.fail(name, traceback.format_exc())


def assert_eq(actual, expected, msg: str = "") -> None:
    if actual != expected:
        raise AssertionError(
            f"{msg}\n  expected: {expected!r}\n  actual:   {actual!r}"
        )


def assert_true(cond, msg: str = "expected True") -> None:
    if not cond:
        raise AssertionError(msg)


def assert_false(cond, msg: str = "expected False") -> None:
    if cond:
        raise AssertionError(msg)


def assert_in(needle, haystack, msg: str = "") -> None:
    if needle not in haystack:
        raise AssertionError(f"{msg}\n  not in: {haystack!r}\n  needle: {needle!r}")


def assert_not_in(needle, haystack, msg: str = "") -> None:
    if needle in haystack:
        raise AssertionError(f"{msg}\n  unexpected in: {haystack!r}\n  needle: {needle!r}")


def reset_db() -> None:
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)


def good_deal(**extra) -> dict:
    """Ein vollständig valider Deal-Payload (Basis)."""
    d = {
        "type": "post",
        "title": "Anker USB-C Ladegerät 240W",
        "affiliate_url": "https://amzn.to/abc123?tag=test-21",
        "image_url": "https://example.com/img/B0ABC12345.jpg",
        "images": ["https://example.com/img/B0ABC12345.jpg"],
        "price": {"raw": "39,99 €", "value": 39.99, "currency_hint": "€"},
        "original_price": {"raw": "79,99 €", "value": 79.99, "currency_hint": "€"},
        "discount_percent": "-50%",
        "market": "AMAZON",
        "rabatt_text": "🔥 Mega-Deal: 40 € sparen!",
    }
    d.update(extra)
    return d


# ─────────────────────────────────────────────────────────────────────────
# Block A — fb_processor: validate_deal_data / _is_empty / download_image
# ─────────────────────────────────────────────────────────────────────────
def test_block_fb_validate(r: Result) -> None:
    section("A) fb_processor.validate_deal_data (Posts)")

    # _is_empty
    _case(r, "A01 _is_empty(None)→True", lambda: assert_true(fb_processor._is_empty(None)))
    _case(r, "A02 _is_empty('')→True", lambda: assert_true(fb_processor._is_empty("")))
    _case(r, "A03 _is_empty('  ')→True", lambda: assert_true(fb_processor._is_empty("   ")))
    _case(r, "A04 _is_empty('N/A')→True", lambda: assert_true(fb_processor._is_empty("N/A")))
    _case(r, "A05 _is_empty('null')→True", lambda: assert_true(fb_processor._is_empty("null")))
    _case(r, "A06 _is_empty('none')→True", lambda: assert_true(fb_processor._is_empty("none")))
    _case(r, "A07 _is_empty('0')→True", lambda: assert_true(fb_processor._is_empty("0")))
    _case(r, "A08 _is_empty('0.00')→True", lambda: assert_true(fb_processor._is_empty("0.00")))
    _case(r, "A09 _is_empty('0.00 €')→True", lambda: assert_true(fb_processor._is_empty("0.00 €")))
    _case(r, "A10 _is_empty('0 €')→True", lambda: assert_true(fb_processor._is_empty("0 €")))
    _case(r, "A11 _is_empty('39,99 €')→False", lambda: assert_false(fb_processor._is_empty("39,99 €")))
    _case(r, "A12 _is_empty('1')→False", lambda: assert_false(fb_processor._is_empty("1")))

    # validate_deal_data — Happy Path
    def _ok():
        v = fb_processor.validate_deal_data(good_deal())
        assert_true(v["valid"], v["reason"])
        assert_eq(v["discount"], 50.0, "discount parsed")
    _case(r, "A13 valid full deal", _ok)

    # Reel-Type wird abgelehnt (gehört zum Reels-Worker)
    _case(r, "A14 reel-type rejected",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(type="reel"))["valid"]))

    # Titel fehlt
    _case(r, "A15 missing title",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(title=""))["valid"]))
    _case(r, "A16 N/A title",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(title="N/A"))["valid"]))
    _case(r, "A17 None title",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(title=None))["valid"]))

    # affiliate_url
    _case(r, "A18 missing affiliate_url",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(affiliate_url=""))["valid"]))
    _case(r, "A19 None affiliate_url",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(affiliate_url=None))["valid"]))

    # Preis-Gates (KEINE Posts mit N/A-Preis!)
    _case(r, "A20 price.raw='N/A' rejected",
          lambda: assert_false(fb_processor.validate_deal_data(
              good_deal(price={"raw": "N/A"}))["valid"]))
    _case(r, "A21 price.raw='' rejected",
          lambda: assert_false(fb_processor.validate_deal_data(
              good_deal(price={"raw": ""}))["valid"]))
    _case(r, "A22 price.raw='0' rejected",
          lambda: assert_false(fb_processor.validate_deal_data(
              good_deal(price={"raw": "0"}))["valid"]))
    _case(r, "A23 price.raw='0.00 €' rejected",
          lambda: assert_false(fb_processor.validate_deal_data(
              good_deal(price={"raw": "0.00 €"}))["valid"]))
    _case(r, "A24 price=None rejected",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(price=None))["valid"]))
    _case(r, "A25 price=empty-dict rejected",
          lambda: assert_false(fb_processor.validate_deal_data(good_deal(price={}))["valid"]))

    # Bild-Gates (KEINE Posts ohne Bild!)
    _case(r, "A26 images=[] + image_url None rejected",
          lambda: assert_false(fb_processor.validate_deal_data(
              good_deal(images=[], image_url=None))["valid"]))
    _case(r, "A27 image_url non-http rejected",
          lambda: assert_false(fb_processor.validate_deal_data(
              good_deal(image_url="ftp://x.de/y.jpg", images=[]))["valid"]))
    _case(r, "A28 fallback images[0] accepted",
          lambda: assert_true(fb_processor.validate_deal_data(
              good_deal(image_url=None,
                        images=["https://cdn.x/i.jpg"]))["valid"]))

    # discount-parsing
    def _disc(raw, expect):
        v = fb_processor.validate_deal_data(good_deal(discount_percent=raw))
        assert_eq(v["discount"], expect, f"raw={raw!r}")
    _case(r, "A29 discount '-33%' → 33.0", lambda: _disc("-33%", 33.0))
    _case(r, "A30 discount '20' → 20.0", lambda: _disc("20", 20.0))
    _case(r, "A31 discount '-12,5%' → 12.5", lambda: _disc("-12,5%", 12.5))
    _case(r, "A32 discount 'N/A' → 0.0", lambda: _disc("N/A", 0.0))
    _case(r, "A33 discount 'abc' → 0.0", lambda: _disc("abc", 0.0))


# ─────────────────────────────────────────────────────────────────────────
# Block B — reels_processor: validate_deal_data (verlangt type=='reel')
# ─────────────────────────────────────────────────────────────────────────
def test_block_reels_validate(r: Result) -> None:
    section("B) reels_processor.validate_deal_data (Reels)")

    base = good_deal(type="reel")
    _case(r, "B01 valid reel passes",
          lambda: assert_true(reels_processor.validate_deal_data(base)["valid"]))
    _case(r, "B02 _is_empty('N/A')→True",
          lambda: assert_true(reels_processor._is_empty("N/A")))
    _case(r, "B03 _is_empty('0.00')→True",
          lambda: assert_true(reels_processor._is_empty("0.00")))
    _case(r, "B04 _is_empty('19,99 €')→False",
          lambda: assert_false(reels_processor._is_empty("19,99 €")))
    _case(r, "B05 missing title rejected",
          lambda: assert_false(reels_processor.validate_deal_data({**base, "title": ""})["valid"]))
    _case(r, "B06 missing affiliate rejected",
          lambda: assert_false(reels_processor.validate_deal_data({**base, "affiliate_url": ""})["valid"]))
    _case(r, "B07 N/A price rejected",
          lambda: assert_false(reels_processor.validate_deal_data(
              {**base, "price": {"raw": "N/A"}})["valid"]))
    _case(r, "B08 0.00 € price rejected",
          lambda: assert_false(reels_processor.validate_deal_data(
              {**base, "price": {"raw": "0.00 €"}})["valid"]))
    _case(r, "B09 no images rejected",
          lambda: assert_false(reels_processor.validate_deal_data(
              {**base, "image_url": None, "images": []})["valid"]))
    _case(r, "B10 non-http image rejected",
          lambda: assert_false(reels_processor.validate_deal_data(
              {**base, "image_url": "data:image/png;base64,xxx", "images": []})["valid"]))


# ─────────────────────────────────────────────────────────────────────────
# Block C — image_processor: url_needs_local_processing + get_best_image_url
# ─────────────────────────────────────────────────────────────────────────
def test_block_image_url(r: Result) -> None:
    section("C) image_processor: URL-Filter")

    NL = image_processor.url_needs_local_processing
    # Simple positive (Standard-JPG → no processing)
    _case(r, "C01 jpg ohne Query → False",
          lambda: assert_false(NL("https://m.media-amazon.com/images/I/abc.jpg")))
    _case(r, "C02 jpeg → False",
          lambda: assert_false(NL("https://x/a.jpeg")))
    _case(r, "C03 png → False",
          lambda: assert_false(NL("https://x/a.png")))
    # Problemformate
    _case(r, "C04 webp → True",
          lambda: assert_true(NL("https://x/y.webp")))
    _case(r, "C05 gif → True",
          lambda: assert_true(NL("https://x/y.gif")))
    _case(r, "C06 WEBP groß → True",
          lambda: assert_true(NL("https://x/Y.WEBP")))
    # Dynamische Filter-Queries
    _case(r, "C07 ?width=800 → True",
          lambda: assert_true(NL("https://x/a.jpg?width=800")))
    _case(r, "C08 ?w=1200 → True",
          lambda: assert_true(NL("https://x/a.jpg?w=1200")))
    _case(r, "C09 ?fit-in=… → True",
          lambda: assert_true(NL("https://x/a.jpg?fit-in=400x400")))
    _case(r, "C10 ?quality=85 → True",
          lambda: assert_true(NL("https://x/a.jpg?quality=85")))
    _case(r, "C11 /filters/… → True",
          lambda: assert_true(NL("https://cdn/filters:format(webp)/foo")))
    _case(r, "C12 ?format=webp → True",
          lambda: assert_true(NL("https://x/a?format=webp")))
    # Keine Standard-Extension → True
    _case(r, "C13 ohne Extension → True",
          lambda: assert_true(NL("https://3dmensionals.de/produkt/abc")))
    # None / leer
    _case(r, "C14 None → False",
          lambda: assert_false(NL(None)))
    _case(r, "C15 '' → False",
          lambda: assert_false(NL("")))
    # extra param zwischen den anderen
    _case(r, "C16 &resize=… → True",
          lambda: assert_true(NL("https://x/a.jpg?foo=1&resize=10x10")))
    _case(r, "C17 jpg mit harmlosem Query → False",
          lambda: assert_false(NL("https://x/a.jpg?foo=bar")))

    # get_best_image_url
    GB = image_processor.get_best_image_url
    _case(r, "C18 main_image priorisiert",
          lambda: assert_eq(GB({"main_image": "https://m/x.jpg",
                                "images": ["https://i/y.jpg"]}),
                            "https://m/x.jpg"))
    _case(r, "C19 fallback auf images[0]",
          lambda: assert_eq(GB({"images": ["https://i/y.jpg", "https://i/z.jpg"]}),
                            "https://i/y.jpg"))
    _case(r, "C20 thumbnail-Fallback",
          lambda: assert_eq(GB({"thumbnail": "https://t/x.jpg"}), "https://t/x.jpg"))
    _case(r, "C21 keine URL → None",
          lambda: assert_eq(GB({"images": ["nonurl"]}), None))


# ─────────────────────────────────────────────────────────────────────────
# Block D — image_processor.download_and_convert_to_jpg (gemockt)
# ─────────────────────────────────────────────────────────────────────────
def _mk_webp_bytes() -> bytes:
    """Erzeugt ein winziges WebP-Bild via Pillow."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, format="WEBP")
    return buf.getvalue()


class _FakeAioResponse:
    def __init__(self, status=200, data=b""):
        self.status = status
        self._data = data

    async def read(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeAioSession:
    def __init__(self, response):
        self._resp = response

    def get(self, *a, **kw):
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_block_image_download(r: Result) -> None:
    section("D) image_processor.download_and_convert_to_jpg (mocked)")

    webp_data = _mk_webp_bytes()

    def _convert_ok():
        fake_resp = _FakeAioResponse(200, webp_data)
        fake_sess = _FakeAioSession(fake_resp)
        with patch.object(image_processor.aiohttp, "ClientSession",
                          return_value=fake_sess):
            out = asyncio.run(
                image_processor.download_and_convert_to_jpg("https://x/a.webp"))
        assert_true(out is not None, "out should be Path")
        assert_true(out.exists(), "file should exist")
        assert_eq(out.suffix.lower(), ".jpg")
        out.unlink(missing_ok=True)
    _case(r, "D01 WebP→JPG-Konvertierung", _convert_ok)

    def _http_404():
        fake_resp = _FakeAioResponse(404, b"")
        fake_sess = _FakeAioSession(fake_resp)
        with patch.object(image_processor.aiohttp, "ClientSession",
                          return_value=fake_sess):
            out = asyncio.run(
                image_processor.download_and_convert_to_jpg("https://x/a.webp"))
        assert_eq(out, None)
    _case(r, "D02 HTTP 404 → None", _http_404)

    def _too_big():
        big = b"x" * (image_processor.MAX_FILE_SIZE + 1)
        fake_resp = _FakeAioResponse(200, big)
        fake_sess = _FakeAioSession(fake_resp)
        with patch.object(image_processor.aiohttp, "ClientSession",
                          return_value=fake_sess):
            out = asyncio.run(
                image_processor.download_and_convert_to_jpg("https://x/a.webp"))
        assert_eq(out, None, "oversized must be rejected")
    _case(r, "D03 Datei > MAX_FILE_SIZE → None", _too_big)

    def _none_url():
        out = asyncio.run(image_processor.download_and_convert_to_jpg(None))
        assert_eq(out, None)
    _case(r, "D04 None-URL → None", _none_url)

    def _broken_bytes():
        fake_resp = _FakeAioResponse(200, b"not_an_image_xxx")
        fake_sess = _FakeAioSession(fake_resp)
        with patch.object(image_processor.aiohttp, "ClientSession",
                          return_value=fake_sess):
            out = asyncio.run(
                image_processor.download_and_convert_to_jpg("https://x/a.webp"))
        assert_eq(out, None, "broken image must return None")
    _case(r, "D05 ungültige Bilddaten → None", _broken_bytes)


# ─────────────────────────────────────────────────────────────────────────
# Block E — product_opener: canonicalize / hash / should_open / chrome
# ─────────────────────────────────────────────────────────────────────────
def test_block_opener(r: Result) -> None:
    section("E) product_opener")

    C = product_opener.canonicalize_amazon_url
    _case(r, "E01 /dp/ASIN bleibt erhalten",
          lambda: assert_eq(C("https://www.amazon.de/dp/B0ABC12345"),
                            "https://www.amazon.de/dp/B0ABC12345"))
    _case(r, "E02 /gp/product/ → /dp/",
          lambda: assert_eq(C("https://www.amazon.de/gp/product/B0ABC12345/ref=foo"),
                            "https://www.amazon.de/dp/B0ABC12345"))
    _case(r, "E03 Query-Strings entfernt",
          lambda: assert_eq(C("https://www.amazon.de/dp/B0ABC12345?tag=test-21&ref=xy"),
                            "https://www.amazon.de/dp/B0ABC12345"))
    _case(r, "E04 host wird lowercased",
          lambda: assert_eq(C("https://WWW.AMAZON.DE/dp/B0ABC12345").split("/dp/")[0],
                            "https://www.amazon.de"))
    _case(r, "E05 ohne ASIN: Pfad bleibt",
          lambda: assert_in("/some/page",
                            C("https://x.de/some/page?q=1")))
    _case(r, "E06 None-safe",
          lambda: assert_eq(C(""), ""))

    H = product_opener.compute_meta_hash
    _case(r, "E07 hash deterministisch",
          lambda: assert_eq(H({"a": 1, "b": 2}), H({"b": 2, "a": 1})))
    _case(r, "E08 hash unterschiedlich bei Änderung",
          lambda: assert_true(H({"a": 1}) != H({"a": 2})))
    _case(r, "E09 hash leeres dict ok",
          lambda: assert_eq(len(H({})), 32))

    A = product_opener.add_trigger_param
    _case(r, "E10 trigger param hinzugefügt",
          lambda: assert_in("ext_trigger=send_html",
                            A("https://www.amazon.de/dp/B0ABC12345")))
    _case(r, "E11 trigger ersetzt vorhandenen Wert",
          lambda: assert_in("ext_trigger=send_html",
                            A("https://x.de/dp/B0?ext_trigger=foo")))

    # should_open — TTL-Logik
    import time as _t
    def _should_open_fresh():
        opened = {}
        ok, _ = product_opener.should_open(
            "B0ABC12345", "https://www.amazon.de/dp/B0ABC12345",
            {"v": 1}, opened)
        assert_true(ok)
    _case(r, "E12 should_open: neuer ASIN → open", _should_open_fresh)

    def _should_open_recent_same_url():
        now = _t.time()
        opened = {
            "B0ABC12345": {
                "last_open": now,
                "meta_hash": product_opener.compute_meta_hash({"v": 1}),
                "canonical_url": "https://www.amazon.de/dp/B0ABC12345",
            }
        }
        ok, _ = product_opener.should_open(
            "B0ABC12345", "https://www.amazon.de/dp/B0ABC12345",
            {"v": 1}, opened)
        assert_false(ok, "should skip recent same URL")
    _case(r, "E13 should_open: kürzlich gleiche URL → skip",
          _should_open_recent_same_url)

    def _should_open_expired():
        old = _t.time() - product_opener.SKIP_TTL_SECONDS - 100
        opened = {
            "B0ABC12345": {
                "last_open": old,
                "meta_hash": product_opener.compute_meta_hash({"v": 1}),
                "canonical_url": "https://www.amazon.de/dp/B0ABC12345",
            }
        }
        ok, _ = product_opener.should_open(
            "B0ABC12345", "https://www.amazon.de/dp/B0ABC12345",
            {"v": 1}, opened)
        assert_true(ok)
    _case(r, "E14 should_open: nach TTL → open", _should_open_expired)

    # open_in_chrome im DRY_RUN
    def _chrome_dry():
        # DRY_RUN env wurde global gesetzt, aber product_opener cached den Wert
        orig_dry = product_opener.DRY_RUN
        product_opener.DRY_RUN = True
        try:
            assert_true(product_opener.open_in_chrome("https://x.de/dp/B0"))
        finally:
            product_opener.DRY_RUN = orig_dry
    _case(r, "E15 open_in_chrome DRY_RUN=True → ok ohne Subprocess",
          _chrome_dry)

    # open_in_chrome mit gemocktem subprocess
    def _chrome_real_call():
        from core.workers import chrome_launcher
        orig_dry = product_opener.DRY_RUN
        product_opener.DRY_RUN = False
        try:
            with patch.object(chrome_launcher.subprocess, "Popen") as popen:
                product_opener.open_in_chrome("https://x.de/dp/B0")
                assert_true(popen.called, "Popen should be called")
                args = popen.call_args[0][0]
                assert_true(any("--new-tab" in a for a in args),
                            "--new-tab in args")
                assert_in("https://x.de/dp/B0", args)
        finally:
            product_opener.DRY_RUN = orig_dry
    _case(r, "E16 open_in_chrome mock Popen → korrekte Args",
          _chrome_real_call)


# ─────────────────────────────────────────────────────────────────────────
# Block F — parser_worker: product_key, _is_visible_row, merge_product
# ─────────────────────────────────────────────────────────────────────────
def test_block_parser_worker(r: Result) -> None:
    section("F) parser_worker (Schlüssel/Merge)")

    PK = parser_worker.product_key
    _case(r, "F01 product_key ASIN-Priorität",
          lambda: assert_eq(
              PK({"asin": "B0ABC12345", "product_url": "https://x/y"}),
              "B0ABC12345"))
    _case(r, "F02 product_key URL-Fallback (lowercase)",
          lambda: assert_eq(
              PK({"asin": "", "product_url": "HTTPS://X.de/Foo"}),
              "https://x.de/foo"))
    _case(r, "F03 product_key SHA1-Fallback",
          lambda: assert_eq(
              len(PK({"asin": "", "product_url": "",
                      "product_name": "Foo Bar",
                      "price": {"value": 9.99}})),
              16))
    _case(r, "F04 product_key invalid ASIN → URL",
          lambda: assert_eq(
              PK({"asin": "x", "product_url": "HTTPS://X/y"}),
              "https://x/y"))

    V = parser_worker._is_visible_row
    _case(r, "F05 visible: nur name", lambda: assert_true(V({"product_name": "Foo"})))
    _case(r, "F06 visible: nur url", lambda: assert_true(V({"product_url": "u"})))
    _case(r, "F07 visible: beides leer → False",
          lambda: assert_false(V({"product_name": "", "product_url": "  "})))

    # merge_product
    def _merge_new():
        store = {}
        key, is_new = parser_worker.merge_product(store, {
            "asin": "B0ABC12345",
            "product_name": "Foo",
            "product_url": "https://x/y",
            "price": {"value": 9.99},
            "discount_percent": 10,
        })
        assert_eq(key, "B0ABC12345")
        assert_true(is_new)
        assert_in("_first_seen", store[key])
        assert_in("_last_seen", store[key])
        assert_eq(len(store[key]["_history"]), 1)
    _case(r, "F08 merge_product neu", _merge_new)

    def _merge_update():
        store = {}
        parser_worker.merge_product(store, {"asin": "B0ABC12345",
                                            "product_name": "Foo",
                                            "price": {"value": 9.99}})
        key, is_new = parser_worker.merge_product(store, {"asin": "B0ABC12345",
                                                          "product_name": "Foo",
                                                          "price": {"value": 7.99}})
        assert_eq(key, "B0ABC12345")
        assert_false(is_new, "should be update")
        assert_eq(store[key]["price"]["value"], 7.99)
        assert_eq(len(store[key]["_history"]), 2)
    _case(r, "F09 merge_product update", _merge_update)

    def _history_truncate():
        store = {}
        for i in range(10):
            parser_worker.merge_product(store, {"asin": "B0ABC12345",
                                                "product_name": "Foo",
                                                "price": {"value": i + 1.0}})
        assert_true(len(store["B0ABC12345"]["_history"]) <= 5,
                    "history truncated to 5")
    _case(r, "F10 merge_product history max 5", _history_truncate)

    # _normalize_row
    N = parser_worker._normalize_row
    _case(r, "F11 _normalize_row mappt Felder",
          lambda: assert_eq(
              N({"asin": "B0ABC12345", "product_name": "Foo",
                 "product_url": "https://x/y",
                 "price": {"value": 9.99},
                 "discount_percent": 10}, "src.html")["_source_file"],
              "src.html"))


# ─────────────────────────────────────────────────────────────────────────
# Block G — amzon_dealsList_parser: clean_price, extract_asin, parse_deals
# ─────────────────────────────────────────────────────────────────────────
def test_block_deals_parser(r: Result) -> None:
    section("G) amzon_dealsList_parser")

    CP = deals_parser.clean_price
    _case(r, "G01 clean_price '99,99 €' → 99.99",
          lambda: assert_eq(CP("99,99 €")["value"], 99.99))
    _case(r, "G02 clean_price '1.234,56 €' → 1234.56",
          lambda: assert_eq(CP("1.234,56 €")["value"], 1234.56))
    _case(r, "G03 clean_price '€ 9,99' → 9.99",
          lambda: assert_eq(CP("€ 9,99")["value"], 9.99))
    # clean_price ist auf deutsche Locale (amazon.de) ausgelegt:
    # '.' = Tausendertrenner. '99.99' wird daher als 9999 interpretiert.
    _case(r, "G04 clean_price '99.99' → 9999.0 (DE-locale)",
          lambda: assert_eq(CP("99.99")["value"], 9999.0))
    _case(r, "G05 clean_price 'kaputt' → None",
          lambda: assert_eq(CP("nichts hier"), None))
    _case(r, "G06 clean_price '' → None",
          lambda: assert_eq(CP(""), None))

    EA = deals_parser.extract_asin_from_url
    _case(r, "G07 ASIN aus /dp/",
          lambda: assert_eq(EA("https://www.amazon.de/dp/B0ABC12345/ref=x"),
                            "B0ABC12345"))
    _case(r, "G08 ASIN aus /gp/product/",
          lambda: assert_eq(EA("https://www.amazon.de/gp/product/B0ABC12345"),
                            "B0ABC12345"))
    _case(r, "G09 ASIN aus Query ?asin=",
          lambda: assert_eq(EA("https://x.de/foo?asin=B0ABC12345"),
                            "B0ABC12345"))
    _case(r, "G10 keine ASIN → None",
          lambda: assert_eq(EA("https://x.de/foo/bar"), None))
    _case(r, "G11 None-safe",
          lambda: assert_eq(EA(None), None))

    # parse_deals_from_html mit synth. HTML
    html = """
    <html><body>
      <div data-testid='grid-deal-card' data-asin='B0ABC12345'>
        <a href='/dp/B0ABC12345/ref=xy'>Toller Artikel</a>
        <span class='a-price'><span class='a-offscreen'>19,99 €</span></span>
        <span class='a-price'><span class='a-offscreen'>39,99 €</span></span>
        <span>-50% Rabatt</span>
      </div>
    </body></html>
    """
    def _parse_synth():
        rows = deals_parser.parse_deals_from_html(html)
        assert_eq(len(rows), 1, "1 card")
        assert_eq(rows[0]["asin"], "B0ABC12345")
        assert_eq(rows[0]["price"]["value"], 19.99)
        assert_eq(rows[0]["discount_percent"], 50)
    _case(r, "G12 parse synth-HTML → 1 deal", _parse_synth)

    # Real fixture (test/test.html falls vorhanden)
    fixture = HERE / "test.html"
    if fixture.exists():
        def _parse_fixture():
            raw = fixture.read_text(encoding="utf-8", errors="ignore")
            rows = deals_parser.parse_deals_from_html(raw)
            # Wir verlangen nur, dass keine Exception fliegt; Anzahl variabel
            assert_true(isinstance(rows, list))
        _case(r, "G13 parse real fixture test.html", _parse_fixture)
    else:
        r.skip("G13 parse real fixture test.html", "test.html nicht vorhanden")


# ─────────────────────────────────────────────────────────────────────────
# Block H — fb_message.create_facebook_message
# ─────────────────────────────────────────────────────────────────────────
def test_block_fb_message(r: Result) -> None:
    section("H) fb_message.create_facebook_message")

    F = fb_message.create_facebook_message

    # Reel-Variante
    msg_reel = F({"type": "reel", "hashtags": ["#Foo", "#Bar"]})
    _case(r, "H01 reel: Kommentar-Hinweis",
          lambda: assert_in("Kommentaren", msg_reel))
    _case(r, "H02 reel: Hashtags",
          lambda: assert_in("#Foo", msg_reel))
    msg_reel_default = F({"type": "reel"})
    _case(r, "H03 reel: Default-Hashtags wenn leer",
          lambda: assert_in("#Angebot", msg_reel_default))

    # Post-Variante
    msg = F(good_deal())
    _case(r, "H04 post: Titel vorhanden",
          lambda: assert_in("Anker", msg))
    _case(r, "H05 post: Preis 39,99 €",
          lambda: assert_in("39,99", msg))
    _case(r, "H06 post: Originalpreis enthalten",
          lambda: assert_in("79,99", msg))
    _case(r, "H07 post: Discount-Text",
          lambda: assert_in("Rabatt", msg))
    _case(r, "H08 post: Default-Hashtag wenn keine",
          lambda: assert_in("#Angebot", F({**good_deal(), "hashtags": []})))

    # N/A-Filter (sollte nicht im Text auftauchen)
    msg_na = F({**good_deal(), "discount_percent": "N/A"})
    _case(r, "H09 post: 'N/A' Rabatt nicht angezeigt",
          lambda: assert_not_in("N/A", msg_na))

    # Coupon
    msg_coup = F({**good_deal(), "coupon": {"code": "ABC123"}})
    _case(r, "H10 post: Coupon-Code dargestellt",
          lambda: assert_in("ABC123", msg_coup))

    # Stars werden gestrippt
    msg_star = F({**good_deal(), "title": "**super** Titel"})
    _case(r, "H11 post: ** entfernt",
          lambda: assert_not_in("**", msg_star))


# ─────────────────────────────────────────────────────────────────────────
# Block I — offer_message.build_caption_html
# ─────────────────────────────────────────────────────────────────────────
def test_block_offer_caption(r: Result) -> None:
    section("I) offer_message.build_caption_html")

    # _badge-Tiers
    B = offer_message._badge
    _case(r, "I01 badge ≥50% → PREISSTURZ",
          lambda: assert_in("PREISSTURZ", B({"discount_percent": "-55%"})))
    _case(r, "I02 badge ≥35% & <50% → TOP-DEAL",
          lambda: assert_in("TOP-DEAL", B({"discount_percent": "-40%"})))
    _case(r, "I03 badge ≥20% → Gutes Angebot",
          lambda: assert_in("Gutes", B({"discount_percent": "-25%"})))
    _case(r, "I04 badge <20% → None",
          lambda: assert_eq(B({"discount_percent": "-10%"}), None))
    _case(r, "I05 badge ohne Discount → None",
          lambda: assert_eq(B({}), None))

    # _stars
    S = offer_message._stars
    _case(r, "I06 stars: 4.5 → Half-Star",
          lambda: assert_in("✩", S(4.5, 100)))
    _case(r, "I07 stars: keine value → leer",
          lambda: assert_eq(S(None, 5), ""))
    _case(r, "I08 stars: Count formatiert (1 000)",
          lambda: assert_in("1 234", S(4.0, 1234)))

    # _as_number
    AS = offer_message._as_number
    _case(r, "I09 _as_number '-22%' → 22 (abs in get_discount)",
          lambda: assert_eq(AS("-22%"), -22.0))
    _case(r, "I10 _as_number '22,5' → 22.5",
          lambda: assert_eq(AS("22,5"), 22.5))

    # build_caption_html – Vollausgabe (Preis muss STRING sein!)
    d = {
        "title": "Test <Produkt> & co",
        "price": "19,99 €",
        "original_price": "39,99 €",
        "discount_percent": "-50%",
        "affiliate_url": "https://amzn.to/x",
        "rabatt_text": "🔥 Mega-Deal",
        "market": "AMAZON",
        "rating": {"value": 4.5, "counts": 1234},
        "availability": "Auf Lager",
    }
    cap = offer_message.build_caption_html(d, affiliate_fallback="https://amzn.to/fb")
    _case(r, "I11 caption: HTML-escape von <Produkt>",
          lambda: assert_in("&lt;Produkt&gt;", cap))
    _case(r, "I12 caption: Preis enthalten",
          lambda: assert_in("19,99", cap))
    _case(r, "I13 caption: original_price <s>",
          lambda: assert_in("<s>", cap))
    _case(r, "I14 caption: Discount-Anteil",
          lambda: assert_in("-50%", cap))
    _case(r, "I15 caption: CTA-Link",
          lambda: assert_in("DIREKT ZUM ANGEBOT", cap))
    _case(r, "I16 caption: Marktplatz",
          lambda: assert_in("AMAZON", cap))
    _case(r, "I17 caption: Bewertungs-Sterne",
          lambda: assert_in("4.5/5", cap))


# ─────────────────────────────────────────────────────────────────────────
# Block J — ws_server: inject_affiliate_link_meta / choose_target_path / safe
# ─────────────────────────────────────────────────────────────────────────
def test_block_ws_server(r: Result) -> None:
    section("J) ws_server (Streaming-Server Helpers)")

    # safe()
    _case(r, "J01 safe entfernt Sonderzeichen",
          lambda: assert_eq(
              ws_server.safe("https://x.de/foo/bar?q=1"),
              "https_x.de_foo_bar_q_1"))
    _case(r, "J02 safe leerer Input",
          lambda: assert_eq(ws_server.safe(""), "page"))
    _case(r, "J03 safe gekürzt auf 120 Zeichen",
          lambda: assert_true(len(ws_server.safe("x" * 500)) <= 120))

    # canonical_url
    _case(r, "J04 canonical_url strips query",
          lambda: assert_eq(
              ws_server.canonical_url("https://x.de/y?a=1#frag"),
              "https://x.de/y"))
    _case(r, "J05 canonical_url None-safe",
          lambda: assert_eq(ws_server.canonical_url(None), "/"))

    # inject_affiliate_link_meta
    html = "<html><head><title>x</title></head><body></body></html>"
    out = ws_server.inject_affiliate_link_meta(html, "https://amzn.to/abc")
    _case(r, "J06 meta tag im head",
          lambda: assert_in('name="x-affiliate-link"', out))
    _case(r, "J07 meta vor </head>",
          lambda: assert_true(out.index("x-affiliate-link") < out.index("</head>")))
    _case(r, "J08 leerer link → unverändert",
          lambda: assert_eq(
              ws_server.inject_affiliate_link_meta(html, ""), html))
    _case(r, "J09 ungültige URL → unverändert",
          lambda: assert_eq(
              ws_server.inject_affiliate_link_meta(html, "not-a-url"),
              html))

    # choose_target_path
    p_prod = ws_server.choose_target_path(
        "https://x.de/y", "12345", "product")
    p_inbox = ws_server.choose_target_path(
        "https://x.de/y", "12345", None)
    _case(r, "J10 docType=product → PRODUCKT_DIR",
          lambda: assert_true(str(p_prod).endswith(".html")
                              and "produckt" in str(p_prod).lower()))
    _case(r, "J11 docType=None → INBOX_DIR",
          lambda: assert_true(str(p_inbox).endswith(".html")
                              and "inbox" in str(p_inbox).lower()))


# ─────────────────────────────────────────────────────────────────────────
# Block K — amzn_utils: parse_price_string / is_amazon_html
# ─────────────────────────────────────────────────────────────────────────
def test_block_utils(r: Result) -> None:
    section("K) amazon/utils.parse_price_string + is_amazon_html")

    P = amzn_utils.parse_price_string
    _case(r, "K01 '399,99 €' → 399.99 EUR",
          lambda: (assert_eq(P("399,99 €")["value"], 399.99),
                   assert_eq(P("399,99 €")["currency_hint"], "€")))
    _case(r, "K02 '1.234,56 €' → 1234.56",
          lambda: assert_eq(P("1.234,56 €")["value"], 1234.56))
    _case(r, "K03 'N/A' → None",
          lambda: assert_eq(P("N/A")["value"], None))
    _case(r, "K04 '0' → None",
          lambda: assert_eq(P("0")["value"], None))
    _case(r, "K05 '' → None",
          lambda: assert_eq(P("")["value"], None))
    _case(r, "K06 '99.99 $' → 99.99",
          lambda: assert_eq(P("99.99 $")["value"], 99.99))
    _case(r, "K07 currency_hint $",
          lambda: assert_eq(P("99.99 $")["currency_hint"], "$"))

    IH = amzn_utils.is_amazon_html
    _case(r, "K08 productTitle erkannt",
          lambda: assert_true(IH('xxx id="productTitle" yyy')))
    _case(r, "K09 data-asin erkannt",
          lambda: assert_true(IH('<div data-asin="B0X">')))
    _case(r, "K10 negativ-Beispiel",
          lambda: assert_false(IH("<html><body>nothing</body></html>")))


# ─────────────────────────────────────────────────────────────────────────
# Block L — AI Extractor (Vertex AI gemockt)
# ─────────────────────────────────────────────────────────────────────────
def test_block_ai_extractor(r: Result) -> None:
    section("L) ai_parser.ai_extractor (mocked Vertex AI)")

    from core.workers.ai_parser import ai_extractor

    # Fake-Antwort, die alle Pflichtfelder von Produktinformation enthält
    fake_json = json.dumps({
        "produkt_titel": "Anker USB-C 240W",
        "marke": "Anker",
        "akt_preis": "39,99 €",
        "original_preis": "79,99 €",
        "rabatt_prozent": "-50%",
        "marktplatz": "Amazon",
        "produkt_id": "B0ABC12345",
        "hauptprodukt_bilder": ["https://m/x.jpg"],
        "url_des_produkts": "https://www.amazon.de/dp/B0ABC12345",
        "bewertung_wert": 4.5,
        "anzahl_reviews": 1234,
        "anzahl_verkauft": "Über 1000 verkauft",
        "haendler_verkaeufer": "Amazon",
        "verfuegbarkeit": "Auf Lager",
        "lieferinformation": "Versand morgen",
        "gutschein_code": "N/A",
        "gutschein_details": "N/A",
        "rabatt_text": "🔥 Mega-Deal",
        "reel_titel": "Anker USB-C 240W",
        "reel_beschreibung": "Schnellste USB-C Ladung",
        "reel_caption": "🔥 Mega-Deal!\nSpare 40 €",
        "hashtags": ["#angebot", "#deal", "#anker"],
        "voiceover_text": "Heute spare 50 Prozent auf dein neues Anker Ladegeraet.",
        "produkt_kategorie": "elektronik",
        "template_type": "typ3_audio",
    })

    fake_response = MagicMock()
    fake_response.text = fake_json
    fake_model = MagicMock()
    fake_model.generate_content.return_value = fake_response

    def _extract_ok():
        pack = {"model": fake_model, "config": MagicMock()}
        out = ai_extractor.extrahiere_produktsignale(
            "Anker Ladegeraet 240W", "https://m/x.jpg", pack)
        assert_eq(out["akt_preis"], "39,99 €")
        assert_eq(out["produkt_id"], "B0ABC12345")
        assert_eq(out["template_type"], "typ3_audio")
        assert_true(len(out["reel_titel"]) <= 22, "reel_titel ≤22")
    _case(r, "L01 mocked extract: valid response", _extract_ok)

    def _retry_on_overload():
        from google.api_core import exceptions as gex
        m = MagicMock()
        # 1× ResourceExhausted, dann ok
        m.generate_content.side_effect = [
            gex.ResourceExhausted("rate-limit"),
            fake_response,
        ]
        pack = {"model": m, "config": MagicMock()}
        # time.sleep monkeypatchen, damit der Test nicht 2s dauert
        with patch.object(ai_extractor.time, "sleep"):
            out = ai_extractor.extrahiere_produktsignale("x", "y", pack)
        assert_eq(out["produkt_titel"], "Anker USB-C 240W")
        assert_eq(m.generate_content.call_count, 2)
    _case(r, "L02 retry on ResourceExhausted", _retry_on_overload)

    def _save_extracted():
        out = _TMP_DIR / "ai_out.json"
        with patch.object(ai_extractor, "baue_pattern_pack",
                          return_value={"model": fake_model, "config": MagicMock()}):
            ai_extractor.extract_and_save_data(
                {"clean_text": "x", "bild_kandidaten": "y",
                 "source_file": "src", "product_title": "t"},
                out)
        assert_true(out.exists())
        data = json.loads(out.read_text(encoding="utf-8"))
        assert_eq(data["extracted_data"]["produkt_id"], "B0ABC12345")
    _case(r, "L03 extract_and_save_data schreibt JSON", _save_extracted)

    def _empty_clean_text():
        out = _TMP_DIR / "ai_empty.json"
        ai_extractor.extract_and_save_data(
            {"clean_text": "", "bild_kandidaten": "x"}, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert_in("Fehler", data["extracted_data"])
    _case(r, "L04 leerer clean_text → Fehler-Marker", _empty_clean_text)


# ─────────────────────────────────────────────────────────────────────────
# Block M — Integration: DB-Lifecycle + Validate-Gate + send_post-Mock
# ─────────────────────────────────────────────────────────────────────────
def test_block_integration(r: Result) -> None:
    section("M) Integration: process_single_deal (mocked fb_service)")

    reset_db()
    sent_ids: set[str] = set()

    fake_svc = MagicMock()
    fake_svc.send_post = AsyncMock(return_value=True)

    # 1) Valider Deal: wird gesendet + mark_sent
    def _valid_flow():
        payload = good_deal()
        deals_repo.enqueue("PID_VALID_1", payload)
        # mock download_image um keine HTTP-Calls zu machen
        with patch.object(fb_processor, "download_image",
                          return_value=None):
            deal = deals_repo.get_by_product_id("PID_VALID_1")
            ok = asyncio.run(
                fb_processor.process_single_deal(deal, sent_ids, fake_svc))
        assert_true(ok, "process_single_deal must return True")
        assert_in("PID_VALID_1", sent_ids)
        deal2 = deals_repo.get_by_product_id("PID_VALID_1")
        assert_eq(deal2["status"], DEAL_STATUS_SENT)
    _case(r, "M01 valider Deal → status=sent", _valid_flow)

    # 2) Deal mit N/A-Preis → wird abgelehnt, mark_failed
    def _na_price_flow():
        payload = good_deal(price={"raw": "N/A"})
        deals_repo.enqueue("PID_NA_1", payload)
        deal = deals_repo.get_by_product_id("PID_NA_1")
        with patch.object(fb_processor, "download_image", return_value=None):
            ok = asyncio.run(
                fb_processor.process_single_deal(deal, set(), fake_svc))
        assert_false(ok, "N/A price must NOT be sent")
        deal2 = deals_repo.get_by_product_id("PID_NA_1")
        assert_eq(deal2["status"], DEAL_STATUS_FAILED)
        # Detail-Begründung steht in Events (deals_repo speichert error_message
        # auf der Deal-Row, aber _to_dict exponiert es nicht). Wir prüfen die Events.
        events = deals_repo.get_events(deal2["id"])
        joined = " ".join(str(e.get("detail") or "") for e in events)
        assert_in("Preis", joined)
    _case(r, "M02 N/A-Preis → status=failed, NICHT gepostet", _na_price_flow)

    # 3) Deal ohne Bild
    def _no_image_flow():
        payload = good_deal(image_url=None, images=[])
        deals_repo.enqueue("PID_NOIMG_1", payload)
        deal = deals_repo.get_by_product_id("PID_NOIMG_1")
        with patch.object(fb_processor, "download_image", return_value=None):
            ok = asyncio.run(
                fb_processor.process_single_deal(deal, set(), fake_svc))
        assert_false(ok, "no image must NOT be sent")
        deal2 = deals_repo.get_by_product_id("PID_NOIMG_1")
        assert_eq(deal2["status"], DEAL_STATUS_FAILED)
    _case(r, "M03 kein Bild → status=failed", _no_image_flow)

    # 4) send_post liefert False → bleibt in Queue, NICHT mark_sent
    def _send_fail_flow():
        payload = good_deal()
        deals_repo.enqueue("PID_SENDFAIL_1", payload)
        deal = deals_repo.get_by_product_id("PID_SENDFAIL_1")
        svc = MagicMock()
        svc.send_post = AsyncMock(return_value=False)
        with patch.object(fb_processor, "download_image", return_value=None):
            ok = asyncio.run(
                fb_processor.process_single_deal(deal, set(), svc))
        assert_false(ok)
        deal2 = deals_repo.get_by_product_id("PID_SENDFAIL_1")
        # Status soll nicht 'sent' werden
        assert_true(deal2["status"] != DEAL_STATUS_SENT)
    _case(r, "M04 send_post=False → kein mark_sent", _send_fail_flow)

    # 5) Doppelter product_id → skip via sent_ids
    def _dedup_flow():
        payload = good_deal()
        deals_repo.enqueue("PID_DUP_1", payload)
        deal = deals_repo.get_by_product_id("PID_DUP_1")
        local_sent: set[str] = {"PID_DUP_1"}
        with patch.object(fb_processor, "download_image", return_value=None):
            ok = asyncio.run(
                fb_processor.process_single_deal(deal, local_sent, fake_svc))
        assert_false(ok, "already-sent must skip")
    _case(r, "M05 bereits gesendet → skip", _dedup_flow)


# ─────────────────────────────────────────────────────────────────────────
# Block N — State-KV (Korrektheit der JSON-Spalten-Updates)
# ─────────────────────────────────────────────────────────────────────────
def test_block_state_kv(r: Result) -> None:
    section("N) state_repo (KV-Updates auf JSON-Spalte)")

    reset_db()

    def _set_get():
        state_repo.put("k1", {"a": 1})
        assert_eq(state_repo.get_dict("k1"), {"a": 1})
    _case(r, "N01 set_dict / get_dict", _set_get)

    def _update_dict_merges():
        state_repo.put("k2", {"x": 1})
        state_repo.update_dict("k2", {"y": 2})
        got = state_repo.get_dict("k2")
        assert_eq(got, {"x": 1, "y": 2})
    _case(r, "N02 update_dict merged keys", _update_dict_merges)

    def _update_dict_overwrites():
        state_repo.put("k3", {"a": 1})
        state_repo.update_dict("k3", {"a": 99})
        assert_eq(state_repo.get_dict("k3"), {"a": 99})
    _case(r, "N03 update_dict overwrites", _update_dict_overwrites)

    def _get_missing():
        assert_eq(state_repo.get_dict("missing-key-xyz"), {})
    _case(r, "N04 get_dict missing → {}", _get_missing)


# ─────────────────────────────────────────────────────────────────────────
# Block O — Deals-Repo Phasen (für Dashboard-Stepper)
# ─────────────────────────────────────────────────────────────────────────
def test_block_phases(r: Result) -> None:
    section("O) deals_repo.get_phases")

    reset_db()
    deals_repo.enqueue("PID_PH_1", good_deal())
    deal = deals_repo.get_by_product_id("PID_PH_1")

    def _initial():
        phases = deals_repo.get_phases(deal["id"])
        # ingested fertig, enriched aktiv (oder pending), Rest pending
        keys = [p["key"] for p in phases]
        assert_eq(keys, ["ingested", "enriched", "claimed", "delivered"])
        assert_eq(phases[0]["state"], "done")
    _case(r, "O01 initial: ingested=done", _initial)

    def _after_claim():
        from core.db.workers_repo import register, set_idle
        register("amzn_test")
        claimed = deals_repo.claim_next("amzn_test")
        assert_true(claimed is not None)
        phases = deals_repo.get_phases(deal["id"])
        states = {p["key"]: p["state"] for p in phases}
        assert_eq(states["claimed"], "done")
    _case(r, "O02 nach claim: claimed=done", _after_claim)

    def _after_send():
        deals_repo.mark_sent(deal["id"], detail="facebook")
        phases = deals_repo.get_phases(deal["id"])
        states = {p["key"]: p["state"] for p in phases}
        assert_eq(states["delivered"], "done")
    _case(r, "O03 nach mark_sent: delivered=done", _after_send)

    def _failed_phase():
        deals_repo.enqueue("PID_PH_2", good_deal())
        d2 = deals_repo.get_by_product_id("PID_PH_2")
        deals_repo.mark_failed(d2["id"], "broken")
        phases = deals_repo.get_phases(d2["id"])
        # Mindestens 1 Phase muss 'failed' sein
        states = [p["state"] for p in phases]
        assert_in("failed", states)
    _case(r, "O04 mark_failed setzt failed-Phase", _failed_phase)


# ─────────────────────────────────────────────────────────────────────────
# Block P — LIVE (--live): echter Chrome-Start + echter Bild-Download
# ─────────────────────────────────────────────────────────────────────────
def test_block_live(r: Result, enabled: bool) -> None:
    section("P) LIVE-Tests (--live)")
    if not enabled:
        r.skip("P01 Chrome real launch", "ohne --live")
        r.skip("P02 echter Bild-Download (WebP)", "ohne --live")
        return

    # Chrome wirklich starten (DRY_RUN=0)
    def _chrome_live():
        chrome_bin = product_opener.CHROME_BIN
        if not shutil.which(chrome_bin) and not Path(chrome_bin).exists():
            raise AssertionError(f"Chrome nicht installiert: {chrome_bin}")
        orig = product_opener.DRY_RUN
        product_opener.DRY_RUN = False
        try:
            ok = product_opener.open_in_chrome(
                "about:blank")
            assert_true(ok, "Chrome subprocess failed")
        finally:
            product_opener.DRY_RUN = orig
    _case(r, "P01 Chrome real launch (about:blank)", _chrome_live)

    # Echter Bild-Download (kleines, stabiles PNG → Pillow)
    def _real_download():
        url = "https://httpbin.org/image/webp"
        out = asyncio.run(image_processor.download_and_convert_to_jpg(url))
        if out is None:
            raise AssertionError("Download/Konvertierung fehlgeschlagen "
                                 "(Netz? httpbin.org down?)")
        assert_true(out.exists() and out.stat().st_size > 100)
        out.unlink(missing_ok=True)
    _case(r, "P02 echter Bild-Download (WebP→JPG)", _real_download)


# ─────────────────────────────────────────────────────────────────────────
# Block Q — EXTERNAL (--external): echter Vertex-AI-Call
# ─────────────────────────────────────────────────────────────────────────
def test_block_external(r: Result, enabled: bool) -> None:
    section("Q) EXTERNAL-Tests (--external)")
    if not enabled:
        r.skip("Q01 echter Vertex AI smoke-call", "ohne --external")
        return

    def _vertex_call():
        from core.workers.ai_parser import ai_extractor
        pack = ai_extractor.baue_pattern_pack()
        out = ai_extractor.extrahiere_produktsignale(
            unstrukturierter_text=(
                "Anker USB-C Ladegerät 240W, 4 Ports, schwarz. "
                "Preis: 39,99 € statt 79,99 € (-50%). ASIN: B0ABC12345"),
            bild_kandidaten_str="https://m.media-amazon.com/images/I/abc.jpg",
            pack=pack,
        )
        assert_true(isinstance(out, dict))
        assert_in("produkt_titel", out)
        assert_in("akt_preis", out)
    _case(r, "Q01 echter Vertex AI smoke-call", _vertex_call)


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────
def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="Startet echten Chrome + echte HTTP-Downloads")
    ap.add_argument("--external", action="store_true",
                    help="Macht 1 echten Vertex-AI-Call (kostet Quota!)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print(f"Test-DB: {_TEST_DB}")
    reset_db()

    r = Result()
    test_block_fb_validate(r)
    test_block_reels_validate(r)
    test_block_image_url(r)
    test_block_image_download(r)
    test_block_opener(r)
    test_block_parser_worker(r)
    test_block_deals_parser(r)
    test_block_fb_message(r)
    test_block_offer_caption(r)
    test_block_ws_server(r)
    test_block_utils(r)
    test_block_ai_extractor(r)
    test_block_integration(r)
    test_block_state_kv(r)
    test_block_phases(r)
    test_block_live(r, args.live)
    test_block_external(r, args.external)

    code = r.summary()
    # Aufräumen
    try:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    except Exception:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
