from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


PACKAGES = (
    ("Ứng dụng", "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.3.2/KPHOTO-YTB_update.zip"),
    ("FFmpeg và BBDown", "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.3.2/KPHOTO-YTB_bin.zip"),
    ("Models", "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.3.2/KPHOTO-YTB_models.zip"),
    ("Runtime cơ bản", "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.3.2/KPHOTO-YTB_runtime_core.zip"),
    ("Runtime CUDA 1/2", "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.3.2/KPHOTO-YTB_runtime_cuda_a.zip"),
    ("Runtime CUDA 2/2", "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.3.2/KPHOTO-YTB_runtime_cuda_b.zip"),
)


def download(url: str, target: Path, label: str) -> None:
    # Windows curl uses the machine certificate store (Schannel), avoiding
    # CERTIFICATE_VERIFY_FAILED on clean employee PCs where the embedded
    # Python/OpenSSL bundle cannot locate the local issuer certificate.
    command = [
        "curl.exe", "-L", "--fail", "--retry", "5",
        "--retry-delay", "2", "--connect-timeout", "30",
        "--user-agent", "KPHOTO-YTB-Setup", "--output", str(target), url,
    ]
    result = subprocess.run(command, check=False)
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(f"Không tải được {label} (curl={result.returncode})")


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
    exe_path = str(app_exe).replace("'", "''")
    work_path = str(app_exe.parent).replace("'", "''")
    script = (
        "$desktop=[Environment]::GetFolderPath('Desktop');"
        "$shell=New-Object -ComObject WScript.Shell;"
        "$shortcut=$shell.CreateShortcut((Join-Path $desktop 'KPHOTO-YTB.lnk'));"
        f"$shortcut.TargetPath='{exe_path}';"
        f"$shortcut.WorkingDirectory='{work_path}';"
        f"$shortcut.IconLocation='{exe_path},0';"
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
