"""Create Chinese SRT files from subtitles burned into video frames."""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path


_BUNDLED_OCR_MODELS = {
    "Det.model_path": "PP-OCRv6_det_small.onnx",
    "Cls.model_path": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec.model_path": "PP-OCRv6_rec_small.onnx",
}


def _bundled_rapidocr_models_dir() -> Path:
    """Return bundled OCR model directory when running from PyInstaller."""
    bundle = Path(getattr(__import__("sys"), "_MEIPASS", Path(__file__).resolve().parents[1]))
    return bundle / "rapidocr" / "models"


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


def _transition_similarity(left: str, right: str) -> float:
    """Similarity tolerant enough to group partial OCR during card changes."""
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return SequenceMatcher(None, left, right).ratio()


def _build_rapidocr(use_cuda: bool):
    from rapidocr import RapidOCR

    params = {
        # Empty detection is normal between subtitle cards; keep it out of the
        # user log and only surface genuine OCR errors.
        "Global.log_level": "error",
        "EngineConfig.onnxruntime.use_cuda": use_cuda,
    }
    model_dir = _bundled_rapidocr_models_dir()
    bundled_paths = {key: model_dir / name for key, name in _BUNDLED_OCR_MODELS.items()}
    if all(path.is_file() for path in bundled_paths.values()):
        params.update({key: str(path) for key, path in bundled_paths.items()})
    return RapidOCR(params=params)


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

    engine = _build_rapidocr(use_cuda)
    if use_cuda:
        # torch.cuda.is_available() does not mean onnxruntime-gpu ships CUDA
        # kernels for THIS GPU's arch - a mismatch only surfaces at inference as
        # cudaErrorNoKernelImageForDevice on the first Relu. Warm up once and
        # drop to CPU so a new employee machine still produces SRT files.
        try:
            import numpy as np

            engine(np.zeros((64, 256, 3), dtype="uint8"), use_cls=False)
        except Exception as exc:
            first_line = str(exc).splitlines()[0][:140] if str(exc).strip() else exc.__class__.__name__
            log_callback(f"[OCR] GPU CUDA không chạy được ({first_line}) — chuyển sang CPU")
            use_cuda = False
            try:
                engine = _build_rapidocr(False)
            except Exception as cpu_exc:
                log_callback(f"[OCR] Không khởi tạo được engine CPU: {cpu_exc}")
                raise

    _OCR_ENGINE = engine
    try:
        providers = _OCR_ENGINE.text_det.session.session.get_providers()
        using_gpu = bool(providers and providers[0] == "CUDAExecutionProvider")
    except Exception:
        using_gpu = False
    log_callback(
        "[OCR] PP-OCRv6 small - " + ("GPU CUDA" if using_gpu else "CPU")
    )
    return _OCR_ENGINE


def _crop_frame(frame, roi=None):
    height, width = frame.shape[:2]
    manual_region = bool(roi and len(roi) == 4)
    if manual_region:
        x, y, width_ratio, height_ratio = (float(value) for value in roi)
        left = max(0, min(width - 1, round(x * width)))
        top = max(0, min(height - 1, round(y * height)))
        right = max(left + 1, min(width, round((x + width_ratio) * width)))
        bottom = max(top + 1, min(height, round((y + height_ratio) * height)))
        crop = frame[top:bottom, left:right]
    else:
        # Automatic fallback for videos where the user did not draw a box.
        crop = frame[int(height * 0.52):height]
    return crop, manual_region


def _frame_signature(frame, roi=None):
    """Return a mask of bright glyphs supported by a nearby dark outline.

    Comparing every bright pixel is ineffective on animated video because
    faces, lamps and scenery keep changing inside the OCR box.  Burned-in
    dialogue in this workflow is bright, low-saturation text with a dark
    outline, so this signature ignores most of that background motion while
    remaining sensitive to a changed Chinese glyph.
    """
    import cv2

    crop, _manual_region = _crop_frame(frame, roi)
    # Keep enough resolution for the thin black outline before reducing the
    # mask used by the cheap frame comparison.
    probe = cv2.resize(crop, (384, 96), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(probe, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(probe, cv2.COLOR_BGR2HSV)[:, :, 1]
    local_min = cv2.erode(gray, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    outlined_bright = (
        (gray >= 210)
        & (saturation <= 60)
        & (local_min <= 110)
    ).astype("uint8") * 255
    return cv2.resize(outlined_bright, (192, 48), interpolation=cv2.INTER_AREA)


def _read_subtitle_text(engine, frame, roi=None) -> tuple[str, float]:
    import cv2

    crop, manual_region = _crop_frame(frame, roi)
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
        if not manual_region and center_y < crop_height * 0.28:
            continue
        if not (crop_width * 0.08 <= center_x <= crop_width * 0.92):
            continue
        candidates.append((center_y, min(xs), clean, float(score)))
    candidates.sort(key=lambda item: (round(item[0] / 18), item[1]))
    text = "".join(item[2] for item in candidates)
    confidence = sum(item[3] for item in candidates) / len(candidates) if candidates else 0.0
    return text, confidence


def _postprocess_segments(segments: list[dict], sample_interval: float) -> list[dict]:
    # Keep the proven transition-stabilisation window even when timeline
    # sampling is refined from 0.5s to 0.1s. Otherwise a fading card can create
    # several tiny partial-text cues merely because it is observed more often.
    stability_interval = max(0.5, sample_interval)
    cleaned = []
    for item in segments:
        text = _normalise_text(item.get("text", ""))
        start = max(0.0, float(item.get("start", 0.0)))
        end = max(start, float(item.get("end", start)))
        if not text or end - start < min(0.32, stability_interval * 0.65):
            continue
        confidence = float(item.get("confidence", 0.0) or 0.0)
        if cleaned and _similar_text(cleaned[-1]["text"], text) and start - cleaned[-1]["end"] <= stability_interval * 1.5:
            cleaned[-1]["end"] = end
            if (len(text), confidence) > (len(cleaned[-1]["text"]), cleaned[-1].get("confidence", 0.0)):
                cleaned[-1]["text"] = text
                cleaned[-1]["confidence"] = confidence
            continue
        cleaned.append({"start": start, "end": end, "text": text, "confidence": confidence})

    # OCR often sees one or two incomplete forms while a subtitle card is
    # fading/changing. Group only adjacent, similar cues where at least one is
    # short, then keep the longest/highest-confidence reading for the cluster.
    stable = []
    for item in cleaned:
        if stable:
            previous = stable[-1]
            adjacent = item["start"] - previous["end"] <= stability_interval * 0.35
            short_transition = min(
                previous["end"] - previous["start"],
                item["end"] - item["start"],
            ) <= stability_interval * 2.2
            similarity = _transition_similarity(previous["text"], item["text"])
            if adjacent and short_transition and similarity >= 0.52:
                candidates = [previous, item]
                best = max(
                    candidates,
                    key=lambda value: (
                        len(value["text"]),
                        value.get("confidence", 0.0),
                        value["end"] - value["start"],
                    ),
                )
                previous["end"] = item["end"]
                previous["text"] = best["text"]
                previous["confidence"] = best.get("confidence", 0.0)
                continue
        stable.append(item)
    return [{"start": x["start"], "end": x["end"], "text": x["text"]} for x in stable]


def run_rapidocr_video(video_path: Path, log_callback=print, roi=None) -> dict:
    """Sample video frames and turn stable burned-in Chinese text into cues."""
    # Suppress FFmpeg/OpenCV decoder chatter for recoverable damaged HEVC
    # packets. Failed frames are skipped while the OCR job continues.
    os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
    import cv2
    try:
        cv2.setLogLevel(0)
    except AttributeError:
        pass

    # The OCR model call is ~95% of the run time, so the sample rate is the
    # main speed lever. 0.3s (~3 checks/s) still catches every dialogue card
    # while roughly halving the calls versus 0.1s. Override per machine with
    # BILI2YT_OCR_INTERVAL (e.g. 0.5 for faster, 0.1 for tightest timing).
    sample_interval = max(0.05, float(os.getenv("BILI2YT_OCR_INTERVAL", "0.3")))
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
        f"[OCR] Quet moc timeline moi {sample_interval:.2f}s, "
        f"chi goi model khi chu thay doi; video {duration / 60:.1f} phut"
    )
    if roi and len(roi) == 4:
        log_callback(
            f"[OCR] Dung khung da chon: x={roi[0]:.3f}, y={roi[1]:.3f}, "
            f"w={roi[2]:.3f}, h={roi[3]:.3f}"
        )
    segments = []
    current_text = ""
    current_start = 0.0
    current_last_seen = 0.0
    current_confidence = 0.0
    previous_signature = None
    last_ocr_signature = None
    previous_observed = ""
    previous_confidence = 0.0
    ocr_calls = 0
    skipped_ocr = 0
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
            signature = _frame_signature(frame, roi)
            visual_change = 999.0
            accumulated_change = 999.0
            if previous_signature is not None:
                visual_change = float(cv2.absdiff(signature, previous_signature).mean())
            if last_ocr_signature is not None:
                accumulated_change = float(cv2.absdiff(signature, last_ocr_signature).mean())
            # A changed glyph produces a much larger delta than residual
            # background edges.  This default is still conservative enough for
            # one-character cards while allowing repeated frames to bypass the
            # expensive OCR model.
            change_threshold = max(0.20, float(os.getenv("BILI2YT_OCR_CHANGE_THRESHOLD", "1.10")))
            should_run_ocr = (
                previous_signature is None
                or visual_change >= change_threshold
                or accumulated_change >= change_threshold * 3.0
            )
            # Always advance the cheap frame-to-frame baseline. The separate
            # last-OCR baseline catches a subtitle that fades in gradually.
            previous_signature = signature
            if not should_run_ocr:
                observed, confidence = previous_observed, previous_confidence
                skipped_ocr += 1
            else:
                observed, confidence = _read_subtitle_text(engine, frame, roi=roi)
                last_ocr_signature = signature
                previous_observed = observed
                previous_confidence = confidence
                ocr_calls += 1
            if observed and current_text and _similar_text(observed, current_text):
                current_last_seen = timestamp
                if (len(observed), confidence) > (len(current_text), current_confidence):
                    current_text = observed
                    current_confidence = confidence
            elif observed:
                if current_text:
                    segments.append({
                        "start": current_start,
                        "end": max(current_start + 0.05, timestamp),
                        "text": current_text,
                        "confidence": current_confidence,
                    })
                current_text = observed
                current_start = timestamp
                current_last_seen = timestamp
                current_confidence = confidence
            elif current_text and timestamp - current_last_seen >= sample_interval * 1.5:
                segments.append({
                    "start": current_start,
                    "end": max(current_start + 0.05, current_last_seen + sample_interval),
                    "text": current_text,
                    "confidence": current_confidence,
                })
                current_text = ""
                current_confidence = 0.0

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
            "confidence": current_confidence,
        })
    segments = _postprocess_segments(segments, sample_interval)
    total_samples = ocr_calls + skipped_ocr
    saved_percent = round(skipped_ocr * 100 / total_samples) if total_samples else 0
    log_callback(
        f"[OCR] Goi model {ocr_calls}/{total_samples} khung, bo qua {saved_percent}% khung khong doi"
    )
    log_callback(f"[OCR] Nhan duoc {len(segments)} cue tu phu de tren hinh")
    log_callback("[SrtProgress] OCR_PERCENT 100")
    return {"language": "zh", "segments": segments}
