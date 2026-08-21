"""
Module ghép nhiều file video part nhỏ (1.mp4, 2.mp4, 3.mp4...) thành 1 file
bằng ffmpeg concat demuxer (không encode lại, giữ nguyên chất lượng, siêu nhanh).
Logic sắp xếp thứ tự file giống hệt GHEP VIDEO BILIBILI.bat (sort tự nhiên theo số).
"""
import os
import re
import shutil
import subprocess
from config import FFMPEG_PATH


def _media_duration_seconds(path: str) -> float:
    try:
        result = subprocess.run(
            [
                FFMPEG_PATH,
                "-hide_banner",
                "-analyzeduration", "1000000",
                "-probesize", "1000000",
                "-i", path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stdout)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _natural_sort_key(path: str):
    """Sắp xếp 'part2' trước 'part10' thay vì theo thứ tự chữ cái thông thường."""
    name = os.path.basename(path)
    return [int(chunk) if chunk.isdigit() else chunk.lower()
            for chunk in re.split(r"(\d+)", name)]


def _merged_title(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    match = re.match(r"^(.*?)\s*-\s*Tập\s*\d+\s*$", stem, re.IGNORECASE)
    return match.group(1).strip() if match else stem


def _concat_auto_normalized(sorted_files, output_path, final_output_path,
                            output_collides, delete_originals, log):
    """Normalize each input first, then copy-join or GPU-encode if needed."""
    first_dir = os.path.dirname(sorted_files[0])
    normalized_dir = os.path.join(first_dir, ".concat_normalized")
    list_path = os.path.join(first_dir, "list_concat.txt")
    os.makedirs(normalized_dir, exist_ok=True)
    normalized_files = []
    try:
        total = len(sorted_files)
        for index, source_path in enumerate(sorted_files, 1):
            normalized_path = os.path.join(normalized_dir, f"part_{index:04d}.mp4")
            log(f"[ConcatProgress] ITEM {index}/{total} {os.path.basename(source_path)}")
            normalize_cmd = [
                FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
                "-i", source_path,
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
                "-af", "pan=stereo|c0=c0|c1=c1,aresample=44100:async=1:first_pts=0",
                "-c:a", "aac", "-b:a", "192k", normalized_path,
            ]
            normalized = subprocess.run(
                normalize_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            if normalized.returncode != 0:
                raise RuntimeError(
                    f"Khong chuan hoa duoc {os.path.basename(source_path)}: "
                    f"{normalized.stderr[-1000:]}"
                )
            normalized_files.append(normalized_path)
            percent = int(index * 80 / total)
            log(f"[ConcatProgress] percent={percent}")
            log(f"[ConcatProgress] {percent}/100")

        with open(list_path, "w", encoding="utf-8") as stream:
            for path in normalized_files:
                stream.write(f"file 'file:{path.replace(chr(39), chr(39) + chr(92) + chr(39))}'\n")

        join_base = [
            FFMPEG_PATH, "-hide_banner", "-nostats", "-progress", "pipe:1",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-map", "0:v:0", "-map", "0:a:0", "-y",
        ]
        fast_cmd = join_base + ["-c", "copy", output_path]
        log("[Concat] Auto: audio da chuan hoa, thu ghep nhanh...")
        fast = subprocess.run(
            fast_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        if fast.returncode != 0:
            log("[Concat] Auto: thong so hinh lech, chuyen sang encode GPU...")
            gpu_cmd = join_base + [
                "-c:v", "hevc_nvenc", "-preset", "p4", "-cq", "28",
                "-c:a", "copy", output_path,
            ]
            gpu = subprocess.run(
                gpu_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            if gpu.returncode != 0:
                raise RuntimeError(f"Auto encode GPU that bai:\n{gpu.stdout[-1500:]}")
        log("[ConcatProgress] percent=100")
        log("[ConcatProgress] 100/100")
        if output_collides:
            os.replace(output_path, final_output_path)
            output_path = final_output_path
        if delete_originals:
            for path in sorted_files:
                if os.path.abspath(path) != os.path.abspath(output_path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        log(f"[Concat] Ghép xong: {output_path}")
        return output_path
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
        shutil.rmtree(normalized_dir, ignore_errors=True)


def concat_videos(file_paths: list[str], output_path: str = None,
                   delete_originals: bool = False, log_callback=None) -> str:
    """
    Ghép nhiều file video thành 1 file duy nhất.

    :param file_paths: danh sách đường dẫn các file part (thứ tự sẽ tự sắp lại theo số trong tên)
    :param output_path: đường dẫn file kết quả, mặc định là "DONE.mp4" cùng thư mục với file đầu tiên
    :param delete_originals: nếu True, xoá các file part gốc sau khi ghép thành công
    :param log_callback: hàm nhận log dạng str
    :return: đường dẫn file đã ghép
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if not file_paths:
        raise ValueError("Chưa chọn file nào để ghép")

    sorted_files = sorted(file_paths, key=_natural_sort_key)
    _log("[Concat] Thứ tự ghép sau khi sắp xếp:")
    for f in sorted_files:
        _log(f"  - {os.path.basename(f)}")

    first_dir = os.path.dirname(sorted_files[0])
    if not output_path:
        output_path = os.path.join(first_dir, f"{_merged_title(sorted_files[0])}_ghép.mp4")
    list_txt_path = os.path.join(first_dir, "list_concat.txt")

    # Nếu tên file kết quả trùng với 1 trong các file part gốc - KHÔNG ghi trực
    # tiếp vào đó (ffmpeg cần đọc chính file này làm input). Ghép ra file tạm
    # trước, chỉ đổi tên đè vào đúng vị trí mong muốn SAU KHI ghép xong.
    final_output_path = output_path
    output_collides = any(os.path.abspath(output_path) == os.path.abspath(p) for p in sorted_files)
    if output_collides:
        tmp_dir = os.path.dirname(output_path)
        tmp_name = f".{os.path.splitext(os.path.basename(output_path))[0]}_tmp_concat.mp4"
        output_path = os.path.join(tmp_dir, tmp_name)

    return _concat_auto_normalized(
        sorted_files, output_path, final_output_path, output_collides,
        delete_originals, _log,
    )

    # Ghi file danh sách cho ffmpeg concat demuxer.
    # Thêm tiền tố "file:" trước mỗi đường dẫn để ép ffmpeg hiểu đây là đường dẫn
    # tuyệt đối - tránh lỗi ffmpeg tự nối nhầm thêm thư mục list_concat.txt vào trước
    # đường dẫn ổ đĩa Windows (D:/...), gây lỗi "Impossible to open" do bị nhân đôi path.
    with open(list_txt_path, "w", encoding="utf-8") as f:
        for path in sorted_files:
            escaped = path.replace("'", "'\\''")
            f.write(f"file 'file:{escaped}'\n")

    total_seconds = sum(_media_duration_seconds(path) for path in sorted_files)
    _log(f"[ConcatProgress] TOTAL seconds={total_seconds:.3f}")

    cmd = [
        FFMPEG_PATH,
        "-hide_banner",
        "-nostats",
        "-progress", "pipe:1",
        "-f", "concat",
        "-safe", "0",
        "-fflags", "+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", list_txt_path,
        "-map", "0:v:0",
        "-map", "0:a:0",
        # Keep HEVC video untouched, but normalize AAC timestamps/packets.
        "-c:v", "copy",
        "-af", "pan=stereo|c0=c0|c1=c1,aresample=44100:async=1:first_pts=0",
        "-c:a", "aac",
        "-b:a", "192k",
        "-y",
        output_path,
    ]

    _log(f"[Concat] Đang chạy: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    last_frame_count = 0
    last_seconds = 0.0
    process_output = []
    for line in process.stdout:
        process_output.append(line.rstrip())
        _log(f"[Concat] {line.rstrip()}")
        frame_match = re.search(r"frame=\s*(\d+)", line)
        if frame_match:
            last_frame_count = int(frame_match.group(1))
        time_match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
        if time_match:
            h, m, s = time_match.groups()
            last_seconds = int(h) * 3600 + int(m) * 60 + float(s)
        progress_match = re.search(r"out_time_ms=(\d+)", line)
        if progress_match and total_seconds > 0:
            current_seconds = int(progress_match.group(1)) / 1_000_000
            percent = min(100, int(current_seconds * 100 / total_seconds))
            _log(f"[ConcatProgress] percent={percent}")
            _log(f"[ConcatProgress] {percent}/100")
    process.wait()

    try:
        os.remove(list_txt_path)
    except OSError:
        pass

    if process.returncode != 0:
        # Normalize each input before fallback so malformed AAC cannot abort
        # the entire GPU encode.
        normalized_dir = os.path.join(first_dir, ".concat_normalized")
        os.makedirs(normalized_dir, exist_ok=True)
        normalized_files = []
        for index, source_path in enumerate(sorted_files):
            normalized_path = os.path.join(normalized_dir, f"part_{index:04d}.mp4")
            normalize_cmd = [
                FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
                "-i", source_path,
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "copy",
                "-af", "pan=stereo|c0=c0|c1=c1,aresample=44100:async=1:first_pts=0",
                "-c:a", "aac", "-b:a", "192k", normalized_path,
            ]
            normalized = subprocess.run(
                normalize_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if normalized.returncode != 0:
                shutil.rmtree(normalized_dir, ignore_errors=True)
                raise RuntimeError(
                    f"Khong chuan hoa duoc audio cua {os.path.basename(source_path)}: "
                    f"{normalized.stderr[-1200:]}"
                )
            normalized_files.append(normalized_path)

        with open(list_txt_path, "w", encoding="utf-8") as f:
            for path in normalized_files:
                escaped = path.replace("'", "'\\''")
                f.write(f"file 'file:{escaped}'\n")
        fallback_path = f"{output_path}.auto_encode.mp4"
        fallback_cmd = list(cmd)
        fallback_cmd[-1] = fallback_path
        _log("[Concat] Audio da chuan hoa, ghep lai bang video copy de tang toc...")
        _log("[Concat] Ghép nhanh lỗi, chuyển sang encode GPU tự động...")
        fallback = subprocess.run(
            fallback_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        process_output.extend(fallback.stdout.splitlines())
        if fallback.returncode == 0 and os.path.isfile(fallback_path):
            os.replace(fallback_path, output_path)
            shutil.rmtree(normalized_dir, ignore_errors=True)
            _log("[Concat] Đã chuyển sang encode GPU thành công.")
            process.returncode = 0
        else:
            details = "\n".join(process_output[-20:])
            shutil.rmtree(normalized_dir, ignore_errors=True)
            raise RuntimeError(f"ffmpeg concat failed ({process.returncode})\n{details}")

    try:
        os.remove(list_txt_path)
    except OSError:
        pass

    if process.returncode != 0:
        details = "\n".join(process_output[-20:])
        raise RuntimeError(f"ffmpeg concat failed ({process.returncode})\n{details}")
        raise RuntimeError(f"ffmpeg (concat) thoát với mã lỗi {process.returncode}")

    if output_collides:
        os.replace(output_path, final_output_path)
        output_path = final_output_path

    _log(f"[Concat] Ghép xong: {output_path}")

    # Kiểm tra an toàn trước khi xoá file gốc: nếu số khung hình quá ít so với
    # thời lượng (ví dụ dưới 5 khung hình/giây trung bình), rất có thể video bị lỗi
    # kiểu "chỉ có ảnh tĩnh + âm thanh" (do ffmpeg chọn nhầm luồng) - không xoá file gốc
    # trong trường hợp này, dù người dùng có tích chọn xoá, để tránh mất dữ liệu gốc.
    suspicious = last_seconds > 5 and last_frame_count < last_seconds * 5

    if delete_originals:
        if suspicious:
            _log(
                f"[Concat] ⚠️ CẢNH BÁO: chỉ có {last_frame_count} khung hình cho "
                f"{last_seconds:.0f} giây - nghi ngờ video bị lỗi (thiếu khung hình). "
                "KHÔNG xoá file gốc. Hãy tự kiểm tra file DONE.mp4 bằng mắt trước khi xoá tay."
            )
        else:
            _log("[Concat] Đang xoá các file part gốc...")
            for path in sorted_files:
                if os.path.abspath(path) == os.path.abspath(output_path):
                    _log(f"[Concat] Giữ lại (đây là file kết quả): {os.path.basename(path)}")
                    continue
                try:
                    os.remove(path)
                    _log(f"[Concat] Đã xoá: {os.path.basename(path)}")
                except OSError as e:
                    _log(f"[Concat] ⚠️ Không xoá được {os.path.basename(path)}: {e}")

    return output_path
