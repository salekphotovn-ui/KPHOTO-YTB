from __future__ import annotations
import json, tempfile, zipfile, subprocess, os, sys
from config import GITHUB_OWNER, GITHUB_REPO

# Windows curl uses the machine certificate store (Schannel), avoiding
# CERTIFICATE_VERIFY_FAILED on clean employee PCs where the embedded
# Python/OpenSSL bundle cannot locate the local issuer certificate.
_CURL = [
    "curl.exe", "-L", "--fail", "--retry", "3", "--retry-delay", "2",
    "--connect-timeout", "30", "--user-agent", "KPHOTO-YTB",
]
_NO_WINDOW = 0x08000000


def _curl_to_file(url: str, target: str, timeout: int) -> bool:
    try:
        result = subprocess.run(
            _CURL + ["--max-time", str(timeout), "--output", target, url],
            check=False, creationflags=_NO_WINDOW,
        )
    except Exception:
        return False
    return (
        result.returncode == 0
        and os.path.isfile(target)
        and os.path.getsize(target) > 0
    )


def latest_release() -> dict | None:
    if not GITHUB_OWNER or not GITHUB_REPO:
        return None
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    temp_dir = tempfile.mkdtemp(prefix="kphoto_relcheck_")
    payload = os.path.join(temp_dir, "release.json")
    try:
        if not _curl_to_file(url, payload, timeout=15):
            return None
        with open(payload, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
    finally:
        try:
            os.remove(payload)
            os.rmdir(temp_dir)
        except OSError:
            pass


def download_and_install(asset_url: str, asset_name: str = "KPHOTO-YTB_update.zip") -> bool:
    """Download a release zip and replace the frozen app after it exits."""
    if not getattr(sys, "frozen", False):
        return False
    app_dir = os.path.dirname(sys.executable)
    temp_dir = tempfile.mkdtemp(prefix="kphoto_update_")
    archive = os.path.join(temp_dir, asset_name)
    try:
        if not _curl_to_file(asset_url, archive, timeout=600):
            return False
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
            f.write("@echo off\r\n")
            f.write("timeout /t 2 /nobreak >nul\r\n")
            f.write(f'copy /Y "{sys.executable}" "{sys.executable}.bak" >nul\r\n')
            f.write("set /a attempt=0\r\n")
            f.write(":copyloop\r\n")
            f.write(f'xcopy /E /I /Y "{extract_dir}\\*" "{app_dir}\\" >nul && goto launch\r\n')
            f.write("set /a attempt+=1\r\n")
            f.write("if %attempt% lss 5 ( timeout /t 2 /nobreak >nul & goto copyloop )\r\n")
            f.write(":launch\r\n")
            f.write(f'start "" "{sys.executable}"\r\n')
        subprocess.Popen(["cmd.exe", "/c", script], creationflags=_NO_WINDOW)
        return True
    except Exception:
        return False
