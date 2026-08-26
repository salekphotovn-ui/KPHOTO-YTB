# Bili2YT V3 - DEV STATUS

## Mục tiêu

V3 là bản source-based mới, chạy trực tiếp bằng Python 3.12 và không phụ thuộc vào bytecode Python 3.11 của V2.

## Trạng thái hiện tại

- Đã tạo project độc lập tại `Bili2YT_V3`.
- Đã khởi tạo Git repository riêng.
- Đã tạo giao diện PyQt6 bằng source `main.py`.
- Đã nối worker/thread và nhật ký realtime.
- Đã đặt Google Translate Web làm model dịch mặc định trong luồng V3.
- Đã giữ `htdemucs.yaml` làm model tách vocal mặc định.
- Đang cài dependencies vào `Bili2YT_V3/venv` bằng Python 3.12.
- Đã tích hợp Qwen3.7-Plus qua API OpenAI-compatible (`QWEN_BASE_URL`).
- Đã hỗ trợ cấu hình máy-local bằng `config.local.json` (được loại khỏi Git).
- Đã thêm log tiến độ dịch theo số cue và ước tính chi phí theo bảng giá Qwen.
- Đã cập nhật prompt dịch theo phong cách tiếng Anh bản địa, ưu tiên ngữ cảnh và sắc thái nhân vật.
- Hybrid đã được đặt làm model dịch mặc định: Qwen3.8-Max dịch chính, Gemini 3.6 Flash-High sửa cue nghi ngờ.
- Hộp thoại dịch luôn chọn sẵn radio Hybrid cuối danh sách; `QWEN_MODEL` không còn ghi đè lựa chọn mặc định trên giao diện.
- Hybrid dùng hai key riêng trong `config.local.json`: `qwen_api_key` và `gemini36_api_key`.
- Đã thêm tính chi phí theo model, QA ký tự Trung/cue dài và retry batch để tránh phát sinh request hàng loạt.
- Đã thêm xem trước video khi chọn phim: phát/tạm dừng, kéo timeline và hiển thị đúng phụ đề `subtitles/en.srt` theo thời gian.
- Trình xem trước chỉ đọc chính xác `subtitles/en.srt`; các tên sai như `en..srt` bị bỏ qua.
- Khi chọn phim, trình phát giải mã im lặng khung hình đầu tiên rồi tự dừng để khung xem không còn màu đen.
- Video và phụ đề được vẽ chung bằng `QGraphicsVideoItem`, tránh lớp video Windows che mất chữ tiếng Anh.
- Sub tiếng Anh không có nền, mặc định nằm sát phía trên sub Trung đã cháy vào hình và có thể giữ chuột kéo trực tiếp để đổi vị trí.
- Vị trí mặc định được tính theo vùng hình 16:9 thực tế (86% chiều cao), không theo vùng letterbox đen; chọn phim khác sẽ đặt lại vị trí chuẩn.
- Vị trí sub kéo trên preview được lưu theo từng video dưới dạng tỷ lệ khung hình và truyền vào FFmpeg khi xuất; `overlay_configs` cũng là nguồn chung cho Blur/Logo.
- Trước khi xuất, preview giải phóng file media; exporter chỉ tạo `_Export.mp4` và luôn giữ video nguồn, vocal, SRT để tránh WinError 32 và mất dữ liệu.
- Sub xem trước được đồng bộ theo timer 50 ms; thao tác kéo dùng proxy chuột riêng để hoạt động ổn định trên Windows.
- Bước tạo SRT tự đo thời lượng video/vocal và co timestamp theo tỷ lệ để loại bỏ drift tích lũy do WAV vocal dài hơn nguồn.
- Hộp Tạo SRT cho chọn nguồn MP4 gốc hoặc file `(Vocals)`; cả Whisper V3 và KPHOTO-Local dùng được FLAC mono 16 kHz, với `aresample=async` khóa audio gốc theo PTS video.
- KPHOTO dùng chunk 10 phút cho audio dài, cache model CUDA giữa các chunk và hiển thị log nguồn/model/tiến độ để tránh `generate()` 33 phút đứng im.
- Whisper V3 ép `zh`, bật word timestamps và chống hallucination; cue được chia theo dấu câu, tối đa khoảng 5,5 giây/18 ký tự để loại cue treo 15-74 giây.
- Nếu bấm chức năng khác khi worker đang hoàn tất, V3 xếp một tác vụ chờ và tự chạy sau khi thread hiện tại đóng thay vì báo lỗi chung.

## Pipeline V3

Luồng tự động:

1. Tách vocal bằng `htdemucs.yaml`.
2. Tạo phụ đề bằng Whisper V3.
3. Dịch Trung - Anh bằng Google Translate Web theo nhóm 10 cue.
4. Ghép vocal vào video.
5. Xuất video.

Các bước cũng có thể chạy riêng từ giao diện.

## File quan trọng

- `main.py`: giao diện và điều phối pipeline V3.
- `modules/separator.py`: tách vocal và xử lý video dài.
- `modules/srt.py`: Whisper V3/KPHOTO tạo SRT.
- `modules/translator.py`: Google Web và Gemini fallback/nâng cao.
- `config.local.json`: cấu hình API cục bộ, không commit (tạo riêng trên từng máy).
- `modules/muxer.py`: ghép vocal vào video.
- `modules/exporter.py`: xuất video.
- `run.bat`: khởi chạy bằng Python 3.12.

## Git checkpoints

- `cc48f83`: khởi tạo workspace V3 source-based.
- `40bdf9e`: thêm launcher Python 3.12.
- `c572989`: nối pipeline xử lý và luồng tự động.

Mỗi thay đổi chức năng mới phải được kiểm tra và commit riêng. Không sửa trực tiếp V2 khi đang phát triển V3.

## Việc cần test

- Mở giao diện bằng `run.bat` sau khi cài dependency.
- Chọn thư mục có video MP4.
- Test riêng tách vocal, Whisper V3 và dịch Google Web.
- Test ghép vocal và xuất video.
- Test luồng tự động với một video ngắn trước.
- Kiểm tra log realtime và file đầu ra.
- Test trên máy sạch trước khi đóng gói cho nhân viên.
- Kiểm tra Qwen3.7-Plus với SRT dài, tiến độ và chi phí trong nhật ký.
- Kiểm tra QA ký tự Trung còn sót, cue thiếu và tên riêng không nhất quán.
- Kiểm tra Hybrid trên SRT dài: xác nhận Qwen dịch chính, Gemini sửa chọn lọc và log chi phí.
- Mở giao diện và kiểm tra hình/âm thanh, kéo timeline, đổi qua lại nhiều phim và phụ đề tiếng Anh tương ứng.
- Whisper video dài dùng chunk WAV PCM mono 16 kHz và xóa từng chunk sau xử lý để tránh lỗi encoder FLAC `invalid block size`.
- Ghép Whisper video dài chỉ giữ vùng lõi mỗi chunk; overlap 8 giây chỉ làm ngữ cảnh, không tạo cue trùng ở mốc 30 phút.
- Thêm PP-OCRv6 Small qua RapidOCR/ONNX để tạo `zh.srt` trực tiếp từ phụ đề Trung đóng trên hình; quét vùng dưới video mỗi 0,5 giây, có tiến độ và dùng CUDA khi khả dụng.
- Khung OCR được vẽ trực tiếp trên xem trước, kéo để tạo, di chuyển và co giãn bằng 4 cạnh/4 góc; mỗi video bắt buộc có ROI riêng, không dùng khung của phim này cho phim khác.
- Timeline xem trước dùng seek debounce 120 ms, tạm dừng khi kéo và cập nhật sub tức thời; ẩn cảnh báo OCR trống/HEVC có thể phục hồi để nhật ký không bị loạn.

## Kiểm chứng OCR thực tế 26/08

- Video `丧尸后周游全国.131.mp4` dài 7.273 giây: PP-OCRv6 tạo 3.125 cue, phủ đến 7.272,209 giây; không overlap, không cue quá 5,5 giây, không câu quá 30 ký tự.
- Đối chiếu 22 mốc từ đầu đến cuối: phần lớn nội dung trùng chữ phụ đề đóng trên hình; timeline sai số khoảng một chu kỳ quét 0,5 giây và tốt hơn rõ rệt so với Whisper.
- Còn nhiễu tại lúc đổi thẻ phụ đề: 193 cue ngắn không quá 0,55 giây (6,2%), đôi khi là chữ chuyển tiếp/thiếu nét như vùng 1.198-1.201 giây. Cần thêm ổn định chuyển cảnh trước khi coi OCR đạt 100%.
- OCR mới gom các cue chuyển tiếp gần giống, giữ bản dài/tin cậy nhất; dùng chữ ký hình ảnh ROI để bỏ qua lần gọi model khi vùng phụ đề không đổi nhưng vẫn giữ chu kỳ timeline 0,5 giây.

## Quy tắc an toàn

- Không gỡ Python 3.11 trước khi V3 được test hoàn chỉnh.
- Không xóa V2 hoặc bytecode V2 trong giai đoạn chuyển đổi.
- Không commit API key, token hoặc file `.env`.
- Trước thay đổi lớn, tạo Git checkpoint mới.
- Không đưa `config.local.json`, API key hoặc token vào Git.
- Khi đóng gói cho nhân viên, chép riêng `config.local.json` cùng tool; không cần máy chủ trung gian.

## Tối ưu tốc độ OCR 26/08

- Bộ lọc khung hình chỉ theo dõi chữ sáng, ít bão hòa có viền tối trong ROI; chuyển động nền không còn dễ kích hoạt PP-OCRv6.
- Giữ chu kỳ lấy mẫu 0,5 giây để không đánh đổi độ chính xác timeline. Đo 120 giây trên video `丧尸后周游全国.131.mp4`: số khung cần OCR giảm từ 227 xuống 122, khoảng 46%.
- Cần chạy lại toàn bộ video để xác nhận thời gian thực tế và kiểm tra các cue một ký tự.

## Dịch SRT ổn định 26/08

- Gemini 3.6 Flash-High là model mặc định trong hộp thoại và quy trình tự động.
- Mỗi batch AI hoàn thành được lưu checkpoint nguyên tử; chạy lại chỉ gửi các cue còn thiếu khi SRT, model và ngôn ngữ không đổi.
- Thanh tiến trình lên 100% khi tác vụ thật sự kết thúc; tỷ lệ cue đã dịch vẫn được ghi riêng trong nhật ký/checkpoint.
- Bộ quản lý tác vụ đóng và chờ QThread tối đa 2 giây ở cả nhánh thành công/lỗi; tác vụ kế tiếp tự chạy, tín hiệu kết thúc cũ không được phép lấy nhầm hàng chờ của tác vụ mới.
