from __future__ import annotations

import sys
import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QMainWindow,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget, QDialog,
    QDialogButtonBox, QComboBox, QProgressBar, QCheckBox, QRadioButton,
)

from modules.separator import separate_folder
from modules.srt import create_srt_batch
from modules.translator import translate_srt_batch
from modules.muxer import mux_folder
from modules.exporter import export_folder
from modules.downloader import bbdown_login, download_multiple, has_login_session
from modules.rename import auto_rename_folder
from modules.concat import concat_videos


def run_auto_pipeline(folder: str, steps: dict[str, bool], log_callback=None):
    def log(message):
        if log_callback:
            log_callback(message)

    if steps.get("separate"):
        log("[AutoStage] Đang tách vocal")
        separate_folder(folder, log_callback=log_callback)
    if steps.get("srt"):
        log("[AutoStage] Đang tạo SRT Whisper V3")
        create_srt_batch(folder, engine="whisper-v3", log_callback=log_callback)
    if steps.get("translate"):
        log("[AutoStage] Đang dịch Google Translate Web")
        translate_srt_batch(folder, "en", "google-web", "", log_callback=log_callback)
    if steps.get("mux"):
        log("[AutoStage] Đang ghép vocal")
        mux_folder(folder, log_callback=log_callback)
    if steps.get("export"):
        log("[AutoStage] Đang xuất video")
        return export_folder(folder, log_callback=log_callback)
    log("[AutoStage] Đã hoàn tất các bước được chọn")
    return folder


def run_download_and_auto_pipeline(folder: str, urls: list[str], dfn_priority: str, steps: dict[str, bool], log_callback=None):
    if log_callback:
        log_callback(f"[AutoStage] Đang tải {len(urls)} link")
    download_multiple(urls, dfn_priority=dfn_priority, output_dir=folder, log_callback=log_callback)
    return run_auto_pipeline(folder, steps, log_callback=log_callback)


class AutoProcessDialog(QDialog):
    def __init__(self, has_pending_download=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn quy trình tự động")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn các bước muốn chạy (mặc định đã chọn hết):"))
        self.checks = {}
        items = [("download", "Tải video (link đã nhập)"), ("separate", "Tách vocal"), ("srt", "Tạo SRT bằng Whisper V3"), ("translate", "Dịch phụ đề bằng Google Web"), ("mux", "Ghép vocal"), ("export", "Xuất video")]
        for key, label in items:
            check = QCheckBox(label)
            check.setChecked(key != "download" or has_pending_download)
            self.checks[key] = check
            layout.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def values(self):
        return {key: check.isChecked() for key, check in self.checks.items()}


class SrtModelDialog(QDialog):
    def __init__(self, kphoto_available: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn model tạo SRT")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn model nhận dạng tiếng Trung:"))
        self.whisper = QRadioButton("Whisper V3 (large-v3) - chất lượng cao")
        self.whisper.setChecked(True)
        layout.addWidget(self.whisper)
        self.kphoto = QRadioButton("KPHOTO-Local - nhanh, dùng GPU")
        self.kphoto.setEnabled(kphoto_available)
        if not kphoto_available:
            self.kphoto.setToolTip("Chưa có model KPHOTO-Local trong Bili2YT_V3/models")
        layout.addWidget(self.kphoto)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def value(self):
        return "kphoto-local" if self.kphoto.isChecked() else "whisper-v3"


class TranslateDialog(QDialog):
    def __init__(self, languages: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dịch phụ đề")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn ngôn ngữ nguồn:"))
        self.source = QComboBox(); self.source.addItems(languages); layout.addWidget(self.source)
        layout.addWidget(QLabel("Chọn ngôn ngữ đầu ra:"))
        self.target = QComboBox()
        for code, label in (("en", "English"), ("vi", "Vietnamese"), ("ja", "Japanese"), ("ko", "Korean"), ("th", "Thai")):
            self.target.addItem(label, code)
        layout.addWidget(self.target)
        layout.addWidget(QLabel("Model dịch:"))
        self.google = QRadioButton("Google Translate Web (miễn phí)"); self.google.setChecked(True); layout.addWidget(self.google)
        self.gemini = QRadioButton("Gemini (cần API key)"); self.gemini.setEnabled(False); layout.addWidget(self.gemini)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def values(self):
        return self.source.currentText(), self.target.currentData(), "google-web"


class TaskWorker(QObject):
    log = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, task, *args, **kwargs):
        super().__init__()
        self.task, self.args, self.kwargs = task, args, kwargs

    def run(self):
        try:
            result = self.task(*self.args, log_callback=self.log.emit, **self.kwargs)
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class DownloadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tải video Bilibili")
        self.resize(620, 430)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Dán mỗi link Bilibili trên một dòng:"))
        self.urls = QTextEdit()
        self.urls.setPlaceholderText("https://www.bilibili.com/video/BV...\nhttps://www.bilibili.com/video/BV...")
        layout.addWidget(self.urls, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Chất lượng:"))
        self.dfn = QComboBox()
        self.dfn.addItem("720P (mặc định)", "720P 高清, 720P")
        self.dfn.addItem("1080P ưu tiên", "1080P 高清, 1080P, 720P 高清, 720P")
        row.addWidget(self.dfn, 1)
        self.login_status = QLabel("Đã đăng nhập" if has_login_session() else "Chưa đăng nhập")
        row.addWidget(self.login_status)
        login = QPushButton("Đăng nhập QR")
        login.clicked.connect(self.login)
        row.addWidget(login)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def login(self):
        bbdown_login()
        self.login_status.setText("Đã đăng nhập" if has_login_session() else "Chưa đăng nhập")

    def values(self):
        return [line.strip() for line in self.urls.toPlainText().splitlines() if line.strip()], self.dfn.currentData()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.root = Path.cwd()
        self.thread = None
        self.worker = None
        self.pending_download_links = []
        self.pending_download_dfn = "720P 高清, 720P"
        self._last_download_percent = None
        self.setWindowTitle("Bili2YT - Video Workspace / V3")
        self.resize(1200, 780)
        self._build_ui_v3()

    def _build_ui_v3(self):
        self.setMinimumSize(1100, 720)
        self.setStyleSheet("""
            QWidget { color: #f4f4f4; font-family: 'Segoe UI'; font-size: 14px; }
            QMainWindow, QWidget { background: #050505; }
            QFrame#bar, QFrame#panel { background: #050505; border: 1px solid #292929; border-radius: 8px; }
            QLabel#brand { font-size: 36px; font-weight: 800; color: #ffffff; }
            QLabel#eyebrow, QLabel#title { font-weight: 900; letter-spacing: 1px; }
            QLabel#title { font-size: 18px; }
            QLabel#muted { color: #dddddd; font-size: 14px; }
            QPushButton { background: #151515; border: 1px solid #484848; border-radius: 6px; padding: 10px 13px; color: #ffffff; font-size: 14px; font-weight: 800; }
            QPushButton:hover { background: #292929; border-color: #ffffff; }
            QPushButton#primary { background: #ffffff; color: #000000; border: none; font-weight: 800; }
            QListWidget, QTextEdit { background: #050505; border: none; }
            QListWidget::item { color: #ffd84d; padding: 5px 7px; border-bottom: 1px solid #292929; }
            QTextEdit { color: #ffd84d; font-family: Consolas; font-size: 14px; font-weight: 700; }
            QRadioButton { spacing: 9px; font-size: 14px; font-weight: 700; padding: 4px; }
            QRadioButton::indicator { width: 18px; height: 18px; border-radius: 9px; border: 2px solid #aaaaaa; background: #050505; }
            QRadioButton::indicator:hover { border-color: #ffffff; }
            QRadioButton::indicator:checked { background: #ffffff; border: 2px solid #ffffff; }
            QRadioButton:disabled { color: #777777; }
            QRadioButton::indicator:disabled { border-color: #555555; background: #151515; }
        """)
        brand = QLabel("Bili2YT"); brand.setObjectName("brand")
        eyebrow = QLabel("VIDEO WORKSPACE / V3"); eyebrow.setObjectName("eyebrow")
        header = QHBoxLayout(); header.addWidget(brand); header.addWidget(eyebrow); header.addStretch(1); header.addWidget(QPushButton("Cài đặt / Đăng nhập"))
        self.folder_label = QLabel("Chưa chọn thư mục tổng"); self.folder_label.setObjectName("muted")
        choose = QPushButton("Chọn thư mục"); choose.clicked.connect(self.choose_folder)
        auto = QPushButton("Chạy tự động  ·  0 phim"); auto.setObjectName("primary"); auto.clicked.connect(self.auto_run)
        folder_bar = QFrame(); folder_bar.setObjectName("bar")
        top = QHBoxLayout(folder_bar); top.addWidget(QLabel("THƯ MỤC TỔNG")); top.addWidget(self.folder_label, 1); top.addWidget(choose); top.addWidget(auto)
        self.movies = QListWidget()
        self.movie_selection = QListWidget()
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.status = QLabel("Sẵn sàng"); self.status.setObjectName("muted")
        film_layout = QVBoxLayout(); film_layout.addWidget(QLabel("PHIM", objectName="title")); film_layout.addWidget(QLabel("☐  Chọn tất cả", objectName="muted")); film_layout.addWidget(QLabel("Tích chọn để chạy hàng loạt  ·  bấm tên để xem", objectName="muted")); film_layout.addStretch(1)
        film_panel = QFrame(); film_panel.setObjectName("panel"); film_panel.setLayout(film_layout)
        list_layout = QVBoxLayout(); list_layout.addWidget(QLabel("DANH SÁCH PHIM", objectName="title")); list_layout.addWidget(self.movies, 1)
        list_panel = QFrame(); list_panel.setObjectName("panel"); list_panel.setLayout(list_layout)
        left_column = QVBoxLayout(); left_column.addWidget(film_panel, 1); left_column.addWidget(list_panel, 1)
        left_widget = QWidget(); left_widget.setLayout(left_column)
        preview = QFrame(); preview.setObjectName("panel"); preview_layout = QVBoxLayout(preview); preview_layout.addWidget(QLabel("XEM TRƯỚC", objectName="title")); blank = QLabel(""); blank.setMinimumHeight(380); blank.setStyleSheet("background:#050505;border:1px solid #050505"); preview_layout.addWidget(blank, 1)
        controls = QHBoxLayout()
        for label in ("▶", "+ Blur", "+ Logo", "+ Khung"):
            controls.addWidget(QPushButton(label))
        controls.addWidget(QLabel("Mức mờ")); controls.addWidget(QLabel("━━━━━━")); controls.addWidget(QLabel("00:00 / 00:00"))
        preview_layout.addLayout(controls)
        log_panel = QFrame(); log_panel.setObjectName("panel"); log_layout = QVBoxLayout(log_panel); log_layout.addWidget(QLabel("NHẬT KÝ CHUNG", objectName="title")); log_layout.addWidget(QLabel("Chưa có lượt tải", objectName="muted")); self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0); self.progress.setFormat("%p%"); log_layout.addWidget(self.progress); log_layout.addWidget(self.log_view, 1)
        middle = QHBoxLayout(); middle.setSpacing(8); middle.addWidget(left_widget, 1); middle.addWidget(preview, 2); middle.addWidget(log_panel, 1)
        actions = QHBoxLayout(); actions.setSpacing(7)
        for label, callback in (("↓ Tải", self.download), ("+ Ghép", self.concat), ("✎ Đặt tên", self.rename), ("✦ Tách vocal", self.separate), ("▣ Tạo SRT", self.create_srt), ("文 Dịch sub", self.translate), ("♫ Ghép vocal", self.mux), ("◆ Xuất video", self.export)):
            button = QPushButton(label); button.setObjectName("primary" if "Xuất" in label else ""); button.clicked.connect(callback); actions.addWidget(button, 1)
        root = QVBoxLayout(); root.setContentsMargins(17, 12, 17, 12); root.addLayout(header); root.addWidget(folder_bar); root.addLayout(middle, 1); root.addLayout(actions); root.addWidget(self.status)
        widget = QWidget(); widget.setLayout(root); self.setCentralWidget(widget)

    def _build_ui(self):
        self.folder_label = QLabel("Chưa chọn thư mục")
        choose = QPushButton("Chọn thư mục")
        choose.clicked.connect(self.choose_folder)
        top = QHBoxLayout()
        top.addWidget(QLabel("THƯ MỤC TỔNG"))
        top.addWidget(self.folder_label, 1)
        top.addWidget(choose)

        self.movies = QListWidget()
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("color:#ffd84d;background:#050505;font-family:Consolas")
        self.status = QLabel("Sẵn sàng")

        self.engine = QComboBox()
        self.engine.addItem("Whisper V3", "whisper-v3")
        self.engine.addItem("KPHOTO-Local", "kphoto-local")
        self.target = QComboBox()
        self.target.addItem("English", "en")

        actions = QHBoxLayout()
        for label, callback in (
            ("Chạy tự động", self.auto_run),
            ("Tách vocal", self.separate),
            ("Tạo SRT", self.create_srt),
            ("Dịch Google Web", self.translate),
            ("Ghép vocal", self.mux),
            ("Xuất video", self.export),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addWidget(self.engine)
        actions.addWidget(self.target)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("PHIM"))
        left.addWidget(self.movies)
        right = QVBoxLayout()
        right.addWidget(QLabel("NHẬT KÝ"))
        right.addWidget(self.log_view)
        body.addLayout(left, 1)
        body.addLayout(right, 2)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addLayout(body, 1)
        root.addLayout(actions)
        root.addWidget(self.status)
        widget = QWidget()
        widget.setLayout(root)
        self.setCentralWidget(widget)

    def choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, "Chọn thư mục")
        if not selected:
            return
        self.root = Path(selected)
        self.folder_label.setText(str(self.root))
        self.movies.clear()
        self.movies.addItems(sorted(p.name for p in self.root.rglob("*.mp4")))
        self.write_log(f"[UI] Đã chọn: {self.root}")

    def download(self):
        dialog = DownloadDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        links, dfn_priority = dialog.values()
        if not links:
            return
        self.pending_download_links = links
        self.pending_download_dfn = dfn_priority
        self.write_log(f"[Download] Đã nhận dạng {len(links)} link")
        self.write_log("[Download] Đã xếp hàng, bấm Chạy tự động để bắt đầu tải")

    def write_log(self, message):
        text = str(message)
        match = re.search(r"(?:\[DownloadProgress\]\s+PERCENT.*?\s+)?percent=([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
        if match:
            percent = max(0, min(100, round(float(match.group(1)))))
            self.progress.setValue(percent)
            # Keep the progress bar live without filling the log with repeats.
            if percent not in (0, 100) and percent % 5 != 0:
                return
            if percent == self._last_download_percent:
                return
            self._last_download_percent = percent
            return
        separator_match = re.search(r"\[Separator\].*?\s(\d{1,3})%\s*\|", text, re.IGNORECASE)
        if separator_match:
            percent = max(0, min(100, int(separator_match.group(1))))
            self.progress.setValue(percent)
            if percent != 0 and percent % 5 != 0:
                return
            if percent == self._last_download_percent:
                return
            self._last_download_percent = percent
            self.log_view.append(f"[Tách vocal] {percent}%")
            return
        chunk_match = re.search(r"\[SrtProgress\]\s+CHUNK\s+(\d+)\/(\d+)", text, re.IGNORECASE)
        if chunk_match:
            current, total = int(chunk_match.group(1)), max(1, int(chunk_match.group(2)))
            percent = round(current * 100 / total)
            self.progress.setValue(percent)
            self.log_view.append(f"[Whisper V3] {current}/{total} chunk ({percent}%)")
            return
        whisper_match = re.search(r"\[SrtProgress\]\s+WHISPER_PERCENT\s+(\d+)", text, re.IGNORECASE)
        if whisper_match:
            percent = max(0, min(100, int(whisper_match.group(1))))
            self.progress.setValue(percent)
            if percent in (0, 100) or percent % 5 == 0:
                self.log_view.append(f"[Whisper V3] {percent}%")
            return
        elif "[DownloadProgress] DONE" in text:
            self.progress.setValue(100)
            self._last_download_percent = 100
        elif text.startswith("[BBDown]") or "[DownloadProgress] START" in text:
            return
        elif text.startswith("[Separator]") and ("ERROR" not in text.upper() and "FAIL" not in text.upper()):
            return
        summary = None
        patterns = (
            (r"\[SeparateProgress\]\s+ITEM\s+\d+/\d+\s+(.+)$", "Đang xử lý: {}"),
            (r"\[SeparateProgress\]\s+DONE\s+\d+/\d+\s+(.+)$", "Đã xử lý xong: {}"),
            (r"\[SrtProgress\]\s+ITEM\s+\d+/\d+\s+(.+)$", "Đang xử lý: {}"),
            (r"\[SrtProgress\]\s+DONE\s+\d+/\d+\s+(.+)$", "Đã xử lý xong: {}"),
            (r"\[Translate\]\s+FILM\s+(.+?)\s+total=", "Đang xử lý: {}"),
            (r"\[Translate\]\s+FILM_DONE\s+(.+?)\s+output=", "Đã xử lý xong: {}"),
            (r"\[MuxProgress\]\s+VIDEO_START\s+(.+)$", "Đang xử lý: {}"),
            (r"\[ExportProgress\]\s+ITEM\s+\d+/\d+\s+(.+)$", "Đang xử lý: {}"),
            (r"\[ExportProgress\]\s+DONE\s+\d+/\d+", "Đã xử lý xong video"),
        )
        for pattern, template in patterns:
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                summary = template.format(found.group(1).strip() if found.groups() else "")
                break
        if "FAIL" in text.upper() or "LỖI" in text.upper() or "ERROR" in text.upper():
            summary = "LỖI: " + text.split("FAIL", 1)[-1].strip(" :")
        if text.startswith("[AutoStage]") or text.startswith("[Download]"):
            summary = text
        if summary:
            self.log_view.append(summary)
        self.log_view.ensureCursorVisible()

    def start_task(self, task, *args, **kwargs):
        if self.thread:
            try:
                if self.thread.isRunning():
                    QMessageBox.information(self, "Đang xử lý", "Một tác vụ khác đang chạy.")
                    return
            except RuntimeError:
                self.thread = None
        self.thread = QThread(self)
        self.worker = TaskWorker(task, *args, **kwargs)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.write_log)
        self.worker.done.connect(self.task_done)
        self.worker.failed.connect(self.task_failed)
        self.worker.done.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._task_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.progress.setValue(0)
        self._last_download_percent = None
        self.status.setText("Đang xử lý...")

    def _task_thread_finished(self):
        self.thread = None
        self.worker = None

    def task_done(self, result):
        self.status.setText("Hoàn tất")
        self.write_log(f"[V3] Hoàn tất: {result}")

    def task_failed(self, error):
        self.status.setText("Lỗi")
        self.write_log(f"[V3] LỖI: {error}")
        self._write_bug_report(error)
        QMessageBox.critical(self, "Lỗi pipeline", error)

    def _write_bug_report(self, error: str):
        report = Path(__file__).with_name("BUG_REPORT.txt")
        existing = report.read_text(encoding="utf-8") if report.exists() else ""
        numbers = [int(value) for value in __import__("re").findall(r"\[BUG-(\d+)\]", existing)]
        bug_id = max(numbers, default=0) + 1
        entry = (
            f"\n[BUG-{bug_id:03d}]\n"
            f"Thoi gian: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"Chuc nang: {self.status.text()} / tac vu dang chay\n"
            f"Video hoac thu muc: {self.root}\n"
            f"Ket qua thuc te: Tac vu that bai\n"
            f"Ket qua mong muon: Tac vu hoan tat\n"
            f"Log loi: {error}\n"
            "Trang thai: Chua xu ly\n"
        )
        try:
            with report.open("a", encoding="utf-8") as handle:
                handle.write(entry)
        except OSError as write_error:
            self.write_log(f"[V3] Khong ghi duoc BUG_REPORT.txt: {write_error}")

    def separate(self):
        self.start_task(separate_folder, str(self.root))

    def rename(self):
        self.start_task(auto_rename_folder, str(self.root))

    def concat(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Chọn video để ghép", str(self.root), "Video (*.mp4 *.mkv *.mov *.avi)"
        )
        if len(files) < 2:
            if files:
                QMessageBox.information(self, "Ghép video", "Cần chọn ít nhất 2 video.")
            return
        self.start_task(concat_videos, files)

    def auto_run(self):
        dialog = AutoProcessDialog(bool(self.pending_download_links), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        steps = dialog.values()
        if self.pending_download_links:
            urls = self.pending_download_links
            dfn = self.pending_download_dfn
            if steps.get("download"):
                self.pending_download_links = []
                self.start_task(run_download_and_auto_pipeline, str(self.root), urls, dfn, steps)
            else:
                self.start_task(run_auto_pipeline, str(self.root), steps)
            return
        self.start_task(run_auto_pipeline, str(self.root), steps)

    def create_srt(self):
        if self.root == Path.cwd() or not self.root.exists():
            QMessageBox.warning(self, "Chưa chọn thư mục", "Hãy chọn thư mục tổng trước khi tạo SRT.")
            return
        kphoto = (Path(__file__).parent / "models" / "kphoto-local" / "zh" / "zh" / "model.pt").is_file()
        dialog = SrtModelDialog(kphoto, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.start_task(create_srt_batch, str(self.root), dialog.value())

    def translate(self):
        if self.root == Path.cwd() or not self.root.exists():
            QMessageBox.warning(self, "Chưa chọn thư mục", "Hãy chọn thư mục tổng trước khi dịch sub.")
            return
        languages = sorted({path.stem.lower() for path in self.root.rglob("*.srt") if path.stem and path.stem.isalpha()})
        if not languages:
            QMessageBox.warning(self, "Không có SRT", "Không tìm thấy file SRT trong thư mục đã chọn.")
            return
        dialog = TranslateDialog(languages, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        source, target, model = dialog.values()
        self.start_task(translate_srt_batch, str(self.root), target, model, "", source_language=source)

    def mux(self):
        self.start_task(mux_folder, str(self.root))

    def export(self):
        self.start_task(export_folder, str(self.root))


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet("QMainWindow,QWidget{background:#050505;color:#fff} QPushButton{padding:8px 12px}")
    window = MainWindow()
    screen = QApplication.primaryScreen().availableGeometry()
    width = int(screen.width() * 0.8)
    height = int(screen.height() * 0.8)
    window.resize(width, height)
    window.move(
        screen.left() + (screen.width() - width) // 2,
        screen.top() + (screen.height() - height) // 2,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
