from __future__ import annotations
import json, urllib.request, tempfile, zipfile, subprocess, os, sys
from config import GITHUB_OWNER, GITHUB_REPO

def latest_release() -> dict | None:
    if not GITHUB_OWNER or not GITHUB_REPO:
        return None

def download_and_install(asset_url: str, asset_name: str = "KPHOTO-YTB_update.zip") -> bool:
    """Download a release zip and replace the frozen app after it exits."""
    if not getattr(sys, "frozen", False):
        return False
    app_dir = os.path.dirname(sys.executable)
    temp_dir = tempfile.mkdtemp(prefix="kphoto_update_")
    archive = os.path.join(temp_dir, asset_name)
    try:
        req = urllib.request.Request(asset_url, headers={"User-Agent":"KPHOTO-YTB"})
        with urllib.request.urlopen(req, timeout=60) as src, open(archive, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk: break
                dst.write(chunk)
        extract_dir = os.path.join(temp_dir, "new")
        with zipfile.ZipFile(archive) as zf:
            base = os.path.abspath(extract_dir) + os.sep
            for member in zf.infolist():
                target = os.path.abspath(os.path.join(extract_dir, member.filename))
                if not target.startswith(base):
                    raise ValueError("Invalid update archive path")
            zf.extractall(extract_dir)
        new_exe = os.path.join(extract_dir, os.path.basename(sys.executable))
        if not os.path.isfile(new_exe):
            raise ValueError("Update archive does not contain KPHOTO-YTB.exe")
        script = os.path.join(temp_dir, "apply_update.bat")
        with open(script, "w", encoding="utf-8") as f:
            f.write("@echo off\r\ntimeout /t 2 /nobreak >nul\r\n")
            f.write(f'copy /Y "{sys.executable}" "{sys.executable}.bak" >nul\r\n')
            f.write(f'xcopy /E /I /Y "{extract_dir}\\*" "{app_dir}\\" >nul\r\n')
            f.write(f'start "" "{sys.executable}"\r\n')
        subprocess.Popen(["cmd.exe", "/c", script], creationflags=0x08000000)
        return True
    except Exception:
        return False
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept":"application/vnd.github+json","User-Agent":"KPHOTO-YTB"})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
