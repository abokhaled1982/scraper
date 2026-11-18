"""Map aligner output (punctuation stripped, sometimes re-tokenised) back onto the original transcript tokens."""

from __future__ import annotations

import difflib
import re
import unicodedata

from .models import Word

_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")


def normalize(token: str) -> str:
    token = unicodedata.normalize("NFKC", token).lower()
    token = _ARABIC_DIACRITICS.sub("", token)
    token = token.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    return "".join(ch for ch in token if unicodedata.category(ch)[0] in "LN")


def tokenize(text: str) -> list[str]:
    """Whitespace tokens; punctuation-only tokens are glued to the previous token."""
    tokens: list[str] = []
    for raw in text.split():
        if not normalize(raw) and tokens:
            tokens[-1] += raw
        else:
            tokens.append(raw)
    if tokens and not normalize(tokens[0]):
        tokens = tokens[1:]
    return tokens


def attach_times(
    tokens: list[str],
    aligned: list[tuple[str, float, float]],
    span: tuple[float, float],
) -> list[Word]:
    """Give every original token a (start, end).

    `aligned` is the aligner output (text, start, end) in absolute seconds; `span` is the
    chunk time range used to interpolate tokens the aligner could not place.
    """
    n = len(tokens)
    starts: list[float | None] = [None] * n
    ends: list[float | None] = [None] * n

    norm_tokens = [normalize(t) for t in tokens]
    norm_aligned = [normalize(a[0]) for a in aligned]
    matcher = difflib.SequenceMatcher(None, norm_tokens, norm_aligned, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                starts[i1 + k] = aligned[j1 + k][1]
                ends[i1 + k] = aligned[j1 + k][2]
        elif tag == "replace":
            # Different tokenisation (e.g. aligner split a compound): spread the span by characters.
            t0, t1 = aligned[j1][1], aligned[j2 - 1][2]
            weights = [max(1, len(norm_tokens[i])) for i in range(i1, i2)]
            total = float(sum(weights))
            cursor = t0
            for i, w in zip(range(i1, i2), weights):
                step = (t1 - t0) * w / total
                starts[i], ends[i] = cursor, cursor + step
                cursor += step

    words = [Word(tok, s if s is not None else -1.0, e if e is not None else -1.0, s is not None) for tok, s, e in zip(tokens, starts, ends)]
    _interpolate_missing(words, span)
    _enforce_monotonic(words, span)
    return words


def _interpolate_missing(words: list[Word], span: tuple[float, float]) -> None:
    i, n = 0, len(words)
    while i < n:
        if words[i].aligned:
            i += 1
            continue
        j = i
        while j < n and not words[j].aligned:
            j += 1
        left = words[i - 1].end if i > 0 else span[0]
        right = words[j].start if j < n else span[1]
        if right < left:
            right = left
        weights = [max(1, len(normalize(w.text))) for w in words[i:j]]
        total = float(sum(weights))
        cursor = left
        for w, wt in zip(words[i:j], weights):
            step = (right - left) * wt / total
            w.start, w.end = cursor, cursor + step
            cursor += step
        i = j


def _enforce_monotonic(words: list[Word], span: tuple[float, float]) -> None:
    prev_end = span[0]
    for w in words:
        w.start = max(w.start, prev_end, span[0])
        w.end = min(max(w.end, w.start + 0.02), max(span[1], w.start + 0.02))
        prev_end = w.end


def interpolate_words(text: str, speech: list[tuple[float, float]], span: tuple[float, float]) -> list[Word]:
    """Last-resort timing when no aligner result exists: distribute tokens over speech regions by length."""
    tokens = tokenize(text)
    regions = [(max(a, span[0]), min(b, span[1])) for a, b in speech if b > span[0] and a < span[1]] or [span]
    total_speech = sum(b - a for a, b in regions) or 1e-6
    weights = [max(1, len(normalize(t))) for t in tokens]
    total_w = float(sum(weights)) or 1.0

    def at(fraction: float) -> float:
        remaining = fraction * total_speech
        for a, b in regions:
            if remaining <= b - a:
                return a + remaining
            remaining -= b - a
        return regions[-1][1]

    words, acc = [], 0.0
    for tok, wt in zip(tokens, weights):
        start = at(acc / total_w)
        acc += wt
        words.append(Word(tok, start, at(acc / total_w), aligned=False))
    return words
