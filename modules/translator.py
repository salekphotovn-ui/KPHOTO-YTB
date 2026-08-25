"""Google Web/Gemini/Qwen subtitle translation for the V3 batch workflow."""

from __future__ import annotations

import json
import re
import time
import traceback
import os
import threading
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
_QWEN_USAGE = {"input": 0, "output": 0, "requests": 0}
_QWEN_USAGE_LOCK = threading.Lock()
_QWEN_PRICING = {"input": 800, "output": 4000, "minimum": 20}


def _qwen_cost_vnd() -> float:
    with _QWEN_USAGE_LOCK:
        raw = (_QWEN_USAGE["input"] * _QWEN_PRICING["input"] + _QWEN_USAGE["output"] * _QWEN_PRICING["output"]) / 1_000_000
        return max(raw, _QWEN_USAGE["requests"] * _QWEN_PRICING["minimum"])


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
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
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
    last_error = None
    for attempt in range(3):
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
    # Google retired the Gemini 2.5 model IDs; keep old settings compatible.
    model = {
        "gemini-2.5-pro": "gemini-3.1-pro-preview",
        "gemini-2.5-flash": "gemini-3-flash-preview",
    }.get(model, model)
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
    fallback_model = "gemini-3-flash-preview" if "flash" in model else "gemini-3.1-pro-preview"
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
            if response.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(2 ** attempt)
        if response is not None and response.ok:
            break
    if response is None or not response.ok:
        status = response.status_code if response is not None else "unknown"
        detail = response.text[:300] if response is not None else "no response"
        raise RuntimeError(f"Gemini HTTP {status}: {detail}")
    candidates = response.json().get("candidates", [])
    text = candidates[0]["content"]["parts"][0].get("text", "") if candidates else ""
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    return _parse_translation_response(text)


def _qwen_translate(items: list[dict], source: str, target: str, model: str, api_key: str) -> dict[int, str]:
    """Call an OpenAI-compatible Qwen endpoint without exposing credentials."""
    if not api_key:
        raise RuntimeError("Thiếu QWEN_API_KEY (đặt trong biến môi trường hoặc ô API key).")
    base_url = os.getenv("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").rstrip("/")
    prompt = (
        f"Translate subtitle lines from {source} to {target}. Write them as a native English subtitle translator would. "
        "Use natural idiomatic English, correct conversational rhythm, and the appropriate register for each speaker "
        "(child, parent, servant, noble, soldier, romantic partner, etc.). Do not translate word-for-word or preserve "
        "Chinese sentence order. Adapt idioms and jokes so an English-speaking viewer understands the intended meaning, "
        "while preserving names, relationships, plot facts, emotion and historical/cultural setting. Keep subtitles concise "
        "and readable; never add explanations. Return JSON only as "
        '{"translations":[{"id":0,"text":"..."}]} and include every ID exactly once.\n\n'
        + json.dumps(items, ensure_ascii=False)
    )
    for attempt in range(3):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a professional native English subtitle translator."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 8192,
                    "stream": False,
                },
                timeout=(30, 300),
            )
            if response.ok:
                if not response.text.strip():
                    raise RuntimeError("Vilao trả response rỗng")
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError(f"Vilao trả body không phải JSON: {response.text[:200]}") from exc
                usage = data.get("usage", {}) or {}
                with _QWEN_USAGE_LOCK:
                    _QWEN_USAGE["input"] += int(usage.get("prompt_tokens", 0) or 0)
                    _QWEN_USAGE["output"] += int(usage.get("completion_tokens", 0) or 0)
                    _QWEN_USAGE["requests"] += 1
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError(f"Vilao trả JSON thiếu choices: {list(data)[:8]}")
                message = choices[0].get("message", {})
                text = message.get("content", "") if isinstance(message, dict) else ""
                if isinstance(text, list):
                    text = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in text)
                if not str(text).strip():
                    raise RuntimeError("Vilao trả choices nhưng content rỗng")
                return _parse_translation_response(text)
            if response.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"Qwen HTTP {response.status_code}: {response.text[:300]}")
        except (requests.RequestException, KeyError, IndexError, ValueError, RuntimeError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Qwen không phản hồi sau nhiều lần thử: {exc}") from exc
        time.sleep(2 ** attempt)
    raise RuntimeError("Qwen không trả về kết quả.")


def translate_srt_batch(
    root_path: str,
    target_language: str,
    model: str = TRANSLATOR_MODEL,
    api_key: str = "",
    log_callback=None,
    source_language: str = "zh",
) -> list[str]:
    # Google Web is the free, reliable default. Gemini remains available when
    # explicitly selected from the UI.
    model = str(model or TRANSLATOR_MODEL).strip().lower()
    target_language = str(target_language or "en").strip().lower() or "en"
    def log(message: str) -> None:
        if log_callback:
            log_callback(message)

    root = Path(root_path)
    results = []
    source_language = str(source_language or "zh").strip().lower()
    with _QWEN_USAGE_LOCK:
        _QWEN_USAGE.update(input=0, output=0, requests=0)
        if model in ("qwen3.8-max", "hybrid-qwen-gemini"):
            _QWEN_PRICING.update(input=285, output=1045, minimum=13)
        elif model == "gemini-3.6-flash-high":
            _QWEN_PRICING.update(input=500, output=1500, minimum=3)
        elif model == "gemini-3.1-flash-lite":
            _QWEN_PRICING.update(input=500, output=1500, minimum=10)
        else:
            _QWEN_PRICING.update(input=800, output=4000, minimum=20)
    source_files = sorted(
        (path for path in root.rglob("*.srt") if path.stem.lower() == source_language),
        key=lambda path: str(path).casefold(),
    )
    if not source_files:
        raise FileNotFoundError("Không tìm thấy zh.srt để dịch.")
    language_names = {"en": "English", "vi": "Vietnamese", "ja": "Japanese", "ko": "Korean", "th": "Thai"}
    target_name = language_names.get(target_language, target_language)
    main_model = "gemini-3-flash-preview" if model == "hybrid-flash-pro" else model
    repair_model = "gemini-3.1-pro-preview" if model == "hybrid-flash-pro" else model
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
        translated = [""] * len(cues)
        log(f"[Translate] Đang dịch {source_path.parent.parent.name} ({len(cues)} câu)")
        try:
            batch_size = int(os.getenv("QWEN_BATCH_SIZE", "100"))
        except ValueError:
            batch_size = 100
        batch_size = max(25, min(batch_size, 300))
        parts = [(start, min(start + batch_size, len(cues))) for start in range(0, len(cues), batch_size)]

        def translate_part(start: int, end: int) -> tuple[int, int, dict[int, str]]:
            log(f"[TranslateProgress] START {start + 1}-{end}/{len(cues)}")
            items = [{"id": index, "text": cues[index]["text"]} for index in range(start, end)]
            if model.startswith("qwen") or model in ("gemini-3.6-flash-high", "gemini-3.1-flash-lite", "hybrid-qwen-gemini"):
                api_model = "qwen3.8-max" if model == "hybrid-qwen-gemini" else model
                mapping = _qwen_translate(items, "Chinese", target_name, api_model, api_key)
            else:
                mapping = _gemini_translate(items, "Chinese", target_name, main_model, api_key)
            # Retry a malformed/empty batch once as a batch. Never fan out to
            # one request per cue: that can multiply cost by hundreds.
            if not mapping:
                log(f"[Translate] Batch {start + 1}-{end} trả kết quả rỗng, retry nguyên batch")
                retry_batch = _qwen_translate if model.startswith("qwen") or model in ("gemini-3.6-flash-high", "gemini-3.1-flash-lite", "hybrid-qwen-gemini") else _gemini_translate
                retry_model = "qwen3.8-max" if model == "hybrid-qwen-gemini" else (model if retry_batch is _qwen_translate else main_model)
                mapping = retry_batch(items, "Chinese", target_name, retry_model, api_key)
            if model == "hybrid-qwen-gemini":
                flagged = [item for item in items if re.search(r"[\u4e00-\u9fff]", mapping.get(item["id"], "")) or len(mapping.get(item["id"], "")) > 120]
                if flagged:
                    gemini_key = os.getenv("GEMINI_API_KEY", "")
                    polished = _qwen_translate(flagged[:10], "Chinese", target_name, "gemini-3.6-flash-high", gemini_key)
                    mapping.update(polished)
                    log(f"[TranslateHybrid] Đã sửa chọn lọc {len(polished)} cue")
            missing = [
                index for index in range(start, end)
                if not mapping.get(index) or mapping[index].strip() == cues[index]["text"].strip()
            ]
            # Hybrid mode uses Pro only for incomplete/source-unchanged lines.
            # Only repair a small number of missing cues individually. Larger
            # failures are left as source text to avoid runaway API cost.
            for index in missing[:3]:
                retry_fn = _qwen_translate if model.startswith("qwen") or model in ("gemini-3.6-flash-high", "gemini-3.1-flash-lite") else _gemini_translate
                retry = retry_fn(
                    [{"id": index, "text": cues[index]["text"]}],
                    "Chinese",
                    target_name,
                    repair_model,
                    api_key,
                )
                if retry.get(index):
                    mapping[index] = retry[index]
            return start, end, mapping

        # Configurable concurrency: 5 is faster for Vilao while remaining
        # below the level that previously caused malformed transient responses.
        try:
            configured_workers = int(os.getenv("QWEN_WORKERS", "10"))
        except ValueError:
            configured_workers = 10
        worker_count = min(max(1, configured_workers), max(1, len(parts)))
        log(f"[Translate] Chia {len(cues)} câu thành {len(parts)} phần xấp xỉ {batch_size} cue, chạy tối đa {worker_count} luồng")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(translate_part, start, end) for start, end in parts]
            completed = 0
            completed_end = 0
            for future in as_completed(futures):
                start, end, mapping = future.result()
                for index, text in mapping.items():
                    if start <= index < end:
                        translated[index] = text
                completed += 1
                completed_end = max(completed_end, end)
                percent = round(completed_end * 100 / max(1, len(cues)))
                log(f"[TranslateProgress] {completed_end}/{len(cues)} câu ({completed}/{len(parts)} phần) percent={percent}")

        for index, text in enumerate(translated):
            if not text:
                translated[index] = cues[index]["text"]
        output_path = source_path.parent / f"{target_language}.srt"
        output_cues = [{**cue, "text": translated[index]} for index, cue in enumerate(cues)]
        leftover_cjk = sum(bool(re.search(r"[\u4e00-\u9fff]", cue["text"])) for cue in output_cues)
        suspicious_long = sum(len(cue["text"]) > 120 for cue in output_cues)
        if leftover_cjk or suspicious_long:
            log(f"[TranslateQA] {film_name}: còn {leftover_cjk} cue có chữ Trung, {suspicious_long} cue quá dài (không gọi thêm request)")
        _write_srt(output_path, output_cues)
        results.append(str(output_path))
        log(f"[Translate] FILM_DONE {film_name} output={output_path.name}")
        log(f"[Translate] Đã lưu {output_path.name}")
        if model.startswith("qwen") or model in ("gemini-3.6-flash-high", "gemini-3.1-flash-lite", "hybrid-qwen-gemini"):
            with _QWEN_USAGE_LOCK:
                usage_snapshot = dict(_QWEN_USAGE)
            log(
                f"[TranslateCost] Qwen {film_name}: input={usage_snapshot['input']:,} token, "
                f"output={usage_snapshot['output']:,} token, requests={usage_snapshot['requests']}, "
                f"tam tinh={_qwen_cost_vnd():,.0f} VND"
            )
    if model.startswith("qwen") or model in ("gemini-3.6-flash-high", "gemini-3.1-flash-lite", "hybrid-qwen-gemini"):
        with _QWEN_USAGE_LOCK:
            total = _qwen_cost_vnd()
            reqs = _QWEN_USAGE["requests"]
        log(f"[TranslateCost] TỔNG PHIÊN: {total:,.0f} VND ({reqs} requests; tối thiểu {_QWEN_PRICING['minimum']} VND/request)")
    return results


# Keep the UI error actionable when an unexpected legacy SRT value reaches the worker.
_translate_srt_batch_impl = translate_srt_batch


def translate_srt_batch(*args, **kwargs):
    try:
        return _translate_srt_batch_impl(*args, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"{exc} [translator.py:{traceback.extract_tb(exc.__traceback__)[-1].lineno}]") from exc
