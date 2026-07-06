# Giai thich timer, ngat, thuat toan tung sensor va ghi nhan UART

## 1. Tong quan cach he thong xu ly thoi gian

He thong thu dong bo ECG va PPG bang cach dua moi mau du lieu ve cung mot truc thoi gian `time_ms`.

Trong firmware, moc thoi gian chung duoc tao khi bat dau mot phien do:

```c
measurement_start_us = esp_timer_get_time();
```

Tu moc nay, ESP32 tinh thoi gian da troi qua:

```c
measurement_time_ms = (esp_timer_get_time() - measurement_start_us) / 1000;
```

Y tuong quan trong:

- ECG va PPG khong can co cung tan so lay mau.
- Moi mau chi can co timestamp theo cung mot he quy chieu thoi gian.
- Khi xuat CSV, cot `time_ms` la cot dung de dong bo hai tin hieu.

## 2. Cac khai niem can nam khi thay hoi

### 2.1. Timer la gi?

Timer la bo dem thoi gian. Trong he thong nay, timer duoc dung de tao chu ky lay mau ECG.

ECG duoc lay mau voi tan so:

```c
#define ADC_SAMPLE_RATE 1000
```

Nghia la moi giay lay 1000 mau, tuong duong moi 1 ms lay 1 mau.

Chu ky timer duoc tinh:

```c
#define ECG_TIMER_PERIOD_US (1000000 / ADC_SAMPLE_RATE)
```

Voi `ADC_SAMPLE_RATE = 1000`:

```text
ECG_TIMER_PERIOD_US = 1000000 / 1000 = 1000 us = 1 ms
```

### 2.2. `esp_timer` la gi?

`esp_timer` la timer do ESP-IDF cung cap. No cho phep goi mot ham callback theo chu ky micro giay.

Trong code, timer ECG duoc tao trong `ad8232_configure()`:

```c
const esp_timer_create_args_t timer_args = {
  .callback = ecg_timer_callback,
  .arg = NULL,
  .dispatch_method = ESP_TIMER_TASK,
  .name = "ecg_1khz",
  .skip_unhandled_events = true,
};
esp_timer_create(&timer_args, &ecg_timer_handle);
```

Y nghia cac truong:

- `callback`: ham se duoc goi moi khi timer den chu ky.
- `arg`: tham so truyen vao callback, trong code nay khong dung nen de `NULL`.
- `dispatch_method = ESP_TIMER_TASK`: callback chay trong task noi bo cua ESP-IDF, khong chay truc tiep trong ISR phan cung.
- `name`: ten timer de debug.
- `skip_unhandled_events = true`: neu he thong bi tre va bo lo nhieu tick, ESP-IDF co the bo qua cac su kien cu de tranh callback bi don qua nhieu.

### 2.3. Callback la gi?

Callback la ham duoc he thong goi lai tu dong khi co su kien.

Trong code nay, callback ECG la:

```c
static void ecg_timer_callback(void *arg)
```

Moi 1 ms, neu timer dang chay, ESP-IDF goi ham nay de doc ADC ECG.

### 2.4. Ngat ISR la gi?

ISR, hay Interrupt Service Routine, la ham xu ly ngat phan cung. ISR thuong chay ngay khi co su kien phan cung, co do uu tien cao, nhung bi han che: khong nen cap phat bo nho, khong nen in log dai, khong nen lam xu ly nang.

Trong he thong hien tai, ECG khong dung ISR ADC phan cung truc tiep. Thay vao do, code dung `esp_timer` voi:

```c
.dispatch_method = ESP_TIMER_TASK
```

Vi vay, can noi chinh xac:

> He thong lay mau ECG bang timer dinh ky 1 kHz cua ESP-IDF. Callback timer duoc goi theo chu ky 1 ms, nhung callback nay chay trong task noi bo cua `esp_timer`, khong phai ISR phan cung truc tiep.

Neu thay hoi "co dung ngat khong?", cau tra loi nen la:

> Ve mat y tuong, he thong co co che kich hoat dinh ky giong ngat timer de lay mau ECG. Tuy nhien trong code hien tai, callback duoc dispatch bang `ESP_TIMER_TASK`, nen no khong chay trong ngat phan cung ma chay o ngu canh task. Cach nay de lap trinh va an toan hon, nhung do chinh xac co the phu thuoc vao scheduler.

### 2.5. Polling la gi?

Polling la cach kiem tra lap lai theo chu ky xem co du lieu hay chua.

PPG trong he thong khong dung timer rieng de lay tung mau. MAX30102 tu lay mau va dua vao FIFO noi bo. ESP32 dung task `readMAX30102_task()` de kiem tra FIFO moi 5 ms:

```c
vTaskDelay(delay_ticks_at_least_1(PPG_FIFO_READ_INTERVAL_MS));
```

Trong `sensor_init.h`:

```c
#define PPG_FIFO_READ_INTERVAL_MS 5
```

Nghia la cu khoang 5 ms, ESP32 hoi cam bien xem FIFO co mau moi khong.

### 2.6. FIFO la gi?

FIFO la viet tat cua First In First Out, nghia la du lieu vao truoc thi ra truoc.

MAX30102 co FIFO noi bo. Cam bien tu lay mau PPG theo `sampleRate = 100 Hz`, sau do xep cac mau vao FIFO. ESP32 khong can doc dung tung thoi diem 10 ms, ma co the doc mot nhom mau tu FIFO.

## 3. Xu ly timer cho ECG AD8232

### 3.1. Cau hinh ADC va timer

AD8232 xuat tin hieu analog. ESP32 doc tin hieu nay bang ADC1 channel 6, tuong ung GPIO34:

```c
#define ADC_CHANNEL ADC_CHANNEL_6
```

Ham `ad8232_configure()` lam cac viec:

1. Cau hinh do rong ADC 12 bit.
2. Cau hinh suy hao ADC.
3. Tao `esp_timer` cho ECG neu timer chua ton tai.
4. Danh dau `adc_ready = true` neu cau hinh thanh cong.

### 3.2. Bat dau timer khi START

Timer ECG khong chay ngay khi boot. No chi bat dau khi PC gui lenh:

```text
START <duration_s>
```

Trong `sensor_start_measurement()`, neu mode co ECG, firmware goi:

```c
esp_timer_start_periodic(ecg_timer_handle, ECG_TIMER_PERIOD_US);
```

Tu luc nay, callback ECG duoc goi moi 1 ms.

### 3.3. Callback doc ADC

Callback ECG:

```c
static void ecg_timer_callback(void *arg)
{
  sensor_mode_t mode = sensor_get_mode();
  if(!measurement_running || !mode_has_ecg(mode)){
    return;
  }

  int raw = adc1_get_raw(ADC_CHANNEL);
  if(raw >= 0){
    store_ecg_sample((uint16_t)raw);
  }
}
```

Giai thich tung buoc:

1. Lay mode hien tai.
2. Neu khong dang do thi thoat.
3. Neu mode khong co ECG thi thoat.
4. Doc gia tri ADC tu AD8232.
5. Neu doc hop le, luu mau vao buffer ECG.

### 3.4. Luu mau ECG va gan timestamp

Trong `store_ecg_sample()`, timestamp chinh cua ECG duoc tinh bang chi so mau:

```c
uint32_t time_ms = (ecg_sample_index * 1000U) / ADC_SAMPLE_RATE;
ecg_sample_index++;
```

Voi 1000 Hz:

```text
Mau 0 -> 0 ms
Mau 1 -> 1 ms
Mau 2 -> 2 ms
...
```

Ham nay cung ghi nhan thong ke:

- `ecg_first_actual_us`: thoi diem thuc te cua mau ECG dau.
- `ecg_last_actual_us`: thoi diem thuc te cua mau ECG cuoi.
- `ecg_min_period_us`: chu ky thuc te nho nhat giua hai mau.
- `ecg_max_period_us`: chu ky thuc te lon nhat giua hai mau.
- `ecg_clip_low_count`: so mau cham nguong thap.
- `ecg_clip_high_count`: so mau cham nguong cao.
- `ecg_overflow`: bao buffer ECG bi day.

### 3.5. Dung timer ECG

Khi du thoi gian do hoac khi PC gui `STOP`, firmware goi:

```c
sensor_stop_measurement(true);
```

Neu mode dang do co ECG, timer se bi dung:

```c
esp_timer_stop(ecg_timer_handle);
```

Sau do firmware dat `dump_pending = true` de task in du lieu CSV.

## 4. Thuat toan doc PPG MAX30102

### 4.1. Cau hinh MAX30102

MAX30102 duoc cau hinh qua I2C:

```c
#define I2C_SDA_GPIO 21
#define I2C_SCL_GPIO 22
#define I2C_FREQ_HZ  400000
```

Thong so PPG:

```c
#define sampleRate 100
```

Nghia la MAX30102 lay mau PPG voi tan so 100 Hz, moi mau cach nhau khoang 10 ms.

### 4.2. Cach doc FIFO

Task `readMAX30102_task()` chay lien tuc. Neu khong dang do hoac mode khong co PPG, task ngu ngan 5 ms roi kiem tra lai.

Khi dang do PPG:

1. Lay thoi gian hien tai cua phien do:

```c
batch_time_ms = measurement_time_ms();
```

2. Kiem tra FIFO co bao nhieu mau:

```c
max30105_check(&ppg_sensor, &sample_count);
available = max30105_available(&ppg_sensor);
```

3. Tinh chu ky PPG:

```c
ppg_period_ms = 1000U / sampleRate;
```

Voi `sampleRate = 100`:

```text
ppg_period_ms = 10 ms
```

4. Doc tung mau trong FIFO va gan timestamp nguoc:

```c
age_ms = (available - 1U - i) * ppg_period_ms;
time_ms = (batch_time_ms > age_ms) ? (batch_time_ms - age_ms) : 0;
```

5. Lay gia tri RED va IR:

```c
red = max30105_get_fifo_red(&ppg_sensor);
ir = max30105_get_fifo_ir(&ppg_sensor);
```

6. Luu vao buffer PPG:

```c
store_ppg_sample(time_ms, red, ir);
```

7. Chuyen sang mau tiep theo trong FIFO:

```c
max30105_next_sample(&ppg_sensor);
```

### 4.3. Vi sao PPG phai gan timestamp nguoc?

Vi ESP32 doc FIFO theo batch, khong doc tung mau ngay khi cam bien vua lay xong. Gia su tai thoi diem `batch_time_ms = 100 ms`, FIFO co 4 mau PPG va sample rate la 100 Hz.

Khi do, 4 mau nay duoc gan thoi gian:

```text
Mau cu nhat -> 70 ms
Mau tiep   -> 80 ms
Mau tiep   -> 90 ms
Mau moi nhat -> 100 ms
```

Cong thuc:

```text
time_ms = batch_time_ms - (so_mau_con_lai_sau_mau_hien_tai * 10 ms)
```

Cach nay giup vi tri thoi gian cua PPG gan dung voi luc cam bien da lay mau.

## 5. Qua trinh ghi nhan va truyen UART

### 5.1. UART dieu khien tu PC xuong ESP32

Trong `command_task()`, ESP32 doc tung ky tu tu UART bang:

```c
int c = getchar();
```

Khi gap ky tu xuong dong `\n` hoac `\r`, firmware coi nhu da nhan xong mot lenh.

Cac lenh ho tro:

- `ECG`: chon chi do ECG.
- `PPG`: chon chi do PPG.
- `BOTH` hoac `ALL`: do ca ECG va PPG.
- `START <duration_s>`: bat dau do trong so giay yeu cau.
- `STOP` hoac `IDLE`: dung do.
- `STATUS`: hoi trang thai cam bien va phien do.

Sau moi lenh hop le, ESP32 tra ACK, vi du:

```text
ACK,MODE,BOTH
ACK,START,10
ACK,STOP
```

Neu loi:

```text
ERR,START,ESP_ERR_INVALID_STATE
ERR,UNKNOWN_CMD
```

### 5.2. UART du lieu tu ESP32 ve PC

Trong luc dang do, firmware khong in tung mau realtime. Ly do:

- In UART lien tuc co the lam cham viec lay mau.
- ECG 1000 Hz tao rat nhieu dong du lieu moi giay.
- Neu vua lay mau vua in, he thong de bi tre hoac mat mau.

Thay vao do, firmware luu du lieu vao RAM truoc. Sau khi het thoi gian do, moi xuat toan bo du lieu ra UART.

Block CSV co dang:

```csv
BEGIN_SYNC_CSV,<ecg_count>,<ppg_count>
time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw
0,2048,,
1,2050,,
10,2060,123470,120995
STATS,ECG_EXPECTED,...
END_SYNC_CSV,<ecg_count>,<ppg_count>,<ecg_overflow>,<ppg_overflow>
```

### 5.3. Cach firmware tron ECG va PPG khi xuat CSV

ECG va PPG duoc luu trong hai buffer rieng:

```c
ecg_buffer[]
ppg_buffer[]
```

Khi xuat CSV, firmware dung hai chi so `i` va `j`:

- `i`: vi tri hien tai trong buffer ECG.
- `j`: vi tri hien tai trong buffer PPG.

Neu `ecg_buffer[i].time_ms == ppg_buffer[j].time_ms`, in chung mot dong.

Neu timestamp ECG nho hon, in dong chi co ECG.

Neu timestamp PPG nho hon, in dong chi co PPG.

Vi vay CSV co the co dong thieu ECG hoac thieu PPG. Day la binh thuong vi hai cam bien co tan so lay mau khac nhau.

### 5.4. UI Python nhan du lieu UART

Trong `UI_Reciever.py`, khi nguoi dung bam Start:

1. UI mo cong COM voi baudrate 115200.
2. Gui mode:

```text
BOTH
```

3. Gui lenh bat dau:

```text
START 10
```

4. Doi den khi nhan:

```text
BEGIN_SYNC_CSV
```

5. Doc tung dong CSV va parse thanh `SyncRow`.
6. Dung khi nhan:

```text
END_SYNC_CSV
```

7. Sap xep lai theo `time_ms`.
8. Luu file raw CSV.
9. Loc tin hieu va luu file filtered CSV.
10. Ve tin hieu tren giao dien.

## 6. Luu do thuat toan ECG AD8232

```text
Bat dau phien do ECG
        |
        v
Kiem tra ADC da san sang?
        |
        +-- Khong --> Bao loi, khong START
        |
        v
Cap phat ECG buffer
        |
        v
Luu measurement_start_us
        |
        v
Start esp_timer chu ky 1 ms
        |
        v
ecg_timer_callback()
        |
        v
Kiem tra measurement_running va mode co ECG?
        |
        +-- Khong --> Thoat callback
        |
        v
Doc ADC AD8232
        |
        v
Tinh time_ms theo ecg_sample_index
        |
        v
Luu time_ms va ecg_raw vao buffer
        |
        v
Cap nhat thong ke jitter, clipping, overflow
        |
        v
Het thoi gian do?
        |
        +-- Chua --> Tiep tuc callback moi 1 ms
        |
        v
Stop esp_timer va cho dump CSV
```

## 7. Luu do thuat toan PPG MAX30102

```text
Bat dau phien do PPG
        |
        v
Kiem tra MAX30102 da san sang?
        |
        +-- Khong --> Bao loi, khong START
        |
        v
Cap phat PPG buffer va clear FIFO
        |
        v
readMAX30102_task chay lap
        |
        v
Dang do va mode co PPG?
        |
        +-- Khong --> Delay 5 ms roi kiem tra lai
        |
        v
Lay batch_time_ms
        |
        v
Kiem tra FIFO co mau moi?
        |
        +-- Khong --> Delay 5 ms roi kiem tra lai
        |
        v
Tinh ppg_period_ms = 1000 / sampleRate
        |
        v
Doc tung mau RED/IR trong FIFO
        |
        v
Gan timestamp nguoc theo vi tri mau trong FIFO
        |
        v
Luu time_ms, red_raw, ir_raw vao buffer
        |
        v
Het thoi gian do?
        |
        +-- Chua --> Tiep tuc polling FIFO
        |
        v
Cho dump CSV
```

## 8. Luu do qua trinh ghi nhan UART

```text
UI Python khoi dong
        |
        v
Nguoi dung chon COM, mode, duration
        |
        v
Kiem tra hop le?
        |
        +-- Khong --> Bao loi tren UI
        |
        v
Mo Serial 115200
        |
        v
Gui mode: ECG / PPG / BOTH
        |
        v
Gui START duration_s
        |
        v
ESP32 nhan lenh trong command_task
        |
        v
ESP32 do va luu du lieu vao RAM
        |
        v
Het thoi gian do hoac STOP
        |
        v
ESP32 dump BEGIN_SYNC_CSV ... END_SYNC_CSV
        |
        v
UI doc block CSV
        |
        v
Parse, sap xep theo time_ms
        |
        v
Luu raw CSV
        |
        v
Loc ECG/PPG va luu filtered CSV
        |
        v
Ve do thi tren UI
```

## 9. Cau tra loi ngan gon khi bao ve

Neu thay hoi ve timer va ngat, co the tra loi:

> Trong he thong, ECG duoc lay mau bang `esp_timer` cua ESP-IDF voi chu ky 1 ms, tuong ung 1000 Hz. Moi chu ky, callback `ecg_timer_callback()` doc ADC cua AD8232 va luu mau vao buffer. Callback nay duoc cau hinh voi `ESP_TIMER_TASK`, nen khong phai ISR phan cung truc tiep, ma la callback chay trong task timer cua ESP-IDF. PPG thi khong dung timer rieng, vi MAX30102 da tu lay mau vao FIFO voi sample rate 100 Hz. ESP32 dung task polling FIFO moi 5 ms, doc cac mau PPG theo batch va gan timestamp nguoc dua tren vi tri mau trong FIFO. Sau khi het thoi gian do, firmware dung timer ECG, tron hai buffer theo `time_ms`, roi gui CSV ve PC qua UART.

