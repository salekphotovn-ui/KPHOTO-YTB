from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMainWindow,
    QMessageBox, QPushButton, QComboBox, QTextEdit, QVBoxLayout, QWidget,
)

from modules.separator import separate_folder
from modules.srt import create_srt_batch
from modules.translator import translate_srt_batch


class TaskWorker(QObject):
    log = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, task, *args):
        super().__init__()
        self.task, self.args = task, args

    def run(self):
        try:
            result = self.task(*self.args, log_callback=self.log.emit)
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.root = Path.cwd()
        self.thread = None
        self.worker = None
        self.setWindowTitle("Bili2YT - Video Workspace / V3")
        self.resize(1100, 720)
        self._build_ui()

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
            ("Tách vocal", self.separate),
            ("Tạo SRT", self.create_srt),
            ("Dịch Google Web", self.translate),
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

    def write_log(self, message):
        self.log_view.append(str(message))
        self.log_view.ensureCursorVisible()

    def start_task(self, task, *args):
        if self.thread and self.thread.isRunning():
            QMessageBox.information(self, "Đang xử lý", "Một tác vụ khác đang chạy.")
            return
        self.thread = QThread(self)
        self.worker = TaskWorker(task, *args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.write_log)
        self.worker.done.connect(self.task_done)
        self.worker.failed.connect(self.task_failed)
        self.worker.done.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.status.setText("Đang xử lý...")

    def task_done(self, result):
        self.status.setText("Hoàn tất")
        self.write_log(f"[V3] Hoàn tất: {result}")

    def task_failed(self, error):
        self.status.setText("Lỗi")
        self.write_log(f"[V3] LỖI: {error}")
        QMessageBox.critical(self, "Lỗi pipeline", error)

    def separate(self):
        self.start_task(separate_folder, str(self.root))

    def create_srt(self):
        self.start_task(create_srt_batch, str(self.root), self.engine.currentData())

    def translate(self):
        self.start_task(translate_srt_batch, str(self.root), self.target.currentData(), "google-web", "")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet("QMainWindow,QWidget{background:#050505;color:#fff} QPushButton{padding:8px 12px}")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
