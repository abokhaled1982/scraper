"""Animated burned-in captions (ASS / libass) + plain SRT export."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ClipperConfig, is_rtl
from .models import Word
from .segment import SENTENCE_END, ends_with

DEFAULT_FONTS = {"ar": "Noto Sans Arabic", "fa": "Noto Sans Arabic"}
LATIN_FONT = "Noto Sans"
STRIP_DISPLAY = ".,،;؛:…"
HOLD = 0.5  # seconds a caption may stay up after its last word


@dataclass
class Page:
    words: list[Word]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def clip_words(words: list[Word], start: float, end: float) -> list[Word]:
    """Words inside [start, end], re-timed relative to the clip start."""
    out = []
    for w in words:
        if w.end <= start + 0.02 or w.start >= end - 0.02:
            continue
        out.append(Word(w.text, max(0.0, w.start - start), min(end - start, w.end - start), w.aligned))
    return out


def paginate(words: list[Word], max_words: int, max_chars: int, max_gap: float = 0.45, max_dur: float = 2.4) -> list[Page]:
    pages: list[Page] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        if cur:
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            if len(cur) >= max_words or chars > max_chars or w.start - cur[-1].end > max_gap or w.end - cur[0].start > max_dur:
                pages.append(Page(cur))
                cur = []
        cur.append(w)
        if ends_with(w.text, SENTENCE_END):
            pages.append(Page(cur))
            cur = []
    if cur:
        pages.append(Page(cur))
    return pages


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(text: str, upper: bool) -> str:
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()
    stripped = text.rstrip(STRIP_DISPLAY)
    text = stripped or text
    return text.upper() if upper else text


def build_ass(words: list[Word], cfg: ClipperConfig, language: str | None, width: int, height: int, duration: float, title: str = "") -> str:
    rtl = is_rtl(language)
    scale = height / 1920.0
    font = cfg.font or DEFAULT_FONTS.get(language or "", LATIN_FONT)
    # Arabic glyphs have a smaller x-height at the same point size.
    size = int(cfg.font_size * scale * (1.25 if cfg.captions == "word" else 1.0) * (1.35 if rtl else 1.0))
    max_words = cfg.max_words_per_line or (1 if cfg.captions == "word" else 3 if rtl else 4)
    max_chars = 20 if rtl else 24
    upper = cfg.uppercase and not rtl
    primary, highlight = _ass_color(cfg.text_color), _ass_color(cfg.highlight_color)
    margin_v = int(cfg.caption_margin_v * scale)
    outline, shadow = max(2, int(6 * scale)), max(1, int(3 * scale))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{primary},{highlight},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,{int(60 * scale)},{int(60 * scale)},{margin_v},1
Style: Title,{font},{int(size * 0.8)},&H00000000,&H00000000,{_ass_color('#FFFFFF')},&H00000000,-1,0,0,0,100,100,0,0,3,{int(18 * scale)},0,8,{int(80 * scale)},{int(80 * scale)},{int(260 * scale)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    pop = r"{\fscx80\fscy80\t(0,90,\fscx100\fscy100)}"
    if cfg.captions != "none":
        pages = paginate(words, max_words, max_chars)
        for p_idx, page in enumerate(pages):
            next_start = pages[p_idx + 1].start if p_idx + 1 < len(pages) else duration
            page_end = min(next_start, page.end + HOLD, duration)
            texts = [_clean(w.text, upper) for w in page.words]
            if cfg.captions in ("phrase", "word"):
                events.append(f"Dialogue: 0,{_ts(page.start)},{_ts(page_end)},Caption,,0,0,0,,{pop}{' '.join(texts)}")
                continue
            for k, w in enumerate(page.words):
                ev_start = page.start if k == 0 else w.start
                ev_end = page.words[k + 1].start if k + 1 < len(page.words) else page_end
                if ev_end - ev_start < 0.01:
                    continue
                parts = [f"{{\\c{highlight}}}{t}{{\\c{primary}}}" if i == k else t for i, t in enumerate(texts)]
                events.append(f"Dialogue: 0,{_ts(ev_start)},{_ts(ev_end)},Caption,,0,0,0,,{pop if k == 0 else ''}{' '.join(parts)}")
    if title:
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(min(3.5, duration))},Title,,0,0,0,,{{\\fad(150,250)}}{_clean(title, upper)}")
    return header + "\n".join(events) + "\n"


def build_srt(words: list[Word], language: str | None) -> str:
    rtl = is_rtl(language)
    pages = paginate(words, max_words=8 if not rtl else 7, max_chars=42, max_gap=0.8, max_dur=5.0)

    def ts(t: float) -> str:
        ms = int(round(max(0.0, t) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for i, p in enumerate(pages):
        end = min(p.end + 0.2, pages[i + 1].start) if i + 1 < len(pages) else p.end + 0.2
        blocks.append(f"{i + 1}\n{ts(p.start)} --> {ts(end)}\n{' '.join(w.text for w in p.words)}\n")
    return "\n".join(blocks)
