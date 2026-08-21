"""Export video with subtitle, natural blur regions, and logo overlays."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from config import FFMPEG_PATH


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}


def _ffmpeg_filter_path(path: Path) -> str:
    # Escape characters meaningful inside an FFmpeg filter expression.
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\\\'")


def _encoder() -> str:
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        if "h264_nvenc" in result.stdout:
            return "h264_nvenc"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "libx264"


def _subtitle_path(video_path: Path) -> Path | None:
    subtitle_dir = video_path.parent / "subtitles"
    candidate = subtitle_dir / "en.srt"
    if candidate.exists():
        return candidate
    return None


def _duration_seconds(video_path: Path) -> float:
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-i", str(video_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0.0


def _build_filter(video_path: Path, blur_boxes: list[list[float]], logo_path: str) -> str:
    chains = ["[0:v]setpts=PTS-STARTPTS[base]"]
    current = "[base]"
    for index, box in enumerate(blur_boxes):
        x, y, width, height = [max(0.0, min(1.0, float(value))) for value in box]
        # Scale down/up the selected crop to make a smooth, natural blur.
        crop = f"crop=iw*{width:.6f}:ih*{height:.6f}:iw*{x:.6f}:ih*{y:.6f},scale=iw/12:ih/12:flags=bilinear,scale=iw*12:ih*12:flags=lanczos"
        chains.append(
            f"{current}split=2[keep{index}][patch{index}];"
            f"[patch{index}]{crop}[blur{index}];"
            f"[keep{index}][blur{index}]overlay=x=main_w*{x:.6f}:y=main_h*{y:.6f}[b{index}]"
        )
        current = f"[b{index}]"
    if logo_path:
        logo_input = _ffmpeg_filter_path(Path(logo_path))
        chains.append(f"movie='{logo_input}',format=rgba[logo];{current}[logo]scale2ref=w=main_w*0.14:h=main_h*0.14[logo_s][base_s];[base_s][logo_s]overlay=x=main_w*0.82:y=main_h*0.02[vlogo]")
        current = "[vlogo]"
    subtitle = _subtitle_path(video_path)
    if subtitle:
        subtitle_expr = _ffmpeg_filter_path(subtitle)
        chains.append(f"{current}subtitles='{subtitle_expr}':force_style='FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=0,Alignment=2,MarginV=45'[vout]")
        current = "[vout]"
    if current != "[vout]":
        chains.append(f"{current}null[vout]")
    return ";".join(chains)


def export_video(
    video_path: str,
    blur_boxes: list[list[float]] | None = None,
    logo_path: str = "",
    log_callback=None,
    progress_callback=None,
) -> str:
    source = Path(video_path)
    output = source.with_name(f"{source.stem}_Export.mp4")
    blur_boxes = blur_boxes or []
    filters = _build_filter(source, blur_boxes, logo_path)
    encoder = _encoder()
    command = [FFMPEG_PATH, "-y", "-progress", "pipe:1", "-nostats", "-i", str(source)]
    command += ["-filter_complex", filters, "-map", "[vout]", "-map", "0:a?", "-c:v", encoder]
    if encoder == "h264_nvenc":
        command += ["-preset", "p4", "-cq", "24", "-pix_fmt", "yuv420p"]
    else:
        command += ["-preset", "medium", "-crf", "23"]
    command += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)]
    duration = _duration_seconds(source)
    if log_callback:
        log_callback(f"[Export] Đang xử lý: {source.name} ({encoder})")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )
    for line in process.stdout or []:
        line = line.strip()
        if log_callback and ("Error" in line or "error" in line):
            log_callback(f"[Export] {line}")
        if line.startswith("out_time_ms="):
            try:
                elapsed = float(line.split("=", 1)[1]) / 1_000_000
                progress_callback(min(99.0, elapsed * 100 / duration) if progress_callback and duration else 0.0)
            except (ValueError, TypeError):
                pass
    process.wait()
    if process.returncode != 0 and encoder == "h264_nvenc":
        if log_callback:
            log_callback("[Export] NVENC của FFmpeg mới không tương thích driver hiện tại, chuyển sang FFmpeg backup 8.1.1 vẫn dùng GPU.")
        if log_callback:
            log_callback("[Export] FFmpeg mới yêu cầu driver NVENC cao hơn, chuyển sang FFmpeg backup 8.1.1 vẫn dùng GPU.")
        fallback = list(command)
        legacy_ffmpeg = Path(FFMPEG_PATH).with_name("ffmpeg_8.1.1_backup.exe")
        if not legacy_ffmpeg.exists():
            raise RuntimeError("FFmpeg NVENC không khởi tạo được và không có bản backup GPU 8.1.1.")
        fallback[0] = str(legacy_ffmpeg)
        process = subprocess.Popen(
            fallback,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        for line in process.stdout or []:
            line = line.strip()
            if log_callback and ("Error" in line or "error" in line):
                log_callback(f"[Export] {line}")
            if line.startswith("out_time_ms="):
                try:
                    elapsed = float(line.split("=", 1)[1]) / 1_000_000
                    progress_callback(min(99.0, elapsed * 100 / duration) if progress_callback and duration else 0.0)
                except (ValueError, TypeError):
                    pass
        process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg xuất video thất bại ({process.returncode})")
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg không tạo được file xuất hợp lệ")
    if progress_callback:
        progress_callback(100.0)
    if log_callback:
        log_callback(f"[Export] Hoàn tất: {output.name}")
    try:
        source.unlink()
        if log_callback:
            log_callback(f"[Export] Đã xóa file nguồn: {source.name}")
    except OSError as exc:
        if log_callback:
            log_callback(f"[Export] Cảnh báo: không xóa được file nguồn {source.name}: {exc}")
    return str(output)


def _cleanup_export_folder(folder: Path, log_callback=None) -> None:
    """Keep exported videos and naming text files after successful export."""
    for path in sorted(folder.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".txt" or path.stem.endswith("_Export"):
            continue
        try:
            path.unlink()
            if log_callback:
                log_callback(f"[Export] Removed source file: {path.name}")
        except OSError as exc:
            if log_callback:
                log_callback(f"[Export] Could not remove {path.name}: {exc}")


def export_folder(
    folder_path: str,
    blur_boxes=None,
    logo_path="",
    log_callback=None,
    progress_callback=None,
    overlay_configs=None,
) -> list[str]:
    root = Path(folder_path)
    videos = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and "_Export" not in path.stem),
        key=lambda path: str(path).casefold(),
    )
    results = []
    exported_folders = set()
    total_videos = max(1, len(videos))
    for index, video in enumerate(videos, 1):
        if log_callback:
            log_callback(f"[ExportProgress] ITEM {index}/{len(videos)} {video.name}")
        try:
            config = (overlay_configs or {}).get(str(video), {})
            video_blur = config.get("blur_boxes", blur_boxes)
            video_logo = config.get("logo_path", logo_path)
            def overall_progress(value, current=index):
                if progress_callback:
                    progress_callback(
                        ((current - 1) + max(0.0, min(100.0, float(value))) / 100.0)
                        * 100.0 / total_videos
                    )

            results.append(export_video(str(video), video_blur, video_logo, log_callback, overall_progress))
            exported_folders.add(video.parent)
            if log_callback:
                log_callback(f"[ExportProgress] DONE {index}/{len(videos)}")
        except Exception as exc:
            if log_callback:
                log_callback(f"[ExportProgress] FAIL {index}/{len(videos)} {video.name}: {exc}")
    for folder in sorted(exported_folders, key=str):
        _cleanup_export_folder(folder, log_callback)
    return results
