"""
Module gọi BBDown để đăng nhập và tải video từ Bilibili.
Tham số --dfn-priority lấy đúng theo tai-video.bat của bạn.
"""
import subprocess
import os
import sys
import glob
import time
import re
import codecs
import queue
import threading
from config import ARIA2_PATH, BBDOWN_PATH, DOWNLOAD_DIR, DEFAULT_DFN_PRIORITY, FFMPEG_PATH

# BBDown lưu thông tin đăng nhập (BBDown.data) cùng thư mục nơi nó được chạy.
# Luôn ép BBDown chạy đúng trong thư mục chứa file .exe của nó, để không bị "mất đăng nhập"
# khi tool được gọi từ 1 thư mục làm việc (cwd) khác.
BBDOWN_DIR = os.path.dirname(BBDOWN_PATH)


class AuthenticationRequired(RuntimeError):
    """Raised when BBDown reports that its Bilibili session is invalid."""


def has_login_session() -> bool:
    return os.path.isfile(os.path.join(BBDOWN_DIR, "BBDown.data"))


def _is_auth_error(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "-101", "-400", "401", "unauthorized", "not login", "未登录",
        "未登入", "登录失效", "登入失效", "请先登录", "cookie无效",
    )
    return any(marker in lowered for marker in markers)


def bbdown_login(log_callback=None):
    """
    Chạy `BBDown login` để đăng nhập tài khoản Bilibili (VIP).
    Lệnh này cần hiển thị mã QR để quét bằng app Bilibili, nên sẽ mở
    trong 1 cửa sổ console riêng (Windows) thay vì chạy ẩn.
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    _log("[BBDown] Đang mở cửa sổ đăng nhập (quét mã QR bằng app Bilibili)...")

    if sys.platform.startswith("win"):
        # Mở cửa sổ cmd riêng để hiển thị mã QR và giữ cửa sổ sau khi xong
        # cwd=BBDOWN_DIR đảm bảo BBDown lưu/đọc đúng file BBDown.data (thông tin đăng nhập)
        subprocess.Popen(
            ["cmd", "/k", BBDOWN_PATH, "login"],
            cwd=BBDOWN_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _log("[BBDown] Đã mở cửa sổ đăng nhập riêng, hãy quét mã QR ở đó.")
    else:
        subprocess.Popen([BBDOWN_PATH, "login"], cwd=BBDOWN_DIR)
        _log("[BBDown] Đã chạy lệnh đăng nhập, kiểm tra terminal để quét mã QR.")

    # Theo dõi file BBDown.data - hễ nó được ghi lại (dấu hiệu vừa đăng nhập xong)
    # là báo ngay vào log của Bili2YT, không cần nhìn cửa sổ console riêng.
    data_file = os.path.join(BBDOWN_DIR, "BBDown.data")
    before_mtime = os.path.getmtime(data_file) if os.path.exists(data_file) else None

    _log("[BBDown] Đang chờ xác nhận đăng nhập (tối đa 90 giây)...")
    for _ in range(90):
        time.sleep(1)
        if os.path.exists(data_file):
            new_mtime = os.path.getmtime(data_file)
            if before_mtime is None or new_mtime != before_mtime:
                _log("[BBDown] ✅ Đăng nhập thành công! (BBDown.data đã được cập nhật)")
                return
    _log("[BBDown] ⚠️ Chưa xác nhận được trong 90 giây - kiểm tra lại cửa sổ QR.")


def _download_video_bbdown(url: str, dfn_priority: str = DEFAULT_DFN_PRIORITY,
                    output_dir: str = None, log_callback=None,
                    progress_index: int = 1, progress_total: int = 1) -> list[str]:
    """
    Tải 1 video bằng BBDown, trả về toàn bộ các file mp4 vừa tải.

    BBDown lưu video nhiều phần theo cấu trúc:
    ``<tên phim>/[P01]...mp4``, ``<tên phim>/[P02]...mp4``.
    Vì vậy phải quét đệ quy và không được chỉ lấy file đầu tiên ở thư mục gốc.

    :param url: Link video Bilibili (hoặc mã BV)
    :param dfn_priority: độ phân giải ưu tiên, mặc định "720P 高清, 720P"
    :param output_dir: thư mục lưu file, mặc định DOWNLOAD_DIR
    :param log_callback: hàm nhận log dạng str để in ra GUI (tuỳ chọn)
    :return: danh sách đường dẫn tuyệt đối tới các file mp4 đã tải
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    output_dir = output_dir or DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)

    def _snapshot_mp4s() -> dict[str, tuple[int, int]]:
        snapshot = {}
        for path in glob.glob(os.path.join(output_dir, "**", "*.mp4"), recursive=True):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            snapshot[os.path.abspath(path)] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    before = _snapshot_mp4s()

    cmd = [
        BBDOWN_PATH,
        url,
        "--work-dir", output_dir,
        "--dfn-priority", dfn_priority,
        # A Bilibili page may contain multiple parts. Keep all parts in the
        # movie folder so the concat stage can process them together.
        "--select-page", "ALL",
        "--multi-file-pattern", "<videoTitle>/[P<pageNumberWithZero>]<pageTitle>",
    ]

    _log(f"[BBDown] Đang tải: {url}")
    _log(f"[BBDown] Lệnh chạy: {' '.join(cmd)}")

    output_text = ""
    pending = ""
    stall_timeout = max(30, int(os.getenv("BBDOWN_STALL_TIMEOUT", "180")))
    max_attempts = max(1, int(os.getenv("BBDOWN_MAX_ATTEMPTS", "3")))

    def _activity_signature():
        total_size = 0
        newest_ns = 0
        for path in glob.glob(os.path.join(output_dir, "**", "*"), recursive=True):
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if path.lower().endswith((".vclip", ".mp4", ".m4s")):
                total_size += stat.st_size
                newest_ns = max(newest_ns, stat.st_mtime_ns)
        return total_size, newest_ns

    process = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            _log(f"[DownloadRetry] Thử lại {attempt}/{max_attempts}; giữ file tạm để tải tiếp")
        process = subprocess.Popen(
            cmd,
            cwd=BBDOWN_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            bufsize=0,
        )
        chunks = queue.Queue()

        def _pump_stdout():
            while True:
                chunk = process.stdout.read(1) if process.stdout else b""
                chunks.put(chunk)
                if not chunk:
                    return

        reader = threading.Thread(target=_pump_stdout, daemon=True)
        reader.start()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        last_activity = time.monotonic()
        last_signature = _activity_signature()
        last_watchdog_log = last_activity
        stalled = False
        stream_closed = False
        while not stream_closed:
            try:
                chunk = chunks.get(timeout=1.0)
            except queue.Empty:
                chunk = None
            if chunk == b"":
                stream_closed = True
            elif chunk:
                decoded = decoder.decode(chunk)
                output_text += decoded
                pending += decoded
                percent_matches = list(re.finditer(r"\[(\d{1,3}(?:\.\d+)?)\]", pending))
                if percent_matches:
                    for percent_match in percent_matches:
                        percent = min(100, float(percent_match.group(1)))
                        _log(
                            f"[DownloadProgress] PERCENT i={progress_index} "
                            f"total={progress_total} percent={percent}"
                        )
                    pending = pending[percent_matches[-1].end():]
                elif len(pending) > 256:
                    pending = pending[-128:]

            signature = _activity_signature()
            if signature != last_signature:
                last_signature = signature
                last_activity = time.monotonic()
            idle = time.monotonic() - last_activity
            if time.monotonic() - last_watchdog_log >= 15:
                _log(
                    f"[DownloadWatchdog] vẫn theo dõi · không có dữ liệu mới {int(idle)}s · "
                    f"đã nhận {signature[0] / 1048576:.1f} MB"
                )
                last_watchdog_log = time.monotonic()
            if idle >= stall_timeout:
                stalled = True
                _log(f"[DownloadWatchdog] BBDown bị treo {int(idle)}s, đang khởi động lại")
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                break

        reader.join(timeout=2)
        pending += decoder.decode(b"", final=True)
        if not stalled:
            process.wait()
            break
        if attempt == max_attempts:
            raise RuntimeError(
                f"BBDown không tải thêm dữ liệu trong {stall_timeout}s sau {max_attempts} lần thử"
            )

    assert process is not None
    for percent_match in re.finditer(r"\[(\d{1,3}(?:\.\d+)?)\]", pending):
            percent = min(100, float(percent_match.group(1)))
            _log(
                f"[DownloadProgress] PERCENT i={progress_index} "
                f"total={progress_total} percent={percent}"
            )
    process.wait()

    if process.returncode != 0:
        if _is_auth_error(output_text):
            raise AuthenticationRequired("Phiên đăng nhập Bilibili đã hết hạn")
        raise RuntimeError(f"BBDown thoát với mã lỗi {process.returncode}")

    after = _snapshot_mp4s()
    new_files = [
        path for path, state in after.items()
        if path not in before or before[path] != state
    ]

    if not new_files:
        all_files = sorted(after, key=lambda path: os.path.getmtime(path), reverse=True)
        if not all_files:
            raise FileNotFoundError("Không tìm thấy file mp4 nào sau khi tải")
        # BBDown may report an already-existing download as skipped. Returning
        # the newest existing file preserves the old retry behavior.
        new_files = [all_files[0]]

    new_files.sort(key=_natural_path_sort_key)
    if len(new_files) > 1:
        _log(f"[BBDown] Tải xong {len(new_files)} phần trong thư mục phim:")
        for path in new_files:
            _log(f"  - {path}")
    else:
        _log(f"[BBDown] Tải xong: {new_files[0]}")
    return new_files


def _download_video_ytdlp(
    url: str, output_dir: str, log_callback=None,
    progress_index: int = 1, progress_total: int = 1,
) -> list[str]:
    def _log(message):
        if log_callback:
            log_callback(message)
        else:
            print(message)

    def _snapshot():
        return {
            os.path.abspath(path): (os.stat(path).st_mtime_ns, os.stat(path).st_size)
            for path in glob.glob(os.path.join(output_dir, "**", "*.mp4"), recursive=True)
            if os.path.isfile(path)
        }

    before = _snapshot()
    browser = os.getenv("YTDLP_BROWSER", "chrome")
    use_browser_login = os.getenv("YTDLP_USE_BROWSER_COOKIES", "0") == "1"
    output_template = os.path.join(
        output_dir, "%(title)s", "[P%(playlist_index)02d]%(title)s.%(ext)s"
    )
    base = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--newline", "--no-color",
        "--force-ipv4", "--windows-filenames",
        "--ffmpeg-location", FFMPEG_PATH,
        "--format", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4", "--continue", "--no-overwrites",
        "--socket-timeout", "20", "--retries", "20", "--fragment-retries", "20",
        "--retry-sleep", "fragment:exp=1:10",
        "--progress-template",
        "download:[YTDLP] percent=%(progress._percent_str)s speed=%(progress._speed_str)s eta=%(progress._eta_str)s",
        "--output", output_template,
    ]
    attempts = [(8, "aria2c"), (4, "aria2c"), (3, "native")]
    last_error = ""
    for connections, downloader_kind in attempts:
        command = list(base)
        if use_browser_login:
            command += ["--cookies-from-browser", browser]
        if downloader_kind == "aria2c" and os.path.isfile(ARIA2_PATH):
            command += [
                "--downloader", "aria2c",
                "--downloader-args",
                f"aria2c:-x {connections} -s {connections} -k 1M --file-allocation=none "
                "--summary-interval=1 --disable-ipv6=true --async-dns=false",
            ]
        else:
            command += ["--downloader", "native", "--concurrent-fragments", str(connections)]
        command.append(url)
        login_mode = f"cookie {browser}" if use_browser_login else "không cookie"
        _log(
            f"[YTDLP] Bắt đầu tải với {downloader_kind}, {connections} kết nối, "
            f"IPv4 ({login_mode})"
        )
        process = subprocess.Popen(
            command, cwd=os.path.dirname(ARIA2_PATH), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
        output_lines = []
        for raw_line in process.stdout or []:
            line = raw_line.strip()
            if not line:
                continue
            output_lines.append(line)
            progress = re.search(r"\[YTDLP\]\s+percent=\s*([0-9.]+)%?\s+speed=(.*?)\s+eta=(.*)$", line)
            if progress:
                percent = min(100.0, float(progress.group(1)))
                _log(
                    f"[DownloadProgress] PERCENT i={progress_index} total={progress_total} "
                    f"percent={percent} speed={progress.group(2).strip()} eta={progress.group(3).strip()}"
                )
            elif any(marker in line.lower() for marker in ("error", "warning", "retry", "download")):
                _log(f"[YTDLP] {line}")
        process.wait()
        if process.returncode == 0:
            after = _snapshot()
            downloaded = [path for path, state in after.items() if before.get(path) != state]
            if not downloaded:
                downloaded = sorted(after, key=lambda path: os.path.getmtime(path), reverse=True)[:1]
            if downloaded:
                downloaded.sort(key=_natural_path_sort_key)
                _log(f"[YTDLP] Tải xong {len(downloaded)} file bằng yt-dlp + {downloader_kind}")
                return downloaded
            last_error = "yt-dlp kết thúc nhưng không tạo MP4"
        else:
            last_error = "\n".join(output_lines[-12:])
        _log(
            f"[YTDLP] {downloader_kind} {connections} kết nối thất bại; "
            "chuyển cấu hình tiếp theo"
        )
    raise RuntimeError(last_error or "yt-dlp tải thất bại")


def download_video(url: str, dfn_priority: str = DEFAULT_DFN_PRIORITY,
                   output_dir: str = None, log_callback=None,
                   progress_index: int = 1, progress_total: int = 1) -> list[str]:
    """Use fast maintained yt-dlp/aria2c first, then BBDown as fallback."""
    output_dir = output_dir or DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)
    try:
        return _download_video_ytdlp(
            url, output_dir, log_callback, progress_index, progress_total
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[YTDLP] Lỗi: {exc}")
            log_callback("[DownloadFallback] Chuyển sang BBDown")
        return _download_video_bbdown(
            url, dfn_priority, output_dir, log_callback,
            progress_index, progress_total,
        )


def _natural_path_sort_key(path: str):
    """Sort downloaded parts by their full relative path and page number."""
    return [
        int(chunk) if chunk.isdigit() else chunk.casefold()
        for chunk in re.split(r"(\d+)", path)
    ]


def download_multiple(urls: list[str], dfn_priority: str = DEFAULT_DFN_PRIORITY,
                       output_dir: str = None, log_callback=None) -> list[str]:
    """
    Tải nhiều video liên tiếp (giống vòng lặp trong tai-video.bat).
    Nếu 1 link lỗi, log lại lỗi và tiếp tục link tiếp theo thay vì dừng hẳn.
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    results = []
    for i, url in enumerate(urls, 1):
        url = url.strip()
        if not url:
            continue
        _log(f"\n--- Video {i}/{len(urls)} ---")
        _log(f"[DownloadProgress] START i={i} total={len(urls)}")
        try:
            paths = download_video(url, dfn_priority=dfn_priority,
                                   output_dir=output_dir, log_callback=log_callback,
                                   progress_index=i, progress_total=len(urls))
            results.extend(paths)
            _log(f"[DownloadProgress] DONE i={i} total={len(urls)}")
        except AuthenticationRequired:
            _log("[BBDown] Phiên đăng nhập đã hết hạn.")
            raise
        except Exception as e:
            _log(f"[DownloadProgress] FAIL i={i} total={len(urls)}")
            _log(f"[BBDown] ❌ Lỗi khi tải '{url}': {e}")
    return results
