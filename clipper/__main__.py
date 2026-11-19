"""CLI:  python -m clipper {run,transcribe,detect,render} VIDEO [options]"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

from .config import LANGUAGES, ClipperConfig


def load_env_file() -> Path | None:
    """Read KEY=VALUE lines from ./.env or clipper/.env; real environment variables win."""
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)
        return path
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clipper", description="Find viral moments, cut them precisely, reframe to 9:16 and caption them.")
    p.add_argument("command", choices=["run", "transcribe", "detect", "render"],
                   help="run = everything; transcribe/detect/render = single (cached) stage")
    p.add_argument("video")
    p.add_argument("-o", "--out", default="clipper_out", help="output root directory")
    p.add_argument("--force", action="store_true", help="ignore cached transcript/moments")
    p.add_argument("--redetect", action="store_true", help="keep transcript, redo moment detection")

    g = p.add_argument_group("ASR")
    g.add_argument("--lang", default="auto", choices=["auto", *LANGUAGES], help="spoken language (auto = detect)")
    g.add_argument("--asr-model", default=ClipperConfig.asr_model)
    g.add_argument("--context", default="", help="names/terms to bias recognition, e.g. guest names")
    g.add_argument("--batch", type=int, default=ClipperConfig.asr_batch_size)

    g = p.add_argument_group("moment detection")
    g.add_argument("--no-llm", action="store_true", help="heuristic scoring only (no GPU LLM)")
    g.add_argument("--llm-model", default=ClipperConfig.llm_model, help="e.g. Qwen/Qwen3-4B for less VRAM, Qwen/Qwen3-14B for quality")
    g.add_argument("--no-4bit", action="store_true", help="load the LLM in full precision")
    g.add_argument("--think", action="store_true", help="enable Qwen3 thinking mode (slower, sometimes better picks)")
    g.add_argument("-n", "--max-clips", type=int, default=ClipperConfig.max_clips)
    g.add_argument("--min", type=float, default=ClipperConfig.min_clip_sec, dest="min_sec")
    g.add_argument("--max", type=float, default=ClipperConfig.max_clip_sec, dest="max_sec")
    g.add_argument("--target", type=float, default=ClipperConfig.target_clip_sec)
    g.add_argument("--llm-only", action="store_true", help="no heuristic filler clips when the LLM finds fewer than -n")

    g = p.add_argument_group("remote GPU workers (defaults from CLIPPER_* environment variables)")
    g.add_argument("--asr-url", default=os.environ.get("CLIPPER_ASR_URL", ""), help="Colab ASR server base URL")
    g.add_argument("--asr-token", default=os.environ.get("CLIPPER_ASR_TOKEN", ""))
    g.add_argument("--llm-url", default=os.environ.get("CLIPPER_LLM_URL", ""), help="llama-server base URL (Gemma 4 on Kaggle)")
    g.add_argument("--llm-token", default=os.environ.get("CLIPPER_LLM_TOKEN", ""))
    g.add_argument("--no-correct", action="store_true", help="skip the LLM transcript correction")

    g = p.add_argument_group("render")
    g.add_argument("--reframe", default="auto", choices=["auto", "face", "center", "fit", "none"])
    g.add_argument("--size", default="1080x1920", help="output WxH")
    g.add_argument("--fps", type=float, default=None)
    g.add_argument("--crf", type=int, default=ClipperConfig.crf)
    g.add_argument("--preset", default=ClipperConfig.preset)

    g = p.add_argument_group("captions")
    g.add_argument("--captions", default="karaoke", choices=["karaoke", "phrase", "word", "none"])
    g.add_argument("--font", default=None)
    g.add_argument("--fonts-dir", default=None)
    g.add_argument("--font-size", type=int, default=ClipperConfig.font_size)
    g.add_argument("--highlight", default=ClipperConfig.highlight_color)
    g.add_argument("--words-per-line", type=int, default=None)
    g.add_argument("--upper", action="store_true", help="UPPERCASE captions (Latin scripts)")
    g.add_argument("--hook-title", action="store_true", help="show the LLM hook text at the top for the first seconds")
    return p


def config_from_args(a: argparse.Namespace) -> ClipperConfig:
    w, h = (int(v) for v in a.size.lower().split("x"))
    return ClipperConfig(
        language=a.lang, asr_model=a.asr_model, asr_context=a.context, asr_batch_size=a.batch,
        use_llm=not a.no_llm, llm_model=a.llm_model, llm_4bit=not a.no_4bit, llm_thinking=a.think,
        max_clips=a.max_clips, min_clip_sec=a.min_sec, max_clip_sec=a.max_sec, target_clip_sec=a.target,
        fill_heuristic=not a.llm_only, asr_url=a.asr_url, asr_token=a.asr_token,
        llm_url=a.llm_url, llm_token=a.llm_token, llm_correct=not a.no_correct,
        reframe=a.reframe, out_width=w, out_height=h, fps=a.fps, crf=a.crf, preset=a.preset,
        captions=a.captions, font=a.font, fonts_dir=a.fonts_dir, font_size=a.font_size,
        highlight_color=a.highlight, max_words_per_line=a.words_per_line, uppercase=a.upper, hook_title=a.hook_title,
    )


def main(argv: list[str] | None = None) -> int:
    env_file = load_env_file()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if env_file:
        logging.getLogger("clipper").info("Settings from %s", env_file)
    cfg = config_from_args(args)
    if cfg.min_clip_sec >= cfg.max_clip_sec:
        print("--min must be smaller than --max", file=sys.stderr)
        return 2

    if args.command != "render" and (cfg.asr_url or cfg.llm_url):
        from .remote import check_workers

        try:
            check_workers(cfg)
        except RuntimeError as exc:
            print(f"Remote worker problem: {exc}\nTunnel cell in Colab/Kaggle running? URL/token in clipper/.env current?", file=sys.stderr)
            return 2

    from .pipeline import Pipeline

    pipe = Pipeline(args.video, args.out, cfg)
    if args.command != "render" and importlib.util.find_spec("torch") is None:
        missing = []
        if not cfg.asr_url and (args.force or not pipe.transcript_path.exists()):
            missing.append("CLIPPER_ASR_URL  (Colab: colab_asr_server.ipynb, tunnel cell)")
        if not cfg.llm_url and cfg.use_llm:
            missing.append("CLIPPER_LLM_URL  (Kaggle: Gemma-4-26B-A4B-Leadbrief-Kaggle.ipynb, section 5)")
        if missing:
            print("No local GPU stack (torch) - these remote workers must be set in clipper/.env:\n  "
                  + "\n  ".join(missing), file=sys.stderr)
            return 2
    if args.command == "transcribe":
        pipe.transcribe(args.force)
    elif args.command == "detect":
        moments = pipe.detect(pipe.transcribe(), force=True)
        for m in moments:
            print(f"{m.score:5.1f}  {m.start:8.2f}-{m.end:8.2f}  [{m.source}] {m.title}")
    elif args.command == "render":
        transcript = pipe.transcribe()
        pipe.render(transcript, pipe.detect(transcript))
    else:
        outputs = pipe.run(force=args.force, redetect=args.redetect)
        print("\n".join(str(o) for o in outputs))
    print(f"Output: {pipe.work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
