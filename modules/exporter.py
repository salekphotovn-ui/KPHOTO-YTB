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


def _local_vocal_path(video_path: Path) -> Path | None:
    """Find a separated vocal track belonging to this video."""
    found = []
    for path in video_path.parent.iterdir():
        if path.is_file() and path.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a"}:
            if "(vocals)" in path.name.casefold() and path.stem.casefold().startswith(video_path.stem.casefold()):
                found.append(path)
    return sorted(found, key=lambda p: p.name.casefold())[0] if found else None


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


def _video_height(video_path: Path) -> int:
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-i", str(video_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        matches = re.findall(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", result.stderr)
        if matches:
            return int(matches[0][1])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 480


def _build_filter(
    video_path: Path, blur_boxes: list[list[float]], logo_path: str,
    subtitle_y_ratio: float = 0.86, logo_box: list[float] | None = None,
    blur_strength: int = 12,
) -> str:
    chains = ["[0:v]setpts=PTS-STARTPTS[base]"]
    current = "[base]"
    blur_sigma = max(0, min(100, int(blur_strength)))
    for index, box in enumerate(blur_boxes):
        x, y, width, height = [max(0.0, min(1.0, float(value))) for value in box]
        # Gaussian blur matches the soft CapCut-style effect instead of the
        # blocky mosaic produced by downscaling and enlarging the crop.
        crop = (
            f"crop=iw*{width:.6f}:ih*{height:.6f}:iw*{x:.6f}:ih*{y:.6f},"
            f"gblur=sigma={blur_sigma}:steps=4"
        )
        chains.append(
            f"{current}split=2[keep{index}][patch{index}];"
            f"[patch{index}]{crop}[blur{index}];"
            f"[keep{index}][blur{index}]overlay=x=main_w*{x:.6f}:y=main_h*{y:.6f}[b{index}]"
        )
        current = f"[b{index}]"
    if logo_path:
        logo_box = logo_box or [0.82, 0.02, 0.14, 0.14]
        logo_x, logo_y, logo_width, logo_height = [
            max(0.0, min(1.0, float(value))) for value in logo_box
        ]
        logo_input = _ffmpeg_filter_path(Path(logo_path))
        chains.append(
            f"movie='{logo_input}',format=rgba[logo];"
            f"{current}[logo]scale2ref=w=main_w*{logo_width:.6f}:h=main_h*{logo_height:.6f}"
            f"[logo_s][base_s];[base_s][logo_s]overlay="
            f"x=main_w*{logo_x:.6f}:y=main_h*{logo_y:.6f}[vlogo]"
        )
        current = "[vlogo]"
    subtitle = _subtitle_path(video_path)
    if subtitle:
        subtitle_expr = _ffmpeg_filter_path(subtitle)
        subtitle_y_ratio = max(0.05, min(0.95, float(subtitle_y_ratio)))
        # libass converts SRT into a default ASS canvas with PlayResY=288.
        # MarginV is expressed in that script space, not in source-video
        # pixels. Passing a 1080p pixel margin made a preview position near the
        # bottom jump into the middle of the exported video. The preview stores
        # the subtitle centre, so subtract half the ASS font height as well.
        ass_play_res_y = 288
        ass_font_size = 16
        margin_v = max(
            0,
            round(ass_play_res_y * (1.0 - subtitle_y_ratio) - ass_font_size / 2),
        )
        chains.append(f"{current}subtitles='{subtitle_expr}':force_style='FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=0,Alignment=2,MarginV={margin_v}'[vout]")
        current = "[vout]"
    if current != "[vout]":
        chains.append(f"{current}null[vout]")
    return ";".join(chains)


def export_video(
    video_path: str,
    blur_boxes: list[list[float]] | None = None,
    logo_path: str = "",
    subtitle_y_ratio: float = 0.86,
    logo_box: list[float] | None = None,
    blur_strength: int = 12,
    log_callback=None,
    progress_callback=None,
) -> str:
    source = Path(video_path)
    output = source.with_name(f"{source.stem}_Export.mp4")
    blur_boxes = blur_boxes or []
    filters = _build_filter(
        source, blur_boxes, logo_path, subtitle_y_ratio, logo_box, blur_strength
    )
    encoder = _encoder()
    vocal = _local_vocal_path(source)
    command = [FFMPEG_PATH, "-y", "-progress", "pipe:1", "-nostats", "-i", str(source)]
    if vocal:
        command += ["-i", str(vocal)]
    command += ["-filter_complex", filters, "-map", "[vout]"]
    command += ["-map", "1:a:0" if vocal else "0:a?", "-c:v", encoder]
    if encoder == "h264_nvenc":
        command += ["-preset", "p4", "-cq", "24", "-pix_fmt", "yuv420p"]
    else:
        command += ["-preset", "medium", "-crf", "23"]
    command += ["-c:a", "aac", "-b:a", "192k"]
    if vocal:
        command += ["-shortest"]
    command += ["-movflags", "+faststart", str(output)]
    duration = _duration_seconds(source)
    if log_callback:
        log_callback(f"[Export] Audio track: {vocal.name}" if vocal else "[Export] Audio track: original (no local vocal)")
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
        log_callback(f"[Export] Giữ nguyên file nguồn: {source.name}")
    return str(output)


def export_folder(
    folder_path: str,
    blur_boxes=None,
    logo_path="",
    log_callback=None,
    progress_callback=None,
    overlay_configs=None,
) -> list[str]:
    root = Path(folder_path)
    all_videos = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and "_Export" not in path.stem]
    muxed_stems = {path.stem[:-len("_Da_Ghep_Vocal")] for path in all_videos if path.stem.endswith("_Da_Ghep_Vocal")}
    videos = sorted(
        (path for path in all_videos if not (path.stem in muxed_stems and not path.stem.endswith("_Da_Ghep_Vocal"))),
        key=lambda path: str(path).casefold(),
    )
    results = []
    total_videos = max(1, len(videos))
    for index, video in enumerate(videos, 1):
        if log_callback:
            log_callback(f"[ExportProgress] ITEM {index}/{len(videos)} {video.name}")
        try:
            configs = overlay_configs or {}
            config = configs.get(str(video.resolve()), configs.get(str(video), {}))
            if not config and video.stem.endswith("_Da_Ghep_Vocal"):
                original = video.with_name(video.stem[:-len("_Da_Ghep_Vocal")] + video.suffix)
                config = configs.get(str(original.resolve()), configs.get(str(original), {}))
            video_blur = config.get("blur_boxes", blur_boxes)
            video_logo = config.get("logo_path", logo_path)
            video_subtitle_y = config.get("subtitle_y_ratio", 0.86)
            video_logo_box = config.get("logo_box")
            video_blur_strength = config.get("blur_strength", 12)
            def overall_progress(value, current=index):
                if progress_callback:
                    progress_callback(
                        ((current - 1) + max(0.0, min(100.0, float(value))) / 100.0)
                        * 100.0 / total_videos
                    )

            results.append(export_video(
                str(video),
                blur_boxes=video_blur,
                logo_path=video_logo,
                subtitle_y_ratio=video_subtitle_y,
                logo_box=video_logo_box,
                blur_strength=video_blur_strength,
                log_callback=log_callback,
                progress_callback=overall_progress,
            ))
            if log_callback:
                log_callback(f"[ExportProgress] DONE {index}/{len(videos)}")
        except Exception as exc:
            if log_callback:
                log_callback(f"[ExportProgress] FAIL {index}/{len(videos)} {video.name}: {exc}")
    return results
