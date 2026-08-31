# KPHOTO-YTB V3 - Technical Notes

## Core Architecture
- Source-based Python 3.12 application; do not modify V2 during V3 development.
- `main.py` owns the PyQt6 UI, task orchestration, pipeline phases, preview, and realtime logging.
- Long-running work runs in `QThread` workers; queued tasks start after the active thread closes.
- Processing modules under `modules/` expose focused functions called by `main.py`.
- Video pipeline: download -> concat/rename -> vocal separation -> SRT/OCR -> translation -> vocal mux -> export.
- Automatic workflow is phased: initial processing pauses before OCR, then pauses before export for manual review.
- Export preserves source video, vocal, SRT, and configuration; generated output uses `_Export.mp4`.
- Runtime paths are resolved from `config.py`; frozen builds use the executable directory and PyInstaller resources.

## Tech Stack
- Python 3.12; launcher: `run.bat`.
- PyQt6, Qt multimedia, `QGraphicsVideoItem`, `QThread`, `QTimer`.
- BBDown 1.6.3 and yt-dlp/aria2c are documented download components; FFmpeg handles media operations.
- `audio-separator` with `htdemucs.yaml` handles vocal separation.
- Whisper V3 via faster-whisper, KPHOTO-Local via FunASR, and PP-OCRv6 via RapidOCR/ONNX Runtime.
- Translation supports Google Translate Web, Gemini, Qwen, and Hybrid workflows.
- OpenCV processes video frames and OCR regions; Torch/Torchaudio support ML inference.
- PyInstaller creates the Windows distribution; `KPHOTO-YTB.spec` defines bundled data.

## Main Directory Structure
- `main.py`: UI, preview, task workers, automatic pipeline, and progress display.
- `config.py`: application version, paths, providers, runtime configuration, and preserved paths.
- `modules/downloader.py`: Bilibili download and BBDown integration.
- `modules/separator.py`: vocal separation and long-video handling.
- `modules/srt.py`: Whisper/KPHOTO SRT generation and timestamp alignment.
- `modules/ocr_subtitles.py`: PP-OCRv6 OCR over burned-in subtitles.
- `modules/translator.py`: translation providers, retries, checkpoints, QA, and cost logs.
- `modules/concat.py`: part concatenation and source cleanup.
- `modules/muxer.py`: vocal/video audio muxing.
- `modules/exporter.py`: FFmpeg effects, subtitles, blur, and final output.
- `modules/rename.py`: video classification, naming, and folder organization.
- `modules/updater.py`: GitHub release lookup and frozen-app update replacement.
- `assets/`: application icon resources.
- `bin/`: BBDown, FFmpeg, and optional aria2c binaries.
- `models/`: KPHOTO-Local model files; RapidOCR models are supplied by the RapidOCR package/build.
- `browser-profile-bilibili/`: optional browser login profile; not required by direct BBDown downloads.
- `dist/`: PyInstaller output; `build/`: disposable PyInstaller intermediates.
- `config.local.json`: machine-local API settings; never commit or distribute secrets.

## Mandatory Conventions
- Use Python 3.12 for V3 development, testing, and packaging.
- Keep each functional change in a separate Git checkpoint/commit.
- Never commit API keys, tokens, `.env`, or `config.local.json`.
- Do not delete V2, V2 bytecode, or Python 3.11 until V3 is fully validated.
- Test a short video before testing long or batch workflows.
- Verify realtime logs, progress, output files, and failure behavior after every pipeline change.
- Each video must have its own OCR ROI; do not reuse another video's region.
- Preserve source media and intermediate assets unless an explicit cleanup step owns them.
- Keep preview subtitle input exactly `subtitles/en.srt`; ignore malformed names such as `en..srt`.
- Test on a clean machine before distributing a packaged build.
- Package the full distribution with its executable, `_internal`, `bin`, and `models`; do not send only the EXE.
- Keep local provider settings separate per machine and use the documented environment/config keys.

## Current Feature Status
- PyQt6 V3 interface, preview playback, timeline seeking, draggable English subtitle overlay, and per-video overlay settings are implemented.
- Separate actions exist for download, concat, rename, vocal separation, SRT creation, translation, mux, and export.
- Automatic pipeline is implemented with manual OCR-region and export-review pauses.
- Whisper V3 supports Chinese transcription, word timestamps, long-video chunks, overlap handling, and hallucination controls.
- KPHOTO-Local supports Chinese audio transcription with cached models and CPU/CUDA selection.
- PP-OCRv6 supports burned-in Chinese subtitle extraction, ROI processing, progress reporting, and CPU/CUDA selection.
- Translation has batching, retries, checkpoints, provider selection, QA, and cost reporting.
- FFmpeg export supports vocal muxing, subtitles, blur, overlay configuration, and source preservation.
- GitHub startup update checking and frozen executable replacement are implemented in source.
- Current documented tests include short/long video, OCR accuracy, timeline alignment, provider behavior, preview switching, and clean-machine packaging.
- OCR quality is functional but transition frames can still produce short/noisy cues; long-video and GPU/CPU validation remain required.
- Packaging must verify all runtime/model assets and the installer must be tested on a clean Windows machine.
