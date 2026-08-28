from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


PACKAGES = (
    ("Ứng dụng", "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.1.0/KPHOTO-YTB_update.zip"),
    ("FFmpeg và BBDown", "http://gofile.me/4PS53/HDi0OPcwW"),
    ("Models", "http://gofile.me/4PS53/AkkezfdHw"),
    ("Runtime", "http://gofile.me/4PS53/jkbfv7qut"),
)


def download(url: str, target: Path, label: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "KPHOTO-YTB-Setup"})
    with urllib.request.urlopen(request, timeout=90) as response, target.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        last_percent = -1
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            received += len(chunk)
            if total:
                percent = int(received * 100 / total)
                if percent != last_percent and (percent % 5 == 0 or percent == 100):
                    print(f"[{label}] {percent}%", flush=True)
                    last_percent = percent
            else:
                print(f"[{label}] {received / 1024 / 1024:.1f} MB", end="\r", flush=True)


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = str(destination.resolve()) + os.sep
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            target = str((destination / member.filename).resolve())
            if not target.startswith(base):
                raise RuntimeError("Gói tải xuống chứa đường dẫn không an toàn")
        zipped.extractall(destination)


def create_desktop_shortcut(app_exe: Path) -> None:
    script = (
        "$desktop=[Environment]::GetFolderPath('Desktop');"
        "$shell=New-Object -ComObject WScript.Shell;"
        "$shortcut=$shell.CreateShortcut((Join-Path $desktop 'KPHOTO-YTB.lnk'));"
        f"$shortcut.TargetPath='{str(app_exe).replace("'", "''")}';"
        f"$shortcut.WorkingDirectory='{str(app_exe.parent).replace("'", "''")}';"
        f"$shortcut.IconLocation='{str(app_exe).replace("'", "''")},0';"
        "$shortcut.Save()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False, creationflags=0x08000000,
    )


def main() -> int:
    install_dir = Path(sys.executable).resolve().parent
    app_exe = install_dir / "KPHOTO-YTB.exe"
    required = (app_exe, install_dir / "_internal", install_dir / "bin", install_dir / "models")
    if all(path.exists() for path in required):
        create_desktop_shortcut(app_exe)
        subprocess.Popen([str(app_exe)], cwd=str(install_dir))
        return 0

    print("KPHOTO-YTB - Cài đặt lần đầu", flush=True)
    print("Không đóng cửa sổ này trong lúc tải và giải nén.", flush=True)
    with tempfile.TemporaryDirectory(prefix="kphoto_setup_") as temp:
        temp_dir = Path(temp)
        for index, (label, url) in enumerate(PACKAGES, 1):
            archive = temp_dir / f"package_{index}.zip"
            print(f"\nĐang tải {label}...", flush=True)
            download(url, archive, label)
            if not zipfile.is_zipfile(archive):
                raise RuntimeError(f"Link {label} không trả về file ZIP trực tiếp")
            print(f"Đang giải nén {label}...", flush=True)
            safe_extract(archive, install_dir)

    if not app_exe.exists():
        raise RuntimeError("Không tìm thấy KPHOTO-YTB.exe sau khi cài đặt")
    create_desktop_shortcut(app_exe)
    print("Cài đặt hoàn tất. Đang mở KPHOTO-YTB...", flush=True)
    subprocess.Popen([str(app_exe)], cwd=str(install_dir))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nLỖI CÀI ĐẶT: {exc}", flush=True)
        input("Nhấn Enter để đóng...")
        raise SystemExit(1)
