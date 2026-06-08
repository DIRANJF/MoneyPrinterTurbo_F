# Project Understanding

## Purpose

MoneyPrinterTurbo is a Python application that generates short videos from a topic,
script, local assets, subtitles, voice-over, and background music. It exposes both a
FastAPI API and a Streamlit WebUI.

## Entry Points

- API server: `main.py`
- FastAPI app: `app/asgi.py`
- API router: `app/router.py`
- Streamlit UI: `webui/Main.py`

## Main Generation Flow

The central orchestration lives in `app/services/task.py`.

1. Generate or accept a video script.
2. Generate or accept search terms.
3. Generate voice-over audio, or use a custom voice file.
4. Generate subtitles from TTS timing, Whisper, or a simple script-based fallback.
5. Resolve video materials from online providers, local uploads, or solid color.
6. Compose final videos with MoviePy/ffmpeg.
7. Optionally cross-post generated videos through Upload-Post.

## Important Modules

- `app/models/schema.py`: Request and parameter models, especially `VideoParams` and
  `MaterialInfo`.
- `app/controllers/v1/video.py`: Video task APIs, task lookup, BGM upload/listing,
  local material upload/listing, video streaming, and download.
- `app/controllers/v1/llm.py`: Script and keyword generation APIs.
- `app/services/llm.py`: LLM provider integration and prompt handling.
- `app/services/voice.py`: TTS providers, voice lists, and subtitle maker handling.
- `app/services/material.py`: Pexels/Pixabay search and video download.
- `app/services/video.py`: Local material preprocessing, clipping, concatenation,
  subtitles, BGM, original audio handling, and final video writing.
- `app/services/state.py`: In-memory or Redis-backed task state.
- `app/controllers/manager/*`: In-memory or Redis-backed task queue management.

## Current Custom Features

This repository already contains features beyond the basic upstream workflow:

- Local material upload and per-material clip configuration.
- Per-material `start_time`, `end_time`, `use_custom_clip`, and
  `use_original_audio`.
- Solid color background video source.
- Subtitle animation settings.
- Video quality presets plus custom bitrate/CRF.
- Safer path handling for uploaded materials, BGM, task output, stream, and download.
- Bounded task queues with optional Redis state/queue support.
- Optional cross-posting to TikTok/Instagram through Upload-Post.

## Development Guidance

Use the existing flow before adding new abstractions:

- New request/UI parameter: update `app/models/schema.py`, then `webui/Main.py` and
  any API/controller surface that needs it.
- New generation stage: update `app/services/task.py`.
- Video composition or rendering behavior: update `app/services/video.py`.
- TTS or voice-provider behavior: update `app/services/voice.py`.
- LLM/provider behavior: update `app/services/llm.py`.
- Upload, task query, stream, or download behavior: update
  `app/controllers/v1/video.py`.

For local material workflows, keep the security boundary intact: uploaded or selected
local files should resolve only inside approved storage directories before MoviePy
opens them.
