from __future__ import annotations

import sys
import re
import os
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, QUrl, QSizeF, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QPen, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QMainWindow,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget, QDialog,
    QDialogButtonBox, QComboBox, QProgressBar, QCheckBox, QRadioButton,
    QSlider, QListWidgetItem, QGraphicsView, QGraphicsScene, QGraphicsItem,
    QGraphicsProxyWidget, QGraphicsRectItem, QGraphicsPixmapItem,
    QGraphicsBlurEffect, QButtonGroup,
)

from modules.separator import separate_folder
from modules.srt import create_srt_batch
from modules.translator import translate_srt_batch
from modules.muxer import mux_folder
from modules.exporter import export_folder
from modules.downloader import bbdown_login, download_multiple, has_login_session
from modules.rename import auto_rename_folder
from modules.concat import concat_videos
from modules.updater import latest_release, download_and_install
from config import VERSION


def run_auto_pipeline(folder: str, steps: dict[str, bool], log_callback=None, ocr_regions=None, overlay_configs=None):
    def log(message):
        if log_callback:
            log_callback(message)

    if steps.get("concat"):
        log("[AutoStage] Đang ghép file video")
        merge_folders = sorted(
            [path for path in Path(folder).iterdir() if path.is_dir()],
            key=lambda path: path.name.casefold(),
        ) if Path(folder).exists() else []
        log(f"[ConcatProgress] START total={len(merge_folders)}")
        for merge_index, part_folder in enumerate(merge_folders, 1):
            direct_mp4s = [
                path for path in part_folder.iterdir()
                if path.is_file() and path.suffix.lower() == ".mp4"
            ]
            has_done = any(path.name.lower() == "done.mp4" for path in direct_mp4s)
            if len(direct_mp4s) >= 2 and not has_done:
                log(f"[Auto] Đang ghép parts: {part_folder.name}")
                try:
                    concat_videos(
                        [str(path) for path in direct_mp4s],
                        delete_originals=True,
                        log_callback=log_callback,
                    )
                except Exception as exc:
                    log(f"[Auto] Lỗi khi ghép thư mục '{part_folder}': {exc}")
            log(f"[ConcatProgress] {merge_index}/{len(merge_folders)}")
    if steps.get("rename"):
        log("[AutoStage] Đang đặt tên / phân loại")
        auto_rename_folder(folder, log_callback=log_callback)
    if steps.get("separate"):
        log("[AutoStage] Đang tách vocal")
        separate_folder(folder, log_callback=log_callback)
    if steps.get("srt"):
        log("[AutoStage] Đang quét OCR PP-OCRv6 (timeline 0,1 giây)")
        create_srt_batch(
            folder, engine="rapidocr-v6", log_callback=log_callback,
            ocr_regions=ocr_regions,
        )
    if steps.get("translate"):
        log("[AutoStage] Đang dịch Gemini 3.6 Flash-High")
        automatic_model = os.getenv("TRANSLATOR_MODEL", "gemini-3.6-flash-high")
        automatic_key = (
            os.getenv("GEMINI36_API_KEY", os.getenv("GEMINI_API_KEY", ""))
            if automatic_model == "gemini-3.6-flash-high"
            else os.getenv("QWEN_API_KEY", "")
        )
        translate_srt_batch(
            folder, "en", automatic_model, automatic_key,
            log_callback=log_callback,
        )
    if steps.get("mux"):
        log("[AutoStage] Đang ghép vocal")
        mux_folder(folder, log_callback=log_callback)
    if steps.get("export"):
        log("[AutoStage] Đang xuất video")
        return export_folder(folder, log_callback=log_callback, overlay_configs=overlay_configs)
    log("[AutoStage] Đã hoàn tất các bước được chọn")
    return folder


def run_download_and_auto_pipeline(folder: str, urls: list[str], dfn_priority: str, steps: dict[str, bool], log_callback=None):
    if log_callback:
        log_callback(f"[AutoStage] Đang tải {len(urls)} link")
    download_multiple(urls, dfn_priority=dfn_priority, output_dir=folder, log_callback=log_callback)
    return run_auto_pipeline(folder, steps, log_callback=log_callback)


def mux_and_export(folder: str, overlay_configs=None, log_callback=None):
    """Ghép vocal local rồi xuất hiệu ứng/phụ đề trong một tác vụ."""
    if log_callback:
        log_callback("[AutoStage] Ghép vocal trước khi xuất")
    mux_folder(folder, log_callback=log_callback)
    if log_callback:
        log_callback("[AutoStage] Xuất video (blur + phụ đề)")
    return export_folder(folder, log_callback=log_callback, overlay_configs=overlay_configs)


class AutoProcessDialog(QDialog):
    def __init__(self, has_pending_download=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn quy trình tự động")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn các bước muốn chạy (mặc định đã chọn hết):"))
        self.checks = {}
        items = [("download", "Tải video (link đã nhập)"), ("concat", "Ghép file video"), ("rename", "Đặt tên / phân loại"), ("separate", "Tách vocal")]
        for key, label in items:
            check = QCheckBox(label)
            check.setChecked(True)
            if key == "mux":
                check.setChecked(True)
                check.setEnabled(False)
                check.setToolTip("Vocal local sẽ được ghép trực tiếp trong bước Xuất video; không tạo file trung gian.")
            self.checks[key] = check
            layout.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def values(self):
        values = {key: check.isChecked() for key, check in self.checks.items()}
        # These phases are intentionally resumed after the user draws OCR
        # regions, then paused again before export for visual adjustments.
        values.update({"srt": True, "translate": True, "mux": False, "export": True})
        return values


class SrtTranslateProcessDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tiếp tục quy trình tự động")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn các bước tiếp theo (mặc định đã chọn hết):"))
        self.srt = QCheckBox("Quét SRT bằng PP-OCRv6 (timeline 0,1 giây)")
        self.translate = QCheckBox("Dịch SRT bằng Gemini 3.6 Flash-High")
        self.srt.setChecked(True)
        self.translate.setChecked(True)
        layout.addWidget(self.srt)
        layout.addWidget(self.translate)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return self.srt.isChecked(), self.translate.isChecked()


class SrtModelDialog(QDialog):
    def __init__(self, kphoto_available: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn model tạo SRT")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn model nhận dạng tiếng Trung:"))
        self.whisper = QRadioButton("Whisper V3 (large-v3) - chất lượng cao")
        layout.addWidget(self.whisper)
        self.rapidocr = QRadioButton("PP-OCRv6 Small - đọc sub Trung trên hình, nhanh và đúng timeline")
        self.rapidocr.setChecked(True)
        layout.addWidget(self.rapidocr)
        layout.addWidget(QLabel("OCR đọc trực tiếp hình ảnh nên không sử dụng nguồn âm thanh bên dưới."))
        self.kphoto = QRadioButton("KPHOTO-Local - nhanh, dùng GPU")
        self.kphoto.setEnabled(kphoto_available)
        if not kphoto_available:
            self.kphoto.setToolTip("Chưa có model KPHOTO-Local trong Bili2YT_V3/models")
        layout.addWidget(self.kphoto)
        self.engine_group = QButtonGroup(self)
        self.engine_group.addButton(self.whisper)
        self.engine_group.addButton(self.rapidocr)
        self.engine_group.addButton(self.kphoto)
        layout.addWidget(QLabel("Chọn nguồn âm thanh:"))
        self.original_audio = QRadioButton("Âm thanh gốc từ video MP4 (đúng timeline)")
        self.original_audio.setChecked(True)
        layout.addWidget(self.original_audio)
        self.vocal_audio = QRadioButton("File (Vocals) đã tách (ít nhạc nền hơn)")
        layout.addWidget(self.vocal_audio)
        self.source_group = QButtonGroup(self)
        self.source_group.addButton(self.original_audio)
        self.source_group.addButton(self.vocal_audio)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def values(self):
        if self.rapidocr.isChecked():
            engine = "rapidocr-v6"
        else:
            engine = "kphoto-local" if self.kphoto.isChecked() else "whisper-v3"
        source_mode = "original" if self.original_audio.isChecked() else "vocals"
        return engine, source_mode


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
        self.google = QRadioButton("Google Translate Web (miễn phí)"); layout.addWidget(self.google)
        self.gemini = QRadioButton("Gemini (cần API key)"); self.gemini.setEnabled(False); layout.addWidget(self.gemini)
        self.qwen38 = QRadioButton("Qwen3.8-Max (cần QWEN_API_KEY)"); layout.addWidget(self.qwen38)
        self.gemini36 = QRadioButton("Gemini 3.6 Flash-High (cần GEMINI_API_KEY)"); self.gemini36.setChecked(True); layout.addWidget(self.gemini36)
        self.gemini31 = QRadioButton("Gemini 3.1 Flash-Lite (cần GEMINI_API_KEY)"); layout.addWidget(self.gemini31)
        self.hybrid = QRadioButton("Hybrid: Qwen3.8-Max + Gemini sửa chọn lọc"); layout.addWidget(self.hybrid)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def values(self):
        model = ("gemini-3.6-flash-high" if self.gemini36.isChecked() else
                 ("gemini-3.1-flash-lite" if self.gemini31.isChecked() else
                 ("hybrid-qwen-gemini" if self.hybrid.isChecked() else
                  ("qwen3.8-max" if self.qwen38.isChecked() else "google-web"))))
        return self.source.currentText(), self.target.currentData(), model


class TaskWorker(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, task, *args, **kwargs):
        super().__init__()
        self.task, self.args, self.kwargs = task, args, kwargs

    def run(self):
        try:
            call_kwargs = dict(self.kwargs)
            call_kwargs["log_callback"] = self.log.emit
            if self.task is export_folder:
                call_kwargs.setdefault("progress_callback", self.progress.emit)
            result = self.task(*self.args, **call_kwargs)
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class PreviewVideoView(QGraphicsView):
    resized = pyqtSignal()
    ocrDragStarted = pyqtSignal(object)
    ocrDragMoved = pyqtSignal(object)
    ocrDragFinished = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ocr_drawing = False
        self._ocr_dragging = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def mousePressEvent(self, event):
        if self.ocr_drawing and event.button() == Qt.MouseButton.LeftButton:
            point = self.mapToScene(event.position().toPoint())
            self._ocr_dragging = True
            self.ocrDragStarted.emit(point)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._ocr_dragging:
            self.ocrDragMoved.emit(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._ocr_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._ocr_dragging = False
            self.ocr_drawing = False
            self.ocrDragFinished.emit(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class OcrRegionItem(QGraphicsRectItem):
    """Movable OCR box with resizable edges and corners."""

    def __init__(self, changed_callback):
        super().__init__()
        self.changed_callback = changed_callback
        self.allowed_rect = QRectF()
        self._resize_edges = set()
        self._press_scene_pos = QPointF()
        self._press_rect = QRectF()
        self.setPen(QPen(QColor(0, 255, 140), 2, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(0, 255, 140, 28)))
        self.setZValue(30)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setToolTip("Kéo bên trong để di chuyển; kéo cạnh hoặc góc để đổi kích thước vùng OCR")

    def scene_box(self) -> QRectF:
        mapped = self.mapRectToScene(self.rect())
        return mapped.boundingRect() if hasattr(mapped, "boundingRect") else QRectF(mapped)

    def set_scene_box(self, box: QRectF):
        box = box.normalized()
        self.setPos(box.topLeft())
        self.setRect(0, 0, box.width(), box.height())

    def _edges_at(self, position: QPointF) -> set[str]:
        margin = 12.0
        rect = self.rect()
        edges = set()
        if abs(position.x() - rect.left()) <= margin:
            edges.add("left")
        if abs(position.x() - rect.right()) <= margin:
            edges.add("right")
        if abs(position.y() - rect.top()) <= margin:
            edges.add("top")
        if abs(position.y() - rect.bottom()) <= margin:
            edges.add("bottom")
        return edges

    def hoverMoveEvent(self, event):
        edges = self._edges_at(event.pos())
        if edges in ({"left", "top"}, {"right", "bottom"}):
            cursor = Qt.CursorShape.SizeFDiagCursor
        elif edges in ({"right", "top"}, {"left", "bottom"}):
            cursor = Qt.CursorShape.SizeBDiagCursor
        elif "left" in edges or "right" in edges:
            cursor = Qt.CursorShape.SizeHorCursor
        elif "top" in edges or "bottom" in edges:
            cursor = Qt.CursorShape.SizeVerCursor
        else:
            cursor = Qt.CursorShape.SizeAllCursor
        self.setCursor(cursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        self._resize_edges = self._edges_at(event.pos())
        self._press_scene_pos = event.scenePos()
        self._press_rect = self.scene_box()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._resize_edges:
            super().mouseMoveEvent(event)
            return
        delta = event.scenePos() - self._press_scene_pos
        box = QRectF(self._press_rect)
        if "left" in self._resize_edges:
            box.setLeft(box.left() + delta.x())
        if "right" in self._resize_edges:
            box.setRight(box.right() + delta.x())
        if "top" in self._resize_edges:
            box.setTop(box.top() + delta.y())
        if "bottom" in self._resize_edges:
            box.setBottom(box.bottom() + delta.y())
        box = box.normalized().intersected(self.allowed_rect)
        if box.width() >= 40 and box.height() >= 28:
            self.set_scene_box(box)
        event.accept()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        box = self.scene_box().intersected(self.allowed_rect)
        if box.width() >= 40 and box.height() >= 28:
            self.set_scene_box(box)
        self._resize_edges = set()
        self.changed_callback()


class DraggableSubtitleProxy(QGraphicsProxyWidget):
    moved = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._drag_offset = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            target = event.scenePos() - self._drag_offset
            bounds = self.scene().sceneRect() if self.scene() else QRectF()
            max_x = max(bounds.left(), bounds.right() - self.size().width())
            max_y = max(bounds.top(), bounds.bottom() - self.size().height())
            target.setX(min(max(target.x(), bounds.left()), max_x))
            target.setY(min(max(target.y(), bounds.top()), max_y))
            self.setPos(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.moved.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


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
        self.login_status.setText("Đã mở Edge riêng - hãy quét QR")

    def values(self):
        return [line.strip() for line in self.urls.toPlainText().splitlines() if line.strip()], self.dfn.currentData()


class ExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Xuất video")
        self.resize(420, 150)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn các bước trước khi xuất:"))
        self.mux_vocal = QCheckBox("Ghép vocal (dùng file Vocals local, tắt audio gốc)")
        self.mux_vocal.setChecked(True)
        layout.addWidget(self.mux_vocal)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.root = Path.cwd()
        self.thread = None
        self.worker = None
        self.pending_task = None
        self.pending_download_links = []
        self.pending_download_dfn = "720P 高清, 720P"
        self.auto_plan = None
        self.auto_phase = 0
        self._auto_active_phase = None
        self.auto_button = None
        self.overlay_configs = {}
        self._last_download_percent = None
        if not hasattr(self, "_activity_timer"):
            self._activity_timer = QTimer(self)
            self._activity_timer.setInterval(5000)
            self._activity_timer.timeout.connect(self._write_activity_heartbeat)
        self.preview_subtitles = []
        self.preview_subtitle_path = None
        self.preview_video_path = None
        self._subtitle_user_position = False
        self._preview_loading_frame = False
        self._preview_was_muted = False
        self._preview_resume_after_seek = False
        self._pending_preview_seek = 0
        self._ocr_draw_origin = None
        self._updating_ocr_region = False
        self._updating_blur_region = False
        self._last_preview_frame_image = None
        self.setWindowTitle("KPHOTO-YTB - Video Workspace")
        self.resize(1200, 780)
        self._build_ui_v3()
        QTimer.singleShot(1500, self._check_for_update)

    def _check_for_update(self):
        """Check the latest release and offer an in-place executable update."""
        release = latest_release()
        if not release:
            return
        tag = str(release.get("tag_name", "")).lstrip("v")
        if tag and tag != VERSION:
            self.status.setText(f"Có bản cập nhật KPHOTO-YTB {tag}")
            self.write_log(f"[Update] Có bản mới {tag}: {release.get('html_url', '')}")
            assets = release.get("assets") or []
            asset = next((item for item in assets if item.get("name") == "KPHOTO-YTB_update.zip"), None)
            if not asset:
                self.write_log("[Update] Release chưa có KPHOTO-YTB_update.zip")
                return
            answer = QMessageBox.question(
                self, "Cập nhật KPHOTO-YTB",
                f"Có phiên bản {tag}. Tải và cài đặt ngay?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.status.setText("Đang tải bản cập nhật...")
                if download_and_install(asset.get("browser_download_url", "")):
                    QApplication.quit()
                else:
                    QMessageBox.warning(self, "Cập nhật lỗi", "Không thể tải hoặc cài bản cập nhật.")

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
        brand = QLabel("KPHOTO-YTB"); brand.setObjectName("brand")
        eyebrow = QLabel("VIDEO WORKSPACE / V3"); eyebrow.setObjectName("eyebrow")
        header = QHBoxLayout(); header.addWidget(brand); header.addWidget(eyebrow); header.addStretch(1); header.addWidget(QPushButton("Cài đặt / Đăng nhập"))
        self.folder_label = QLabel("Chưa chọn thư mục tổng"); self.folder_label.setObjectName("muted")
        choose = QPushButton("Chọn thư mục"); choose.clicked.connect(self.choose_folder)
        auto = QPushButton("Chạy tự động  ·  0 phim"); auto.setObjectName("primary"); auto.clicked.connect(self.auto_run)
        self.auto_button = auto
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
        preview = QFrame(); preview.setObjectName("panel"); preview_layout = QVBoxLayout(preview); preview_layout.addWidget(QLabel("XEM TRƯỚC", objectName="title"))
        self.video_view = PreviewVideoView()
        self.video_view.setMinimumHeight(380)
        self.video_view.setFrameShape(QFrame.Shape.NoFrame)
        self.video_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_view.setStyleSheet("background:#050505;border:none")
        self.video_scene = QGraphicsScene(self.video_view)
        self.video_view.setScene(self.video_scene)
        self.video_item = QGraphicsVideoItem()
        self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video_scene.addItem(self.video_item)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setContentsMargins(8, 0, 8, 0)
        self.subtitle_label.setStyleSheet(
            "QLabel { color:white; background:transparent; border:none; "
            "font-size:22px; font-weight:800; padding:2px; }"
        )
        self.subtitle_proxy = DraggableSubtitleProxy()
        self.subtitle_proxy.setWidget(self.subtitle_label)
        self.video_scene.addItem(self.subtitle_proxy)
        self.subtitle_proxy.setZValue(10)
        self.subtitle_proxy.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.subtitle_proxy.setCursor(Qt.CursorShape.OpenHandCursor)
        self.subtitle_proxy.setToolTip("Giữ chuột và kéo để đổi vị trí phụ đề tiếng Anh")
        self.subtitle_proxy.moved.connect(self._subtitle_was_moved)
        self.ocr_region_item = OcrRegionItem(self._ocr_region_changed)
        self.ocr_region_item.hide()
        self.video_scene.addItem(self.ocr_region_item)
        self.blur_region_item = OcrRegionItem(self._blur_region_changed)
        self.blur_region_item.setPen(QPen(QColor(255, 190, 0), 2, Qt.PenStyle.DashLine))
        self.blur_region_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.blur_region_item.setToolTip(
            "Kéo để di chuyển; kéo 4 cạnh hoặc 4 góc để đổi kích thước vùng Blur"
        )
        self.blur_region_item.setZValue(25)
        self.blur_region_item.hide()
        self.video_scene.addItem(self.blur_region_item)
        self.blur_preview_item = QGraphicsPixmapItem()
        self.blur_preview_item.setZValue(24)
        self.blur_preview_item.hide()
        self.blur_preview_effect = QGraphicsBlurEffect()
        self.blur_preview_effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        self.blur_preview_item.setGraphicsEffect(self.blur_preview_effect)
        self.video_scene.addItem(self.blur_preview_item)
        self.video_view.resized.connect(self._resize_preview_scene)
        self.video_view.ocrDragStarted.connect(self._ocr_drag_started)
        self.video_view.ocrDragMoved.connect(self._ocr_drag_moved)
        self.video_view.ocrDragFinished.connect(self._ocr_drag_finished)
        preview_layout.addWidget(self.video_view, 1)
        self.audio_output = QAudioOutput(self)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_item)
        self.video_item.videoSink().videoFrameChanged.connect(self._preview_frame_for_blur)
        self.media_player.positionChanged.connect(self._preview_position_changed)
        self.media_player.durationChanged.connect(self._preview_duration_changed)
        self.media_player.playbackStateChanged.connect(self._preview_state_changed)
        self.media_player.mediaStatusChanged.connect(self._preview_media_status_changed)
        self.media_player.errorOccurred.connect(self._preview_error)
        self.subtitle_timer = QTimer(self)
        self.subtitle_timer.setInterval(50)
        self.subtitle_timer.timeout.connect(self._refresh_preview_subtitle)
        self.subtitle_timer.start()
        self.preview_seek_timer = QTimer(self)
        self.preview_seek_timer.setSingleShot(True)
        self.preview_seek_timer.setInterval(120)
        self.preview_seek_timer.timeout.connect(self._apply_preview_seek)
        controls = QHBoxLayout()
        self.preview_play = QPushButton("▶")
        self.preview_play.setEnabled(False)
        self.preview_play.clicked.connect(self._toggle_preview)
        controls.addWidget(self.preview_play)
        self.blur_button = QPushButton("+ Blur")
        self.blur_button.clicked.connect(self._enable_blur_region)
        controls.addWidget(self.blur_button)
        controls.addWidget(QPushButton("+ Logo"))
        controls.addWidget(QPushButton("+ Khung"))
        self.blur_strength_label = QLabel("Mờ 30")
        controls.addWidget(self.blur_strength_label)
        self.blur_strength = QSlider(Qt.Orientation.Horizontal)
        self.blur_strength.setRange(0, 100)
        self.blur_strength.setValue(30)
        self.blur_strength.setMaximumWidth(90)
        self.blur_strength.valueChanged.connect(self._blur_strength_changed)
        controls.addWidget(self.blur_strength)
        self.ocr_region_button = QPushButton("▣ Khung OCR")
        self.ocr_region_button.clicked.connect(self._enable_ocr_region_draw)
        controls.addWidget(self.ocr_region_button)
        self.preview_timeline = QSlider(Qt.Orientation.Horizontal)
        self.preview_timeline.setRange(0, 0)
        self.preview_timeline.sliderPressed.connect(self._preview_slider_pressed)
        self.preview_timeline.sliderMoved.connect(self._seek_preview)
        self.preview_timeline.sliderReleased.connect(self._preview_slider_released)
        controls.addWidget(self.preview_timeline, 1)
        self.preview_time = QLabel("00:00 / 00:00")
        controls.addWidget(self.preview_time)
        preview_layout.addLayout(controls)
        log_panel = QFrame(); log_panel.setObjectName("panel"); log_layout = QVBoxLayout(log_panel); log_layout.addWidget(QLabel("NHẬT KÝ CHUNG", objectName="title")); log_layout.addWidget(QLabel("Chưa có lượt tải", objectName="muted")); self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0); self.progress.setFormat("%p%"); log_layout.addWidget(self.progress); log_layout.addWidget(self.log_view, 1)
        middle = QHBoxLayout(); middle.setSpacing(8); middle.addWidget(left_widget, 1); middle.addWidget(preview, 2); middle.addWidget(log_panel, 1)
        actions = QHBoxLayout(); actions.setSpacing(7)
        for label, callback in (("↓ Tải", self.download), ("+ Ghép", self.concat), ("✎ Đặt tên", self.rename), ("✦ Tách vocal", self.separate), ("▣ Tạo SRT", self.create_srt), ("文 Dịch sub", self.translate), ("♫ Ghép vocal", self.mux), ("◆ Xuất video", self.export)):
            button = QPushButton(label); button.setObjectName("primary" if "Xuất" in label else ""); button.clicked.connect(callback); actions.addWidget(button, 1)
        root = QVBoxLayout(); root.setContentsMargins(17, 12, 17, 12); root.addLayout(header); root.addWidget(folder_bar); root.addLayout(middle, 1); root.addLayout(actions); root.addWidget(self.status)
        widget = QWidget(); widget.setLayout(root); self.setCentralWidget(widget)
        self.movies.currentItemChanged.connect(self._select_preview_video)

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
        self._refresh_movie_list()

    def _refresh_movie_list(self):
        current_item = self.movies.currentItem()
        selected_path = (
            current_item.data(Qt.ItemDataRole.UserRole) if current_item is not None else None
        )
        item_to_restore = None
        self.movies.clear()
        video_paths = sorted(self.root.rglob("*.mp4"), key=lambda path: str(path).lower())
        for path in video_paths:
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.movies.addItem(item)
            if selected_path and str(path) == selected_path:
                item_to_restore = item
        if item_to_restore is not None:
            self.movies.setCurrentItem(item_to_restore)
        self.write_log(f"[UI] Đã chọn: {self.root}")

    def _select_preview_video(self, current, _previous=None):
        if current is None:
            return
        stored_path = current.data(Qt.ItemDataRole.UserRole)
        if not stored_path:
            return
        video_path = Path(stored_path)
        if not video_path.exists():
            self.status.setText(f"Không tìm thấy video: {video_path.name}")
            return
        self.media_player.stop()
        self._preview_loading_frame = True
        self._preview_was_muted = self.audio_output.isMuted()
        self.preview_video_path = video_path
        self._last_preview_frame_image = None
        self.blur_preview_item.hide()
        video_config = self.overlay_configs.get(str(video_path.resolve()), {})
        self._subtitle_user_position = "subtitle_y_ratio" in video_config
        self.preview_subtitles = self._load_preview_subtitles(video_path)
        self.subtitle_label.clear()
        self.media_player.setSource(QUrl.fromLocalFile(str(video_path.resolve())))
        self._resize_preview_scene()
        self.preview_play.setEnabled(True)
        self.preview_play.setText("▶")
        self.preview_timeline.setValue(0)
        subtitle_name = (f"subtitles/{self.preview_subtitle_path.name}"
                         if self.preview_subtitle_path else "không có subtitles/en.srt")
        self.status.setText(f"Xem trước: {video_path.name} · {subtitle_name}")

    def _resize_preview_scene(self):
        viewport_size = self.video_view.viewport().size()
        width = max(1, viewport_size.width())
        height = max(1, viewport_size.height())
        native_size = self.video_item.nativeSize()
        native_width = native_size.width() if native_size.width() > 0 else 16
        native_height = native_size.height() if native_size.height() > 0 else 9
        display_scale = min(width / native_width, height / native_height)
        display_width = native_width * display_scale
        display_height = native_height * display_scale
        display_left = (width - display_width) / 2
        display_top = (height - display_height) / 2
        video_display_rect = QRectF(display_left, display_top, display_width, display_height)
        video_config = (self.overlay_configs.get(str(self.preview_video_path.resolve()), {})
                        if self.preview_video_path else {})
        subtitle_y_ratio = float(video_config.get("subtitle_y_ratio", 0.86))
        subtitle_center_ratio = (display_top + display_height * subtitle_y_ratio) / height
        scene_rect = QRectF(0, 0, width, height)
        self.video_scene.setSceneRect(scene_rect)
        self.video_item.setSize(QSizeF(width, height))
        subtitle_height = min(72, max(54, int(height * 0.10)))
        subtitle_width = max(120, width - 70)
        subtitle_y = subtitle_center_ratio * height - subtitle_height / 2
        subtitle_y = min(max(0, subtitle_y), height - subtitle_height)
        self.subtitle_proxy.setGeometry(QRectF(35, subtitle_y, subtitle_width, subtitle_height))
        self.ocr_region_item.allowed_rect = video_display_rect
        self.blur_region_item.allowed_rect = video_display_rect
        if self.preview_video_path:
            region = video_config.get("ocr_roi")
            if region and len(region) == 4:
                x, y, region_width, region_height = (float(value) for value in region)
                box = QRectF(
                    display_left + x * display_width,
                    display_top + y * display_height,
                    region_width * display_width,
                    region_height * display_height,
                ).intersected(video_display_rect)
                self._updating_ocr_region = True
                self.ocr_region_item.set_scene_box(box)
                self.ocr_region_item.show()
                self._updating_ocr_region = False
            else:
                self.ocr_region_item.hide()
            blur_boxes = video_config.get("blur_boxes") or []
            blur_box = blur_boxes[0] if blur_boxes and len(blur_boxes[0]) == 4 else None
            self._updating_blur_region = True
            if blur_box:
                x, y, box_width, box_height = (float(value) for value in blur_box)
                box = QRectF(
                    display_left + x * display_width,
                    display_top + y * display_height,
                    box_width * display_width,
                    box_height * display_height,
                ).intersected(video_display_rect)
                self.blur_region_item.set_scene_box(box)
                self.blur_region_item.show()
                self._render_blur_preview()
            else:
                self.blur_region_item.hide()
                self.blur_preview_item.hide()
            strength = max(0, min(100, int(video_config.get("blur_strength", 30))))
            self.blur_strength.setValue(strength)
            self.blur_strength_label.setText(f"Mờ {strength}")
            self._update_blur_item_brush(strength)
            self._updating_blur_region = False

    def _enable_blur_region(self):
        if not self.preview_video_path:
            QMessageBox.information(
                self, "Khung Blur", "Hãy chọn một video ở danh sách bên trái trước."
            )
            return
        allowed = self.blur_region_item.allowed_rect
        if allowed.width() <= 0 or allowed.height() <= 0:
            return
        if not self.blur_region_item.isVisible():
            default_box = QRectF(
                allowed.left() + allowed.width() * 0.72,
                allowed.top() + allowed.height() * 0.04,
                allowed.width() * 0.23,
                allowed.height() * 0.13,
            ).intersected(allowed)
            self.blur_region_item.set_scene_box(default_box)
            self.blur_region_item.show()
            self._blur_region_changed()
        self._render_blur_preview()
        self.status.setText(
            "Khung Blur: kéo bên trong để di chuyển; kéo cạnh hoặc góc để co giãn."
        )

    def _blur_region_changed(self):
        if (self._updating_blur_region or not self.preview_video_path
                or not self.blur_region_item.isVisible()):
            return
        allowed = self.blur_region_item.allowed_rect
        box = self.blur_region_item.scene_box().intersected(allowed)
        if allowed.width() <= 0 or allowed.height() <= 0:
            return
        blur_box = [
            max(0.0, min(1.0, (box.left() - allowed.left()) / allowed.width())),
            max(0.0, min(1.0, (box.top() - allowed.top()) / allowed.height())),
            max(0.01, min(1.0, box.width() / allowed.width())),
            max(0.01, min(1.0, box.height() / allowed.height())),
        ]
        config = self.overlay_configs.setdefault(str(self.preview_video_path.resolve()), {})
        config["blur_boxes"] = [blur_box]
        config["blur_strength"] = self.blur_strength.value()
        self._render_blur_preview()
        self.status.setText(
            f"Đã lưu Blur riêng cho {self.preview_video_path.name}"
        )

    def _update_blur_item_brush(self, strength):
        self.blur_region_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        # Blur is rendered into the cropped pixmap itself.  Keeping a
        # QGraphicsBlurEffect on top creates transparent/gradient edges.
        self.blur_preview_effect.setEnabled(False)
        self._render_blur_preview()

    def _preview_frame_for_blur(self, frame):
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._last_preview_frame_image = image
        self._render_blur_preview()

    def _render_blur_preview(self):
        image = self._last_preview_frame_image
        if (image is None or image.isNull() or not self.blur_region_item.isVisible()
                or not self.preview_video_path):
            self.blur_preview_item.hide()
            return
        config = self.overlay_configs.get(str(self.preview_video_path.resolve()), {})
        boxes = config.get("blur_boxes") or []
        if not boxes or len(boxes[0]) != 4:
            self.blur_preview_item.hide()
            return
        x, y, width, height = [float(value) for value in boxes[0]]
        left = max(0, min(image.width() - 1, round(x * image.width())))
        top = max(0, min(image.height() - 1, round(y * image.height())))
        crop_width = max(1, min(image.width() - left, round(width * image.width())))
        crop_height = max(1, min(image.height() - top, round(height * image.height())))
        crop = image.copy(left, top, crop_width, crop_height)
        strength = int(config.get("blur_strength", self.blur_strength.value()))
        if strength > 0:
            # Downsample then upsample the exact rectangle. This produces a
            # uniform CapCut-like blur patch with no feathered border.
            factor = max(1, round(18 - strength * 0.16))
            small = crop.scaled(
                max(1, crop_width // factor), max(1, crop_height // factor),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            crop = small.scaled(
                crop_width, crop_height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        box = self.blur_region_item.scene_box()
        target_width = max(1, round(box.width()))
        target_height = max(1, round(box.height()))
        pixmap = QPixmap.fromImage(crop).scaled(
            target_width, target_height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.blur_preview_item.setPixmap(pixmap)
        self.blur_preview_item.setPos(box.topLeft())
        self.blur_preview_item.show()

    def _blur_strength_changed(self, value):
        value = max(0, min(100, int(value)))
        self.blur_strength_label.setText(f"Mờ {value}")
        self._update_blur_item_brush(value)
        if self._updating_blur_region or not self.preview_video_path:
            return
        config = self.overlay_configs.setdefault(str(self.preview_video_path.resolve()), {})
        config["blur_strength"] = value
        self._render_blur_preview()
        if self.blur_region_item.isVisible():
            self.status.setText(
                f"Độ mờ {value} cho {self.preview_video_path.name}"
            )

    def _enable_ocr_region_draw(self):
        if not self.preview_video_path:
            QMessageBox.information(self, "Khung OCR", "Hãy chọn một video ở danh sách bên trái trước.")
            return
        self.video_view.ocr_drawing = True
        self.video_view.setCursor(Qt.CursorShape.CrossCursor)
        self.status.setText("Giữ chuột và kéo trên video để tạo khung OCR.")

    def _ocr_drag_started(self, point):
        allowed = self.ocr_region_item.allowed_rect
        self._ocr_draw_origin = QPointF(
            min(max(point.x(), allowed.left()), allowed.right()),
            min(max(point.y(), allowed.top()), allowed.bottom()),
        )
        self.ocr_region_item.set_scene_box(QRectF(self._ocr_draw_origin, QSizeF(1, 1)))
        self.ocr_region_item.show()

    def _ocr_drag_moved(self, point):
        if self._ocr_draw_origin is None:
            return
        allowed = self.ocr_region_item.allowed_rect
        current = QPointF(
            min(max(point.x(), allowed.left()), allowed.right()),
            min(max(point.y(), allowed.top()), allowed.bottom()),
        )
        self.ocr_region_item.set_scene_box(QRectF(self._ocr_draw_origin, current).normalized())

    def _ocr_drag_finished(self, point):
        self._ocr_drag_moved(point)
        self._ocr_draw_origin = None
        self.video_view.unsetCursor()
        if self.ocr_region_item.scene_box().width() < 40 or self.ocr_region_item.scene_box().height() < 28:
            self.ocr_region_item.hide()
            self.status.setText("Khung OCR quá nhỏ; hãy kéo lại.")
            return
        self._ocr_region_changed()

    def _ocr_region_changed(self):
        if self._updating_ocr_region or not self.preview_video_path or not self.ocr_region_item.isVisible():
            return
        allowed = self.ocr_region_item.allowed_rect
        box = self.ocr_region_item.scene_box().intersected(allowed)
        if allowed.width() <= 0 or allowed.height() <= 0:
            return
        roi = [
            max(0.0, min(1.0, (box.left() - allowed.left()) / allowed.width())),
            max(0.0, min(1.0, (box.top() - allowed.top()) / allowed.height())),
            max(0.01, min(1.0, box.width() / allowed.width())),
            max(0.01, min(1.0, box.height() / allowed.height())),
        ]
        config = self.overlay_configs.setdefault(str(self.preview_video_path.resolve()), {})
        config["ocr_roi"] = roi
        self.status.setText(
            f"Đã lưu khung OCR {roi[2] * 100:.1f}% × {roi[3] * 100:.1f}% cho {self.preview_video_path.name}"
        )

    def _subtitle_was_moved(self):
        self._subtitle_user_position = True
        if not self.preview_video_path:
            return
        scene_height = self.video_scene.sceneRect().height()
        scene_width = self.video_scene.sceneRect().width()
        native_size = self.video_item.nativeSize()
        native_width = native_size.width() if native_size.width() > 0 else 16
        native_height = native_size.height() if native_size.height() > 0 else 9
        display_scale = min(scene_width / native_width, scene_height / native_height)
        display_height = native_height * display_scale
        display_top = (scene_height - display_height) / 2
        subtitle_center = self.subtitle_proxy.geometry().center().y()
        subtitle_y_ratio = ((subtitle_center - display_top) / display_height
                            if display_height > 0 else 0.86)
        subtitle_y_ratio = max(0.05, min(0.95, subtitle_y_ratio))
        config = self.overlay_configs.setdefault(str(self.preview_video_path.resolve()), {})
        config["subtitle_y_ratio"] = subtitle_y_ratio
        self.status.setText(
            f"Đã lưu vị trí sub {subtitle_y_ratio * 100:.1f}% cho {self.preview_video_path.name}"
        )

    @staticmethod
    def _srt_time_ms(value: str) -> int:
        hours, minutes, seconds_ms = value.strip().replace(".", ",").split(":")
        seconds, milliseconds = seconds_ms.split(",")
        return (((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
                + int(milliseconds.ljust(3, "0")[:3]))

    def _load_preview_subtitles(self, video_path: Path):
        subtitle_folder = video_path.parent / "subtitles"
        subtitle_path = subtitle_folder / "en.srt"
        self.preview_subtitle_path = subtitle_path if subtitle_path.exists() else None
        if self.preview_subtitle_path is None:
            return []
        try:
            raw = self.preview_subtitle_path.read_text(encoding="utf-8-sig", errors="replace")
            cues = []
            for block in re.split(r"\r?\n\s*\r?\n", raw.strip()):
                lines = block.splitlines()
                timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
                if timing_index < 0:
                    continue
                start_text, end_text = (part.strip() for part in lines[timing_index].split("-->", 1))
                text = "\n".join(line.strip() for line in lines[timing_index + 1:] if line.strip())
                if text:
                    cues.append((self._srt_time_ms(start_text), self._srt_time_ms(end_text), text))
            return cues
        except (OSError, ValueError) as exc:
            self.write_log(f"[Preview] Không đọc được {subtitle_path}: {exc}")
            return []

    def _toggle_preview(self):
        if not self.preview_video_path:
            return
        if self._preview_loading_frame:
            self._preview_loading_frame = False
            self.audio_output.setMuted(self._preview_was_muted)
            self.media_player.play()
            return
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()

    def _seek_preview(self, position: int):
        self._pending_preview_seek = max(0, int(position))
        # Subtitle text and time follow the mouse immediately even while an
        # HEVC keyframe seek is still being decoded in the background.
        self.preview_time.setText(
            f"{self._format_preview_time(self._pending_preview_seek)} / "
            f"{self._format_preview_time(self.media_player.duration())}"
        )
        self._update_preview_subtitle(self._pending_preview_seek)
        self.preview_seek_timer.start()

    def _preview_slider_pressed(self):
        self._preview_resume_after_seek = (
            self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        if self._preview_resume_after_seek:
            self.media_player.pause()

    def _apply_preview_seek(self):
        self.media_player.setPosition(self._pending_preview_seek)

    def _preview_slider_released(self):
        self.preview_seek_timer.stop()
        self._pending_preview_seek = self.preview_timeline.value()
        self._apply_preview_seek()
        self._update_preview_subtitle(self._pending_preview_seek)
        if self._preview_resume_after_seek:
            QTimer.singleShot(140, self.media_player.play)
        self._preview_resume_after_seek = False

    def _preview_duration_changed(self, duration: int):
        self.preview_timeline.setRange(0, max(0, duration))
        self.preview_time.setText(f"{self._format_preview_time(0)} / {self._format_preview_time(duration)}")

    def _preview_position_changed(self, position: int):
        if self._preview_loading_frame and position > 0:
            self._preview_loading_frame = False
            self.media_player.pause()
            self.audio_output.setMuted(self._preview_was_muted)
        if not self.preview_timeline.isSliderDown():
            self.preview_timeline.setValue(position)
        self.preview_time.setText(
            f"{self._format_preview_time(position)} / {self._format_preview_time(self.media_player.duration())}"
        )
        self._update_preview_subtitle(position)

    def _refresh_preview_subtitle(self):
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._update_preview_subtitle(self.media_player.position())

    def _update_preview_subtitle(self, position: int):
        subtitle = ""
        for start, end, text in self.preview_subtitles:
            if start <= position <= end:
                subtitle = text
                break
            if start > position:
                break
        self.subtitle_label.setText(subtitle)

    def _preview_state_changed(self, state):
        self.preview_play.setText("Ⅱ" if state == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _preview_media_status_changed(self, status):
        if (self._preview_loading_frame
                and status in (QMediaPlayer.MediaStatus.LoadedMedia,
                               QMediaPlayer.MediaStatus.BufferedMedia)):
            self._resize_preview_scene()
            self.audio_output.setMuted(True)
            self.media_player.play()

    def _preview_error(self, _error, error_text: str):
        if self._preview_loading_frame:
            self._preview_loading_frame = False
            self.audio_output.setMuted(self._preview_was_muted)
        if error_text:
            self.write_log(f"[Preview] Lỗi phát video: {error_text}")
            self.status.setText(f"Không thể phát {self.preview_video_path.name if self.preview_video_path else 'video'}")

    @staticmethod
    def _format_preview_time(milliseconds: int) -> str:
        total_seconds = max(0, int(milliseconds)) // 1000
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

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
        if "[TranslateCost]" in text:
            self.log_view.append("[Chi phí] " + text.split("[TranslateCost]", 1)[-1].strip())
            self.log_view.ensureCursorVisible()
            return
        match = re.search(r"(?:\[DownloadProgress\]\s+PERCENT.*?\s+)?percent=([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
        if match:
            percent = max(0, min(100, round(float(match.group(1)))))
            self.progress.setValue(percent)
            if "[TranslateProgress]" in text:
                detail = text.split("[TranslateProgress]", 1)[-1].strip()
                if detail.upper().startswith("START"):
                    return
                self.log_view.append("[Dịch] " + detail)
                self.log_view.ensureCursorVisible()
                return
            # Keep the progress bar live without filling the log with repeats.
            if percent not in (0, 100) and percent % 5 != 0:
                return
            if percent == self._last_download_percent:
                return
            self._last_download_percent = percent
            detail = text.split("[DownloadProgress]", 1)[-1].strip()
            self.log_view.append(f"[Tải] {detail}")
            self.log_view.ensureCursorVisible()
            return
        separator_match = re.search(r"\[Separator\].*?\s(\d{1,3})%\s*\|", text, re.IGNORECASE)
        if separator_match:
            percent = max(0, min(100, int(separator_match.group(1))))
            self.progress.setValue(percent)
            if percent != 0 and percent % 5 != 0:
                return
            last_separator = getattr(self, "_last_separator_percent", None)
            if percent == last_separator:
                return
            self._last_separator_percent = percent
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
        ocr_match = re.search(r"\[SrtProgress\]\s+OCR_PERCENT\s+(\d+)", text, re.IGNORECASE)
        if ocr_match:
            percent = max(0, min(100, int(ocr_match.group(1))))
            self.progress.setValue(percent)
            if percent in (0, 100) or percent % 5 == 0:
                self.log_view.append(f"[OCR PP-OCRv6] {percent}%")
            return
        elif text.startswith("[SrtSource]") or text.startswith("[SrtSync]") or text.startswith("KPHOTO-Local:") or text.startswith("[OCR]"):
            self.log_view.append(text)
            self.log_view.ensureCursorVisible()
            return
        elif "[DownloadProgress] DONE" in text:
            self.progress.setValue(100)
            self._last_download_percent = 100
        elif text.startswith("[BBDown]") or "[DownloadProgress] START" in text:
            return
        elif text.startswith("[TranslateProgress]"):
            if text.upper().startswith("[TRANSLATEPROGRESS] START") and self.progress.value() == 0:
                self.progress.setValue(1)
            if not text.upper().startswith("[TRANSLATEPROGRESS] START"):
                self.log_view.append("[Dịch] " + text.replace("[TranslateProgress]", "", 1).strip())
            self.log_view.ensureCursorVisible()
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
            (r"\[Translate\]\s+Chia\s+(.+)$", "Dịch: {}"),
            (r"\[MuxProgress\]\s+VIDEO_START\s+(.+)$", "Đang xử lý: {}"),
            (r"\[ExportProgress\]\s+ITEM\s+\d+/\d+\s+(.+)$", "Đang xử lý: {}"),
            (r"\[ExportProgress\]\s+DONE\s+\d+/\d+", "Đã xử lý xong video"),
        )
        # Keep the ffmpeg command diagnostic informational: it contains
        # ``-loglevel error`` but does not indicate a failure.
        if text.startswith("[Mux] ") and ("-loglevel error" in text.lower() or "-loglevel ERROR" in text):
            self.log_view.append(text)
            self.log_view.ensureCursorVisible()
            return
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
                    task_names = {
                        export_folder: "Xuất video",
                        translate_srt_batch: "Dịch sub",
                        create_srt_batch: "Tạo SRT",
                        separate_folder: "Tách vocal",
                        mux_folder: "Ghép vocal",
                    }
                    task_name = task_names.get(task, getattr(task, "__name__", "Tác vụ tiếp theo"))
                    if self.pending_task is None:
                        self.pending_task = (task, args, kwargs)
                        self.status.setText(f"Đã xếp hàng: {task_name}")
                        self.write_log(
                            f"[Queue] {task_name} đang chờ và sẽ tự chạy ngay khi tác vụ hiện tại đóng"
                        )
                        QTimer.singleShot(300, self._start_pending_when_idle)
                    else:
                        queued_name = task_names.get(
                            self.pending_task[0],
                            getattr(self.pending_task[0], "__name__", "Tác vụ"),
                        )
                        self.status.setText(f"Đang chờ tự chạy: {queued_name}")
                    return
            except RuntimeError:
                self.thread = None
        task_labels = {
            export_folder: "Xuất video", translate_srt_batch: "Dịch phụ đề",
            create_srt_batch: "Tạo SRT", separate_folder: "Tách vocal",
            mux_folder: "Ghép vocal", run_download_and_auto_pipeline: "Tải video",
        }
        self._active_task_label = task_labels.get(task, getattr(task, "__name__", "Quy trình"))
        self.log_view.clear()
        self._last_download_percent = None
        self._last_separator_percent = None
        self.progress.setValue(0)
        self.log_view.append(f"[Bắt đầu] {self._active_task_label}")
        self.thread = QThread(self)
        self.worker = TaskWorker(task, *args, **kwargs)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.write_log)
        self.worker.progress.connect(self._set_task_progress)
        self.worker.done.connect(self.task_done)
        self.worker.failed.connect(self.task_failed)
        # Quit synchronously when the worker settles. A queued quit left the UI
        # showing 100% while QThread still reported isRunning(), which blocked
        # the next pipeline action for an unpredictable amount of time.
        self.worker.done.connect(self.thread.quit, Qt.ConnectionType.DirectConnection)
        self.worker.failed.connect(self.thread.quit, Qt.ConnectionType.DirectConnection)
        self.thread.finished.connect(self._task_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.progress.setValue(0)
        self._last_download_percent = None
        self._activity_timer.start()
        self.status.setText("Đang xử lý...")

    def _set_task_progress(self, value):
        try:
            percent = max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            return
        self.progress.setValue(percent)

    def _write_activity_heartbeat(self):
        if self.thread and self.thread.isRunning():
            self.write_log(
                f"[Đang chạy] Tác vụ vẫn đang hoạt động · tiến độ {self.progress.value()}%"
            )

    def _start_pending_when_idle(self):
        """Reliably launch a queued action after the previous QThread exits."""
        if self.pending_task is None:
            return
        try:
            running = bool(self.thread and self.thread.isRunning())
        except RuntimeError:
            running = False
        if running:
            QTimer.singleShot(300, self._start_pending_when_idle)
            return
        self.thread = None
        self.worker = None
        pending = self.pending_task
        self.pending_task = None
        task, args, kwargs = pending
        QTimer.singleShot(0, lambda: self.start_task(task, *args, **kwargs))

    def _task_thread_finished(self):
        finished_thread = self.sender()
        if self.thread is finished_thread or self.thread is None or finished_thread is None:
            self.thread = None
            self.worker = None
        else:
            # A delayed finished signal from the previous task must never
            # consume a pending action belonging to the new task.
            return
        pending = self.pending_task
        self.pending_task = None
        if pending:
            task, args, kwargs = pending
            QTimer.singleShot(0, lambda: self.start_task(task, *args, **kwargs))

    def _release_completed_task(self):
        """Close the settled worker before another pipeline action is used."""
        current = self.thread
        stopped = True
        if current is not None:
            try:
                current.quit()
                stopped = current.wait(2000)
            except RuntimeError:
                stopped = True
        if stopped:
            self.thread = None
            self.worker = None
            pending = self.pending_task
            self.pending_task = None
            if pending:
                task, args, kwargs = pending
                QTimer.singleShot(0, lambda: self.start_task(task, *args, **kwargs))
        elif self.pending_task is not None:
            QTimer.singleShot(300, self._start_pending_when_idle)

    def task_done(self, result):
        if hasattr(self, "_activity_timer"):
            self._activity_timer.stop()
        if hasattr(self, "root") and self.root.exists():
            self._refresh_movie_list()
        self.progress.setValue(100)
        self.status.setText("Hoàn tất")
        self.write_log(f"[V3] Hoàn tất: {result}")
        label = getattr(self, "_active_task_label", "Quy trình")
        self.log_view.clear()
        self.log_view.append(f"[Hoàn tất] {label}: {self.root.name}")
        self.log_view.ensureCursorVisible()
        if self._auto_active_phase is not None:
            finished_phase = self._auto_active_phase
            self._auto_active_phase = None
            if finished_phase == 0:
                self.auto_phase = 1
                self.status.setText("Đã dừng trước tạo SRT; hãy thêm khung OCR cho từng video.")
                self._set_auto_button("Chạy tự động · tiếp tục tạo SRT")
            elif finished_phase == 1:
                self.auto_phase = 2
                self.ocr_region_item.hide()
                self.ocr_region_button.setChecked(False)
                current = self.movies.currentItem()
                if current is not None:
                    self._select_preview_video(current)
                self.status.setText("Đã dừng trước xuất; hãy căn sub Anh và Blur.")
                self._set_auto_button("Chạy tự động · tiếp tục xuất")
            else:
                self.auto_plan = None
                self.auto_phase = 0
                self._set_auto_button("Chạy tự động  ·  0 phim")
        self._release_completed_task()

    def task_failed(self, error):
        if hasattr(self, "_activity_timer"):
            self._activity_timer.stop()
        self.status.setText("Lỗi")
        self.write_log(f"[V3] LỖI: {error}")
        self._write_bug_report(error)
        if self._auto_active_phase is not None:
            self._auto_active_phase = None
            self._set_auto_button("Chạy tự động · thử lại bước hiện tại")
        self._release_completed_task()
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

    def _set_auto_button(self, text):
        if self.auto_button is not None:
            self.auto_button.setText(text)

    def _auto_ocr_regions_ready(self):
        videos = [
            path for path in self.root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".mp4" and "_Export" not in path.stem
        ]
        missing = [
            path.name for path in videos
            if not self.overlay_configs.get(str(path.resolve()), {}).get("ocr_roi")
        ]
        if missing:
            self.status.setText(f"Còn {len(missing)} video chưa có khung OCR")
            QMessageBox.information(
                self, "Thiếu khung OCR",
                "Hãy chọn từng video và vẽ Khung OCR trước khi tiếp tục:\n\n"
                + "\n".join(missing[:12])
                + ("\n..." if len(missing) > 12 else ""),
            )
            return False
        return True

    def _run_auto_phase(self):
        if not self.auto_plan:
            return
        phase = self.auto_phase
        self._auto_active_phase = phase
        self._set_auto_button("Đang chạy tự động..." )
        if phase == 0:
            steps = {
                "download": bool(self.auto_plan.get("download")),
                "concat": bool(self.auto_plan.get("concat")),
                "rename": bool(self.auto_plan.get("rename")),
                "separate": bool(self.auto_plan.get("separate")),
                "srt": False, "translate": False, "mux": False, "export": False,
            }
            if self.pending_download_links and steps["download"]:
                urls, dfn = self.pending_download_links, self.pending_download_dfn
                self.pending_download_links = []
                self.start_task(run_download_and_auto_pipeline, str(self.root), urls, dfn, steps)
            elif any(steps.get(key) for key in ("concat", "rename", "separate")):
                self.start_task(run_auto_pipeline, str(self.root), steps)
            else:
                self._auto_active_phase = None
                self.auto_phase = 1
                self._set_auto_button("Chạy tự động · tiếp tục tạo SRT")
        elif phase == 1:
            if not self._auto_ocr_regions_ready():
                self._auto_active_phase = None
                self._set_auto_button("Chạy tự động · tiếp tục tạo SRT")
                return
            dialog = SrtTranslateProcessDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self._auto_active_phase = None
                self._set_auto_button("Chạy tự động · tiếp tục tạo SRT")
                return
            run_srt, run_translate = dialog.values()
            if not run_srt and not run_translate:
                self._auto_active_phase = None
                self.status.setText("Chưa chọn bước Quét SRT hoặc Dịch SRT")
                self._set_auto_button("Chạy tự động · tiếp tục tạo SRT")
                return
            steps = {"download": False, "separate": False, "srt": run_srt,
                     "translate": run_translate,
                     "mux": False, "export": False}
            regions = {
                path: list(config["ocr_roi"])
                for path, config in self.overlay_configs.items()
                if config.get("ocr_roi")
            }
            self.start_task(run_auto_pipeline, str(self.root), steps, ocr_regions=regions)
        else:
            steps = {"download": False, "separate": False, "srt": False,
                     "translate": False, "mux": False,
                     "export": bool(self.auto_plan.get("export"))}
            if steps["export"]:
                self.start_task(run_auto_pipeline, str(self.root), steps,
                                overlay_configs={path: dict(config) for path, config in self.overlay_configs.items()})
            else:
                self.auto_plan = None
                self._set_auto_button("Chạy tự động  ·  0 phim")

    def auto_run(self):
        if self._auto_active_phase is not None:
            self.status.setText("Tự động đang chạy; hãy đợi bước hiện tại hoàn tất.")
            return
        if self.auto_plan is None:
            dialog = AutoProcessDialog(bool(self.pending_download_links), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self.auto_plan = dialog.values()
            self.auto_phase = 0
        self._run_auto_phase()

    def create_srt(self):
        if self.root == Path.cwd() or not self.root.exists():
            QMessageBox.warning(self, "Chưa chọn thư mục", "Hãy chọn thư mục tổng trước khi tạo SRT.")
            return
        kphoto = (Path(__file__).parent / "models" / "kphoto-local" / "zh" / "zh" / "model.pt").is_file()
        dialog = SrtModelDialog(kphoto, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        engine, source_mode = dialog.values()
        ocr_regions = None
        if engine == "rapidocr-v6":
            ocr_regions = {
                path: list(config["ocr_roi"])
                for path, config in self.overlay_configs.items()
                if config.get("ocr_roi")
            }
        self.start_task(create_srt_batch, str(self.root), engine, source_mode, ocr_regions)

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
        api_key = (os.getenv("GEMINI36_API_KEY", os.getenv("GEMINI_API_KEY", "")) if model == "gemini-3.6-flash-high" else
                   (os.getenv("GEMINI31_API_KEY", os.getenv("GEMINI_API_KEY", "")) if model == "gemini-3.1-flash-lite" else
                   (os.getenv("QWEN_API_KEY", "") if model.startswith("qwen") or model == "hybrid-qwen-gemini" else "")))
        self.start_task(translate_srt_batch, str(self.root), target, model, api_key, source_language=source)

    def mux(self):
        self.start_task(mux_folder, str(self.root))

    def export(self):
        dialog = ExportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # Keep the selected video and current frame visible during export.
        # FFmpeg only reads the source and exporter no longer deletes it, so
        # releasing the QMediaPlayer source is unnecessary and caused a black
        # preview for the entire long-running render.
        self.media_player.pause()
        self.preview_play.setText("▶")
        configs = {path: dict(config) for path, config in self.overlay_configs.items()}
        if dialog.mux_vocal.isChecked():
            self.start_task(mux_and_export, str(self.root), overlay_configs=configs)
        else:
            self.start_task(export_folder, str(self.root), overlay_configs=configs)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet("QMainWindow,QWidget{background:#050505;color:#fff} QPushButton{padding:8px 12px}")
    window = MainWindow()
    screen = QApplication.primaryScreen().availableGeometry()
    width = int(screen.width() * 0.9)
    height = int(screen.height() * 0.9)
    window.resize(width, height)
    window.move(
        screen.left() + (screen.width() - width) // 2,
        screen.top() + (screen.height() - height) // 2,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
