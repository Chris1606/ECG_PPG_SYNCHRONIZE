# Thuc nghiem do BPM va danh gia do lech ECG-PPG

## 1. Muc tieu

Phan thuc nghiem nay dung du lieu raw thu duoc tu he thong ECG AD8232 va PPG MAX30102 de:

- Tinh nhip tim BPM tu tin hieu ECG.
- Tinh nhip tim BPM tu tin hieu PPG.
- So sanh BPM giua hai kenh.
- Uoc luong do lech thoi gian giua dinh R cua ECG va dinh mach PPG.

Can chu y: do lech tu dinh R ECG den dinh PPG khong phai la "sai so dong bo" thuan tuy cua he thong. Gia tri nay bao gom:

- Pulse Transit Time, tuc thoi gian song mach di tu tim den vi tri dat cam bien PPG.
- Dap ung cua cam bien PPG.
- Anh huong cua bo loc tin hieu.
- Sai so phat hien dinh.
- Sai so dong bo thoi gian cua he thong thu.

Vi vay, trong bao cao nen goi chi so nay la `ECG-to-PPG delay` hoac `do tre ECG-PPG`. Neu muon danh gia do on dinh dong bo, nen nhin vao `std/jitter` cua delay thay vi chi nhin delay trung binh.

## 2. Dau vao thuc nghiem

Dau vao la file CSV raw co dang:

```csv
person_name,time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw
unknown,0,2048,,
unknown,1,2050,,
unknown,10,2060,123470,120995
```

Cot `time_ms` la moc thoi gian chung de dong bo ECG va PPG.

## 3. Tien xu ly tin hieu ECG

Tin hieu ECG raw duoc tien xu ly theo cac buoc:

1. Tru baseline bang median.
2. Loc mien tan so bang FFT, giu dai 0.5-45 Hz.
3. Notch vung 50 Hz de giam nhieu dien luoi.
4. Khu nhieu bang wavelet, mac dinh `db4`, level 3.
5. Dua tin hieu ve quanh 0 de de phat hien dinh R.

Dinh R duoc phat hien bang thuat toan local maxima:

- Chon cuc dai cuc bo cua tin hieu da loc.
- Dat nguong theo median va MAD:

```text
threshold = median(signal) + k * robust_scale(signal)
```

- Dung khoang cach toi thieu giua hai dinh ECG la 0.30 s de tranh bat nham nhieu dinh trong mot chu ky tim.

## 4. Tien xu ly tin hieu PPG

Tin hieu PPG co the dung kenh IR hoac RED. Mac dinh nen dung PPG IR vi thuong on dinh hon.

PPG duoc tien xu ly:

1. Tru baseline.
2. Loc dai tan phu hop nhip tim, khoang 0.4-8 Hz.
3. Khu nhieu bang wavelet, mac dinh `db4`, level 3.
4. Phat hien dinh mach PPG bang local maxima.

Khoang cach toi thieu giua hai dinh PPG mac dinh la 0.35 s.

## 5. Cong thuc tinh BPM

Voi ECG, sau khi phat hien cac thoi diem dinh R:

```text
R = [t1, t2, t3, ...]
RR_i = t(i+1) - t(i)
BPM_ECG = 60 / median(RR_i)
```

Voi PPG:

```text
P = [p1, p2, p3, ...]
PP_i = p(i+1) - p(i)
BPM_PPG = 60 / median(PP_i)
```

Ngoai ra co the tinh BPM theo so luong dinh:

```text
BPM_count = (so_dinh - 1) * 60 / (t_dinh_cuoi - t_dinh_dau)
```

## 6. So sanh BPM ECG va PPG

Sai khac BPM giua hai kenh:

```text
BPM_diff = abs(BPM_ECG - BPM_PPG)
```

Sai khac theo phan tram:

```text
BPM_diff_percent = BPM_diff / BPM_ECG * 100%
```

Neu ECG va PPG duoc thu tot, BPM tinh tu hai kenh nen gan nhau. Neu sai khac lon, cac kha nang thuong gap la:

- ECG bi nhieu hoac dien cuc tiep xuc kem.
- PPG bi rung tay, dat ngon tay khong on dinh.
- Nguong phat hien dinh chua phu hop.
- Tin hieu qua ngan, so chu ky tim qua it.

## 7. Uoc luong do lech ECG-PPG

Voi moi dinh R ECG tai thoi diem `R_i`, tim dinh PPG dau tien xuat hien sau no trong cua so:

```text
R_i + min_delay <= P_j <= R_i + max_delay
```

Mac dinh:

```text
min_delay = 80 ms
max_delay = 800 ms
```

Do lech tung nhip:

```text
delay_i = P_j - R_i
```

Cac chi so can bao cao:

- `mean delay`: do tre trung binh ECG den PPG.
- `median delay`: do tre trung vi, it bi anh huong boi ngoai lai.
- `delay std/jitter`: do dao dong cua delay, dung de danh gia do on dinh.
- `min delay`, `max delay`: bien do tre.
- `matched beats`: so nhip ghep duoc giua ECG va PPG.

## 8. Cong cu UI da tao

File UI:

```text
UI_BPM_Sync_Analyzer.py
```

Chay bang:

```powershell
python UI_BPM_Sync_Analyzer.py
```

Chuc nang:

- Mo file raw CSV.
- Chon kenh PPG IR hoac RED.
- Chon wavelet, mac dinh `db4`.
- Chon level wavelet.
- Dieu chinh nguong phat hien dinh ECG va PPG.
- Tinh BPM tu ECG.
- Tinh BPM tu PPG.
- Tinh sai khac BPM.
- Tinh delay ECG R-peak den PPG peak.
- Ve ECG, PPG va delay theo tung nhip.
- Export report CSV.

## 9. Cach trinh bay ket qua

Bang ket qua nen co dang:

| Chi so | Gia tri |
|---|---:|
| So dinh ECG phat hien | ... |
| BPM ECG | ... bpm |
| So dinh PPG phat hien | ... |
| BPM PPG | ... bpm |
| Chenh lech BPM | ... bpm |
| Chenh lech BPM | ... % |
| So nhip ghep ECG-PPG | ... |
| Mean ECG-PPG delay | ... ms |
| Median ECG-PPG delay | ... ms |
| Delay jitter/std | ... ms |

Nhan xet mau:

> BPM tinh tu ECG va PPG co xu huong gan nhau, cho thay hai tin hieu cung phan anh chu ky tim. Do tre tu dinh R ECG den dinh PPG co gia tri duong, phu hop voi hien tuong PPG xuat hien sau hoat dong dien hoc cua tim. Do lech nay khong duoc xem la sai so dong bo thuan tuy, vi no bao gom ca thoi gian lan truyen mach sinh ly va sai so cua thuat toan phat hien dinh. Do on dinh dong bo duoc danh gia tot hon thong qua do lech chuan cua delay giua cac nhip.

