"""Python 3.12 worker for speech-to-text model loading."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from srt import _run_whisper_v3

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: srt_runtime.py <engine> <audio>", file=sys.stderr)
        return 2
    _engine, audio = sys.argv[1], Path(sys.argv[2])
    log = lambda message: print(message, file=sys.stderr)
    # faster-whisper/tqdm writes progress directly to stdout; keep the IPC
    # channel JSON-only.
    with redirect_stdout(sys.stderr):
        result = _run_whisper_v3(audio, log)
    print(json.dumps(result, ensure_ascii=False))
    return 0


raise SystemExit(main())
