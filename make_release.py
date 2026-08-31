"""Build KPHOTO-YTB and package every GitHub release asset for one version.

Run from the repo root with the project venv:

    venv\\Scripts\\python.exe make_release.py

Steps:
  1. PyInstaller build of KPHOTO-YTB.spec and KPHOTO-YTB_Setup.spec
  2. Repackage release_assets_<VERSION>/:
       KPHOTO-YTB_update.zip          -> dist/KPHOTO-YTB/KPHOTO-YTB.exe (at zip root)
       KPHOTO-YTB_bin.zip             -> ./bin/
       KPHOTO-YTB_models.zip          -> ./models/
       KPHOTO-YTB_runtime_core.zip    -> dist/KPHOTO-YTB/_internal/ minus torch/
       KPHOTO-YTB_runtime_cuda_a..    -> dist/KPHOTO-YTB/_internal/torch/ split < 1.8 GiB
  3. Print the `gh release create` command.

Nothing here uploads anything; the final gh command is run by hand.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_APP = ROOT / "dist" / "KPHOTO-YTB"
INTERNAL = DIST_APP / "_internal"
PART_LIMIT = int(1.9 * 1024 ** 3)          # keep each asset under GitHub's 2 GiB cap
GH_ASSET_CAP = 2 * 1024 ** 3

sys.path.insert(0, str(ROOT))
from config import VERSION  # noqa: E402

OUT = ROOT / f"release_assets_v{VERSION}"


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"Lệnh thất bại ({result.returncode}): {' '.join(cmd)}")


def build() -> None:
    pyinstaller = ROOT / "venv" / "Scripts" / "pyinstaller.exe"
    if not pyinstaller.is_file():
        raise SystemExit(f"Không thấy {pyinstaller}")
    for spec in ("KPHOTO-YTB.spec", "KPHOTO-YTB_Setup.spec"):
        run([str(pyinstaller), "--noconfirm", "--clean", spec])
    if not (DIST_APP / "KPHOTO-YTB.exe").is_file():
        raise SystemExit("Build không tạo ra dist/KPHOTO-YTB/KPHOTO-YTB.exe")


def _zip_files(out_zip: Path, items: list[tuple[Path, str]]) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for src, arcname in items:
            zf.write(src, arcname)
    size = out_zip.stat().st_size
    flag = "  !! OVER 2 GiB" if size > GH_ASSET_CAP else ""
    print(f"  {out_zip.name:32s} {size / 1024 ** 3:6.2f} GiB{flag}", flush=True)


def _tree(base: Path, arc_base: str, skip_top: set[str] | None = None):
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if skip_top and rel.parts and rel.parts[0] in skip_top:
            continue
        yield path, f"{arc_base}/{rel.as_posix()}"


def package(minimal: bool = False) -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    _zip_files(OUT / "KPHOTO-YTB_update.zip",
               [(DIST_APP / "KPHOTO-YTB.exe", "KPHOTO-YTB.exe")])

    _zip_files(OUT / "KPHOTO-YTB_bin.zip", list(_tree(ROOT / "bin", "bin")))

    if minimal:
        # Only the exe + bin changed; reuse the existing runtime/model assets.
        shutil.copy2(DIST_APP.parent / "KPHOTO-YTB_Setup.exe", OUT / "KPHOTO-YTB_Setup.exe")
        print("  (minimal: bỏ qua models/runtime/cuda)")
        return

    _zip_files(OUT / "KPHOTO-YTB_models.zip", list(_tree(ROOT / "models", "models")))

    _zip_files(OUT / "KPHOTO-YTB_runtime_core.zip",
               list(_tree(INTERNAL, "_internal", skip_top={"torch"})))

    torch_dir = INTERNAL / "torch"
    files = [p for p in sorted(torch_dir.rglob("*")) if p.is_file()]
    parts: list[list[Path]] = [[]]
    running = 0
    for path in files:
        size = path.stat().st_size
        if size > PART_LIMIT:
            raise SystemExit(f"File torch quá lớn cho một phần: {path} ({size})")
        if running + size > PART_LIMIT and parts[-1]:
            parts.append([])
            running = 0
        parts[-1].append(path)
        running += size
    letters = "abcdef"
    if len(parts) > len(letters):
        raise SystemExit(f"torch cần {len(parts)} phần, vượt {len(letters)}")
    for letter, chunk in zip(letters, parts):
        items = [(p, f"_internal/torch/{p.relative_to(torch_dir).as_posix()}") for p in chunk]
        _zip_files(OUT / f"KPHOTO-YTB_runtime_cuda_{letter}.zip", items)

    shutil.copy2(DIST_APP.parent / "KPHOTO-YTB_Setup.exe", OUT / "KPHOTO-YTB_Setup.exe")
    print(f"  {'KPHOTO-YTB_Setup.exe':32s} "
          f"{(OUT / 'KPHOTO-YTB_Setup.exe').stat().st_size / 1024 ** 2:6.1f} MiB")


def main() -> int:
    minimal = "--minimal" in sys.argv
    if "--package-only" not in sys.argv:
        build()
    package(minimal=minimal)
    assets = " ".join(f'"{p.name}"' for p in sorted(OUT.glob("*")))
    print("\nTiếp theo, chạy trong thư mục", OUT)
    if minimal:
        print(f'\n  gh release upload v{VERSION} {assets} --clobber\n')
        print("  (nếu chưa có release v" + VERSION + " thì đổi 'upload' -> "
              "'create ... --target main --title ...')")
    else:
        print(f'\n  gh release create v{VERSION} --target main '
              f'--title "KPHOTO-YTB v{VERSION}" --notes "..." {assets}\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
