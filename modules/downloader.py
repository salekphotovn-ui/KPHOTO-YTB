"""BBDown-only Bilibili downloader for V3."""
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from config import BBDOWN_PATH, DOWNLOAD_DIR, DEFAULT_DFN_PRIORITY, FFMPEG_PATH

BBDOWN_DIR = os.path.dirname(BBDOWN_PATH)

# BBDown --multi-thread downloads each stream as many *.vclip / *.aclip segment
# files and only merges them into the final MP4 at the end. If it is interrupted
# or the merge fails, those segments are left orphaned and no MP4 appears.
_PARTIAL_SUFFIXES = {".vclip", ".aclip", ".aria2", ".part", ".dtmp", ".tmp"}


def _partial_files(root):
    return [
        p for p in Path(root).rglob("*")
        if p.is_file() and p.suffix.lower() in _PARTIAL_SUFFIXES
    ]


def _cleanup_partials(root):
    removed = 0
    for p in _partial_files(root):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    # Drop now-empty leftover folders so the naming stage does not trip on them.
    for d in sorted(Path(root).rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return removed

# BBDown 1.6.3 only renders its progress bar on a real console; when its stdout
# is a pipe (as it is here) it prints no percentage at all. Progress is instead
# derived by polling the size of the files BBDown writes into the work dir and
# comparing against the "~NNN MB" estimates it prints for the selected streams.
_SIZE_RE = re.compile(r"~\s*([\d.]+)\s*([KMG])B", re.IGNORECASE)
_UNIT_SCALE = {"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}
_PHASE_HINTS = ("下载", "合并", "完成", "多线程", "失败", "错误", "重试", "找不到", "无法", "403",
                "error", "failed", "warning", "exception", "retry", "unable", "not found", "forbidden")


class AuthenticationRequired(RuntimeError):
    pass

def has_login_session() -> bool:
    return os.path.isfile(os.path.join(BBDOWN_DIR, "BBDown.data"))

# A Bilibili web QR login (SESSDATA) is good for roughly a month. Past this we
# warn that the saved session is probably dead even though the file still exists.
_LOGIN_STALE_DAYS = 25


def login_session_status() -> tuple[str, int]:
    """('ok' | 'stale' | 'none', age_in_days). Content + file age, no network."""
    path = os.path.join(BBDOWN_DIR, "BBDown.data")
    if not os.path.isfile(path):
        return "none", -1
    try:
        blob = open(path, "rb").read(8192).decode("utf-8", "replace")
    except OSError:
        return "none", -1
    # Before a QR login completes, BBDown writes a ~100-byte guest bili_ticket
    # stub with no SESSDATA. That is NOT a logged-in session.
    if "SESSDATA" not in blob:
        return "none", -1
    age_days = int((time.time() - os.path.getmtime(path)) // 86400)
    return ("stale" if age_days >= _LOGIN_STALE_DAYS else "ok"), age_days


# BBDown / Bilibili output that means "we blocked this because you are not
# logged in" rather than "the link is bad". BBDown 1.6.3 prints the HTTP layer
# error in English ("412, Precondition Failed" / "statuscode_reason"); the
# Chinese phrases cover the API-level -352 / risk-control messages.
_RISK_HINTS = (
    "Precondition Failed", "statuscode_reason", "-352", "-412",
    "风控", "请求被拦截", "账号未登录", "请先登录", "大会员", "会员专享",
)

# "P1: [...]" / "P2: [...]" lines from `BBDown --only-show-info`; only printed
# when a video has more than one 分P. The timestamp prefix and the Chinese
# summary line are codepage-mangled over a pipe, but this ASCII marker survives.
_PART_LINE_RE = re.compile(r"\bP(\d+):\s*\[")
_PAGE_PARAM_RE = re.compile(r"[?&]p=\d+(?:&|$)")


def _extra_bbdown_args() -> list[str]:
    """Per-machine BBDown tweaks resolved by config.py into env vars. Currently
    just a fixed UPOS CDN mirror for machines whose default mirror is throttled.
    Not --multi-thread / --force-http - those stalled long downloads."""
    host = os.getenv("BILI2YT_BBDOWN_UPOS_HOST", "").strip()
    if host:
        return ["--upos-host", host, "--force-replace-host"]
    return []


def _probe_part_count(url: str) -> int:
    """Number of 分P pages for a Bilibili video (1 when single/unknown/EP).

    Uses `BBDown --only-show-info`, a quick metadata call with no download, so a
    multi-part video can be fetched one part per plain BBDown run instead of one
    giant `-p ALL` run where an early part failing loses the rest.
    """
    if _PAGE_PARAM_RE.search(url):
        return 1  # caller already pinned a page
    try:
        out = subprocess.run(
            [BBDOWN_PATH, url, "--only-show-info", *_extra_bbdown_args()],
            cwd=BBDOWN_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=90,
        ).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return 1
    parts = {int(n) for n in _PART_LINE_RE.findall(out)}
    return max(parts) if len(parts) > 1 else 1

def bbdown_login(log_callback=None):
    def log(msg):
        (log_callback or print)(msg)
    log("[BBDown] Đang mở cửa sổ đăng nhập QR...")
    qr_path = os.path.join(BBDOWN_DIR, "qrcode.png")
    # Drop a stale QR so we only ever auto-open the fresh one.
    try:
        os.remove(qr_path)
    except OSError:
        pass
    if os.name == "nt":
        subprocess.Popen(["cmd", "/k", BBDOWN_PATH, "login"], cwd=BBDOWN_DIR,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen([BBDOWN_PATH, "login"], cwd=BBDOWN_DIR)

    def _open_qr_image():
        # The console QR renders as broken glyphs under the cmd codepage;
        # BBDown also writes a real qrcode.png - open that for scanning.
        for _ in range(30):
            time.sleep(0.5)
            if os.path.isfile(qr_path):
                if os.name == "nt":
                    try:
                        os.startfile(qr_path)  # type: ignore[attr-defined]
                    except OSError:
                        pass
                return

    threading.Thread(target=_open_qr_image, daemon=True).start()
    log("[BBDown] Quét mã QR (ảnh bin/qrcode.png sẽ tự mở). Trên điện thoại "
        "nhớ bấm 'Xác nhận', đợi cửa sổ hiện '登录成功' rồi mới đóng.")

def _snapshot(root):
    return {str(p.resolve()): p.stat().st_size for p in Path(root).rglob("*.mp4") if p.is_file()}

def _tree_bytes(root):
    total = 0
    for p in Path(root).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total

# Alternate UPOS CDN edges to force-replace into the m4s URLs when the mirror
# Bilibili handed back is throttled/unreachable. Tried in order, only after an
# attempt produced no MP4 - no mid-download kill, no infinite loop.
_UPOS_MIRRORS = (
    "upos-sz-mirrorcos.bilivideo.com",
    "upos-sz-upcdnbda2.bilivideo.com",
    "upos-sz-mirrorali.bilivideo.com",
    "upos-sz-mirrorhw.bilivideo.com",
    "upos-sz-mirrorcosb.bilivideo.com",
)


def _run_bbdown(url, dfn_priority, output_dir, upos_host, log,
                progress_index, progress_total):
    """One BBDown run. Returns (new_mp4_files, returncode, risk_control_lines);
    it never raises on a plain no-MP4 failure so the caller can rotate CDN."""
    before = _snapshot(output_dir)
    baseline_bytes = _tree_bytes(output_dir)
    # BBDown 1.6.3 turns --multi-thread ON by default; its segmented downloader
    # thrashes on a throttled CDN and stalls long videos at ~80%. The user's
    # own working tai-video.bat runs `BBDown "%link%" --multi-thread false` and
    # pulls 10-hour videos down fine, so match that: one sequential connection.
    # --upos-host + --force-replace-host swap the CDN edge when one is bad.
    cmd = [BBDOWN_PATH, url, "--work-dir", output_dir,
           "--dfn-priority", dfn_priority, "--ffmpeg-path", FFMPEG_PATH,
           "--multi-thread", "false"]
    if upos_host:
        cmd += ["--upos-host", upos_host, "--force-replace-host"]

    process = subprocess.Popen(cmd, cwd=BBDOWN_DIR, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=False, bufsize=0)
    recent_sizes: list[int] = []
    risk_control: list[str] = []

    def _drain():
        pending = ""
        while True:
            chunk = process.stdout.read(4096) if process.stdout else b""
            if not chunk:
                break
            pending += chunk.decode("utf-8", errors="replace")
            parts = re.split(r"[\r\n]", pending)
            pending = parts.pop()
            for line in parts:
                line = line.strip()
                if not line:
                    continue
                for value, unit in _SIZE_RE.findall(line):
                    try:
                        recent_sizes.append(int(float(value) * _UNIT_SCALE[unit.upper()]))
                    except (ValueError, KeyError):
                        pass
                if not risk_control and any(hint in line for hint in _RISK_HINTS):
                    risk_control.append(line)
                safe = re.sub(r"https?://\S+", "[CDN URL]", line)
                if len(safe) > 300:
                    safe = safe[:300] + "..."
                if any(hint in line.lower() if hint.isascii() else hint in line
                       for hint in _PHASE_HINTS):
                    log(f"[BBDown] {safe}")
        try:
            process.stdout.close()
        except Exception:
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    last_bytes, last_time, last_pct = 0, time.monotonic(), -1
    while process.poll() is None:
        time.sleep(1.0)
        got = max(0, _tree_bytes(output_dir) - baseline_bytes)
        expected = sum(recent_sizes[-2:]) if len(recent_sizes) >= 2 else 0
        now = time.monotonic()
        speed = ""
        if now > last_time and got >= last_bytes:
            rate = (got - last_bytes) / (now - last_time)
            if rate > 0:
                speed = f" speed={rate / (1024 * 1024):.2f} MB/s"
        last_bytes, last_time = got, now
        if expected > 0:
            pct = max(1, min(99, int(got * 100 / expected)))
            if pct != last_pct:
                last_pct = pct
                log(f"[DownloadProgress] PERCENT i={progress_index} total={progress_total} percent={pct}{speed}")
        elif got > 0:
            log(f"[BBDown] Đã tải {got / (1024 * 1024):.1f} MB{speed}")

    reader.join(timeout=5)
    after = _snapshot(output_dir)
    new_files = sorted(p for p, size in after.items() if before.get(p) != size)
    return new_files, process.returncode, risk_control


def download_video(url: str, dfn_priority: str = DEFAULT_DFN_PRIORITY,
                   output_dir: str = None, log_callback=None,
                   progress_index: int = 1, progress_total: int = 1) -> list[str]:
    output_dir = output_dir or DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)
    def log(msg):
        (log_callback or print)(msg)

    pinned = os.getenv("BILI2YT_BBDOWN_UPOS_HOST", "").strip()
    # A machine that pinned a mirror (Tải dialog / config.local.json) gets that
    # one and no rotation. Otherwise BBDown's own pick first, then rotate the
    # mirrors - only after an attempt produced no MP4.
    host_plan = [pinned] if pinned else ["", *_UPOS_MIRRORS]

    log(f"[BBDown] Đang tải link {progress_index}/{progress_total}")
    log(f"[DownloadProgress] START i={progress_index} total={progress_total}")

    last_rc, last_risk = 1, []
    for attempt, host in enumerate(host_plan):
        if attempt:
            swept = _cleanup_partials(output_dir)
            log(f"[BBDown] CDN lỗi/bị bóp băng thông — đổi sang {host} rồi thử lại "
                f"(mirror {attempt}/{len(host_plan) - 1}"
                + (f", đã dọn {swept} mảnh tạm)" if swept else ")"))
        if host:
            label = f"CDN cố định: {host}" if pinned else f"CDN: {host}"
        else:
            label = "CDN mặc định của BBDown"
        log(f"[BBDown] BBDown 1.6.3 (--multi-thread false) — {label}")

        new_files, rc, risk = _run_bbdown(
            url, dfn_priority, output_dir, host, log, progress_index, progress_total
        )
        if new_files:
            swept = _cleanup_partials(output_dir)
            if swept:
                log(f"[BBDown] Đã dọn {swept} mảnh tạm sau khi ghép")
            log(f"[DownloadProgress] PERCENT i={progress_index} total={progress_total} percent=100")
            log(f"[DownloadProgress] DONE i={progress_index} total={progress_total}")
            log(f"[BBDown] Tải xong {len(new_files)} file"
                + ("" if rc == 0 else f" (BBDown thoát mã {rc})"))
            return new_files
        last_rc, last_risk = rc, risk
        if risk:
            break  # not-logged-in / 风控: a different CDN won't help

    removed = _cleanup_partials(output_dir)
    if last_risk:
        log(f"[BBDown] {re.sub(r'https?://\\S+', '[CDN URL]', last_risk[0])}")
        raise RuntimeError(
            f"Link {progress_index}: Bilibili chặn (风控/chưa đăng nhập). "
            "Mở lại hộp thoại Tải, bấm 'Đăng nhập QR' quét mã rồi tải lại. "
            f"Đã dọn {removed} file tạm."
        )
    tried = 1 if pinned else len(host_plan)
    raise RuntimeError(
        f"BBDown không tạo được MP4 cho link {progress_index} sau {tried} lần thử"
        + ("" if pinned else " (đã đổi qua các CDN mirror)")
        + f" (thoát mã {last_rc}); đã dọn {removed} file tạm."
    )


def _expand_multipart(urls: list[str], log_callback=None) -> list[str]:
    """Turn a bare multi-分P link into one `...?p=k` link per page.

    BBDown names the output subfolder after the video title, so every page lands
    in the same folder and the auto pipeline's concat step joins them into one
    video. Downloading page by page also means a failed page (e.g. a 10-hour P1
    on a throttled CDN) no longer takes the other pages down with it.
    """
    out: list[str] = []
    for raw in urls:
        url = raw.strip()
        if not url:
            continue
        count = _probe_part_count(url)
        if count > 1:
            if log_callback:
                log_callback(
                    f"[BBDown] Link nhiều phần: {count} phần — tải lần lượt P1..P{count} "
                    "(cùng thư mục, tool sẽ tự ghép)"
                )
            sep = "&" if "?" in url else "?"
            out.extend(f"{url}{sep}p={page}" for page in range(1, count + 1))
        else:
            out.append(url)
    return out


def download_multiple(urls: list[str], dfn_priority: str = DEFAULT_DFN_PRIORITY,
                      output_dir: str = None, log_callback=None) -> list[str]:
    urls = _expand_multipart(urls, log_callback)
    results = []
    failures = []
    for i, url in enumerate(urls, 1):
        try:
            results.extend(download_video(url.strip(), dfn_priority, output_dir,
                                          log_callback, i, len(urls)))
        except Exception as exc:
            failures.append(f"Link {i} ({url.strip()}): {exc}")
            if log_callback: log_callback(f"[BBDown] Lỗi link {i}: {exc}")
    if failures and results and log_callback:
        # Partial failure: the run continues, so a per-link line that scrolls
        # out of the log is not enough. (When results is empty the raise below
        # already carries the full list, so don't print it twice.)
        log_callback(
            f"[BBDown] LỖI: chỉ tải được {len(results)}/{len(urls)} link — "
            f"{len(failures)} link thất bại:\n" + "\n".join(failures)
        )
    if not results and failures:
        # Every link in the batch failed - don't let the pipeline carry on to
        # concat/rename/OCR/export on an empty folder and report "Hoàn tất" as
        # if a video had actually come down.
        raise RuntimeError(
            f"Không tải được video nào ({len(failures)}/{len(urls)} link lỗi):\n"
            + "\n".join(failures)
        )
    return results
