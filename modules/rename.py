"""
Module tự động đặt tên và phân loại các video đã hoàn chỉnh (dựa theo AUTO_NAME.bat):
- File .mp4 lẻ nằm ngay trong thư mục gốc (phim 1 phần) -> tạo thư mục riêng, lưu tên gốc.
- Thư mục con đã có sẵn DONE.mp4 (phim nhiều phần đã ghép ở Tab 3) -> đổi tên thư mục
  và file DONE.mp4 theo cùng quy tắc, lưu tên thư mục gốc (chính là tên phim).
- Thư mục con CHƯA có DONE.mp4 (chưa ghép xong) -> bỏ qua hoàn toàn, không đụng tới.
"""
import os
import shutil
import re
from modules.concat import _natural_sort_key

def _derive_movie_title(filename: str) -> str:
    """
    Trích tên phim thật từ tên file, CHỈ khi khớp đúng mẫu "Tên phim - Tập NN.mp4"
    (mẫu file gốc Hongguo). Không khớp (vd file Bilibili) -> trả về "" để dùng
    tên thư mục như hành vi cũ, không đoán bừa.
    """
    name = os.path.splitext(os.path.basename(filename))[0]
    match = re.match(r"^(.*?)\s*-\s*[Tt][ậa]p\s*\d+\s*$", name)
    if not match:
        return ""
    title = match.group(1).strip()
    return "" if not title or title.lower() == "done" else title


def auto_rename_folder(folder_path: str, log_callback=None) -> list[str]:
    """
    Quét 1 thư mục, tự động đặt tên và phân loại các video đã hoàn chỉnh trong đó.

    :param folder_path: thư mục gốc cần xử lý
    :param log_callback: hàm nhận log dạng str
    :return: danh sách các thư mục con đã được tạo/đổi tên thành công
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    parent_name = os.path.basename(os.path.normpath(folder_path))

    entries = []
    for name in sorted(os.listdir(folder_path), key=str.lower):
        full = os.path.join(folder_path, name)
        if os.path.isfile(full) and name.lower().endswith(".mp4"):
            entries.append(("file", full, name, None))
        elif os.path.isdir(full):
            # Do not process output folders again on a later pipeline pass.
            if re.fullmatch(re.escape(parent_name) + r"\.\d+", name, re.IGNORECASE):
                continue
            mp4s_inside = [f for f in os.listdir(full) if f.lower().endswith(".mp4")]
            done_match = next(
                (
                    f for f in mp4s_inside
                    if f.lower() == "done.mp4"
                    or re.search(r"_(?:ghep|ghép|merged)\.mp4$", f, re.IGNORECASE)
                ),
                None,
            )
            if done_match:
                entries.append(("folder_done", full, name, done_match))
            elif len(mp4s_inside) == 1:
                # Thư mục chỉ có đúng 1 file .mp4 (không phải nhiều phần cần ghép) -> coi như đã hoàn chỉnh
                entries.append(("folder_done", full, name, mp4s_inside[0]))
            elif not mp4s_inside:
                # Thư mục con không có .mp4 nào (vd 'subtitles', thư mục rỗng còn sót)
                # -> không phải phim, bỏ qua thay vì crash ở sorted_mp4s[0].
                _log(f"⏭️ Bỏ qua thư mục '{name}' (không có file .mp4 bên trong).")
            else:
                _log(f"⏭️ Bỏ qua thư mục '{name}' (chưa có DONE.mp4 - cần ghép ở Tab 3 trước).")
                # Vẫn tạo sẵn file .txt 2 dòng (tên thư mục + tên tập 1) để
                # giữ lại thông tin, dù chưa ghép. Sau khi ghép ở Tab 3 và
                # quay lại Tab "Đặt tên", file này sẽ TỰ ĐỘNG bị ghi đè thành
                # bản 1 dòng bình thường (xem phần dọn dẹp ở nhánh folder_done).
                pre_txt_path = os.path.join(full, f"{name}.txt")
                if not os.path.exists(pre_txt_path):
                    sorted_mp4s = sorted(mp4s_inside, key=_natural_sort_key)
                    with open(pre_txt_path, "w", encoding="utf-8") as f:
                        f.write(f"{name}\n{sorted_mp4s[0]}")
                    _log(f"   📝 Đã tạo tạm '{name}.txt' (2 dòng: tên thư mục + tên tập 1).")

    if not entries:
        _log("⚠️ Không tìm thấy file .mp4 lẻ hoặc thư mục đã ghép xong (DONE.mp4) nào để xử lý.")
        return []

    total = len(entries)
    _log(f"[RenameProgress] START total={total}")

    results = []
    existing_numbers = []
    for name in os.listdir(folder_path):
        match = re.fullmatch(re.escape(parent_name) + r"\.(\d+)", name, re.IGNORECASE)
        if match and os.path.isdir(os.path.join(folder_path, name)):
            existing_numbers.append(int(match.group(1)))
    i = max(existing_numbers, default=0) + 1
    for kind, full_path, display_name, source_mp4 in entries:
        new_name = f"{parent_name}.{i}"
        new_dir = os.path.join(folder_path, new_name)
        while os.path.exists(new_dir):
            i += 1
            new_name = f"{parent_name}.{i}"
            new_dir = os.path.join(folder_path, new_name)

        if kind == "file":
            orig = os.path.splitext(display_name)[0]
            ext = os.path.splitext(display_name)[1]
            os.makedirs(new_dir, exist_ok=True)
            txt_path = os.path.join(new_dir, f"{new_name}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(orig)
            new_file_path = os.path.join(new_dir, f"{new_name}{ext}")
            shutil.move(full_path, new_file_path)
            _log(f"[{i}] File lẻ: '{display_name}' -> '{new_name}/{new_name}{ext}'")

        else:  # folder_done - thư mục phim nhiều phần đã ghép xong, hoặc chỉ có 1 file mp4 lẻ
            orig = display_name
            extracted_title = _derive_movie_title(source_mp4)
            if extracted_title:
                orig = extracted_title
            else:
                merged_match = re.match(
                    r"^(.*?)_(?:ghep|ghép|merged)$",
                    os.path.splitext(source_mp4)[0],
                    re.IGNORECASE,
                )
                if merged_match:
                    orig = merged_match.group(1).strip()
            os.rename(full_path, new_dir)
            done_old = os.path.join(new_dir, source_mp4)
            done_new = os.path.join(new_dir, f"{new_name}.mp4")
            os.rename(done_old, done_new)

            if not os.path.isfile(done_new) or os.path.getsize(done_new) == 0:
                raise RuntimeError(f"Video ghép không hợp lệ, không xóa các part nguồn: {done_new}")

            # The merged file has already been created and renamed safely;
            # remove its small source parts and keep only the final MP4.
            for old_video in os.listdir(new_dir):
                old_video_path = os.path.join(new_dir, old_video)
                if (
                    old_video.lower().endswith(".mp4")
                    and os.path.abspath(old_video_path) != os.path.abspath(done_new)
                ):
                    try:
                        os.remove(old_video_path)
                        _log(f"[Rename] Đã xoá part cũ: '{old_video}'")
                    except OSError as exc:
                        _log(f"[Rename] Không xoá được part '{old_video}': {exc}")

            # Dọn mọi file .txt cũ còn sót lại trong thư mục (vd bản 2 dòng
            # được tạo tạm lúc thư mục còn nhiều tập chưa ghép), tránh còn
            # 2 file .txt cùng lúc sau khi đã ghép xong.
            for old_txt in os.listdir(new_dir):
                if old_txt.lower().endswith(".txt"):
                    try:
                        os.remove(os.path.join(new_dir, old_txt))
                    except OSError:
                        pass

            txt_path = os.path.join(new_dir, f"{new_name}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(orig)
            _log(f"[{i}] Thư mục đã ghép: '{display_name}' -> '{new_name}/{new_name}.mp4'")

        results.append(new_dir)
        _log(f"[RenameProgress] {i}/{total}")
        i += 1

    _log(f"\n✅ Đã xử lý xong {len(results)} video.")
    return results
