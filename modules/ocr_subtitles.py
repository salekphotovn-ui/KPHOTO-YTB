"""Create Chinese SRT files from subtitles burned into video frames."""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path


_OCR_ENGINE = None
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_SPACE_RE = re.compile(r"\s+")


def _normalise_text(text: str) -> str:
    return _SPACE_RE.sub("", str(text or "")).strip()


def _similar_text(left: str, right: str) -> bool:
    if left == right:
        return True
    if not left or not right:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.86


def _load_engine(log_callback):
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE

    use_cuda = False
    try:
        import torch

        if os.name == "nt":
            torch_lib = Path(torch.__file__).resolve().parent / "lib"
            if torch_lib.is_dir():
                os.add_dll_directory(str(torch_lib))
        use_cuda = bool(torch.cuda.is_available())
    except Exception:
        use_cuda = False

    from rapidocr import RapidOCR

    params = {
        "Global.log_level": "warning",
        "EngineConfig.onnxruntime.use_cuda": use_cuda,
    }
    _OCR_ENGINE = RapidOCR(params=params)
    try:
        providers = _OCR_ENGINE.text_det.session.session.get_providers()
        using_gpu = bool(providers and providers[0] == "CUDAExecutionProvider")
    except Exception:
        using_gpu = False
    log_callback(
        "[OCR] PP-OCRv6 small - " + ("GPU CUDA" if using_gpu else "CPU")
    )
    return _OCR_ENGINE


def _read_subtitle_text(engine, frame) -> tuple[str, float]:
    import cv2

    height, width = frame.shape[:2]
    # Dialogue subtitles are normally in the lower half. Cropping also avoids
    # permanent channel names/warnings near the top of unrelated videos.
    crop = frame[int(height * 0.52):height]
    target_width = min(960, crop.shape[1])
    if crop.shape[1] != target_width:
        target_height = max(1, round(crop.shape[0] * target_width / crop.shape[1]))
        crop = cv2.resize(crop, (target_width, target_height), interpolation=cv2.INTER_AREA)

    result = engine(crop, use_cls=False, text_score=0.62, box_thresh=0.40)
    raw_boxes = getattr(result, "boxes", None)
    raw_texts = getattr(result, "txts", None)
    raw_scores = getattr(result, "scores", None)
    boxes = list(raw_boxes) if raw_boxes is not None else []
    texts = list(raw_texts) if raw_texts is not None else []
    scores = list(raw_scores) if raw_scores is not None else []
    candidates = []
    crop_height, crop_width = crop.shape[:2]
    for box, text, score in zip(boxes, texts, scores):
        clean = _normalise_text(text)
        if not clean or not _CJK_RE.search(clean) or float(score) < 0.62:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        center_x = (min(xs) + max(xs)) / 2
        center_y = (min(ys) + max(ys)) / 2
        # Retain centred dialogue lines and reject side labels/UI elements.
        if center_y < crop_height * 0.28:
            continue
        if not (crop_width * 0.08 <= center_x <= crop_width * 0.92):
            continue
        candidates.append((center_y, min(xs), clean, float(score)))
    candidates.sort(key=lambda item: (round(item[0] / 18), item[1]))
    text = "".join(item[2] for item in candidates)
    confidence = sum(item[3] for item in candidates) / len(candidates) if candidates else 0.0
    return text, confidence


def _postprocess_segments(segments: list[dict], sample_interval: float) -> list[dict]:
    cleaned = []
    for item in segments:
        text = _normalise_text(item.get("text", ""))
        start = max(0.0, float(item.get("start", 0.0)))
        end = max(start, float(item.get("end", start)))
        if not text or end - start < min(0.32, sample_interval * 0.65):
            continue
        if cleaned and _similar_text(cleaned[-1]["text"], text) and start - cleaned[-1]["end"] <= sample_interval * 1.5:
            cleaned[-1]["end"] = end
            if len(text) > len(cleaned[-1]["text"]):
                cleaned[-1]["text"] = text
            continue
        cleaned.append({"start": start, "end": end, "text": text})
    return cleaned


def run_rapidocr_video(video_path: Path, log_callback=print) -> dict:
    """Sample video frames and turn stable burned-in Chinese text into cues."""
    import cv2

    sample_interval = max(0.25, float(os.getenv("BILI2YT_OCR_INTERVAL", "0.5")))
    engine = _load_engine(log_callback)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"OCR khong mo duoc video: {video_path.name}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or total_frames <= 0:
        capture.release()
        raise RuntimeError(f"OCR khong doc duoc FPS/thoi luong: {video_path.name}")

    stride = max(1, round(fps * sample_interval))
    duration = total_frames / fps
    log_callback(
        f"[OCR] Quet phu de tren hinh moi {sample_interval:.2f}s, video {duration / 60:.1f} phut"
    )
    segments = []
    current_text = ""
    current_start = 0.0
    current_last_seen = 0.0
    frame_index = 0
    last_percent = -1
    try:
        while True:
            grabbed = capture.grab()
            if not grabbed:
                break
            if frame_index % stride:
                frame_index += 1
                continue
            ok, frame = capture.retrieve()
            if not ok:
                frame_index += 1
                continue
            timestamp = frame_index / fps
            observed, _confidence = _read_subtitle_text(engine, frame)
            if observed and current_text and _similar_text(observed, current_text):
                current_last_seen = timestamp
                if len(observed) > len(current_text):
                    current_text = observed
            elif observed:
                if current_text:
                    segments.append({
                        "start": current_start,
                        "end": max(current_start + 0.05, timestamp),
                        "text": current_text,
                    })
                current_text = observed
                current_start = timestamp
                current_last_seen = timestamp
            elif current_text and timestamp - current_last_seen >= sample_interval * 1.5:
                segments.append({
                    "start": current_start,
                    "end": max(current_start + 0.05, current_last_seen + sample_interval),
                    "text": current_text,
                })
                current_text = ""

            percent = min(99, int(timestamp * 100 / duration))
            if percent >= last_percent + 1:
                last_percent = percent
                log_callback(f"[SrtProgress] OCR_PERCENT {percent}")
            frame_index += 1
    finally:
        capture.release()
    if current_text:
        segments.append({
            "start": current_start,
            "end": min(duration, current_last_seen + sample_interval),
            "text": current_text,
        })
    segments = _postprocess_segments(segments, sample_interval)
    log_callback(f"[OCR] Nhan duoc {len(segments)} cue tu phu de tren hinh")
    log_callback("[SrtProgress] OCR_PERCENT 100")
    return {"language": "zh", "segments": segments}
