import os
import sys
import time
import threading
from datetime import datetime

import cv2
from ultralytics import YOLO

MODEL_PATH = os.environ.get("YOLO_MODEL_PATH", "best.pt")
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
CAPTURE_DIR = os.path.join(os.path.dirname(__file__), "captures")
CONF_THRESHOLD = float(os.environ.get("YOLO_CONF", "0.5"))

# Berapa kali YOLO dijalankan per detik saat streaming.
# Tidak perlu setinggi FPS kamera - 5-8x/detik sudah cukup responsif
# dan jauh lebih ringan untuk CPU.
INFERENCE_FPS = float(os.environ.get("INFERENCE_FPS", "6"))

# Sesuaikan mapping ini dengan nama kelas yang dipakai saat training best.pt
CLASS_MAP = {
    "organic": "ORGANIC",
    "non-organic": "NON_ORGANIC",
    "nonorganic": "NON_ORGANIC",
    "non_organic": "NON_ORGANIC",
    "inorganic": "NON_ORGANIC",
    "anorganik": "NON_ORGANIC",
    "organik": "ORGANIC",
}

os.makedirs(CAPTURE_DIR, exist_ok=True)

if sys.platform.startswith("win"):
    CAPTURE_BACKEND = cv2.CAP_DSHOW
else:
    CAPTURE_BACKEND = cv2.CAP_ANY


class Detector:
    """Kamera + YOLO berjalan sebagai stream kontinyu di background thread.

    Kamera dibuka SEKALI saat start() dan tidak pernah ditutup/dibuka ulang
    selama app berjalan (kecuali error/disconnect). Thread background terus
    membaca frame, menjalankan YOLO tiap ~1/INFERENCE_FPS detik, dan
    menyimpan:
      - self._latest_frame        -> frame mentah terbaru
      - self._latest_annotated    -> frame + bounding box (untuk MJPEG & histori)
      - self._latest_result       -> dict hasil deteksi terakhir

    classify() dipanggil saat ESP32 minta CAPTURE: cukup ambil snapshot dari
    _latest_result / _latest_annotated yang sudah ada, tanpa buka kamera baru.
    """

    def __init__(self, model_path=MODEL_PATH, camera_index=CAMERA_INDEX):
        self.model = YOLO(model_path)
        self.camera_index = camera_index

        self._cap = None
        self._lock = threading.Lock()
        self._model_lock = threading.Lock()   # cegah 2 thread predict() bersamaan ke model yg sama
        self._stop_event = threading.Event()
        self._thread = None

        self._latest_frame = None
        self._latest_annotated = None
        self._latest_result = {"classification": None, "raw_label": None, "confidence": 0.0}
        self._latest_jpeg = None   # bytes, siap kirim ke MJPEG stream

        self.camera_ok = False

    # ---------------------------------------------------------- lifecycle

    def start(self):
        """Buka kamera & mulai thread streaming. Panggil sekali saat app start."""
        self._open_camera()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass

    def _open_camera(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass

        cap = cv2.VideoCapture(self.camera_index, CAPTURE_BACKEND)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            self._cap = None
            self.camera_ok = False
            raise RuntimeError(
                f"Tidak bisa membuka kamera index {self.camera_index}. "
                f"Pastikan tidak ada aplikasi lain yang memakai kamera ini."
            )

        for _ in range(5):
            cap.read()
            time.sleep(0.03)

        self._cap = cap
        self.camera_ok = True

    # ---------------------------------------------------------- background loop

    def _stream_loop(self):
        min_interval = 1.0 / INFERENCE_FPS
        last_infer_time = 0.0

        while not self._stop_event.is_set():
            if self._cap is None or not self._cap.isOpened():
                try:
                    self._open_camera()
                except RuntimeError:
                    self.camera_ok = False
                    time.sleep(1.0)
                    continue

            ok, frame = self._cap.read()
            if not ok or frame is None:
                self.camera_ok = False
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
                time.sleep(0.3)
                continue

            self.camera_ok = True
            now = time.time()

            if now - last_infer_time >= min_interval:
                last_infer_time = now
                annotated, result = self._infer(frame)
                with self._lock:
                    self._latest_frame = frame
                    self._latest_annotated = annotated
                    self._latest_result = result
                    ok_enc, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok_enc:
                        self._latest_jpeg = buf.tobytes()
            else:
                # tidak infer tiap frame (hemat CPU), tapi tetap update preview mentah
                with self._lock:
                    self._latest_frame = frame
                    if self._latest_annotated is None:
                        self._latest_annotated = frame

            time.sleep(0.01)  # jangan spin 100% CPU

    def _infer(self, frame):
        """Jalankan YOLO pada satu frame, kembalikan (frame_annotated, result_dict)."""
        with self._model_lock:
            results = self.model.predict(source=frame, conf=CONF_THRESHOLD, verbose=False)

        best_label = None
        best_conf = 0.0
        annotated = frame

        if results and len(results) > 0:
            r = results[0]
            annotated = r.plot()  # frame dengan bounding box + label tergambar
            if r.boxes is not None and len(r.boxes) > 0:
                for box in r.boxes:
                    conf = float(box.conf[0])
                    cls_id = int(box.cls[0])
                    cls_name = r.names.get(cls_id, str(cls_id)).lower()
                    if conf > best_conf:
                        best_conf = conf
                        best_label = cls_name

        classification = CLASS_MAP.get(best_label, None) if best_label else None

        return annotated, {
            "classification": classification,
            "raw_label": best_label,
            "confidence": best_conf,
        }

    # ---------------------------------------------------------- public API

    def classify(self):
        """Dipanggil saat ESP32 kirim CAPTURE. Ambil frame MENTAH terbaru dari
        stream (tidak membuka kamera baru) lalu jalankan inferensi YOLO yang
        FRESH saat itu juga (bukan cache dari loop background yang bisa basi
        s.d. ~1/INFERENCE_FPS detik) - supaya hasil setepat mungkin dengan
        momen sampah benar-benar berada di depan kamera.

        Tidak ada fallback/default classification. Jika tidak ada objek
        dengan confidence >= CONF_THRESHOLD, classification = None.
        """
        with self._lock:
            if self._latest_frame is None:
                raise RuntimeError("Stream kamera belum menghasilkan frame apapun")
            frame = self._latest_frame.copy()

        annotated, result = self._infer(frame)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"capture_{timestamp}.jpg"
        filepath = os.path.join(CAPTURE_DIR, filename)
        cv2.imwrite(filepath, annotated)

        result["image_path"] = os.path.relpath(filepath, os.path.dirname(__file__))
        return result

    def get_mjpeg_frame(self):
        """Kembalikan bytes JPEG terbaru (untuk endpoint streaming /video_feed)."""
        with self._lock:
            return self._latest_jpeg

    def get_status(self):
        with self._lock:
            return {
                "camera_ok": self.camera_ok,
                "latest_classification": self._latest_result.get("classification"),
                "latest_confidence": self._latest_result.get("confidence"),
            }