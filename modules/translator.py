"""Gemini 3.6 Flash-High subtitle translation for the V3 batch workflow.

Translation goes through one OpenAI-compatible endpoint (a third-party Gemini
proxy): base url + bearer key come from config.local.json
(gemini_base_url / gemini_api_key), model name from GEMINI_MODEL.
"""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import traceback
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from config import TRANSLATOR_MODEL


TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)

_MODEL = "gemini-3.6-flash-high"

_KEY_INVALID_MARKERS = (
    "API_KEY_INVALID", "API key not valid", "API_KEY_SERVICE_BLOCKED",
    "PERMISSION_DENIED", "authentication", "invalid authentication",
    "invalid_api_key", "unauthorized",
)


class GeminiConfigError(RuntimeError):
    """A failure that retrying cannot fix (bad key / bad base url / bad model).

    ``hint`` is a short Vietnamese sentence telling the user what to change.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


def _proxy_base_url() -> str:
    return os.getenv("GEMINI_BASE_URL", "").rstrip("/")


def _proxy_model_name() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.6-flash-high").strip() or "gemini-3.6-flash-high"


# Ordered proxy model names, most-preferred first (config.local.json
# "translate_models" -> GEMINI_MODELS). A batch tries them in order; a model
# that is slow / rate-limited / empty is parked for a short cooldown so the
# next batch rolls straight to the following model instead of waiting.
_MODEL_COOLDOWN_LOCK = threading.Lock()
_model_cooldown: dict[str, float] = {}


def _proxy_models() -> list[str]:
    raw = os.getenv("GEMINI_MODELS", "").strip()
    names = [part.strip() for part in raw.split(",") if part.strip()] if raw else []
    return names or [_proxy_model_name()]


def _model_ready(name: str) -> bool:
    with _MODEL_COOLDOWN_LOCK:
        return _model_cooldown.get(name, 0.0) <= time.monotonic()


def _cool_model(name: str, seconds: float) -> None:
    with _MODEL_COOLDOWN_LOCK:
        _model_cooldown[name] = max(
            _model_cooldown.get(name, 0.0), time.monotonic() + max(1.0, seconds)
        )


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

    # Recover valid id/text pairs when the model inserted raw control characters.
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


def _post_chat(base_url: str, api_key: str, model_name: str, target: str, prompt: str):
    return requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model_name,
            "messages": [
                {"role": "system", "content": f"You are a professional native {target} subtitle translator."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
            "stream": False,
        },
        timeout=(20, 240),
    )


def _translate_batch(items: list[dict], source: str, target: str, api_key: str, log=None) -> dict[int, str]:
    """Translate one batch, rolling through the model pool on slow / 429 answers."""
    base_url = _proxy_base_url()
    if not base_url:
        raise GeminiConfigError(
            "Thiếu GEMINI_BASE_URL cho Gemini 3.6 Flash-High.",
            hint="Thêm gemini_base_url (URL API bên thứ 3) vào config.local.json.",
        )
    if not api_key:
        raise GeminiConfigError(
            "Thiếu GEMINI_API_KEY.",
            hint="Đặt gemini_api_key trong config.local.json rồi mở lại app.",
        )
    prompt = (
        f"Translate subtitle lines from {source} to {target}. Write them as a native "
        f"{target} subtitle translator would: natural idiomatic wording, correct "
        "conversational rhythm, and the right register for each speaker. Do not translate "
        "word-for-word or keep Chinese sentence order. Preserve names, relationships, plot "
        "facts, emotion and setting. Keep subtitles concise; never add explanations. Return "
        'JSON only as {"translations":[{"id":0,"text":"..."}]} and include every ID once.\n\n'
        + json.dumps(items, ensure_ascii=False)
    )

    def emit(message: str) -> None:
        if log:
            log(message)

    models = _proxy_models()
    primary = models[0]
    dead: set[str] = set()
    last_error = "unknown error"

    # Pass 0 respects each model's cooldown; pass 1 ignores it (everything is
    # cooling, so just push through in priority order rather than stall).
    for pass_no in range(2):
        for name in models:
            if name in dead:
                continue
            if pass_no == 0 and not _model_ready(name):
                continue
            try:
                response = _post_chat(base_url, api_key, name, target, prompt)
            except requests.RequestException as exc:
                last_error = f"{name}: {exc}"
                _cool_model(name, 20)
                emit(f"[TranslateModel] {name} lỗi mạng, đổi endpoint")
                continue
            if response.ok:
                try:
                    data = response.json() if response.text.strip() else {}
                except ValueError:
                    data = {}
                # A broken upstream (e.g. rcloud) answers HTTP 200 with an error
                # envelope after a long hang. Treat it like a 5xx: park the model
                # for a while so the rest of the run skips it.
                if isinstance(data, dict) and (data.get("error") or data.get("type") == "error"):
                    err = data.get("error") or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    last_error = f"{name}: 200-lỗi {str(msg)[:120]}"
                    _cool_model(name, 180)
                    emit(f"[TranslateModel] {name} upstream lỗi, đổi endpoint")
                    continue
                choices = data.get("choices") or []
                message = choices[0].get("message", {}) if choices else {}
                text = message.get("content", "") if isinstance(message, dict) else ""
                if isinstance(text, list):
                    text = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in text)
                parsed = _parse_translation_response(text) if str(text).strip() else {}
                if parsed:
                    if name != primary:
                        emit(f"[TranslateModel] dùng {name} ({len(parsed)} câu)")
                    return parsed
                last_error = f"{name}: nội dung rỗng / không đọc được JSON"
                _cool_model(name, 45)
                continue
            body = response.text[:400]
            low = body.lower()
            if response.status_code in (400, 401, 403) and any(m.lower() in low for m in _KEY_INVALID_MARKERS):
                raise GeminiConfigError(
                    f"HTTP {response.status_code}: {body[:200]}",
                    hint="gemini_api_key sai hoặc hết hạn — cập nhật trong config.local.json.",
                )
            if response.status_code in (401, 403):
                raise GeminiConfigError(
                    f"HTTP {response.status_code}: {body[:200]}",
                    hint="API key bị endpoint từ chối — kiểm tra gemini_api_key / gemini_base_url trong config.local.json.",
                )
            if response.status_code == 404:
                dead.add(name)
                last_error = f"{name}: HTTP 404 {body[:160]}"
                emit(f"[TranslateModel] {name} không có trên key này, bỏ qua")
                continue
            last_error = f"{name}: HTTP {response.status_code}: {body[:160]}"
            if response.status_code in (429, 500, 502, 503, 504):
                try:
                    cooldown = float(response.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    cooldown = 20.0
                _cool_model(name, min(90.0, max(10.0, cooldown)))
                emit(f"[TranslateModel] {name} {response.status_code} chậm/nghẽn, đổi endpoint")
                continue
            raise RuntimeError(last_error)
        if models and all(name in dead for name in models):
            raise GeminiConfigError(
                f"Không model nào chạy được: {last_error}",
                hint="Kiểm tra tên model trong translate_models của config.local.json (có/không tiền tố provider).",
            )
        if pass_no == 0:
            time.sleep(8)
    raise RuntimeError(f"Tất cả model dịch đều bận: {last_error}")


def translate_srt_batch(
    root_path: str,
    target_language: str,
    model: str = TRANSLATOR_MODEL,
    api_key: str = "",
    log_callback=None,
    source_language: str = "zh",
) -> list[str]:
    target_language = str(target_language or "en").strip().lower() or "en"

    def log(message: str) -> None:
        if log_callback:
            log_callback(message)

    root = Path(root_path)
    results = []
    failed_films: list[str] = []
    source_language = str(source_language or "zh").strip().lower()
    if source_language == target_language:
        raise RuntimeError(
            f"Ngôn ngữ nguồn và đầu ra đều là '{target_language}'. "
            "Chọn nguồn 'zh' và đầu ra 'en' (hoặc ngôn ngữ khác nguồn)."
        )
    source_files = sorted(
        (path for path in root.rglob("*.srt") if path.stem.lower() == source_language),
        key=lambda path: str(path).casefold(),
    )
    if not source_files:
        raise FileNotFoundError(f"Không tìm thấy {source_language}.srt để dịch.")
    language_names = {"en": "English", "vi": "Vietnamese", "ja": "Japanese", "ko": "Korean", "th": "Thai"}
    target_name = language_names.get(target_language, target_language)
    source_names = {"zh": "Chinese", "en": "English", "vi": "Vietnamese", "ja": "Japanese", "ko": "Korean", "th": "Thai"}
    source_name = source_names.get(source_language, "Chinese")

    for source_path in source_files:
        cues = _read_srt(source_path)
        film_folder = source_path.parent
        if film_folder.name.casefold() == "subtitles":
            film_folder = film_folder.parent
        film_name = film_folder.name
        log(f"[Translate] FILM {film_name} total={len(cues)}")

        checkpoint_name = f".translate_checkpoint_{target_language}_{_MODEL}.json"
        checkpoint_path = source_path.parent / checkpoint_name
        checkpoint_signature = _translation_signature(cues, _MODEL, target_language)
        translated = _load_translation_checkpoint(checkpoint_path, checkpoint_signature, len(cues))
        resumed_count = sum(bool(text) for text in translated)
        output_path = source_path.parent / f"{target_language}.srt"
        # Recover useful work from an incomplete output made by an older app
        # version that had no checkpoint yet.
        if not resumed_count and output_path.is_file():
            previous_output = _read_srt(output_path)
            aligned = len(previous_output) == len(cues) and all(
                abs(old["start"] - source["start"]) <= 0.002
                and abs(old["end"] - source["end"]) <= 0.002
                for old, source in zip(previous_output, cues)
            )
            incomplete_output = aligned and any(
                re.search(r"[㐀-鿿]", cue["text"]) for cue in previous_output
            )
            if aligned and not incomplete_output and all(cue["text"].strip() for cue in previous_output):
                # A finished, fully non-Chinese en.srt from an earlier run: this
                # film is already done, so skip re-sending it to the endpoint.
                for index, old in enumerate(previous_output):
                    translated[index] = old["text"].strip()
                resumed_count = len(cues)
                log(f"[TranslateResume] {film_name}: {output_path.name} đã dịch xong, bỏ qua")
            elif incomplete_output:
                for index, old in enumerate(previous_output):
                    old_text = old["text"].strip()
                    if old_text and not re.search(r"[㐀-鿿]", old_text):
                        translated[index] = old_text
                resumed_count = sum(bool(text) for text in translated)
                if resumed_count:
                    _save_translation_checkpoint(checkpoint_path, checkpoint_signature, translated)
                    log(f"[TranslateResume] Tận dụng {resumed_count}/{len(cues)} câu từ file dịch dở cũ")
        if resumed_count:
            log(
                f"[TranslateResume] Đã khôi phục {resumed_count}/{len(cues)} câu; "
                "chỉ gửi lại phần còn thiếu"
            )
        log(f"[Translate] Đang dịch {film_name} ({len(cues)} câu)")
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
            # _translate_batch already rolls through the whole model pool twice,
            # so an empty result here means every model is congested - retrying
            # the same call would only add load. Keep the source for now; the
            # checkpoint lets the next Dịch press fill it in.
            try:
                mapping = _translate_batch(items, source_name, target_name, api_key, log=log)
            except GeminiConfigError:
                raise
            except RuntimeError as exc:
                log(f"[TranslateSkip] Batch {start + 1}-{end} chưa dịch được: {exc}")
                mapping = {}
            missing = [
                index for index in range(start, end)
                if not mapping.get(index) or mapping[index].strip() == cues[index]["text"].strip()
            ]
            # Single-cue fixups only when the batch mostly worked (a real gap),
            # not when the whole batch failed (endpoint down).
            if mapping and len(missing) <= max(5, (end - start) // 4):
                for index in missing[:5]:
                    try:
                        retry = _translate_batch(
                            [{"id": index, "text": cues[index]["text"]}],
                            source_name, target_name, api_key, log=log,
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
            configured_workers = int(os.getenv("TRANSLATE_WORKERS", "3"))
        except ValueError:
            configured_workers = 3
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
                    _save_translation_checkpoint(checkpoint_path, checkpoint_signature, translated)
                    completed += 1
                    saved_count = sum(bool(text) for text in translated)
                    percent = round(saved_count * 100 / max(1, len(cues)))
                    log(f"[TranslateProgress] {saved_count}/{len(cues)} câu ({completed}/{len(parts)} phần) percent={percent}")
            except GeminiConfigError as exc:
                for future in futures:
                    future.cancel()
                log(f"[Translate] Endpoint từ chối: {exc}")
                raise RuntimeError(f"Lỗi dịch: {exc.hint}  ·  {exc}") from exc

        missing_after_run = sum(not text for text in translated)
        for index, text in enumerate(translated):
            if not text:
                translated[index] = cues[index]["text"]
        output_cues = [{**cue, "text": translated[index]} for index, cue in enumerate(cues)]
        leftover_cjk = sum(bool(re.search(r"[一-鿿]", cue["text"])) for cue in output_cues)
        suspicious_long = sum(len(cue["text"]) > 120 for cue in output_cues)
        if leftover_cjk or suspicious_long:
            log(f"[TranslateQA] {film_name}: còn {leftover_cjk} cue có chữ Trung, {suspicious_long} cue quá dài (không gọi thêm request)")
        # Persist whatever did translate so a re-run resumes, and keep the
        # checkpoint, but do NOT let the pipeline treat a mostly-Chinese file as
        # a finished translation (this is what a rate-limit storm produces).
        _write_srt(output_path, output_cues)
        tolerance = max(3, len(cues) // 20)
        if missing_after_run > tolerance or leftover_cjk > tolerance:
            _save_translation_checkpoint(checkpoint_path, checkpoint_signature, translated)
            results.append(str(output_path))
            failed_films.append(f"{film_name} ({max(missing_after_run, leftover_cjk)}/{len(cues)})")
            log(
                f"[Translate] FILM_FAIL {film_name}: {missing_after_run} câu chưa dịch, "
                f"{leftover_cjk} cue còn chữ Trung — giữ checkpoint để dịch lại"
            )
            continue
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

    if failed_films:
        raise RuntimeError(
            "Dịch chưa xong (endpoint bị giới hạn tốc độ) cho: "
            + ", ".join(failed_films)
            + ". Chờ vài phút rồi bấm Dịch lại — phần đã dịch được giữ lại, "
            "không cần làm lại từ đầu."
        )
    return results


# Keep the UI error actionable when an unexpected legacy SRT value reaches the worker.
_translate_srt_batch_impl = translate_srt_batch


def translate_srt_batch(*args, **kwargs):
    try:
        return _translate_srt_batch_impl(*args, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"{exc} [translator.py:{traceback.extract_tb(exc.__traceback__)[-1].lineno}]") from exc
