from __future__ import annotations

from dataclasses import dataclass

# code -> (Qwen3-ASR language name, ISO 639-3 for the MMS/CTC aligner, right-to-left script)
LANGUAGES: dict[str, tuple[str, str, bool]] = {
    "ar": ("Arabic", "ara", True),
    "de": ("German", "deu", False),
    "en": ("English", "eng", False),
    "fr": ("French", "fra", False),
    "es": ("Spanish", "spa", False),
    "it": ("Italian", "ita", False),
    "pt": ("Portuguese", "por", False),
    "ru": ("Russian", "rus", False),
    "tr": ("Turkish", "tur", False),
    "fa": ("Persian", "fas", True),
    "hi": ("Hindi", "hin", False),
    "nl": ("Dutch", "nld", False),
    "pl": ("Polish", "pol", False),
    "id": ("Indonesian", "ind", False),
}

# Languages Qwen3-ForcedAligner-0.6B supports (others fall back to the MMS CTC aligner).
QWEN_ALIGNER_LANGS = {"zh", "en", "yue", "fr", "de", "it", "ja", "ko", "pt", "ru", "es"}

_NAME_TO_CODE = {name.lower(): code for code, (name, _, _) in LANGUAGES.items()}
_NAME_TO_CODE.update({"chinese": "zh", "cantonese": "yue", "japanese": "ja", "korean": "ko"})


def language_code(name_or_code: str | None) -> str | None:
    if not name_or_code:
        return None
    value = name_or_code.strip().lower()
    if value in LANGUAGES or value in QWEN_ALIGNER_LANGS:
        return value
    return _NAME_TO_CODE.get(value)


def qwen_language_name(code: str | None) -> str | None:
    if code is None or code == "auto":
        return None
    if code not in LANGUAGES:
        raise ValueError(f"Unsupported language code {code!r}; known: {', '.join(sorted(LANGUAGES))}")
    return LANGUAGES[code][0]


def is_rtl(code: str | None) -> bool:
    return bool(code in LANGUAGES and LANGUAGES[code][2])


@dataclass
class ClipperConfig:
    # --- ASR / alignment ---
    language: str = "auto"
    asr_model: str = "Qwen/Qwen3-ASR-1.7B"
    qwen_aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    ctc_aligner_model: str = "MahmoudAshraf/mms-300m-1130-forced-aligner"
    asr_context: str = ""  # names / jargon that help recognition
    asr_batch_size: int = 8
    chunk_max_sec: float = 30.0
    vad_threshold: float = 0.5

    # --- moment detection ---
    use_llm: bool = True
    llm_model: str = "Qwen/Qwen3-8B"
    llm_4bit: bool = True
    llm_thinking: bool = False
    llm_window_words: int = 2500
    max_clips: int = 5
    min_clip_sec: float = 15.0
    max_clip_sec: float = 60.0
    target_clip_sec: float = 35.0
    max_overlap: float = 0.15  # max shared time between two clips, as a fraction of the shorter one
    fill_heuristic: bool = True  # top up with heuristic picks when the LLM returns fewer than max_clips

    # --- remote GPU workers (empty = run locally) ---
    asr_url: str = ""  # Colab ASR server (colab_asr_server.ipynb)
    asr_token: str = ""
    llm_url: str = ""  # OpenAI-compatible llama-server, e.g. Gemma 4 on Kaggle
    llm_token: str = ""
    llm_api_model: str = "gemma-4-26B-A4B-it"
    llm_max_tokens: int = 4096
    llm_correct: bool = True  # let the remote LLM fix ASR errors before moment picking
    llm_correct_words: int = 600  # words per correction request

    # --- cutting ---
    pad_start: float = 0.12
    pad_end: float = 0.30

    # --- rendering ---
    out_width: int = 1080
    out_height: int = 1920
    fps: float | None = None  # None = keep source fps (capped at 60)
    crf: int = 20
    preset: str = "medium"
    reframe: str = "auto"  # auto | face | center | fit | none

    # --- captions ---
    captions: str = "karaoke"  # karaoke | phrase | word | none
    font: str | None = None  # None = language default
    fonts_dir: str | None = None
    font_size: int = 92
    highlight_color: str = "#FFD400"
    text_color: str = "#FFFFFF"
    caption_margin_v: int = 520  # px from bottom, keeps captions above platform UI
    max_words_per_line: int | None = None  # None = language default
    uppercase: bool = False
    hook_title: bool = False
