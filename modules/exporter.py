"""Export video with subtitle, natural blur regions, and logo overlays."""

from __future__ import annotations

import re
import subprocess
from collections import deque
from pathlib import Path

from config import FFMPEG_PATH


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

_ERROR_HINT_RE = re.compile(
    r"error|invalid|no such file|failed|cannot|unable|not found|"
    r"impossible|denied|conversion failed|nvenc|out of memory|killed",
    re.IGNORECASE,
)


def _ffmpeg_filter_path(path: Path) -> str:
    # Escape characters meaningful inside an FFmpeg filter expression.
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\\\'")


def _encoder() -> str:
    # Listing h264_nvenc in -encoders only means FFmpeg was built with it, not
    # that this machine's GPU/driver can actually run it. Do a tiny real encode
    # so a box without a working NVENC quietly falls back to libx264 instead of
    # dying mid-export.
    try:
        probe = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=256x144:r=10", "-t", "0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if probe.returncode == 0:
            return "h264_nvenc"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "libx264"


def _srt_has_cues(path: Path) -> bool:
    """A usable SRT has at least one timestamp line; libass errors on a broken
    or empty file and FFmpeg then writes a 0-byte export."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    return "-->" in text and bool(text.strip())


def _subtitle_path(video_path: Path) -> Path | None:
    subtitle_dir = video_path.parent / "subtitles"
    candidate = subtitle_dir / "en.srt"
    if candidate.exists() and _srt_has_cues(candidate):
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


def _hex_to_ass_colour(value: str) -> str:
    """Convert a "#RRGGBB" preview colour into an ASS "&HAABBGGRR" literal."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return "&H00FFFFFF"
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except ValueError:
        return "&H00FFFFFF"
    return f"&H00{blue:02X}{green:02X}{red:02X}"


def _subtitle_force_style(subtitle_style, top_margin_ratio: float) -> str:
    """Build the libass force_style string from the preview font settings."""
    style = subtitle_style if isinstance(subtitle_style, dict) else {}
    family = str(style.get("family") or "Arial").strip() or "Arial"
    family = family.replace(",", " ").replace("{", "").replace("}", "").strip()
    try:
        preview_size = float(style.get("size", 22) or 22)
    except (TypeError, ValueError):
        preview_size = 22.0
    # The preview label defaults to 22 px; the exported ASS canvas defaults to
    # FontSize 16 on PlayResY 288. Scale proportionally so the default is
    # unchanged and a bigger preview size yields a bigger burned-in cue.
    ass_font_size = max(6, round(16 * preview_size / 22))
    colour = _hex_to_ass_colour(style.get("color", "#FFFFFF"))
    margin_v = max(0, round(288 * max(0.0, top_margin_ratio) - ass_font_size / 2))
    return (
        f"FontName={family},FontSize={ass_font_size},"
        f"PrimaryColour={colour},OutlineColour=&H00000000,"
        f"Outline=2,Shadow=0,Alignment=2,MarginV={margin_v}"
    )


def _build_filter(
    video_path: Path, blur_boxes: list[list[float]], logo_path: str,
    subtitle_y_ratio: float = 0.86, logo_box: list[float] | None = None,
    blur_strength: int = 12, subtitle_style=None,
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
        # the subtitle centre, so _subtitle_force_style subtracts half the ASS
        # font height as well. FontName/FontSize/PrimaryColour come from the
        # per-video "Font" dialog captured in the preview.
        force_style = _subtitle_force_style(subtitle_style, 1.0 - subtitle_y_ratio)
        chains.append(
            f"{current}subtitles='{subtitle_expr}':force_style='{force_style}'[vout]"
        )
        current = "[vout]"
    if current != "[vout]":
        chains.append(f"{current}null[vout]")
    return ";".join(chains)


def _run_ffmpeg(command, duration, log_callback, progress_callback):
    """Run one FFmpeg pass, streaming progress and keeping the last output lines
    so a failure can be explained instead of leaving a silent 0-byte file."""
    tail: deque[str] = deque(maxlen=80)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )
    for raw in process.stdout or []:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("out_time_ms=", "out_time_us=")):
            try:
                elapsed = float(line.split("=", 1)[1]) / 1_000_000
                if progress_callback and duration:
                    progress_callback(min(99.0, elapsed * 100 / duration))
            except (ValueError, TypeError):
                pass
            continue
        if line.startswith(("frame=", "fps=", "bitrate=", "total_size=", "speed=",
                            "out_time=", "dup_frames=", "drop_frames=", "progress=", "stream_")):
            continue
        tail.append(line)
        if log_callback and _ERROR_HINT_RE.search(line):
            log_callback(f"[Export] {line}")
    process.wait()
    return process.returncode, list(tail)


def _rebuild_with_encoder(command, encoder):
    out = []
    skip_next = 0
    for i, token in enumerate(command):
        if skip_next:
            skip_next -= 1
            continue
        if token == "-c:v":
            out += ["-c:v", encoder]
            skip_next = 1  # drop the old encoder name
            continue
        if token in ("-preset", "-cq", "-crf", "-pix_fmt") and i + 1 < len(command):
            skip_next = 1
            continue
        out.append(token)
    # Re-insert quality flags for the chosen encoder right after -c:v.
    idx = out.index("-c:v") + 2
    if encoder == "h264_nvenc":
        extra = ["-preset", "p4", "-cq", "24", "-pix_fmt", "yuv420p"]
    else:
        extra = ["-preset", "medium", "-crf", "23"]
    return out[:idx] + extra + out[idx:]


def export_video(
    video_path: str,
    blur_boxes: list[list[float]] | None = None,
    logo_path: str = "",
    subtitle_y_ratio: float = 0.86,
    logo_box: list[float] | None = None,
    blur_strength: int = 12,
    subtitle_style=None,
    log_callback=None,
    progress_callback=None,
) -> str:
    source = Path(video_path)
    output = source.with_name(f"{source.stem}_Export.mp4")
    blur_boxes = blur_boxes or []
    if log_callback:
        sub = _subtitle_path(source)
        raw_sub = source.parent / "subtitles" / "en.srt"
        if sub:
            log_callback(f"[Export] Phụ đề: {sub}")
        elif raw_sub.exists():
            log_callback("[Export] subtitles/en.srt rỗng/hỏng — bỏ qua phần phụ đề")
        else:
            log_callback("[Export] Không có subtitles/en.srt — xuất không phụ đề")
        log_callback(f"[Export] Vùng blur: {len(blur_boxes)}")
    filters = _build_filter(
        source, blur_boxes, logo_path, subtitle_y_ratio, logo_box, blur_strength,
        subtitle_style,
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
        log_callback(f"[Export] Đang xử lý: {source.name} ({encoder})")

    returncode, tail = _run_ffmpeg(command, duration, log_callback, progress_callback)

    if returncode != 0 and encoder == "h264_nvenc":
        legacy_ffmpeg = Path(FFMPEG_PATH).with_name("ffmpeg_8.1.1_backup.exe")
        if legacy_ffmpeg.exists():
            if log_callback:
                log_callback("[Export] NVENC lỗi — thử lại bằng FFmpeg backup 8.1.1 (vẫn GPU).")
            gpu_cmd = list(command)
            gpu_cmd[0] = str(legacy_ffmpeg)
            returncode, tail = _run_ffmpeg(gpu_cmd, duration, log_callback, progress_callback)
        if returncode != 0:
            if log_callback:
                log_callback("[Export] NVENC không dùng được trên máy này — chuyển sang libx264 (CPU).")
            cpu_cmd = _rebuild_with_encoder(command, "libx264")
            returncode, tail = _run_ffmpeg(cpu_cmd, duration, log_callback, progress_callback)

    if returncode != 0 or not output.exists() or output.stat().st_size == 0:
        try:
            if output.exists() and output.stat().st_size == 0:
                output.unlink()
        except OSError:
            pass
        detail = next((line for line in reversed(tail) if _ERROR_HINT_RE.search(line)), "")
        if not detail and tail:
            detail = tail[-1]
        if log_callback:
            for line in tail[-12:]:
                log_callback(f"[Export][ffmpeg] {line}")
        raise RuntimeError(
            f"FFmpeg xuất video thất bại ({returncode})"
            + (f": {detail}" if detail else "")
        )
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
    cleanup_after_export=True,
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
            video_subtitle_style = config.get("subtitle_style")
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
                subtitle_style=video_subtitle_style,
                log_callback=log_callback,
                progress_callback=overall_progress,
            ))
            if log_callback:
                log_callback(f"[ExportProgress] DONE {index}/{len(videos)}")
            if cleanup_after_export:
                folder = video.parent
                output_path = Path(results[-1]).resolve()
                removed = 0
                for item in folder.iterdir():
                    if item.resolve() == output_path:
                        continue
                    if item.is_dir() and item.name.casefold() == "subtitles":
                        continue
                    if item.is_file() and item.suffix.casefold() == ".txt":
                        continue
                    try:
                        if item.is_file():
                            item.unlink()
                            removed += 1
                    except OSError as cleanup_error:
                        if log_callback:
                            log_callback(f"[Export] Không xóa được {item.name}: {cleanup_error}")
                if log_callback:
                    log_callback(f"[Export] Đã dọn thư mục {folder.name}, xóa {removed} file trung gian")
        except Exception as exc:
            if log_callback:
                log_callback(f"[ExportProgress] FAIL {index}/{len(videos)} {video.name}: {exc}")
    return results
