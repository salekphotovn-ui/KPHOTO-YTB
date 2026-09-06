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

def download_video(url: str, dfn_priority: str = DEFAULT_DFN_PRIORITY,
                   output_dir: str = None, log_callback=None,
                   progress_index: int = 1, progress_total: int = 1) -> list[str]:
    output_dir = output_dir or DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)
    def log(msg):
        (log_callback or print)(msg)
    before = _snapshot(output_dir)
    baseline_bytes = _tree_bytes(output_dir)
    # Match a plain `BBDown <url>` run. --work-dir / --dfn-priority only decide
    # where files land and which quality; --ffmpeg-path just points at the
    # bundled ffmpeg so the merge step never fails for lack of it. No
    # --multi-thread / --force-http: those are what stalled long downloads.
    cmd = [BBDOWN_PATH, url, "--work-dir", output_dir,
           "--dfn-priority", dfn_priority, "--ffmpeg-path", FFMPEG_PATH]
    log("[BBDown] BBDown 1.6.3 (đơn luồng, như chạy tay)")
    log(f"[BBDown] Đang tải link {progress_index}/{progress_total}")
    log(f"[DownloadProgress] START i={progress_index} total={progress_total}")

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
    if not new_files:
        removed = _cleanup_partials(output_dir)
        if risk_control:
            log(f"[BBDown] {re.sub(r'https?://\\S+', '[CDN URL]', risk_control[0])}")
            raise RuntimeError(
                f"Link {progress_index}: Bilibili chặn (风控/chưa đăng nhập). "
                "Mở lại hộp thoại Tải, bấm 'Đăng nhập QR' quét mã rồi tải lại. "
                f"Đã dọn {removed} file tạm."
            )
        raise RuntimeError(
            f"BBDown không tạo được MP4 cho link {progress_index} (thoát mã {process.returncode}); "
            f"đã dọn {removed} file tạm. Thử tải riêng link này bằng nút Tải để xem lỗi BBDown."
        )
    swept = _cleanup_partials(output_dir)
    if swept:
        log(f"[BBDown] Đã dọn {swept} mảnh tạm sau khi ghép")
    log(f"[DownloadProgress] PERCENT i={progress_index} total={progress_total} percent=100")
    log(f"[DownloadProgress] DONE i={progress_index} total={progress_total}")
    log(f"[BBDown] Tải xong {len(new_files)} file"
        + ("" if process.returncode == 0 else f" (BBDown thoát mã {process.returncode})"))
    return new_files

def download_multiple(urls: list[str], dfn_priority: str = DEFAULT_DFN_PRIORITY,
                      output_dir: str = None, log_callback=None) -> list[str]:
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
