"""Runtime paths and release configuration for Bili2YT V3."""

from __future__ import annotations

import sys
import os
import json
from pathlib import Path


APP_NAME = "KPHOTO-YTB"
VERSION = "0.3.6"
# Release tag that hosts the large, rarely-changing bootstrap payloads
# (bin / models / runtime). The versioned executable update is published
# separately as KPHOTO-YTB_update.zip on the release matching VERSION.
RUNTIME_RELEASE_TAG = "v0.3.4"


def load_local_provider_config() -> None:
    """Load optional machine-local API settings without committing secrets."""
    module_dir = Path(__file__).resolve().parent
    # Source run: next to config.py. Frozen run: config.py lives inside
    # _internal, so also accept the file dropped next to the executable or in
    # _internal itself, whichever the operator used when shipping the app.
    candidates = [
        module_dir / "config.local.json",
        module_dir.parent / "config.local.json",
    ]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent / "config.local.json")
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        # An explicit config.local.json is the shared, persistent configuration
        # selected by the user. It must win over stale variables inherited from
        # a PowerShell window. Translation uses Gemini only:
        #   - "Gemini 3.6 Flash-High" -> gemini_base_url + gemini_api_key
        #     (an OpenAI-compatible third-party endpoint)
        #   - "Gemini" -> Google's official API, gemini_api_key
        gemini_key = (
            data.get("gemini_api_key")
            or data.get("gemini36_api_key")
            or data.get("gemini31_api_key")
            or data.get("qwen_api_key")
        )
        if gemini_key:
            os.environ["GEMINI_API_KEY"] = str(gemini_key)
        base_url = data.get("gemini_base_url") or data.get("qwen_base_url")
        if base_url:
            os.environ["GEMINI_BASE_URL"] = str(base_url)
        # Proxy model name for "Gemini 3.6 Flash-High"; defaults to
        # gemini-3.6-flash-high in translator.py when unset.
        if data.get("gemini_model"):
            os.environ["GEMINI_MODEL"] = str(data["gemini_model"])

# GitHub repository that owns the dedicated V3 releases.
GITHUB_OWNER = "salekphotovn-ui"
GITHUB_REPO = "KPHOTO-YTB"
GITHUB_ASSET_NAME = "KPHOTO-YTB_update.zip"
# Direct first-run resource bundles published with the application release.
REMOTE_BIN_URL = "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.1.0/KPHOTO-YTB_bin.zip"
REMOTE_MODELS_URL = "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.1.0/KPHOTO-YTB_models.zip"
REMOTE_RUNTIME_URL = "https://github.com/salekphotovn-ui/KPHOTO-YTB/releases/download/v0.1.0/KPHOTO-YTB_runtime_core.zip"


def app_dir() -> Path:
    """Return the writable application directory for source or frozen mode."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


load_local_provider_config()


BIN_DIR = app_dir() / "bin"
FFMPEG_PATH = str(BIN_DIR / "ffmpeg.exe")
BBDOWN_PATH = str(BIN_DIR / "BBDown.exe")
ARIA2_PATH = str(BIN_DIR / "aria2c.exe")
WORK_DIR = app_dir() / "workspace"
DOWNLOAD_DIR = str(WORK_DIR / "1_downloaded")
DEFAULT_DFN_PRIORITY = "720P 高清, 720P"
# Default subtitle model selected in the V3 UI and automatic pipeline.
TRANSLATOR_MODEL = "gemini-3.6-flash-high"
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
    "bin/aria2c.exe",
    "bin/ffmpeg.exe",
    "workspace",
    "token.json",
    "client_secrets.json",
    "config.local.json",
}
