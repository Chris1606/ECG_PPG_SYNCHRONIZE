# Thuyết minh bài toán: Thu đồng bộ tín hiệu ECG và PPG trên ESP32

## 1. Mục tiêu hệ thống

Hệ thống được xây dựng để thu đồng bộ hai loại tín hiệu sinh học:

- ECG từ module AD8232, đọc bằng ADC của ESP32.
- PPG từ cảm biến MAX30102, đọc qua giao tiếp I2C và FIFO nội bộ của cảm biến.

Mục tiêu chính là đưa hai tín hiệu về cùng một trục thời gian `time_ms`, sau đó gửi dữ liệu thô về máy tính qua UART để lưu thành CSV, lọc nhiễu, hiển thị và phục vụ các bước phân tích tiếp theo.

## 2. Thành phần chương trình

Phần firmware ESP32 nằm chủ yếu trong `PPG_PCG_ECG.c` và `sensor_init.c`.

- `app_main()` khởi tạo MAX30102, AD8232, mutex in dữ liệu, sau đó tạo các task FreeRTOS.
- `command_task()` nhận lệnh UART từ máy tính: `ECG`, `PPG`, `BOTH`, `START <duration_s>`, `STOP`, `STATUS`.
- `readMAX30102_task()` đọc dữ liệu PPG từ FIFO của MAX30102.
- ECG không đọc trong vòng lặp task, mà được lấy mẫu bằng `esp_timer` chu kỳ 1 ms.
- `printData_task()` giám sát thời gian đo, dừng phiên đo khi đủ thời lượng và xuất toàn bộ dữ liệu CSV.

Phần máy tính nằm trong `UI_Reciever.py`.

- Giao diện Tkinter cho phép chọn cổng COM, tên người đo, mode và thời gian đo.
- Khi bấm Start, chương trình gửi mode và lệnh `START <duration_s>` qua UART.
- Sau khi ESP32 đo xong, UI đọc block `BEGIN_SYNC_CSV ... END_SYNC_CSV`.
- Dữ liệu được lưu thành CSV thô, lọc tín hiệu, lưu CSV đã lọc và vẽ bằng Matplotlib.

## 3. Thuật toán tổng quát

1. Máy tính mở UI, chọn cổng COM, mode đo và thời gian đo.
2. UI gửi mode xuống ESP32, ví dụ `BOTH`, sau đó gửi `START 10`.
3. ESP32 kiểm tra trạng thái hệ thống: có đang đo không, cảm biến cần dùng đã sẵn sàng chưa, thời lượng đo có hợp lệ không.
4. ESP32 cấp phát buffer theo mode:
   - ECG: `duration_s * 1000 + 1000` mẫu.
   - PPG: `duration_s * 100 + phần dự phòng FIFO`.
5. ESP32 reset trạng thái phiên đo, xóa FIFO PPG, lưu mốc `measurement_start_us`.
6. Nếu mode có ECG, ESP32 bật timer 1 kHz để đọc ADC.
7. Nếu mode có PPG, task PPG đọc FIFO MAX30102 định kỳ mỗi 5 ms.
8. Mỗi mẫu ECG và PPG được lưu kèm timestamp `time_ms`.
9. Khi đủ thời gian đo hoặc nhận `STOP`, ESP32 dừng đo và đặt cờ `dump_pending`.
10. `printData_task()` xuất CSV qua UART, kèm thống kê số mẫu, overflow và clipping.
11. UI nhận CSV, parse dữ liệu, lưu file raw, lọc tín hiệu, lưu file filtered và vẽ đồ thị.

## 4. Đồng bộ thời gian

Nguyên tắc đồng bộ của hệ thống là đồng bộ bằng timestamp. ESP32 dùng một mốc thời gian chung:

```c
measurement_start_us = esp_timer_get_time();
```

Từ mốc này, mọi dữ liệu được quy về đơn vị mili giây.

Với ECG, hệ thống lấy mẫu 1000 Hz nên chu kỳ là 1 ms. Timestamp được tính theo chỉ số mẫu:

```c
time_ms = ecg_sample_index * 1000 / ADC_SAMPLE_RATE;
```

Với `ADC_SAMPLE_RATE = 1000`, mẫu ECG thứ 0 ứng với 0 ms, mẫu thứ 1 ứng với 1 ms, mẫu thứ 2 ứng với 2 ms.

Với PPG, MAX30102 được cấu hình `sampleRate = 100`, tức chu kỳ mẫu khoảng 10 ms. Dữ liệu PPG được đọc theo batch từ FIFO. Tại thời điểm đọc batch, chương trình lấy thời gian hiện tại rồi gán timestamp ngược cho từng mẫu trong FIFO:

```c
ppg_period_ms = 1000 / sampleRate;
age_ms = (available - 1 - i) * ppg_period_ms;
time_ms = batch_time_ms - age_ms;
```

Cách làm này giúp mẫu PPG cũ hơn trong FIFO vẫn được gán đúng vị trí tương đối trên trục thời gian.

## 5. Định dạng dữ liệu đầu ra

ESP32 chỉ gửi dữ liệu sau khi phiên đo kết thúc, không stream từng mẫu realtime. Block dữ liệu có dạng:

```csv
BEGIN_SYNC_CSV,<ecg_count>,<ppg_count>
time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw
0,2048,,
1,2050,,
10,2060,123470,120995
STATS,...
END_SYNC_CSV,<ecg_count>,<ppg_count>,<ecg_overflow>,<ppg_overflow>
```

Vì ECG và PPG có tần số lấy mẫu khác nhau nên không phải dòng nào cũng có đủ cả ECG và PPG. Điểm đồng bộ là cột `time_ms`.

## 6. Xử lý trên máy tính

Sau khi nhận block CSV, `UI_Reciever.py` thực hiện:

1. Parse từng dòng thành `SyncRow(time_ms, ecg_raw, ppg_red_raw, ppg_ir_raw)`.
2. Sắp xếp dữ liệu theo `time_ms`.
3. Lưu CSV thô vào `data_csv/SYNC/raw`.
4. Lọc ECG:
   - Trừ baseline bằng median.
   - Lọc miền tần số bằng FFT, giữ dải 0.5-45 Hz.
   - Notch vùng 50 Hz để giảm nhiễu điện lưới.
   - Khử nhiễu wavelet `db4`, level 3 nếu có thư viện PyWavelets.
5. Lọc PPG IR bằng wavelet `db4`, level 3.
6. Lưu CSV đã lọc vào `data_csv/SYNC/filtered`.
7. Vẽ ECG raw, ECG filtered, PPG IR raw và PPG IR filtered trên giao diện.

## 7. Các trạng thái và nhánh lỗi quan trọng

Firmware có các nhánh bảo vệ chính:

- Nếu đang đo mà nhận lệnh đổi mode, ESP32 bỏ qua và giữ mode hiện tại.
- Nếu nhận `START` khi đang đo, trả lỗi `ESP_ERR_INVALID_STATE`.
- Nếu mode cần ECG nhưng ADC chưa sẵn sàng, không bắt đầu đo.
- Nếu mode cần PPG nhưng MAX30102 chưa sẵn sàng, không bắt đầu đo.
- Nếu cấp phát buffer thất bại, dừng phiên đo và giải phóng tài nguyên.
- Nếu buffer đầy trong lúc đo, đặt cờ `ecg_overflow` hoặc `ppg_overflow`.
- Với ECG, hệ thống đếm số mẫu bị clipping thấp hoặc cao để đánh giá chất lượng tín hiệu.

## 8. Ý nghĩa của lưu đồ draw.io

File `system_algorithm_flow.drawio` mô tả hệ thống theo ba vùng:

- Vùng máy tính: khởi tạo UI, chọn cấu hình, gửi lệnh, nhận CSV, lưu file và lọc tín hiệu.
- Vùng ESP32 firmware: khởi tạo cảm biến, tạo task, xử lý lệnh UART, bắt đầu và dừng phiên đo.
- Vùng cảm biến và dữ liệu: lấy mẫu ECG, đọc FIFO PPG, gán timestamp, dump CSV và dọn bộ nhớ.

Lưu đồ này có thể import trực tiếp vào draw.io bằng cách mở diagrams.net, chọn `File` -> `Open From` -> `Device`, rồi chọn file `.drawio`.

## 9. Kết luận

Hệ thống sử dụng ESP32 làm bộ thu và đồng bộ tín hiệu. ECG được lấy mẫu đều bằng timer 1 kHz, còn PPG được đọc từ FIFO của MAX30102 với sample rate 100 Hz. Hai tín hiệu không cần cùng tần số lấy mẫu, vì chúng được căn chỉnh bằng timestamp chung `time_ms`.

Cách thiết kế đo theo phiên và chỉ gửi CSV sau khi đo xong giúp giảm tải UART trong lúc lấy mẫu, hạn chế mất mẫu do in dữ liệu liên tục. Dữ liệu sau đó được xử lý trên máy tính, phù hợp cho các bước phân tích tín hiệu như lọc nhiễu, đánh giá chất lượng, phát hiện đỉnh R ECG hoặc khai thác đặc trưng PPG.
