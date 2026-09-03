"""Batch SRT extraction from vocal WAV files."""

from __future__ import annotations

import re
import json
import os
import subprocess
import threading
import sys
import shutil
import tempfile
from pathlib import Path
from config import FFMPEG_PATH, app_dir


_WHISPER_MODEL = None
_WHISPER_MODEL_KEY = None
_KPHOTO_MODEL = None
_KPHOTO_MODEL_KEY = None
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm")


def _format_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_srt(segments: list[dict], output_path: Path) -> None:
    ordered = sorted(segments, key=lambda item: float(item.get("start") or 0))
    with output_path.open("w", encoding="utf-8") as handle:
        index = 1
        for position, segment in enumerate(ordered):
            text = str(segment.get("text") or "").strip()
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or 0)
            if position + 1 < len(ordered):
                next_start = float(ordered[position + 1].get("start") or 0)
                if next_start > start:
                    end = min(end, max(start + 0.05, next_start - 0.03))
            if not text or end <= start:
                continue
            handle.write(f"{index}\n")
            handle.write(f"{_format_srt_time(start)} --> {_format_srt_time(end)}\n")
            handle.write(f"{text}\n\n")
            index += 1


_WHISPER_CUE_PUNCTUATION = set(",，。.!！?？;；:：")


def _split_whisper_segment(segment, max_seconds: float = 5.5, max_chars: int = 18) -> list[dict]:
    """Build subtitle-sized cues from Whisper word timestamps."""
    words = [word for word in (getattr(segment, "words", None) or [])
             if getattr(word, "start", None) is not None and getattr(word, "end", None) is not None]
    if not words:
        text = str(getattr(segment, "text", "") or "").strip()
        start = float(getattr(segment, "start", 0.0) or 0.0)
        end = float(getattr(segment, "end", start) or start)
        # Never leave a short hallucinated phrase visible for tens of seconds.
        return ([{"start": start, "end": min(end, start + max_seconds), "text": text}]
                if text and end > start else [])

    cues = []
    current = []
    cue_start = None
    cue_end = None
    for word in words:
        token = str(getattr(word, "word", "") or "").strip()
        if not token:
            continue
        word_start = float(word.start)
        word_end = float(word.end)
        # Whisper can occasionally assign a short word an end timestamp tens
        # of seconds later (usually across silence).  Never let that malformed
        # word timestamp keep a subtitle visible for the whole gap.
        word_end = min(word_end, word_start + max_seconds)
        if cue_start is None:
            cue_start = word_start
        cue_end = word_end
        current.append(token)
        text = "".join(current).strip()
        duration = cue_end - cue_start
        punctuation_break = token[-1] in _WHISPER_CUE_PUNCTUATION and duration >= 0.15
        if punctuation_break or duration >= max_seconds or len(text) >= max_chars:
            cues.append({
                "start": cue_start,
                "end": min(cue_end, cue_start + max_seconds),
                "text": text,
            })
            current = []
            cue_start = None
            cue_end = None
    if current and cue_start is not None and cue_end is not None:
        cues.append({
            "start": cue_start,
            "end": min(cue_end, cue_start + max_seconds),
            "text": "".join(current).strip(),
        })
    return [cue for cue in cues if cue["text"] and cue["end"] > cue["start"]]


def _whisper_transcribe_options() -> dict:
    return {
        "language": "zh",
        "task": "transcribe",
        "vad_filter": True,
        "vad_parameters": {
            "threshold": 0.5,
            "min_silence_duration_ms": 250,
            "speech_pad_ms": 120,
        },
        "condition_on_previous_text": False,
        "beam_size": 5,
        "word_timestamps": True,
        "hallucination_silence_threshold": 1.5,
    }


def _run_kphoto_local(audio_path: Path, log_callback) -> dict:
    # The UI currently runs on Python 3.11 while the bundled ML packages are
    # installed for Python 3.12. Run model loading in the matching interpreter.
    if sys.version_info[:2] != (3, 12) and not os.getenv("BILI2YT_SRT_NATIVE"):
        return _run_srt_native(audio_path, "kphoto-local", log_callback)
    from funasr import AutoModel

    # A frozen build reports __file__ under _internal, while the Setup extracts
    # KPHOTO-YTB_models.zip next to the executable. Check both layouts plus the
    # source tree.
    candidate_roots = [
        Path(__file__).resolve().parents[1] / "models" / "kphoto-local" / "zh",
        app_dir() / "models" / "kphoto-local" / "zh",
    ]
    roots = list(dict.fromkeys(candidate_roots))
    model_root = next((root for root in roots if (root / "zh" / "model.pt").is_file()), None)
    if model_root is None:
        raise RuntimeError("Chua co model KPHOTO-Local tieng Trung.")

    try:
        import torch
        cuda_available = bool(torch.cuda.is_available())
        device = "cuda:0" if cuda_available else "cpu"
        if cuda_available:
            log_callback(f"KPHOTO-Local: GPU CUDA ({torch.cuda.get_device_name(0)})")
        else:
            log_callback("KPHOTO-Local: CPU (khong phat hien CUDA)")
    except Exception as exc:
        device = "cpu"
        log_callback(f"KPHOTO-Local: CPU (CUDA khong kha dung: {exc})")
    global _KPHOTO_MODEL, _KPHOTO_MODEL_KEY
    model_key = (str(model_root), device)
    if _KPHOTO_MODEL is None or _KPHOTO_MODEL_KEY != model_key:
        log_callback("KPHOTO-Local: dang nap model...")
        _KPHOTO_MODEL = AutoModel(
            model=str(model_root / "zh"),
            vad_model=str(model_root / "v"),
            punc_model=str(model_root / "p"),
            device=device,
            disable_update=True,
        )
        _KPHOTO_MODEL_KEY = model_key
    else:
        log_callback("KPHOTO-Local: tai su dung model da nap")
    model = _KPHOTO_MODEL
    # KPHOTO is fastest and most stable with its original 300-second batches;
    # larger batches can increase memory pressure without improving throughput.
    batch_size_s = 300
    log_callback(f"KPHOTO-Local: batch {batch_size_s}s, dang nhan dang...")
    output = model.generate(
        input=str(audio_path),
        batch_size_s=batch_size_s,
        sentence_timestamp=True,
        use_itn=True,
    )
    item = output[0] if output else {}
    segments = []
    for sentence in item.get("sentence_info") or []:
        text = str(sentence.get("text") or "").strip()
        start = float(sentence.get("start") or 0) / 1000
        end = float(sentence.get("end") or 0) / 1000
        if text and end > start:
            segments.append({"start": start, "end": end, "text": text})
    return {"language": "zh", "segments": segments}


def _audio_duration(audio_path: Path) -> float:
    try:
        ffprobe = Path(FFMPEG_PATH).with_name("ffprobe.exe")
        if not ffprobe.is_file():
            result = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-i", str(audio_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
            return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3)) if match else 0.0
        result = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(audio_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return float(result.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def _source_video_for_vocal(vocal_path: Path) -> Path | None:
    base_name = re.sub(r"[\s_-]*\(Vocals\)$", "", vocal_path.stem, flags=re.IGNORECASE)
    for extension in VIDEO_EXTENSIONS:
        candidate = vocal_path.with_name(base_name + extension)
        if candidate.is_file():
            return candidate
    matches = [
        path for path in vocal_path.parent.iterdir()
        if path.is_file()
        and path.suffix.casefold() in VIDEO_EXTENSIONS
        and path.stem.casefold() == base_name.casefold()
    ]
    return sorted(matches, key=lambda path: path.name.casefold())[0] if matches else None


def _align_segments_to_video_timeline(
    segments: list[dict], vocal_path: Path, log_callback
) -> list[dict]:
    """Undo progressive timestamp drift when a separated vocal is time-stretched."""
    source_video = _source_video_for_vocal(vocal_path)
    if source_video is None:
        log_callback("[SrtSync] Khong tim thay video goc, giu nguyen timeline vocal.")
        return segments
    vocal_duration = _audio_duration(vocal_path)
    video_duration = _audio_duration(source_video)
    if vocal_duration <= 0 or video_duration <= 0:
        log_callback("[SrtSync] Khong doc duoc thoi luong, giu nguyen timeline vocal.")
        return segments
    drift = vocal_duration - video_duration
    scale = video_duration / vocal_duration
    if abs(drift) < 0.020:
        log_callback(
            f"[SrtSync] Timeline da khop: video={video_duration:.3f}s, "
            f"vocal={vocal_duration:.3f}s."
        )
        return segments
    corrected = []
    for segment in segments:
        item = dict(segment)
        item["start"] = max(0.0, float(item.get("start") or 0) * scale)
        item["end"] = min(video_duration, max(0.0, float(item.get("end") or 0) * scale))
        corrected.append(item)
    log_callback(
        f"[SrtSync] Da sua drift tich luy {drift:+.3f}s: "
        f"video={video_duration:.3f}s, vocal={vocal_duration:.3f}s, "
        f"he so={scale:.9f}."
    )
    return corrected


def _run_kphoto_chunked(audio_path: Path, log_callback) -> dict:
    """Transcribe long audio in overlapping chunks and restore global timestamps."""
    duration = _audio_duration(audio_path)
    # A single long generate() can stop reporting progress and occasionally
    # stall in FunASR. Keep each request bounded and reuse the cached model.
    if duration <= 10 * 60:
        return _run_kphoto_local(audio_path, log_callback)

    chunk_seconds = 10 * 60
    overlap = 2.0
    chunk_count = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    work_dir = Path(tempfile.mkdtemp(prefix="bili2yt_srt_", dir=str(audio_path.parent)))
    combined = []
    try:
        log_callback(f"KPHOTO-Local: chia {duration / 60:.1f} phut thanh {chunk_count} doan ({chunk_seconds // 60} phut).")
        for index in range(chunk_count):
            start = max(0.0, index * chunk_seconds - (overlap if index else 0.0))
            length = min(duration - start, chunk_seconds + (overlap if index and index + 1 < chunk_count else 0.0))
            chunk_path = work_dir / f"chunk_{index:04d}.flac"
            extracted = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(audio_path), "-c:a", "flac", str(chunk_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
            )
            if extracted.returncode != 0:
                raise RuntimeError(extracted.stderr[-800:] or "Khong tach duoc audio chunk.")
            result = _run_kphoto_local(chunk_path, log_callback)
            for segment in result.get("segments", []):
                item = dict(segment)
                item["start"] = float(item.get("start", 0.0)) + start
                item["end"] = float(item.get("end", 0.0)) + start
                if item["end"] > start + overlap or index == 0:
                    combined.append(item)
            log_callback(f"[SrtProgress] CHUNK {index + 1}/{chunk_count}")
        combined.sort(key=lambda item: (item["start"], item["end"]))
        return {"language": "zh", "segments": combined}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_whisper_v3_long(audio_path: Path, log_callback, model, duration: float) -> dict:
    """Transcribe long audio in resumable overlapping chunks with one model load."""
    chunk_seconds = 30 * 60
    overlap = 8.0
    count = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    checkpoint = audio_path.with_name(f".{audio_path.stem}_whisper_checkpoint.json")
    combined = []
    completed = set()
    if checkpoint.is_file():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            # Older checkpoints kept both sides of the 8-second overlap and
            # can reintroduce duplicate cues at 30-minute boundaries.
            if saved.get("version") == 2:
                combined = saved.get("segments", [])
                completed = set(saved.get("completed", []))
        except (OSError, ValueError, TypeError):
            pass
    work_dir = Path(tempfile.mkdtemp(prefix="bili2yt_whisper_", dir=str(audio_path.parent)))
    try:
        log_callback(f"Whisper V3: video dai {duration / 3600:.2f} gio, chia {count} chunk 30 phut")
        for index in range(count):
            if index in completed:
                continue
            start = max(0.0, index * chunk_seconds - (overlap if index else 0.0))
            end = min(duration, (index + 1) * chunk_seconds + (overlap if index + 1 < count else 0.0))
            core_start = index * chunk_seconds
            core_end = min(duration, (index + 1) * chunk_seconds)
            length = max(0.0, end - start)
            # PCM WAV avoids sporadic FFmpeg FLAC encoder failures such as
            # "invalid block size: 15" on long sources. Keep only one chunk
            # on disk at a time so multi-hour videos do not consume GBs.
            chunk_path = work_dir / f"chunk_{index:04d}.wav"
            extracted = subprocess.run(
                [
                    FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{start:.3f}", "-i", str(audio_path),
                    "-t", f"{length:.3f}", "-map", "0:a:0", "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                    str(chunk_path),
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
            )
            if extracted.returncode != 0:
                raise RuntimeError(extracted.stderr[-800:] or "Khong tach duoc audio chunk Whisper.")
            try:
                iterator, _info = model.transcribe(str(chunk_path), **_whisper_transcribe_options())
                for segment in iterator:
                    for local_cue in _split_whisper_segment(segment):
                        item = dict(local_cue)
                        item["start"] = float(item["start"]) + start
                        item["end"] = float(item["end"]) + start
                        if item["start"] >= core_start and (
                            index + 1 == count or item["start"] < core_end
                        ):
                            combined.append(item)
            finally:
                chunk_path.unlink(missing_ok=True)
            completed.add(index)
            checkpoint.write_text(
                json.dumps(
                    {"version": 2, "completed": sorted(completed), "segments": combined},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            log_callback(f"[SrtProgress] CHUNK {index + 1}/{count}")
        checkpoint.unlink(missing_ok=True)
        return {"language": "zh", "segments": sorted(combined, key=lambda item: item["start"])}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_whisper_v3(audio_path: Path, log_callback) -> dict:
    if sys.version_info[:2] != (3, 12) and not os.getenv("BILI2YT_SRT_NATIVE"):
        return _run_srt_native(audio_path, "whisper-v3", log_callback)
    from faster_whisper import WhisperModel

    try:
        import torch
        use_cuda = bool(torch.cuda.is_available())
    except Exception:
        use_cuda = False
    device = "cuda" if use_cuda else "cpu"
    compute_type = "float16" if use_cuda else "int8"
    log_callback(f"Whisper V3: {'GPU CUDA' if use_cuda else 'CPU'}")
    global _WHISPER_MODEL, _WHISPER_MODEL_KEY
    model_key = (device, compute_type)
    if _WHISPER_MODEL is None or _WHISPER_MODEL_KEY != model_key:
        log_callback("Whisper V3: dang nap model large-v3...")
        _WHISPER_MODEL = WhisperModel("large-v3", device=device, compute_type=compute_type)
        _WHISPER_MODEL_KEY = model_key
    else:
        log_callback("Whisper V3: tai su dung model large-v3 da nap")
    model = _WHISPER_MODEL
    duration = _audio_duration(audio_path)
    if duration >= 60 * 60:
        return _run_whisper_v3_long(audio_path, log_callback, model, duration)
    segments_iter, info = model.transcribe(str(audio_path), **_whisper_transcribe_options())
    segments = []
    segment_count = 0
    last_percent = -1
    duration = max(0.0, _audio_duration(audio_path))
    for segment in segments_iter:
        split_cues = _split_whisper_segment(segment)
        for cue in split_cues:
            segments.append(cue)
            segment_count += 1
            if segment_count == 1 or segment_count % 10 == 0:
                log_callback(f"[SrtProgress] Whisper đã nhận dạng {segment_count} đoạn, đến {cue['end']:.1f}s")
            if duration > 0:
                percent = max(0, min(99, int(float(cue["end"]) * 100 / duration)))
                if percent >= last_percent + 5:
                    last_percent = percent
                    log_callback(f"[SrtProgress] WHISPER_PERCENT {percent}")
    log_callback("[SrtProgress] WHISPER_PERCENT 100")
    return {"language": getattr(info, "language", "auto"), "segments": segments}


def _run_srt_native(audio_path: Path, engine: str, log_callback=None) -> dict:
    """Run the ML backend with the Python version used to install its wheels."""
    base_dir = Path(__file__).resolve().parents[1]
    python_path = base_dir / "venv" / "Scripts" / "python.exe"
    worker_path = Path(__file__).with_name("srt_runtime.py")
    if not python_path.is_file():
        raise RuntimeError(f"Khong tim thay Python runtime cua venv: {python_path}")
    env = os.environ.copy()
    env["BILI2YT_SRT_NATIVE"] = "1"
    process = subprocess.Popen(
        [str(python_path), str(worker_path), engine, str(audio_path)],
        cwd=str(base_dir), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    stderr_lines = []
    def read_stderr():
        for line in process.stderr:
            line = line.rstrip()
            if line:
                stderr_lines.append(line)
                log_callback(line)
    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    try:
        stdout, _ = process.communicate(timeout=7200)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("Whisper vuot qua thoi gian toi da 2 gio.")
    reader.join(timeout=2)
    if process.returncode != 0:
        detail = "\n".join(stderr_lines).strip()
        raise RuntimeError(detail or f"SRT runtime exited with code {process.returncode}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"SRT runtime returned invalid data: {stdout[-500:]}") from exc


def _find_vocal_files(folder: Path) -> list[Path]:
    return sorted(
        [
            path for path in folder.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".wav", ".flac"}
            and "(vocals)" in path.stem.lower()
        ],
        key=lambda path: path.name.casefold(),
    )


def _find_source_videos(root: Path) -> list[Path]:
    return sorted(
        (
            path for path in root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in VIDEO_EXTENSIONS
            and not path.stem.casefold().endswith(("_export", "_da_ghep_vocal"))
        ),
        key=lambda path: str(path).casefold(),
    )


def _extract_original_audio(video_path: Path, log_callback) -> tuple[Path, Path]:
    work_dir = Path(tempfile.mkdtemp(prefix=".bili2yt_original_audio_", dir=str(video_path.parent)))
    audio_path = work_dir / f"{video_path.stem}.flac"
    log_callback(f"[SrtSource] Dang trich audio goc: {video_path.name}")
    result = subprocess.run(
        [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path), "-map", "0:a:0", "-vn",
            "-af", "aresample=async=1000:first_pts=0",
            "-ac", "1", "-ar", "16000", "-c:a", "flac", str(audio_path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200,
    )
    if result.returncode != 0 or not audio_path.is_file() or audio_path.stat().st_size == 0:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise RuntimeError(result.stderr[-1000:] or f"Khong trich duoc audio tu {video_path.name}.")
    video_duration = _audio_duration(video_path)
    audio_duration = _audio_duration(audio_path)
    log_callback(
        f"[SrtSource] Audio goc san sang: {audio_duration:.3f}s / video {video_duration:.3f}s"
    )
    return audio_path, work_dir


def create_srt_batch(
    root_path: str, engine: str = "kphoto-local", source_mode: str = "vocals",
    ocr_regions: dict | None = None, log_callback=print, clean_transcript: bool = False,
) -> list[str]:
    root = Path(root_path)
    source_mode = str(source_mode or "vocals").strip().lower()
    if engine == "rapidocr-v6":
        source_mode = "video"
        sources = _find_source_videos(root)
    elif source_mode == "original":
        sources = _find_source_videos(root)
    else:
        folders = sorted(
            [root, *[path for path in root.rglob("*") if path.is_dir()]],
            key=lambda path: str(path.relative_to(root)).casefold(),
        )
        sources = [path for folder in folders for path in _find_vocal_files(folder)]
    if engine == "rapidocr-v6":
        verb_run, verb_done, verb_all = "Đang quét OCR", "Đã hoàn thành quét OCR", "Đã quét OCR toàn bộ"
    else:
        verb_run, verb_done, verb_all = "Đang tạo SRT", "Đã tạo xong SRT", "Đã tạo SRT toàn bộ"
    log_callback(f"[SrtProgress] START total={len(sources)} source={source_mode}")
    results = []
    errors = []
    for index, source_path in enumerate(sources, 1):
        folder = source_path.parent
        subtitle_dir = folder / "subtitles"
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        base_name = (source_path.stem if source_mode == "original" else
                     re.sub(r"[\s_-]*\(Vocals\)$", "", source_path.stem, flags=re.IGNORECASE))
        log_callback(f"[SrtProgress] ITEM {index}/{len(sources)} {verb_run} :: {source_path.name}")
        work_dir = None
        try:
            if engine == "rapidocr-v6":
                try:
                    from .ocr_subtitles import run_rapidocr_video
                except ImportError:
                    from ocr_subtitles import run_rapidocr_video
                regions = ocr_regions or {}
                region = regions.get(str(source_path.resolve()))
                if not region:
                    raise RuntimeError(
                        f"Chua ve khung OCR rieng cho video {source_path.name}. "
                        "Chon video ben trai, bam Khung OCR va keo vung phu de."
                    )
                transcript = run_rapidocr_video(source_path, log_callback, roi=region)
                audio_path = None
            elif source_mode == "original":
                audio_path, work_dir = _extract_original_audio(source_path, log_callback)
            else:
                audio_path = source_path
            if engine == "rapidocr-v6":
                pass
            elif engine == "kphoto-local":
                transcript = _run_kphoto_chunked(audio_path, log_callback)
            else:
                transcript = _run_whisper_v3(audio_path, log_callback)
            segments = transcript.get("segments", [])
            if not segments:
                raise RuntimeError("Khong nhan dang duoc cau thoai.")
            if source_mode == "video":
                pass  # OCR reads frame timeline directly; nothing to align
            elif source_mode == "original":
                log_callback("[SrtSync] Dung timeline audio goc cua video, khong can co timestamp.")
            else:
                segments = _align_segments_to_video_timeline(segments, source_path, log_callback)
            language = str(
                transcript.get("language") or ("zh" if engine == "kphoto-local" else "auto")
            )
            language = language.lower().split("-")[0].split("_")[0]
            if language not in {"zh", "en", "vi", "ja", "ko", "th", "id", "fr", "de", "es", "pt", "ru", "ar", "auto"}:
                language = "auto"
            output_path = subtitle_dir / f"{language}.srt"
            _write_srt(segments, output_path)
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise RuntimeError(f"Khong ghi duoc file SRT: {output_path}")
            if clean_transcript and engine != "rapidocr-v6" and language == "zh":
                try:
                    try:
                        from .translator import clean_transcript_srt
                    except ImportError:
                        from translator import clean_transcript_srt
                    clean_transcript_srt(output_path, log_callback=log_callback)
                except Exception as clean_exc:
                    log_callback(f"[CleanSRT] Lỗi làm sạch (giữ transcript gốc): {clean_exc}")
            results.append(str(output_path))
            log_callback(f"[SrtProgress] OUTPUT {output_path}")
            log_callback(f"[SrtProgress] DONE {index}/{len(sources)} {verb_done} :: {source_path.name}")
        except Exception as exc:
            errors.append((source_path.name, str(exc)))
            log_callback(f"[SrtProgress] FAIL {index}/{len(sources)} :: {source_path.name} :: {exc}")
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
    log_callback(
        f"[SrtProgress] SUMMARY {verb_all} :: {root.name} :: ok={len(results)} :: total={len(sources)}"
    )
    for name, msg in errors:
        log_callback(f"[SrtProgress] ERRITEM {name} :: {msg}")
    if not results:
        raise RuntimeError(
            "Không tạo được file SRT nào. Kiểm tra model OCR và vùng Khung OCR đã chọn."
        )
    return results
