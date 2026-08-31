"""
Module tách vocal / nhạc nền bằng audio-separator (model htdemucs.yaml),
gọi qua CLI y hệt cách auto_run.py của bạn đang dùng.
"""
import os
import subprocess
import threading
import queue
import re
import shutil
import sys
from pathlib import Path
from config import FFMPEG_PATH, LONG_VIDEO_THRESHOLD_SECONDS, SEPARATOR_MODEL


def _system_memory_gb() -> float:
    """Return total system RAM without requiring an extra dependency."""
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong),
                    ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total_phys / (1024 ** 3)
        except (AttributeError, OSError):
            pass
    return 0.0


def _choose_mdx_batch_size(log) -> int:
    """Use batch 2 only when both RAM and free CUDA VRAM are comfortable."""
    try:
        import torch

        ram_gb = _system_memory_gb()
        # Windows reports a nominal 16 GB machine as roughly 15.8 GB.
        if not torch.cuda.is_available() or ram_gb < 12:
            log(f"[Separator] Tự chọn batch_size=1 (RAM {ram_gb:.1f} GB hoặc không có CUDA).")
            return 1
        free_bytes, total_bytes = torch.cuda.mem_get_info()
        free_gb = free_bytes / (1024 ** 3)
        total_gb = total_bytes / (1024 ** 3)
        batch = 2 if total_gb >= 8 and free_gb >= 6 else 1
        log(
            f"[Separator] Tự chọn batch_size={batch} "
            f"(RAM {ram_gb:.1f} GB, VRAM trống {free_gb:.1f}/{total_gb:.1f} GB)."
        )
        return batch
    except Exception as exc:
        log(f"[Separator] Không kiểm tra được bộ nhớ GPU, dùng batch_size=1: {exc}")
        return 1


def _choose_chunk_duration(log) -> int:
    """Choose a safe long-video chunk size from the current machine capacity."""
    ram_gb = _system_memory_gb()
    try:
        import torch

        if not torch.cuda.is_available():
            log(f"[Separator] Chunk tự động: 30 phút (RAM {ram_gb:.1f} GB, không có CUDA).")
            return 30 * 60
        _free_bytes, total_bytes = torch.cuda.mem_get_info()
        vram_gb = total_bytes / (1024 ** 3)
        if ram_gb >= 15 and vram_gb >= 10:
            log(f"[Separator] Chunk tự động: 60 phút (RAM {ram_gb:.1f} GB, VRAM {vram_gb:.1f} GB).")
            return 60 * 60
        log(f"[Separator] Chunk tự động: 30 phút (RAM {ram_gb:.1f} GB, VRAM {vram_gb:.1f} GB).")
        return 30 * 60
    except Exception as exc:
        log(f"[Separator] Không kiểm tra được tài nguyên GPU, dùng chunk 30 phút: {exc}")
        return 30 * 60


def _separator_executable() -> list[str]:
    """Return the argv prefix that runs the audio-separator CLI.

    Frozen build: re-invoke this same executable with ``--run-audio-separator``
    (main.py dispatches it to ``audio_separator.utils.cli``), so the bundled
    torch / onnxruntime are reused and no second Python environment ships.
    Source checkout: the ``audio-separator.exe`` console script from the venv.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-audio-separator"]

    project_root = Path(__file__).resolve().parents[1]
    exe_dir = Path(sys.executable).resolve().parent
    names = ("audio-separator.exe", "audio-separator")
    roots = [
        project_root / "venv" / "Scripts",
        exe_dir,
        exe_dir / "Scripts",
        project_root,
    ]
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return [str(candidate)]
    raise FileNotFoundError(
        "Không tìm thấy audio-separator. Cài audio-separator[gpu] vào venv của V3."
    )


def _is_hevc_source(input_path: str) -> bool:
    """Only force FLAC normalization for HEVC/H.265 sources that need it."""
    ffprobe = Path(FFMPEG_PATH).with_name("ffprobe.exe")
    if not ffprobe.is_file():
        try:
            probe = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-i", input_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            return bool(re.search(r"video:\s*(?:hevc|h265)\b", probe.stderr, re.IGNORECASE))
        except Exception:
            return Path(input_path).suffix.casefold() in {".mp4", ".mkv", ".mov"}
    probe_cmd = [
        str(ffprobe), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", input_path,
    ]
    try:
        probe = subprocess.run(
            probe_cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        codec_text = probe.stdout.strip().casefold()
        if codec_text in {"hevc", "h265"}:
            return True
        return False
    except Exception:
        # MP4 inputs are normalized on the safe path when probing is unavailable.
        return Path(input_path).suffix.casefold() in {".mp4", ".mkv", ".mov"}

def _run_subprocess_idle_timeout(cmd, idle_timeout_seconds, log_callback):
    """
    Chạy subprocess, đọc log real-time. Nếu KHÔNG có dòng log mới nào xuất hiện
    trong idle_timeout_seconds giây liên tục (audio-separator vẫn in tiến trình %
    đều đặn khi còn hoạt động, kể cả video rất dài - im lặng kéo dài là dấu hiệu
    treo do thiếu RAM/VRAM), tự kill tiến trình và raise TimeoutError thay vì
    treo vô thời hạn.
    """
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                part for part in (
                    str(Path(__file__).resolve().parents[1]),
                    os.environ.get("PYTHONPATH", ""),
                ) if part
            ),
        },
    )
    line_queue = queue.Queue()

    def _reader():
        # tqdm commonly refreshes progress with carriage returns instead of newlines.
        buffer = []
        while True:
            char = process.stdout.read(1)
            if not char:
                break
            if char in "\r\n":
                if buffer:
                    line_queue.put("".join(buffer))
                    buffer.clear()
            else:
                buffer.append(char)
        if buffer:
            line_queue.put("".join(buffer))
        line_queue.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    while True:
        try:
            line = line_queue.get(timeout=idle_timeout_seconds)
        except queue.Empty:
            process.kill()
            process.wait()
            raise TimeoutError(
                f"Không có tiến triển nào trong {idle_timeout_seconds // 60} phút - "
                "nghi ngờ tiến trình bị treo (thường do thiếu RAM/VRAM với video quá "
                "dài). Đã tự huỷ tiến trình, chuyển sang video/bước tiếp theo."
            )
        if line is None:
            break
        log_callback(f"[Separator] {line.rstrip()}")

    process.wait()
    return process.returncode


def _media_duration_seconds(input_path: str) -> float:
    """Read duration without decoding the media stream."""
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-i", input_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stdout)
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return 0.0


def _separate_long_vocal_single_pass(input_path: str, output_dir: str, model: str,
                                     duration: float, separator_exe: list, log) -> str:
    """Run one UVR process and let audio-separator chunk the long audio internally."""
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    work_dir = os.path.join(output_dir, f".{base_name}_separator_work")
    os.makedirs(work_dir, exist_ok=True)
    temp_output_dir = os.path.join(work_dir, "separated")
    os.makedirs(temp_output_dir, exist_ok=True)
    final_path = os.path.join(output_dir, f"{base_name}_(Vocals).flac")

    try:
        log("[Separator] Xu ly truc tiep MP4, khong tao audio tam toan bo.")
        chunk_seconds = _choose_chunk_duration(log)
        separator_cmd = [
            *separator_exe, input_path,
            "--model_filename", model,
            "--output_format", "FLAC",
            "--output_dir", temp_output_dir,
            "--chunk_duration", str(chunk_seconds),
            "--mdx_segment_size", "256",
            "--mdx_overlap", "0.25",
            "--mdx_batch_size", "1",
        ]
        log("[Separator] UVR xu ly noi bo theo doan 30 phut, chi nap model mot lan.")
        returncode = _run_subprocess_idle_timeout(
            separator_cmd, idle_timeout_seconds=3600, log_callback=log
        )
        if returncode != 0:
            raise RuntimeError(f"audio-separator thoat voi ma loi {returncode}")

        candidates = [
            os.path.join(temp_output_dir, name)
            for name in os.listdir(temp_output_dir)
            if "(Vocals)" in name and name.lower().endswith(".flac")
        ]
        vocal_file = max(candidates, key=os.path.getmtime) if candidates else None
        if not vocal_file:
            raise FileNotFoundError("Khong tim thay file (Vocals).wav sau khi tach video dai")

        output_duration = _media_duration_seconds(vocal_file)
        if output_duration < duration - max(30.0, duration * 0.01):
            raise RuntimeError(
                f"File vocal chua du thoi luong: {output_duration:.0f}s / {duration:.0f}s"
            )
        os.replace(vocal_file, final_path)
        log(f"[Separator] Da tach du {output_duration / 3600:.2f} gio vocal")
        return final_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _separate_large_source_chunked(input_path: str, output_dir: str, model: str,
                                    duration: float, separator_exe: list, log) -> str:
    """Process oversized media in 30-minute audio chunks without a >4GB temp file."""
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    chunk_seconds = _choose_chunk_duration(log)
    chunk_count = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    work_dir = os.path.join(output_dir, f".{base_name}_separator_large")
    os.makedirs(work_dir, exist_ok=True)
    vocal_chunks = []
    final_path = os.path.join(output_dir, f"{base_name}_(Vocals).flac")

    try:
        batch_size = _choose_mdx_batch_size(log)
        for index in range(chunk_count):
            start = index * chunk_seconds
            length = min(chunk_seconds, duration - start)
            chunk_input = os.path.join(work_dir, f"chunk_{index:04d}.flac")
            chunk_output = os.path.join(work_dir, f"out_{index:04d}")
            os.makedirs(chunk_output, exist_ok=True)
            log(f"[Separator] Äang xá»­ lÃ½ Ä‘oáº¡n {index + 1}/{chunk_count}")
            extract_cmd = [
                FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", input_path,
                "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100", "-c:a", "flac",
                chunk_input,
            ]
            extracted = subprocess.run(
                extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            if extracted.returncode != 0:
                raise RuntimeError(f"KhÃ´ng trÃ­ch xuáº¥t Ä‘Æ°á»£c Ä‘oáº¡n {index + 1}: {extracted.stderr[-800:]}")

            separator_cmd = [
                *separator_exe, chunk_input,
                "--model_filename", model,
                "--output_format", "FLAC",
                "--output_dir", chunk_output,
                "--mdx_segment_size", "256",
                "--mdx_overlap", "0.25",
                "--mdx_batch_size", str(batch_size),
            ]
            if model.lower().endswith((".yaml", ".yml")):
                separator_cmd = [
                    *separator_exe, chunk_input,
                    "--model_filename", model,
                    "--output_format", "FLAC",
                    "--output_dir", chunk_output,
                    "--demucs_segment_size", "10",
                ]
            returncode = _run_subprocess_idle_timeout(
                separator_cmd, idle_timeout_seconds=1200, log_callback=log
            )
            if returncode != 0 and batch_size > 1:
                batch_size = 1
                shutil.rmtree(chunk_output, ignore_errors=True)
                os.makedirs(chunk_output, exist_ok=True)
                separator_cmd[-1] = "1"
                returncode = _run_subprocess_idle_timeout(
                    separator_cmd, idle_timeout_seconds=1200, log_callback=log
                )
            if returncode != 0:
                raise RuntimeError(f"audio-separator lá»—i á»Ÿ Ä‘oáº¡n {index + 1}/{chunk_count}, mÃ£ {returncode}")
            vocal = next(
                (os.path.join(chunk_output, name) for name in os.listdir(chunk_output)
                 if "(Vocals)" in name and name.lower().endswith(".flac")),
                None,
            )
            if not vocal:
                raise FileNotFoundError(f"KhÃ´ng tÃ¬m tháº¥y vocal á»Ÿ Ä‘oáº¡n {index + 1}/{chunk_count}")
            vocal_chunks.append(vocal)
            log(f"[SeparatorGlobal] percent={int((index + 1) * 100 / chunk_count)}")

        join_cmd = [FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y"]
        for vocal in vocal_chunks:
            join_cmd += ["-i", vocal]
        inputs = "".join(f"[{index}:a]" for index in range(len(vocal_chunks)))
        join_cmd += [
            "-filter_complex", f"{inputs}concat=n={len(vocal_chunks)}:v=0:a=1[aout]",
            "-map", "[aout]", "-c:a", "flac", final_path,
        ]
        joined = subprocess.run(
            join_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        if joined.returncode != 0:
            raise RuntimeError(f"KhÃ´ng ná»‘i Ä‘Æ°á»£c cÃ¡c Ä‘oáº¡n vocal: {joined.stderr[-800:]}")
        output_duration = _media_duration_seconds(final_path)
        if output_duration < duration - max(30.0, duration * 0.01):
            raise RuntimeError(f"File vocal chÆ°a Ä‘á»§ thá»i lÆ°á»£ng: {output_duration:.0f}s / {duration:.0f}s")
        log(f"[Separator] ÄÃ£ tÃ¡ch Ä‘á»§ {output_duration / 3600:.2f} giá» vocal")
        return final_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _separate_long_vocal(input_path: str, output_dir: str, model: str,
                         duration: float, separator_exe: list, log) -> str:
    """Separate long media in bounded audio chunks, then join vocal stems."""
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    # Normalize once, then use larger chunks to reduce model startup overhead.
    chunk_seconds = _choose_chunk_duration(log)
    overlap_seconds = 2.0
    chunk_count = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    work_dir = os.path.join(output_dir, f".{base_name}_separator_chunks")
    os.makedirs(work_dir, exist_ok=True)
    normalized_audio = os.path.join(work_dir, "source_normalized.flac")
    vocal_chunks = []
    final_name = f"{base_name}_(Vocals).flac"
    final_path = os.path.join(output_dir, final_name)

    try:
        log("[Separator] Chuẩn hóa audio một lần trước khi chia đoạn.")
        normalize_cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
            "-i", input_path, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
            "-c:a", "flac", normalized_audio,
        ]
        normalized = subprocess.run(
            normalize_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        if normalized.returncode != 0 or not os.path.isfile(normalized_audio):
            raise RuntimeError(
                "Không chuẩn hóa được audio nguồn: "
                f"{normalized.stderr[-800:]}"
            )

        batch_size = _choose_mdx_batch_size(log)
        # Keep one model process alive and let audio-separator split internally.
        separated_dir = os.path.join(work_dir, "separated")
        os.makedirs(separated_dir, exist_ok=True)
        separator_cmd = [
            *separator_exe, normalized_audio,
            "--model_filename", model,
            "--output_format", "FLAC",
            "--output_dir", separated_dir,
            "--chunk_duration", str(chunk_seconds),
            "--mdx_segment_size", "256",
            "--mdx_overlap", "0.25",
            "--mdx_batch_size", str(batch_size),
        ]

        progress_state = {"current": 0, "total": chunk_count}
        oversized_flag = {"hit": False}

        def single_log(message):
            if re.search(r">?\s*4\s*GB|4\s*GiB|larger than 4", message, re.IGNORECASE):
                oversized_flag["hit"] = True
            chunk_match = re.search(r"Processing chunk (\d+)/(\d+)", message)
            if chunk_match:
                current, total = (int(value) for value in chunk_match.groups())
                progress_state["current"] = current
                progress_state["total"] = total
                log(f"[Separator] Đang xử lý đoạn {current}/{total}")
                log(f"[SeparatorGlobal] percent={int((current - 1) * 100 / total)}")
            percent_match = re.search(r"(?<!\d)(\d{1,3})%", message)
            if percent_match and progress_state["current"]:
                local_percent = min(100, int(percent_match.group(1)))
                current = progress_state["current"]
                total = progress_state["total"]
                global_percent = int(
                    ((current - 1 + local_percent / 100) / total) * 100
                )
                log(f"[SeparatorGlobal] percent={global_percent}")
            elif re.search(
                r"error|exception|failed|out of memory|not found|traceback",
                message,
                re.IGNORECASE,
            ):
                log(f"[Separator] {message.rstrip()}")

        log("[Separator] Tách chunk nội bộ, giữ model GPU trong một tiến trình.")
        returncode = _run_subprocess_idle_timeout(
            separator_cmd, idle_timeout_seconds=3600, log_callback=single_log
        )
        if oversized_flag["hit"]:
            log("[Separator] audio-separator tu choi file tren 4GB - chuyen sang tach truc tiep tung doan.")
            return _separate_large_source_chunked(
                input_path, output_dir, model, duration, separator_exe, log
            )
        if returncode != 0 and batch_size > 1:
            log("[Separator] GPU không ổn định với batch_size=2, hạ xuống 1 và thử lại.")
            shutil.rmtree(separated_dir, ignore_errors=True)
            os.makedirs(separated_dir, exist_ok=True)
            separator_cmd[-1] = "1"
            returncode = _run_subprocess_idle_timeout(
                separator_cmd, idle_timeout_seconds=3600, log_callback=single_log
            )
        if returncode != 0:
            raise RuntimeError(f"audio-separator lỗi, mã {returncode}")

        candidates = [
            os.path.join(separated_dir, name)
            for name in os.listdir(separated_dir)
            if "(Vocals)" in name and name.lower().endswith(".flac")
        ]
        vocal_file = max(candidates, key=os.path.getmtime) if candidates else None
        if not vocal_file:
            raise FileNotFoundError("Không tìm thấy file vocal sau khi xử lý chunk nội bộ")
        output_duration = _media_duration_seconds(vocal_file)
        if output_duration < duration - max(30.0, duration * 0.01):
            raise RuntimeError(
                f"File vocal chưa đủ thời lượng: {output_duration:.0f}s / {duration:.0f}s"
            )
        os.replace(vocal_file, final_path)
        log(f"[Separator] Đã nối đủ {output_duration / 3600:.2f} giờ vocal")
        log("[SeparatorGlobal] percent=100")
        return final_path

        for index in range(chunk_count):
            start = index * chunk_seconds
            extract_start = max(0.0, start - overlap_seconds) if index else start
            length = min(chunk_seconds, duration - start) + (start - extract_start)
            chunk_input = os.path.join(work_dir, f"chunk_{index:04d}.flac")
            chunk_output_dir = os.path.join(work_dir, f"out_{index:04d}")
            os.makedirs(chunk_output_dir, exist_ok=True)
            log(f"[Separator] CHUNK {index + 1}/{chunk_count}")
            log(f"[SeparatorGlobal] percent={int(index * 100 / chunk_count)}")

            def chunk_log(message, chunk_index=index):
                percent_match = re.search(r"(?<!\d)(\d{1,3})%", message)
                if percent_match:
                    local_percent = min(100, int(percent_match.group(1)))
                    global_percent = int(
                        ((chunk_index + local_percent / 100) / chunk_count) * 100
                    )
                    log(f"[SeparatorGlobal] percent={global_percent}")
                elif re.search(
                    r"error|exception|failed|out of memory|not found|traceback",
                    message,
                    re.IGNORECASE,
                ):
                    log(f"[Separator] {message.rstrip()}")

            extract_cmd = [
                FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
                "-ss", f"{extract_start:.3f}", "-t", f"{length:.3f}",
                "-i", normalized_audio, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
                "-c:a", "flac", chunk_input,
            ]
            extracted = subprocess.run(
                extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            if extracted.returncode != 0:
                raise RuntimeError(
                    f"Không trích xuất được đoạn {index + 1}/{chunk_count}: "
                    f"{extracted.stderr[-800:]}"
                )

            separator_cmd = [
                *separator_exe, chunk_input,
                "--model_filename", model,
                "--output_format", "FLAC",
                "--output_dir", chunk_output_dir,
                "--mdx_segment_size", "256",
                "--mdx_overlap", "0.25",
                "--mdx_batch_size", str(batch_size),
            ]
            if model.lower().endswith((".yaml", ".yml")):
                separator_cmd += ["--demucs_segment_size", "10"]
            returncode = _run_subprocess_idle_timeout(
                separator_cmd, idle_timeout_seconds=1200, log_callback=chunk_log
            )
            if returncode != 0 and batch_size > 1:
                log("[Separator] GPU không ổn định với batch_size=2, hạ xuống 1 và thử lại đoạn này.")
                batch_size = 1
                shutil.rmtree(chunk_output_dir, ignore_errors=True)
                os.makedirs(chunk_output_dir, exist_ok=True)
                separator_cmd[-1] = "1"
                returncode = _run_subprocess_idle_timeout(
                    separator_cmd, idle_timeout_seconds=1200, log_callback=chunk_log
                )
            if returncode != 0:
                raise RuntimeError(
                    f"audio-separator lỗi ở đoạn {index + 1}/{chunk_count}, "
                    f"mã lỗi {returncode}"
                )
            vocal = next(
                (
                    os.path.join(chunk_output_dir, name)
                    for name in os.listdir(chunk_output_dir)
                    if "(Vocals)" in name and name.lower().endswith(".flac")
                ),
                None,
            )
            if not vocal:
                raise FileNotFoundError(
                    f"Không tìm thấy vocal ở đoạn {index + 1}/{chunk_count}"
                )
            vocal_chunks.append(vocal)
            log(f"[SeparatorGlobal] percent={int((index + 1) * 100 / chunk_count)}")

        join_cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
        ]
        for vocal in vocal_chunks:
            join_cmd += ["-i", vocal]
        if len(vocal_chunks) == 1:
            join_cmd += ["-map", "0:a"]
        else:
            filters = []
            previous = "[0:a]"
            for index in range(1, len(vocal_chunks)):
                output = f"[a{index}]"
                filters.append(
                    f"{previous}[{index}:a]acrossfade=d={overlap_seconds}:"
                    f"c1=tri:c2=tri{output}"
                )
                previous = output
            join_cmd += ["-filter_complex", ";".join(filters), "-map", previous]
        join_cmd += ["-c:a", "flac", final_path]
        joined = subprocess.run(
            join_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        if joined.returncode != 0:
            raise RuntimeError(f"Không nối được các đoạn vocal: {joined.stderr[-800:]}")

        output_duration = _media_duration_seconds(final_path)
        if output_duration < duration - max(30.0, duration * 0.01):
            raise RuntimeError(
                f"File vocal chưa đủ thời lượng: {output_duration:.0f}s / {duration:.0f}s"
            )
        log(f"[Separator] Đã nối đủ {output_duration / 3600:.2f} giờ vocal")
        return final_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def separate_vocal(input_path: str, output_dir: str = None,
                    model: str = None, log_callback=None) -> str:
    """
    Tách vocal ra khỏi 1 file mp4/wav/mp3, trả về đường dẫn file vocal (.wav).

    :param input_path: đường dẫn file video/audio gốc
    :param output_dir: thư mục xuất kết quả, mặc định = cùng thư mục với input_path
    :param model: tên model audio-separator, mặc định "htdemucs.yaml"
    :param log_callback: hàm nhận log dạng str
    :return: đường dẫn file (Vocals).wav vừa tạo
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    output_dir = output_dir or os.path.dirname(input_path)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    separator_exe = _separator_executable()
    duration = _media_duration_seconds(input_path)
    requested_model = model or SEPARATOR_MODEL
    # Keep htdemucs as the default; duration alone must not switch models.
    selected_model = requested_model
    estimated_pcm_bytes = duration * 44100 * 2 * 4
    if estimated_pcm_bytes >= 3.8 * (1024 ** 3):
        _log(f"[Separator] Video dài {duration / 3600:.2f} giờ - chia đoạn 30 phút.")
        estimated_pcm_bytes = duration * 44100 * 2 * 4
        if estimated_pcm_bytes >= 3.8 * (1024 ** 3):
            _log("[Separator] Audio trung gian vuot 4GB - tach truc tiep tung doan tu video.")
            return _separate_large_source_chunked(
                input_path, output_dir, selected_model, duration, separator_exe, _log
            )
        return _separate_long_vocal(
            input_path, output_dir, selected_model, duration, separator_exe, _log
        )
    # Normal MP4 is tried directly for speed. HEVC/H.265 is normalized first
    # because its audio/container combinations are the common failure case.
    short_work_dir = os.path.join(output_dir, f".{base_name}_separator_input")
    short_output_dir = os.path.join(short_work_dir, "separated")
    os.makedirs(short_output_dir, exist_ok=True)
    separator_input = os.path.join(short_work_dir, f"{base_name}.flac")
    # audio-separator uses soundfile/librosa and cannot reliably open video
    # containers on Windows, even when the MP4 itself is perfectly valid.
    # Normalize every video to a temporary FLAC; direct mode remains for audio
    # files such as WAV/MP3.
    direct_input = Path(input_path).suffix.casefold() not in {".mp4", ".mkv", ".mov", ".avi", ".webm"}
    if direct_input:
        separator_input = input_path
        _log("[Separator] Nguon khong phai HEVC - thu tach truc tiep, bo qua FLAC tam.")
    else:
        normalize_cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
            "-i", input_path, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
            "-c:a", "flac", separator_input,
        ]
        normalized = subprocess.run(
            normalize_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        if normalized.returncode != 0 or not os.path.isfile(separator_input):
            shutil.rmtree(short_work_dir, ignore_errors=True)
            raise RuntimeError(f"Khong chuan hoa duoc audio nguon: {normalized.stderr[-800:]}")

    # Long files otherwise make Demucs materialize tens of GB in CPU memory.
    automatic_segment_size = None

    def _build_cmd(segment_size=None):
        cmd = [
            *separator_exe,
            separator_input,
            "--model_filename", selected_model,
            "--output_format", "WAV",
            "--output_dir", short_output_dir,
        ]
        if segment_size:
            cmd += ["--demucs_segment_size", str(segment_size)]
        return cmd

    # Bắt tín hiệu "hết VRAM" (CUDA OOM) ngay trong lúc log chạy qua, để biết
    # có cần tự động thử lại với segment nhỏ hơn hay không - không cần bạn
    # phải tự cấu hình tay cho từng video dài.
    oom_flag = {"hit": False}

    def _log_and_watch_oom(msg):
        lowered = msg.lower()
        if any(
            marker in lowered
            for marker in (
                "cuda out of memory",
                "outofmemoryerror",
                "defaultcpuallocator",
                "not enough memory",
            )
        ):
            oom_flag["hit"] = True
        _log(msg)

    cmd = _build_cmd(segment_size=automatic_segment_size)
    _log(f"[Separator] Đang tách vocal: {input_path}")
    _log(f"[Separator] Model: {selected_model}")
    if automatic_segment_size:
        _log(
            f"[Separator] Video dài {duration / 3600:.2f} giờ - xử lý theo đoạn "
            f"{automatic_segment_size} giây để tiết kiệm RAM."
        )
    _log(f"[Separator] Lệnh chạy: {' '.join(cmd)}")

    returncode = _run_subprocess_idle_timeout(
        cmd, idle_timeout_seconds=1200, log_callback=_log_and_watch_oom
    )

    if returncode != 0 and oom_flag["hit"]:
        fallback_segment_size = 5 if automatic_segment_size else 10
        _log(
            f"[Separator] ⚠️ Hết VRAM (CUDA out of memory) - tự động thử lại với "
            f"--demucs_segment_size={fallback_segment_size} (xử lý theo đoạn nhỏ hơn, "
            f"chậm hơn nhưng ít tốn VRAM hơn)..."
        )
        oom_flag["hit"] = False
        cmd = _build_cmd(segment_size=fallback_segment_size)
        _log(f"[Separator] Lệnh chạy lại: {' '.join(cmd)}")
        returncode = _run_subprocess_idle_timeout(
            cmd, idle_timeout_seconds=1200, log_callback=_log_and_watch_oom
        )

    if returncode != 0:
        _log("[Separator] Tach truc tiep that bai - chuyen sang FLAC va thu lai.")
        # Never write the fallback audio beside the input variable when direct
        # mode points at the original MP4; FFmpeg rejects input == output.
        fallback_input = os.path.join(short_work_dir, f"{base_name}.fallback.flac")
        normalize_cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
            "-i", input_path, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
            "-c:a", "flac", fallback_input,
        ]
        normalized = subprocess.run(
            normalize_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        if normalized.returncode == 0 and os.path.isfile(fallback_input):
            separator_input = fallback_input
            oom_flag["hit"] = False
            cmd = _build_cmd(segment_size=automatic_segment_size)
            returncode = _run_subprocess_idle_timeout(
                cmd, idle_timeout_seconds=1200, log_callback=_log_and_watch_oom
            )
        else:
            _log(
                f"[Separator] Khong tao duoc FLAC tam (ma {normalized.returncode}): "
                f"{normalized.stderr[-800:]}"
            )

    if returncode != 0:
        raise RuntimeError(f"audio-separator thoát với mã lỗi {returncode}")

    # Tìm đúng file (Vocals).wav vừa được tạo ra, giống logic trong auto_run.py
    candidates = [
        os.path.join(short_output_dir, f)
        for f in os.listdir(short_output_dir)
        if f.startswith(base_name) and "(Vocals)" in f and f.lower().endswith(".wav")
    ]
    vocal_file = max(candidates, key=os.path.getmtime) if candidates else None

    if not vocal_file:
        raise FileNotFoundError(
            f"Không tìm thấy file (Vocals).wav trong {output_dir} sau khi tách. "
            "Kiểm tra lại model hoặc log phía trên."
        )

    _log(f"[Separator] Tách xong: {vocal_file}")
    canonical_file = os.path.join(output_dir, f"{base_name}_(Vocals).wav")
    if os.path.abspath(vocal_file) != os.path.abspath(canonical_file):
        os.replace(vocal_file, canonical_file)
        vocal_file = canonical_file

    shutil.rmtree(short_work_dir, ignore_errors=True)
    return vocal_file


def separate_folder(folder_path: str, model: str = SEPARATOR_MODEL,
                     skip_suffix: str = "_Da_Ghep_Vocal.mp4", log_callback=None) -> list[tuple[str, str]]:
    """
    Quét toàn bộ thư mục (kể cả thư mục con) và tách vocal cho từng file .mp4,
    bỏ qua các file đã xử lý xong (kết thúc bằng skip_suffix).

    :return: danh sách các cặp (video_path, vocal_path) đã tách thành công
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    video_paths = []
    for root, _dirs, files in os.walk(folder_path):
        for file in files:
            if file.lower().endswith(".mp4") and not file.endswith(skip_suffix):
                video_paths.append(os.path.join(root, file))

    total = len(video_paths)
    _log(f"[SeparateProgress] START total={total}")

    results = []
    errors = []  # danh sách (tên file, thông báo lỗi) để tổng kết cuối cùng
    for count, video_path in enumerate(video_paths, 1):
        file = os.path.basename(video_path)
        root = os.path.dirname(video_path)
        _log(f"[SeparateProgress] ITEM {count}/{total} {file}")
        try:
            vocal_path = separate_vocal(video_path, output_dir=root,
                                         model=model, log_callback=log_callback)
            results.append((video_path, vocal_path))
            _log(f"[SeparateProgress] DONE {count}/{total} {file}")
        except Exception as e:
            errors.append((file, str(e)))
            _log(f"[SeparateProgress] FAIL {count}/{total} {file} :: {e}")

    folder_name = os.path.basename(os.path.normpath(folder_path))
    _log(f"[SeparateProgress] ALLDONE folder={folder_name} ok={len(results)} total={total}")
    for file, err_msg in errors:
        _log(f"[SeparateProgress] ERRITEM {file} :: {err_msg}")

    return results
