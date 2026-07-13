# SmartBin — Sistem Deteksi Sampah Organik & Non-Organik

## Arsitektur Alur Data

```
[HC-SR04 masuk] --(sampah lewat)--> ESP32 --Serial "CAPTURE"--> Flask
                                                                    |
                                                          webcam.capture()
                                                          YOLO(best.pt).predict()
                                                                    |
                                              simpan ke DB (Detection) 
                                                                    |
                              Flask --Serial "ORGANIC"/"NON_ORGANIC"--> ESP32
                                                                    |
                                                   ESP32 gerakkan servo terkait
                                                   
[HC-SR04 organic / non-organic] --tiap 1 detik--> ESP32 --Serial "LEVEL,x,y"--> Flask --> DB (BinLevel)
                                                                    |
                                                        Dashboard polling /api/*
```

## 1. Upload sketch ke ESP32

Buka `esp32_smartbin/esp32_smartbin.ino` di Arduino IDE, upload ke ESP32 DevKit
(pastikan library `Adafruit PWM Servo Driver Library` sudah terinstall).

**PENTING:** setelah upload dan ESP32 terhubung ke Flask via Serial, jangan buka
Serial Monitor Arduino IDE secara bersamaan — port serial cuma bisa dipakai satu
program dalam satu waktu (Arduino IDE **atau** Flask, bukan keduanya).

## 2. Siapkan backend

```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Copy file model `best.pt` kamu ke folder `backend/`.

## 3. Set konfigurasi (opsional, via environment variable)

| Variable          | Default          | Keterangan                              |
|--------------------|------------------|------------------------------------------|
| `SERIAL_PORT`      | `/dev/ttyUSB0`   | Port ESP32 (Windows: `COM5`, dst)         |
| `SERIAL_BAUD`      | `115200`         | Harus sama dengan `Serial.begin()` di ESP32 |
| `YOLO_MODEL_PATH`  | `best.pt`        | Path ke model YOLO                        |
| `CAMERA_INDEX`     | `0`              | Index webcam (0 = kamera default)         |
| `YOLO_CONF`        | `0.5`            | Confidence threshold                      |

Contoh Linux/Mac:
```bash
export SERIAL_PORT=/dev/ttyUSB0
export YOLO_MODEL_PATH=../best.pt
```

Contoh Windows (PowerShell):
```powershell
$env:SERIAL_PORT="COM5"
$env:YOLO_MODEL_PATH="../best.pt"
```

## 4. Sesuaikan `CLASS_MAP` di `detector.py`

Buka `backend/detector.py`, cek dictionary `CLASS_MAP` — sesuaikan key-nya
dengan nama class persis seperti saat training YOLO kamu (bisa dicek lewat
`model.names` atau file `data.yaml` saat training).

## 5. Jalankan

```bash
python app.py
```

Dashboard bisa diakses di `http://localhost:5000` (atau `http://<ip-server>:5000`
dari perangkat lain di jaringan yang sama).

## Catatan penting soal desain

- **Satu koneksi Serial** dipakai bolak-balik: ESP32 -> Flask (`CAPTURE`, `LEVEL`,
  `FULL_*`, `ACK`) dan Flask -> ESP32 (`ORGANIC`/`NON_ORGANIC`). Ini menghindari
  perlu 2 kabel/koneksi terpisah, tapi konsekuensinya ESP32 harus nunggu (`waitForClassifyResult`,
  timeout 15 detik) sebelum lanjut ke event masuk sampah berikutnya — sudah ditangani di sketch.
- **Reconnect otomatis**: kalau kabel USB/serial putus, `SerialWorker` akan terus
  mencoba konek ulang tiap beberapa detik tanpa perlu restart Flask.
- **Threshold penuh** di-set di dua tempat: firmware ESP32 (`FULL_THRESHOLD_CM`)
  untuk trigger event instan, dan juga dihitung ulang di Flask (`handle_level`)
  dari nilai `LEVEL,x,y` biasa sebagai redundansi/history.
- **Fallback klasifikasi**: kalau YOLO tidak yakin/tidak ada objek terdeteksi
  (`classification is None`), backend saat ini default ke `NON_ORGANIC` (lihat
  `handle_capture` di `app.py`) — silakan ubah sesuai kebutuhan (mis. buka
  keduanya, atau minta ESP32 tolak dan kembalikan sampah).
- Gambar hasil capture disimpan di `backend/captures/` dan bisa diakses lewat
  `image_path` pada tiap row `Detection` (di-serve via route `/captures/<file>`).
