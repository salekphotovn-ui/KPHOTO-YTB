from __future__ import annotations
import json, urllib.request
from config import GITHUB_OWNER, GITHUB_REPO

def latest_release() -> dict | None:
    if not GITHUB_OWNER or not GITHUB_REPO:
        return None
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept":"application/vnd.github+json","User-Agent":"KPHOTO-YTB"})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
