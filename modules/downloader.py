"""BBDown-only Bilibili downloader for V3."""
import os
import re
import subprocess
from pathlib import Path
from config import BBDOWN_PATH, DOWNLOAD_DIR, DEFAULT_DFN_PRIORITY

BBDOWN_DIR = os.path.dirname(BBDOWN_PATH)

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

def download_video(url: str, dfn_priority: str = DEFAULT_DFN_PRIORITY,
                   output_dir: str = None, log_callback=None,
                   progress_index: int = 1, progress_total: int = 1) -> list[str]:
    output_dir = output_dir or DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)
    def log(msg):
        (log_callback or print)(msg)
    before = _snapshot(output_dir)
    cmd = [BBDOWN_PATH, url, "--work-dir", output_dir,
           "--dfn-priority", dfn_priority, "--force-http", "--multi-thread"]
    log("[BBDown] Dùng BBDown 1.6.3 (multi-thread, force-http)")
    log(f"[BBDown] Đang tải link {progress_index}/{progress_total}")
    process = subprocess.Popen(cmd, cwd=BBDOWN_DIR, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", bufsize=1)
    for raw in process.stdout or []:
        line = raw.strip()
        if not line:
            continue
        safe = re.sub(r"https?://\S+", "[CDN URL]", line)
        if len(safe) > 300: safe = safe[:300] + "..."
        m = re.search(r"(\d{1,3})%", line)
        if m:
            log(f"[DownloadProgress] PERCENT i={progress_index} total={progress_total} percent={m.group(1)}")
        elif any(x in line.lower() for x in ("error", "failed", "warning", "download")):
            log(f"[BBDown] {safe}")
    process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"BBDown thoát với mã lỗi {process.returncode}")
    after = _snapshot(output_dir)
    files = [p for p, size in after.items() if before.get(p) != size]
    if not files:
        files = sorted(after, key=lambda p: os.path.getmtime(p), reverse=True)[:1]
    if not files:
        raise FileNotFoundError("BBDown không tạo file MP4")
    log(f"[BBDown] Tải xong {len(files)} file")
    return sorted(files)

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
