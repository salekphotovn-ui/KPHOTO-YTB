"""Hide child-process console windows in the frozen Windows GUI build.

main.py is packaged with ``console=False``. Every console helper it then
spawns (BBDown, ffmpeg, audio-separator, yt-dlp, ...) would otherwise flash
or keep its own black console window, because Windows gives a console-less
parent's console children a brand new window unless ``CREATE_NO_WINDOW`` is
passed. Rather than thread that flag through ~36 call sites, patch
``subprocess.Popen`` once at startup.
"""

from __future__ import annotations

import subprocess
import sys

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010
_DETACHED_PROCESS = 0x00000008


def install() -> None:
    """Force CREATE_NO_WINDOW on child processes of the frozen Windows app."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    original_init = subprocess.Popen.__init__
    if getattr(original_init, "_kphoto_no_console", False):
        return

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        flags = kwargs.get("creationflags", 0)
        # Respect deliberate console requests, e.g. the BBDown QR-login window.
        if not flags & (_CREATE_NEW_CONSOLE | _DETACHED_PROCESS):
            kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
        original_init(self, *args, **kwargs)

    patched_init._kphoto_no_console = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = patched_init  # type: ignore[assignment]
