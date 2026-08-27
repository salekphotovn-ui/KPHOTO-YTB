"""
Module ghép (mux) file vocal đã tách vào lại video gốc bằng ffmpeg.
Giống hệt logic trong GHEPNHACVAVOCAL.bat / auto_run.py:
  -c:v copy -c:a aac -map 0:v:0 -map 1:a:0 -shortest
Tên file output: {ten_goc}_Da_Ghep_Vocal.mp4
"""
import os
import re
import subprocess
from config import FFMPEG_PATH

OUTPUT_SUFFIX = "_Da_Ghep_Vocal.mp4"


def _probe_duration(video_path):
    """Dò tổng thời lượng (giây) của video bằng ffmpeg -i."""
    import re
    proc = subprocess.run(
        [FFMPEG_PATH, "-i", video_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stdout)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    return None


def mux_vocal_into_video(video_path: str, vocal_path: str, output_path: str = None,
                          cleanup_folder: bool = False, log_callback=None) -> str:
    """
    Ghép audio vocal vào video gốc, thay thế track audio cũ.

    :param video_path: file mp4 gốc
    :param vocal_path: file audio vocal đã tách (wav/mp3)
    :param output_path: đường dẫn file kết quả, mặc định "{ten_goc}_Da_Ghep_Vocal.mp4" cùng thư mục video gốc
    :param cleanup_folder: nếu True, sau khi ghép thành công sẽ xoá mọi file khác trong cùng
        thư mục (video gốc, các file .wav tách ra...), chỉ giữ lại file đã ghép và các file .txt
    :param log_callback: hàm nhận log dạng str
    :return: đường dẫn file mp4 sau khi ghép
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if not output_path:
        folder = os.path.dirname(video_path)
        name = os.path.splitext(os.path.basename(video_path))[0]
        output_path = os.path.join(folder, f"{name}{OUTPUT_SUFFIX}")

    video_name = os.path.basename(video_path)
    _log(f"[MuxProgress] VIDEO_START {video_name}")
    cmd = [
        FFMPEG_PATH,
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-i", video_path,
        "-i", vocal_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-y",
        output_path,
    ]

    duration = _probe_duration(video_path)
    if duration:
        _log(f"[MuxProgress] DURATION {duration:.2f}")

    _log(f"[Mux] Đang ghép: {os.path.basename(video_path)} + {os.path.basename(vocal_path)}")
    # Do not dump the full FFmpeg command into the compact UI log: paths and
    # ``-loglevel error`` make it look like a failure and overwhelm the log.
    _log("[Mux] FFmpeg đang ghép video và vocal...")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    last_percent = -1
    progress_keys = {
        "frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time_us=",
        "out_time_ms=", "out_time=", "dup_frames=", "drop_frames=", "speed=",
        "progress=",
    }
    for line in process.stdout:
        line = line.rstrip()
        progress_match = re.search(r"out_time_ms=(\d+)", line)
        if progress_match and duration:
            percent = min(100, int((int(progress_match.group(1)) / 1_000_000) * 100 / duration))
            if percent != last_percent:
                last_percent = percent
                _log(f"[MuxProgress] percent={percent}")
            continue
        if line and not line.startswith(tuple(progress_keys)):
            _log(f"[Mux] {line}")
    process.wait()

    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg (mux) thoát với mã lỗi {process.returncode}")

    _log(f"[Mux] Hoàn tất: {output_path}")

    _log(f"[MuxProgress] VIDEO_DONE {video_name}")
    if cleanup_folder:
        folder = os.path.dirname(os.path.abspath(output_path))
        output_abspath = os.path.abspath(output_path)
        _log("[Mux] Đang dọn dẹp thư mục, chỉ giữ lại file đã ghép và .txt...")
        for f in os.listdir(folder):
            full = os.path.join(folder, f)
            if not os.path.isfile(full):
                continue
            if os.path.abspath(full) == output_abspath:
                continue
            if f.lower().endswith(".txt"):
                continue
            try:
                os.remove(full)
                _log(f"[Mux] Đã xoá: {f}")
            except OSError as e:
                _log(f"[Mux] ⚠️ Không xoá được {f}: {e}")

    return output_path


def mux_folder(folder_path: str, skip_suffix: str = OUTPUT_SUFFIX,
                cleanup_folder: bool = False, log_callback=None) -> list[str]:
    """
    Quét thư mục (kể cả thư mục con), với mỗi file .mp4 gốc (chưa xử lý),
    tự tìm file (Vocals).wav cùng tên trong cùng thư mục để ghép.
    Giống hệt logic quét + ghép trong auto_run.py, nhưng KHÔNG tự tách vocal trước
    (dùng khi bạn đã tách vocal từ tab "Tách vocal" riêng biệt).

    :param cleanup_folder: nếu True, mỗi thư mục sau khi ghép thành công sẽ được dọn dẹp,
        chỉ giữ lại file đã ghép và .txt (áp dụng riêng cho từng video được xử lý)
    :return: danh sách đường dẫn các file đã ghép thành công
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    pairs = []
    for root, _dirs, files in os.walk(folder_path):
        for file in files:
            if file.lower().endswith(".mp4") and not file.endswith(skip_suffix):
                base_name = os.path.splitext(file)[0]
                video_path = os.path.join(root, file)

                vocal_file = None
                for f in os.listdir(root):
                    if f.startswith(base_name) and "(Vocals)" in f and f.lower().endswith((".wav", ".flac")):
                        vocal_file = os.path.join(root, f)
                        break

                if vocal_file:
                    pairs.append((video_path, vocal_file, file))

    total = len(pairs)
    _log(f"[MuxProgress] START total={total}")

    results = []
    for count, (video_path, vocal_file, file) in enumerate(pairs, 1):
        _log(f"\n--- [{count}] Ghép: {file} ---")
        try:
            output_path = mux_vocal_into_video(
                video_path, vocal_file,
                cleanup_folder=cleanup_folder,
                log_callback=log_callback,
            )
            results.append(output_path)
            _log(f"[MuxProgress] {count}/{total}")
        except Exception as e:
            _log(f"❌ Lỗi khi ghép '{file}': {e}")

    if total == 0:
        _log("⚠️ Không tìm thấy cặp (video gốc + file Vocals.wav) nào để ghép trong thư mục này.")
    else:
        _log(f"\n✅ Đã ghép xong {len(results)}/{total} video.")

    return results
