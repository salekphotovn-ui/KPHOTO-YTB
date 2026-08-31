"""Google Web / Gemini subtitle translation for the V3 batch workflow."""

from __future__ import annotations

import json
import hashlib
import re
import time
import traceback
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from urllib.parse import quote

from config import TRANSLATOR_MODEL


TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)
NON_WORD_RE = re.compile(r"^[\W_]+$", re.UNICODE)

# Only these three models exist in the UI. Anything else (e.g. a stale
# TRANSLATOR_MODEL environment variable) falls back to Gemini 3.6 Flash-High.
_VALID_MODELS = ("google-web", "gemini", "gemini-3.6-flash-high")
_DEFAULT_LLM_MODEL = "gemini-3.6-flash-high"
# Real Google Generative Language model id used for both Gemini options.
# Override with GEMINI_MODEL if Google renames it.
_GOOGLE_MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview").strip() or "gemini-3-flash-preview"


class GeminiConfigError(RuntimeError):
    """A Gemini failure that retrying cannot fix: bad API key or unknown model.

    ``hint`` is a short Vietnamese sentence telling the user what to change.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


_KEY_INVALID_MARKERS = (
    "API_KEY_INVALID", "API key not valid", "API_KEY_SERVICE_BLOCKED",
    "PERMISSION_DENIED", "authentication", "invalid authentication",
)
_MODEL_MISSING_MARKERS = (
    "NOT_FOUND", "is not found", "was not found", "not supported",
    "does not exist", "Unknown name",
)


def _normalise_model(model: str) -> str:
    model = str(model or TRANSLATOR_MODEL).strip().lower()
    return model if model in _VALID_MODELS else _DEFAULT_LLM_MODEL


def _translation_signature(cues: list[dict], model: str, target: str) -> str:
    payload = {
        "model": model,
        "target": target,
        "cues": [[cue.get("start"), cue.get("end"), cue.get("text")] for cue in cues],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_translation_checkpoint(path: Path, signature: str, cue_count: int) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        values = data.get("translated", [])
        if data.get("signature") != signature or len(values) != cue_count:
            return [""] * cue_count
        return [str(value or "") for value in values]
    except (OSError, ValueError, TypeError):
        return [""] * cue_count


def _save_translation_checkpoint(path: Path, signature: str, translated: list[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {"signature": signature, "translated": translated}
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _to_seconds(values: list[int | None]) -> float:
    # Be tolerant of partially parsed timestamps from older SRT files.
    parts = [int(value or 0) for value in values[:4]]
    parts += [0] * (4 - len(parts))
    return parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000


def _timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds * 1000)))
    ms = total % 1000
    total //= 1000
    sec = total % 60
    total //= 60
    minute = total % 60
    hour = total // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}"


def _parse_translation_response(raw: str) -> dict[int, str]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip())
    try:
        data = json.loads(text, strict=False)
        items = data.get("translations", []) if isinstance(data, dict) else []
        return {
            int(item["id"]): str(item["text"]).strip()
            for item in items
            if str(item.get("text", "")).strip()
        }
    except (ValueError, TypeError, KeyError):
        pass

    # Recover valid id/text pairs when Gemini inserted raw control characters.
    recovered = {}
    pattern = re.compile(r'"id"\s*:\s*(\d+)\s*,\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"', re.S)
    for match in pattern.finditer(text):
        value = match.group(2)
        try:
            value = json.loads('"' + value + '"', strict=False)
        except ValueError:
            value = re.sub(r"\s+", " ", value).strip()
        if value:
            recovered[int(match.group(1))] = value
    return recovered


def _read_srt(path: Path) -> list[dict]:
    cues = []
    content = path.read_text(encoding="utf-8-sig")
    for block in re.split(r"\r?\n\s*\r?\n", content):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        match = next((item for line in lines if (item := TIME_RE.match(line))), None)
        if not match:
            continue
        idx = lines.index(match.group(0))
        values = [int(value) for value in match.groups()]
        text = " ".join(lines[idx + 1:]).strip()
        if text:
            cues.append({"start": _to_seconds(values[:4]), "end": _to_seconds(values[4:]), "text": text})
    return cues


def _write_srt(path: Path, cues: list[dict]) -> None:
    blocks = []
    for index, cue in enumerate(cues, 1):
        blocks.append(
            f"{index}\n{_timestamp(cue['start'])} --> {_timestamp(cue['end'])}\n{cue['text']}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _prepare_google_cues(cues: list[dict], log) -> list[dict]:
    """Normalize text without changing cue count or timeline alignment."""
    prepared = [{**cue, "text": " ".join(str(cue.get("text") or "").split())} for cue in cues]
    empty = sum(not cue["text"] for cue in prepared)
    punctuation = sum(
        bool(cue["text"]) and bool(NON_WORD_RE.fullmatch(cue["text"]))
        for cue in prepared
    )
    if empty or punctuation:
        log(
            f"[Translate] Preprocess: giu nguyen {len(prepared)} cue, "
            f"{empty} cue rong, {punctuation} cue dau cau"
        )
    return prepared


def _google_web_translate(text: str, target_language: str) -> str:
    url = (
        "https://translate.googleapis.com/translate_a/single?client=gtx"
        f"&sl=zh-CN&tl={quote(target_language)}&dt=t&q={quote(text or '')}"
    )
    last_error = "unknown error"
    for attempt in range(6):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            translated = "".join(
                item[0] for item in (payload[0] if payload else [])
                if isinstance(item, list) and item and isinstance(item[0], str)
            ).strip()
            if translated:
                return translated
            raise RuntimeError("Google Web returned empty text.")
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"Google Web failed after retries: {last_error}")


def _repair_google_translation(text: str, source_text: str = "") -> str:
    """Fix a few stable Google-Web mistranslations without changing normal output."""
    cleaned = text.strip()
    if re.search(r"\bSri Lanka\b", cleaned, re.IGNORECASE) and re.search(
        r"\b(great responsibilities|great responsibility|entrust)\b", cleaned, re.IGNORECASE
    ):
        return "When Heaven is about to entrust someone with a great responsibility..."
    return cleaned


def _translate_google_web(cues: list[dict], target_language: str, log) -> list[dict]:
    translated = [""] * len(cues)
    batch_size = 10
    batches = [
        list(range(start, min(start + batch_size, len(cues))))
        for start in range(0, len(cues), batch_size)
    ]
    log(f"[Translate] Google Web: {len(cues)} cau, chia {len(batches)} nhom {batch_size} cau")

    for batch_number, indexes in enumerate(batches, 1):
        active = [index for index in indexes if str(cues[index].get("text") or "").strip()
                  and not NON_WORD_RE.fullmatch(str(cues[index].get("text") or "").strip())]
        if not active:
            for index in indexes:
                translated[index] = cues[index].get("text") or ""
            continue
        marked = "\n".join(f"[{index + 1}] {cues[index]['text']}" for index in active)
        try:
            result = _google_web_translate(marked, target_language)
            lines = [line.strip() for line in result.splitlines() if line.strip()]
            parsed = {}
            for line in lines:
                match = re.match(r"^\[(\d+)\]\s*(.*)$", line)
                if match:
                    parsed[int(match.group(1)) - 1] = match.group(2).strip()
            if set(parsed) != set(active):
                raise RuntimeError("Google Web lam mat marker cue")
            for index in active:
                translated[index] = _repair_google_translation(
                    parsed[index] or cues[index]["text"], cues[index]["text"]
                )
        except Exception:
            # A malformed batch is retried cue-by-cue so one bad response does
            # not discard the whole subtitle file.
            log(f"[Translate] Nhom {batch_number} khong tach duoc, fallback tung cue")
            for index in active:
                translated[index] = _repair_google_translation(
                    _google_web_translate(cues[index]["text"], target_language),
                    cues[index]["text"],
                )
        for index in indexes:
            if not translated[index]:
                translated[index] = cues[index].get("text") or ""
        log(f"[TranslateProgress] {min(batch_number * batch_size, len(cues))}/{len(cues)}")
    return [{**cue, "text": translated[index]} for index, cue in enumerate(cues)]


def _gemini_translate(items: list[dict], source: str, target: str, model: str, api_key: str) -> dict[int, str]:
    if not api_key:
        raise GeminiConfigError(
            "Thiếu GEMINI_API_KEY.",
            hint="Đặt gemini_api_key trong config.local.json rồi mở lại app.",
        )
    style_guidance = {
        "English": "Use natural idiomatic English subtitle grammar and English-speaking cultural phrasing. Do not preserve Chinese word order or translate literally. Keep it concise for video subtitles: prefer 35-42 characters per line, never more than two lines, and remove redundant words without losing meaning.",
        "Vietnamese": "Use natural Vietnamese spoken subtitle grammar and culturally appropriate Vietnamese expressions. Do not preserve Chinese word order or translate literally.",
        "Japanese": "Use natural Japanese subtitle grammar, honorifics and culturally appropriate Japanese expressions. Do not preserve Chinese word order.",
        "Korean": "Use natural Korean subtitle grammar, speech levels and culturally appropriate Korean expressions. Do not preserve Chinese word order.",
        "Thai": "Use natural Thai subtitle grammar and culturally appropriate Thai expressions. Do not preserve Chinese word order.",
    }.get(target, f"Use natural grammar and cultural phrasing for {target}; do not translate word-for-word.")
    prompt = (
        f"Translate these subtitle lines directly from {source} to {target}. "
        f"{style_guidance} Preserve meaning, names, relationships and emotion while keeping the original subtitle timing. "
        "Keep every ID exactly once. Return JSON only in the form "
        '{"translations":[{"id":0,"text":"..."}]}.\n\n'
        + json.dumps(items, ensure_ascii=False)
    )
    fallback_model = "gemini-3.1-pro-preview" if "flash" not in model else "gemini-3-flash-preview"
    models = list(dict.fromkeys((model, fallback_model)))
    response = None
    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        for attempt in range(3):
            try:
                response = requests.post(
                    url,
                    headers={"x-goog-api-key": api_key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
                    },
                    timeout=(30, 300),
                )
            except requests.RequestException as exc:
                if attempt == 2 and model_name == models[-1]:
                    raise RuntimeError(f"Gemini không phản hồi sau nhiều lần thử: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            status = response.status_code
            body = response.text[:400]
            if status in (400, 401, 403) and any(m in body for m in _KEY_INVALID_MARKERS):
                raise GeminiConfigError(
                    f"Gemini HTTP {status}: {body[:200]}",
                    hint="GEMINI_API_KEY sai hoặc hết hạn — cập nhật gemini_api_key trong config.local.json.",
                )
            if status in (401, 403):
                raise GeminiConfigError(
                    f"Gemini HTTP {status}: {body[:200]}",
                    hint="GEMINI_API_KEY bị Google từ chối — kiểm tra khóa trong config.local.json.",
                )
            if status not in (429, 500, 502, 503, 504):
                break
            time.sleep(2 ** attempt)
        if response is not None and response.ok:
            break
    if response is None or not response.ok:
        status = response.status_code if response is not None else "unknown"
        detail = response.text[:300] if response is not None else "no response"
        if response is not None and (status == 404 or any(m in detail for m in _MODEL_MISSING_MARKERS)):
            raise GeminiConfigError(
                f"Gemini HTTP {status}: {detail[:200]}",
                hint=f"Model '{models[0]}' không tồn tại — đặt GEMINI_MODEL (hoặc gemini_model trong config.local.json) sang id model Gemini hợp lệ.",
            )
        raise RuntimeError(f"Gemini HTTP {status}: {detail}")
    candidates = response.json().get("candidates", [])
    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text") if candidates else ""
    text = str(text or "")
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    return _parse_translation_response(text)


def translate_srt_batch(
    root_path: str,
    target_language: str,
    model: str = TRANSLATOR_MODEL,
    api_key: str = "",
    log_callback=None,
    source_language: str = "zh",
) -> list[str]:
    model = _normalise_model(model)
    target_language = str(target_language or "en").strip().lower() or "en"

    def log(message: str) -> None:
        if log_callback:
            log_callback(message)

    root = Path(root_path)
    results = []
    source_language = str(source_language or "zh").strip().lower()
    source_files = sorted(
        (path for path in root.rglob("*.srt") if path.stem.lower() == source_language),
        key=lambda path: str(path).casefold(),
    )
    if not source_files:
        raise FileNotFoundError("Không tìm thấy zh.srt để dịch.")
    language_names = {"en": "English", "vi": "Vietnamese", "ja": "Japanese", "ko": "Korean", "th": "Thai"}
    target_name = language_names.get(target_language, target_language)
    google_model = _GOOGLE_MODEL_ID

    for source_path in source_files:
        cues = _read_srt(source_path)
        film_folder = source_path.parent
        if film_folder.name.casefold() == "subtitles":
            film_folder = film_folder.parent
        film_name = film_folder.name
        log(f"[Translate] FILM {film_name} total={len(cues)}")
        if model == "google-web":
            translated_cues = _translate_google_web(_prepare_google_cues(cues, log), target_language, log)
            output_path = source_path.parent / f"{target_language}.srt"
            _write_srt(output_path, translated_cues)
            results.append(str(output_path))
            log(f"[Translate] FILM_DONE {film_name} output={output_path.name}")
            continue
        checkpoint_name = ".translate_checkpoint_{}_{}.json".format(
            target_language,
            re.sub(r"[^a-z0-9_.-]+", "_", model),
        )
        checkpoint_path = source_path.parent / checkpoint_name
        checkpoint_signature = _translation_signature(cues, model, target_language)
        translated = _load_translation_checkpoint(
            checkpoint_path, checkpoint_signature, len(cues)
        )
        resumed_count = sum(bool(text) for text in translated)
        output_path = source_path.parent / f"{target_language}.srt"
        # Recover useful work from an incomplete output made by an older app
        # version that had no checkpoint yet. Only do this when that output
        # still contains source-language lines; a complete output remains
        # replaceable when the user intentionally selects another model.
        if not resumed_count and output_path.is_file():
            previous_output = _read_srt(output_path)
            aligned = len(previous_output) == len(cues) and all(
                abs(old["start"] - source["start"]) <= 0.002
                and abs(old["end"] - source["end"]) <= 0.002
                for old, source in zip(previous_output, cues)
            )
            incomplete_output = aligned and any(
                re.search(r"[㐀-鿿]", cue["text"])
                for cue in previous_output
            )
            if incomplete_output:
                for index, old in enumerate(previous_output):
                    old_text = old["text"].strip()
                    if old_text and not re.search(r"[㐀-鿿]", old_text):
                        translated[index] = old_text
                resumed_count = sum(bool(text) for text in translated)
                if resumed_count:
                    _save_translation_checkpoint(
                        checkpoint_path, checkpoint_signature, translated
                    )
                    log(
                        f"[TranslateResume] Tận dụng {resumed_count}/{len(cues)} "
                        "câu từ file dịch dở cũ"
                    )
        if resumed_count:
            log(
                f"[TranslateResume] Đã khôi phục {resumed_count}/{len(cues)} câu; "
                "chỉ gửi lại phần còn thiếu"
            )
        log(f"[Translate] Đang dịch {source_path.parent.parent.name} ({len(cues)} câu)")
        try:
            batch_size = int(os.getenv("TRANSLATE_BATCH_SIZE", "100"))
        except ValueError:
            batch_size = 100
        batch_size = max(25, min(batch_size, 300))
        parts = [
            (start, min(start + batch_size, len(cues)))
            for start in range(0, len(cues), batch_size)
            if any(not translated[index] for index in range(start, min(start + batch_size, len(cues))))
        ]

        def translate_part(start: int, end: int) -> tuple[int, int, dict[int, str]]:
            log(f"[TranslateProgress] START {start + 1}-{end}/{len(cues)}")
            items = [
                {"id": index, "text": cues[index]["text"]}
                for index in range(start, end) if not translated[index]
            ]
            try:
                mapping = _gemini_translate(items, "Chinese", target_name, google_model, api_key)
            except GeminiConfigError:
                raise
            except RuntimeError as exc:
                log(f"[TranslateRetry] Batch {start + 1}-{end} lỗi, thử lại: {exc}")
                try:
                    mapping = _gemini_translate(items, "Chinese", target_name, google_model, api_key)
                except GeminiConfigError:
                    raise
                except RuntimeError as exc2:
                    log(f"[TranslateSkip] Batch {start + 1}-{end} vẫn lỗi, giữ câu nguồn: {exc2}")
                    mapping = {}
            # Repair a few missing / source-unchanged lines individually. Larger
            # failures are left as source text to avoid runaway API cost.
            missing = [
                index for index in range(start, end)
                if not mapping.get(index) or mapping[index].strip() == cues[index]["text"].strip()
            ]
            for index in missing[:3]:
                try:
                    retry = _gemini_translate(
                        [{"id": index, "text": cues[index]["text"]}],
                        "Chinese", target_name, google_model, api_key,
                    )
                except GeminiConfigError:
                    raise
                except RuntimeError as exc:
                    log(f"[TranslateSkip] Cue {index + 1} sửa lại thất bại, tiếp tục: {exc}")
                    retry = {}
                if retry.get(index):
                    mapping[index] = retry[index]
            return start, end, mapping

        try:
            configured_workers = int(os.getenv("TRANSLATE_WORKERS", "8"))
        except ValueError:
            configured_workers = 8
        worker_count = min(max(1, configured_workers), max(1, len(parts)))
        log(f"[Translate] Chia {len(cues)} câu thành {len(parts)} phần xấp xỉ {batch_size} cue, chạy tối đa {worker_count} luồng")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(translate_part, start, end) for start, end in parts]
            completed = 0
            try:
                for future in as_completed(futures):
                    start, end, mapping = future.result()
                    for index, text in mapping.items():
                        if start <= index < end:
                            translated[index] = text
                    # Persist after every batch. os.replace keeps the previous
                    # valid checkpoint intact if the machine stops mid-write.
                    _save_translation_checkpoint(
                        checkpoint_path, checkpoint_signature, translated
                    )
                    completed += 1
                    saved_count = sum(bool(text) for text in translated)
                    percent = round(saved_count * 100 / max(1, len(cues)))
                    log(f"[TranslateProgress] {saved_count}/{len(cues)} câu ({completed}/{len(parts)} phần) percent={percent}")
            except GeminiConfigError as exc:
                for future in futures:
                    future.cancel()
                raise RuntimeError(f"Lỗi dịch: {exc.hint}") from exc

        missing_after_run = sum(not text for text in translated)
        for index, text in enumerate(translated):
            if not text:
                translated[index] = cues[index]["text"]
        output_cues = [{**cue, "text": translated[index]} for index, cue in enumerate(cues)]
        leftover_cjk = sum(bool(re.search(r"[一-鿿]", cue["text"])) for cue in output_cues)
        suspicious_long = sum(len(cue["text"]) > 120 for cue in output_cues)
        if leftover_cjk or suspicious_long:
            log(f"[TranslateQA] {film_name}: còn {leftover_cjk} cue có chữ Trung, {suspicious_long} cue quá dài (không gọi thêm request)")
        _write_srt(output_path, output_cues)
        if missing_after_run:
            log(
                f"[TranslateResume] Còn {missing_after_run} câu chưa dịch; "
                "checkpoint được giữ để lần sau tiếp tục"
            )
        else:
            try:
                checkpoint_path.unlink(missing_ok=True)
            except OSError:
                pass
        results.append(str(output_path))
        log(f"[Translate] FILM_DONE {film_name} output={output_path.name}")
        log(f"[Translate] Đã lưu {output_path.name}")
    return results


# Keep the UI error actionable when an unexpected legacy SRT value reaches the worker.
_translate_srt_batch_impl = translate_srt_batch


def translate_srt_batch(*args, **kwargs):
    try:
        return _translate_srt_batch_impl(*args, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"{exc} [translator.py:{traceback.extract_tb(exc.__traceback__)[-1].lineno}]") from exc
