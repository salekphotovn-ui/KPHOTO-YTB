"""Runtime paths and release configuration for Bili2YT V3."""

from __future__ import annotations

import sys
import os
import json
from pathlib import Path


APP_NAME = "Bili2YT V3"
VERSION = "0.1.0"


def load_local_provider_config() -> None:
    """Load optional machine-local API settings without committing secrets."""
    path = Path(__file__).resolve().parent / "config.local.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        if data.get("qwen_api_key"):
            os.environ.setdefault("QWEN_API_KEY", str(data["qwen_api_key"]))
        if data.get("qwen_base_url"):
            os.environ.setdefault("QWEN_BASE_URL", str(data["qwen_base_url"]))
        if data.get("qwen_model"):
            os.environ.setdefault("QWEN_MODEL", str(data["qwen_model"]))
        if data.get("gemini_api_key"):
            os.environ.setdefault("GEMINI_API_KEY", str(data["gemini_api_key"]))

# GitHub repository that owns the dedicated V3 releases.
GITHUB_OWNER = ""
GITHUB_REPO = ""
GITHUB_ASSET_NAME = "Bili2YT_V3_update.zip"


def app_dir() -> Path:
    """Return the writable application directory for source or frozen mode."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


load_local_provider_config()


BIN_DIR = app_dir() / "bin"
BBDOWN_PATH = str(BIN_DIR / "BBDown.exe")
FFMPEG_PATH = str(BIN_DIR / "ffmpeg.exe")
WORK_DIR = app_dir() / "workspace"
DOWNLOAD_DIR = str(WORK_DIR / "1_downloaded")
DEFAULT_DFN_PRIORITY = "720P 高清, 720P"
# Free web translator used by the V3 UI unless an AI model is selected.
TRANSLATOR_MODEL = "google-web"
# Default model used by the legacy tool.
SEPARATOR_MODEL = "htdemucs.yaml"
LONG_VIDEO_THRESHOLD_SECONDS = 2 * 3600


def resource_dir() -> Path:
    """Return bundled read-only resources in a PyInstaller build."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    return Path(bundle_dir) if bundle_dir else app_dir()


PRESERVE_PATHS = {
    "venv",
    "bin/BBDown.exe",
    "bin/BBDown.data",
    "bin/ffmpeg.exe",
    "workspace",
    "token.json",
    "client_secrets.json",
    "config.local.json",
}
