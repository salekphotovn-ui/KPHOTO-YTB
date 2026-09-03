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
_PHASE_HINTS = ("下载", "合并", "完成", "多线程",
                "error", "failed", "warning", "exception")


class AuthenticationRequired(RuntimeError):
    pass

def has_login_session() -> bool:
    return os.path.isfile(os.path.join(BBDOWN_DIR, "BBDown.data"))

def bbdown_login(log_callback=None):
    def log(msg):
        (log_callback or print)(msg)
    log("[BBDown] Đang mở cửa sổ đăng nhập QR...")
    if os.name == "nt":
        subprocess.Popen(["cmd", "/k", BBDOWN_PATH, "login"], cwd=BBDOWN_DIR,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen([BBDOWN_PATH, "login"], cwd=BBDOWN_DIR)
    log("[BBDown] Hãy quét mã QR trong cửa sổ BBDown.")

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
    cmd = [BBDOWN_PATH, url, "--work-dir", output_dir,
           "--dfn-priority", dfn_priority, "--force-http", "--multi-thread",
           "--ffmpeg-path", FFMPEG_PATH]
    log("[BBDown] Dùng BBDown 1.6.3 (multi-thread, force-http)")
    log(f"[BBDown] Đang tải link {progress_index}/{progress_total}")
    log(f"[DownloadProgress] START i={progress_index} total={progress_total}")

    def _run_once() -> int:
        process = subprocess.Popen(cmd, cwd=BBDOWN_DIR, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=False, bufsize=0)
        recent_sizes: list[int] = []

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
        return process.returncode

    last_reason = ""
    for attempt in range(1, 4):
        if attempt > 1:
            log(f"[BBDown] Chưa xong ({last_reason}) — thử lại lần {attempt}/3, tiếp tục từ phần đã tải...")
        code = _run_once()
        after = _snapshot(output_dir)
        new_files = sorted(p for p, size in after.items() if before.get(p) != size)
        partials = _partial_files(output_dir)
        if code == 0 and new_files and not partials:
            log(f"[DownloadProgress] PERCENT i={progress_index} total={progress_total} percent=100")
            log(f"[DownloadProgress] DONE i={progress_index} total={progress_total}")
            log(f"[BBDown] Tải xong {len(new_files)} file")
            return new_files
        last_reason = (f"mã lỗi {code}" if code != 0
                       else f"còn {len(partials)} mảnh .vclip chưa ghép" if partials
                       else "không tạo được MP4")

    removed = _cleanup_partials(output_dir)
    raise RuntimeError(
        f"BBDown tải link {progress_index} không xong sau 3 lần ({last_reason}); "
        f"đã dọn {removed} file tạm. Kiểm tra mạng / link rồi tải lại."
    )

def download_multiple(urls: list[str], dfn_priority: str = DEFAULT_DFN_PRIORITY,
                      output_dir: str = None, log_callback=None) -> list[str]:
    results = []
    for i, url in enumerate(urls, 1):
        try:
            results.extend(download_video(url.strip(), dfn_priority, output_dir,
                                          log_callback, i, len(urls)))
        except Exception as exc:
            if log_callback: log_callback(f"[BBDown] Lỗi link {i}: {exc}")
    return results
