from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


# KPHOTO-YTB_update.zip is the full app (exe + _internal minus torch + bin) and
# lives on APP_TAG; the auto-updater ships every fix through it. Only the
# never-changing torch and models stay on RUNTIME_TAG.
APP_TAG = "v0.3.10"
RUNTIME_TAG = "v0.3.4"
_BASE = "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download"

# Required packages, always fetched.
PACKAGES = (
    ("Ứng dụng", f"{_BASE}/{APP_TAG}/KPHOTO-YTB_update.zip"),
    ("Models", f"{_BASE}/{RUNTIME_TAG}/KPHOTO-YTB_models.zip"),
)
# Torch is split into KPHOTO-YTB_runtime_cuda_<a..>.zip; the packager decides how
# many parts fit under GitHub's 2 GB asset cap, so fetch a..f and stop at the
# first one that is missing.
CUDA_PART_LETTERS = "abcdef"


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


def try_download(url: str, target: Path, label: str) -> bool:
    """Download an optional asset; return False (no raise) if it is absent."""
    try:
        download(url, target, label)
        return True
    except RuntimeError:
        if target.exists():
            target.unlink()
        return False


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


def place_local_config(install_dir: Path) -> None:
    """Copy a config.local.json shipped beside the installer into the app dir."""
    import shutil

    destination = install_dir / "config.local.json"
    sources = [
        Path(sys.executable).resolve().parent / "config.local.json",
        Path(getattr(sys, "_MEIPASS", "")) / "config.local.json",
        Path.cwd() / "config.local.json",
    ]
    for source in sources:
        try:
            if not source.is_file() or source.resolve() == destination.resolve():
                continue
            shutil.copy2(source, destination)
            print("Đã sao chép config.local.json vào thư mục cài đặt.", flush=True)
            return
        except OSError:
            continue


def main() -> int:
    install_dir = Path(sys.executable).resolve().parent
    app_exe = install_dir / "KPHOTO-YTB.exe"
    required = (app_exe, install_dir / "_internal", install_dir / "bin", install_dir / "models")
    if all(path.exists() for path in required):
        place_local_config(install_dir)
        create_desktop_shortcut(app_exe)
        subprocess.Popen([str(app_exe)], cwd=str(install_dir))
        return 0

    print("KPHOTO-YTB - Cài đặt lần đầu", flush=True)
    print("Không đóng cửa sổ này trong lúc tải và giải nén.", flush=True)
    with tempfile.TemporaryDirectory(prefix="kphoto_setup_") as temp:
        temp_dir = Path(temp)
        index = 0
        for label, url in PACKAGES:
            index += 1
            archive = temp_dir / f"package_{index}.zip"
            print(f"\nĐang tải {label}...", flush=True)
            download(url, archive, label)
            if not zipfile.is_zipfile(archive):
                raise RuntimeError(f"Link {label} không trả về file ZIP trực tiếp")
            print(f"Đang giải nén {label}...", flush=True)
            safe_extract(archive, install_dir)

        for order, letter in enumerate(CUDA_PART_LETTERS, 1):
            url = f"{_BASE}/{RUNTIME_TAG}/KPHOTO-YTB_runtime_cuda_{letter}.zip"
            index += 1
            archive = temp_dir / f"package_{index}.zip"
            label = f"Runtime CUDA phần {order}"
            print(f"\nĐang tải {label}...", flush=True)
            if not try_download(url, archive, label):
                if order == 1:
                    raise RuntimeError("Thiếu gói Runtime CUDA (cuda_a).")
                break
            if not zipfile.is_zipfile(archive):
                raise RuntimeError(f"Link {label} không trả về file ZIP trực tiếp")
            print(f"Đang giải nén {label}...", flush=True)
            safe_extract(archive, install_dir)

    if not app_exe.exists():
        raise RuntimeError("Không tìm thấy KPHOTO-YTB.exe sau khi cài đặt")
    place_local_config(install_dir)
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
