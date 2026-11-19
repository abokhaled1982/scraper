# clipper – viral moment cutter (Qwen3-ASR)

Long video in → short, captioned 9:16 clips out. Arabic, German and English (plus other Qwen3-ASR languages).

```
video ──ffmpeg──► 16 kHz audio ──Silero VAD──► ≤30 s chunks cut in silence
      ──Qwen3-ASR-1.7B──► text per chunk (language forced or auto-detected per chunk)
      ──Qwen3-ForcedAligner-0.6B (de/en/fr/es/it/pt/ru) | MMS-300m CTC aligner (ar + rest)──► word timestamps
      ──sentences──► Qwen3-8B (local, 4-bit) picks hook→payoff ranges as JSON  ┐ blended 75/25,
                     audio/text heuristics (loudness, pace, hook words, …)     ┘ overlap-suppressed
      ──cut points snapped to the quietest spot between words──►
      ──face tracking (YuNet) + shot detection → smooth 9:16 camera path──►
      ──animated ASS captions (RTL-safe) + loudnorm −14 LUFS──► clips/NN_title.mp4 (+ .srt)
```

## Why these models

| Stage | Model | Reason |
|---|---|---|
| ASR | `Qwen/Qwen3-ASR-1.7B` | Best open ASR for ar/de/en, robust to music/noise, `context` biasing for names |
| Word timing (de/en/…) | `Qwen/Qwen3-ForcedAligner-0.6B` | More accurate than CTC/E2E aligners, but supports only 11 languages – **not Arabic** |
| Word timing (Arabic) | `MahmoudAshraf/mms-300m-1130-forced-aligner` via `ctc-forced-aligner` | MMS covers 1100+ languages incl. Arabic dialects (uroman romanisation) |
| VAD | Silero VAD | Chunks never split a word; silence map used for clean cuts |
| Moment picking | `Qwen/Qwen3-8B` 4-bit (`--llm-model Qwen/Qwen3-4B` for less VRAM, `Qwen3-14B` on L4/A100) | Understands ar/de/en, runs locally, no API costs |
| Reframing | OpenCV YuNet face detector (Haar fallback) | Fast on CPU, no extra deps |

## Run

Colab: open [colab_clipper.ipynb](colab_clipper.ipynb) (T4 GPU is enough).

CLI (GPU machine, `pip install -r clipper/requirements-gpu.txt`, ffmpeg with libass, `fonts-noto-core`):

```bash
python -m clipper run talk.mp4 --lang ar -n 5 --hook-title
python -m clipper run talk.mp4 --lang de --min 20 --max 45 --captions phrase
python -m clipper run talk.mp4 --no-llm            # heuristic picks only
```

### Remote mode (local orchestrator + free cloud GPUs)

Local machine: orchestration and cutting (ffmpeg with libass, `pip install -r clipper/requirements.txt`).
Colab runs ASR ([colab_asr_server.ipynb](colab_asr_server.ipynb)), Kaggle 2×T4 runs Gemma 4 26B-A4B via llama-server
([kaggle_gemma4_server.ipynb](kaggle_gemma4_server.ipynb)). Gemma fixes ASR errors
(new words inherit the original timings, rewrites are rejected) and picks the moments.

```bash
export CLIPPER_ASR_URL=... CLIPPER_ASR_TOKEN=...   # printed by the Colab tunnel cell
export CLIPPER_LLM_URL=... CLIPPER_LLM_TOKEN=...   # printed by the Kaggle tunnel cell
python -m clipper run talk.mp4 -n 8 --min 8 --max 20 --target 14 --llm-only --hook-title
```

`--max` is a hard limit for the file length (padding included). `corrections.json` lists every transcript fix,
`transcript.raw.json` keeps the uncorrected ASR output.

Stages are cached in `clipper_out/<video>/`:

| File | Content |
|---|---|
| `transcript.json` / `.txt` | words with timestamps, VAD speech regions, ASR chunks |
| `moments.json` | chosen clips: `start`, `end`, `score`, `title`, `hook`, `reason` |
| `clips/NN_title.mp4/.ass/.srt` | rendered clip, caption source, plain subtitles |
| `summary.json` | moments + clip file + clip text |

Edit `moments.json` (e.g. move `start`/`end`) and run `python -m clipper render talk.mp4` to re-render only.
`detect` re-runs moment detection on the cached transcript, `--redetect` does the same inside `run`, `--force` redoes everything.

## Useful options

- `--context "Guest Name, Brand"` – bias ASR towards names/terms.
- `--reframe auto|face|center|fit|none` – `auto` tracks faces and switches to a blurred-background fit when faces are rare (screen recordings, gameplay); vertical sources are fitted.
- `--captions karaoke|phrase|word|none`, `--font`, `--fonts-dir`, `--font-size`, `--highlight "#FFD400"`, `--words-per-line`, `--upper`.
- `--think` – Qwen3 thinking mode for the moment picker (slower, sometimes better).

## Reliability measures

- No word is split between ASR chunks (VAD chunking) → chunk texts join with spaces (the `qwen-asr` built-in chunking glues them).
- Aligner output is mapped back onto the original tokens with sequence matching, so captions keep punctuation/diacritics; misses are interpolated and reported.
- LLM output is validated (JSON repair, id range checks, one retry), forced into the duration limits keeping the hook sentence, and backfilled with heuristic picks; without GPU/LLM the heuristic path runs alone.
- Single re-encode per clip with input seeking → frame-accurate cuts; 30/80 ms audio fades avoid clicks.

## Next upgrades

- Active-speaker detection (LR-ASD / TalkNet) for multi-person podcasts instead of largest-face tracking.
- Speaker diarization (pyannote) to keep clips inside one speaker's answer.
- Audio-event signals (laughter/applause) from an audio LLM such as Qwen2.5-Omni / Qwen3-Omni.
- Jump cuts that remove long pauses inside clips.
