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
from config import FFMPEG_PATH


_WHISPER_MODEL = None
_WHISPER_MODEL_KEY = None


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


def _run_kphoto_local(audio_path: Path, log_callback) -> dict:
    # The UI currently runs on Python 3.11 while the bundled ML packages are
    # installed for Python 3.12. Run model loading in the matching interpreter.
    if sys.version_info[:2] != (3, 12) and not os.getenv("BILI2YT_SRT_NATIVE"):
        return _run_srt_native(audio_path, "kphoto-local", log_callback)
    from funasr import AutoModel

    roots = [
        Path(__file__).resolve().parents[1] / "models" / "kphoto-local" / "zh",
    ]
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
    model = AutoModel(
        model=str(model_root / "zh"),
        vad_model=str(model_root / "v"),
        punc_model=str(model_root / "p"),
        device=device,
        disable_update=True,
    )
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


def _run_kphoto_chunked(audio_path: Path, log_callback) -> dict:
    """Transcribe long audio in overlapping chunks and restore global timestamps."""
    duration = _audio_duration(audio_path)
    # FunASR already performs VAD internally; avoid reloading the large model
    # for ordinary 1-3 hour videos. Chunk only unusually long audio.
    if duration <= 3 * 60 * 60:
        return _run_kphoto_local(audio_path, log_callback)

    chunk_seconds = 60 * 60 if duration <= 6 * 60 * 60 else 30 * 60
    overlap = 2.0
    chunk_count = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    work_dir = Path(tempfile.mkdtemp(prefix="bili2yt_srt_", dir=str(audio_path.parent)))
    combined = []
    try:
        log_callback(f"KPHOTO-Local: chia {duration / 3600:.2f} gio thanh {chunk_count} doan ({chunk_seconds // 60} phut).")
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
            chunk_path = work_dir / f"chunk_{index:04d}.flac"
            extracted = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(audio_path), "-c:a", "flac", str(chunk_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
            )
            if extracted.returncode != 0:
                raise RuntimeError(extracted.stderr[-800:] or "Khong tach duoc audio chunk Whisper.")
            iterator, _info = model.transcribe(str(chunk_path), vad_filter=True, vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False, beam_size=5)
            for segment in iterator:
                text = str(segment.text or "").strip()
                global_start = float(segment.start) + start
                global_end = float(segment.end) + start
                if text and global_end > global_start and (index == 0 or global_start >= index * chunk_seconds):
                    combined.append({"start": global_start, "end": global_end, "text": text})
            completed.add(index)
            checkpoint.write_text(json.dumps({"completed": sorted(completed), "segments": combined}, ensure_ascii=False), encoding="utf-8")
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
    segments_iter, info = model.transcribe(
        str(audio_path),
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        beam_size=5,
    )
    segments = []
    segment_count = 0
    last_percent = -1
    duration = max(0.0, _audio_duration(audio_path))
    for segment in segments_iter:
        text = str(segment.text or "").strip()
        if text and segment.end > segment.start:
            segments.append({"start": segment.start, "end": segment.end, "text": text})
            segment_count += 1
            if segment_count == 1 or segment_count % 10 == 0:
                log_callback(f"[SrtProgress] Whisper đã nhận dạng {segment_count} đoạn, đến {segment.end:.1f}s")
            if duration > 0:
                percent = max(0, min(99, int(float(segment.end) * 100 / duration)))
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


def create_srt_batch(root_path: str, engine: str = "kphoto-local", log_callback=print) -> list[str]:
    root = Path(root_path)
    folders = sorted(
        [root, *[path for path in root.rglob("*") if path.is_dir()]],
        key=lambda path: str(path.relative_to(root)).casefold(),
    )
    vocal_files = [path for folder in folders for path in _find_vocal_files(folder)]
    log_callback(f"[SrtProgress] START total={len(vocal_files)}")
    results = []
    for index, vocal_path in enumerate(vocal_files, 1):
        folder = vocal_path.parent
        subtitle_dir = folder / "subtitles"
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        base_name = re.sub(r"\s*\(Vocals\)$", "", vocal_path.stem, flags=re.IGNORECASE)
        log_callback(f"[SrtProgress] ITEM {index}/{len(vocal_files)} {vocal_path.name}")
        try:
            if engine == "kphoto-local":
                transcript = _run_kphoto_local(vocal_path, log_callback)
            else:
                transcript = _run_whisper_v3(vocal_path, log_callback)
            segments = transcript.get("segments", [])
            if not segments:
                raise RuntimeError("Khong nhan dang duoc cau thoai.")
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
            results.append(str(output_path))
            log_callback(f"[SrtProgress] OUTPUT {output_path}")
            log_callback(f"[SrtProgress] DONE {index}/{len(vocal_files)}")
        except Exception as exc:
            log_callback(f"[SrtProgress] FAIL {index}/{len(vocal_files)} {vocal_path.name}: {exc}")
    return results
