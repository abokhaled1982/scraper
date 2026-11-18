"""Frame-accurate cut points: never inside a word, preferably in the quietest spot of the surrounding gap."""

from __future__ import annotations

import numpy as np

from .models import Word


def _quietest(env_db: np.ndarray, lo: float, hi: float, prefer: float, hop: float = 0.01) -> float:
    if hi - lo < 2 * hop:
        return min(max(prefer, lo), hi)
    i0, i1 = int(lo / hop), int(hi / hop)
    seg = env_db[i0:i1]
    if not len(seg):
        return prefer
    # Quietness dominates; a mild pull towards `prefer` breaks ties between equally silent frames.
    dist = np.abs(np.arange(i0, i0 + len(seg)) * hop - prefer)
    cost = seg + 0.5 * dist / max(hi - lo, hop)
    return (i0 + int(np.argmin(cost))) * hop


def snap_bounds(
    words: list[Word],
    first: int,
    last: int,
    env_db: np.ndarray,
    duration: float,
    pad_start: float,
    pad_end: float,
) -> tuple[float, float]:
    w0, w1 = words[first], words[last]
    prev_end = words[first - 1].end if first > 0 else 0.0
    next_start = words[last + 1].start if last + 1 < len(words) else duration

    # Start: somewhere in the gap before the first word, at most pad_start early.
    lo = max(prev_end + 0.02, w0.start - pad_start)
    hi = w0.start - 0.01
    start = _quietest(env_db, lo, hi, prefer=hi) if hi > lo else max(0.0, (prev_end + w0.start) / 2)

    # End: in the gap after the last word, letting the final syllable ring out.
    lo = w1.end + 0.03
    hi = min(next_start - 0.03, w1.end + pad_end)
    end = _quietest(env_db, lo, hi, prefer=hi) if hi > lo else min(duration, (w1.end + next_start) / 2)

    return max(0.0, round(start, 3)), min(duration, round(max(end, start + 0.5), 3))
