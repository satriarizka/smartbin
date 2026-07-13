import os
import logging
from datetime import datetime, timedelta

import time
from flask import Flask, jsonify, render_template, send_from_directory, Response
from sqlalchemy import func

from models import db, Detection, BinLevel, SystemEvent
from detector import Detector
from serial_worker import SerialWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("app")

# ==================== KONFIG ====================
SERIAL_PORT = os.environ.get("SERIAL_PORT", "COM3")   # Windows: "COM5"
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))
# Kalibrasi sensor HC-SR04 kepenuhan bin:
#   BIN_EMPTY_CM = jarak sensor->sampah saat bin KOSONG (mentok fisik di dalam bin)  -> 0%
#   BIN_FULL_CM  = jarak sensor->sampah saat bin PENUH (harus sama dgn FULL_THRESHOLD_CM di firmware) -> 100%
BIN_EMPTY_CM = float(os.environ.get("BIN_EMPTY_CM", "15"))
BIN_FULL_CM = float(os.environ.get("BIN_FULL_CM", "3"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "smartbin.db")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

def _run_light_migrations():
    """Tambah kolom yang hilang di tabel-tabel existing (SQLite ALTER TABLE ADD COLUMN).

    db.create_all() HANYA membuat tabel yang belum ada - kalau tabel sudah ada
    tapi modelnya nambah kolom baru (mis. 'raw_label'), create_all() tidak akan
    otomatis menambahkannya. Ini migrasi ringan untuk kasus itu, supaya tidak
    perlu hapus file .db manual tiap ada perubahan skema kecil.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)

    # (nama_tabel, [(nama_kolom, definisi_sql), ...])
    expected_columns = {
        "detections": [
            ("raw_label", "VARCHAR(50)"),
        ],
    }

    for table_name, columns in expected_columns.items():
        if table_name not in inspector.get_table_names():
            continue  # tabel baru akan dibuat lengkap oleh create_all()

        existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
        for col_name, col_def in columns:
            if col_name not in existing_cols:
                logger.warning(f"Migrasi: menambah kolom '{col_name}' ke tabel '{table_name}'")
                with db.engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"))


with app.app_context():
    db.create_all()
    _run_light_migrations()

detector = Detector()
detector.start()   # buka kamera SEKALI & mulai streaming background (dipakai juga oleh /video_feed)

# Cache level terakhir supaya bisa dipakai lintas thread tanpa query terus
latest_state = {
    "organic_cm": None,
    "nonorganic_cm": None,
    "organic_full": False,
    "nonorganic_full": False,
    "esp32_connected": False,
    "last_seen": None,
}


# ==================== CALLBACK SERIAL ====================

def _level_to_percent(cm):
    """Konversi jarak sensor (cm) jadi persentase kapasitas, dikalibrasi dengan
    kondisi fisik bin: BIN_EMPTY_CM (mentok kosong) = 0%, BIN_FULL_CM = 100%.
    """
    if cm is None or cm < 0:
        return None
    span = BIN_EMPTY_CM - BIN_FULL_CM
    if span <= 0:
        return None
    pct = (BIN_EMPTY_CM - cm) / span * 100
    return max(0, min(100, round(pct, 1)))


def _capacity_status(pct):
    if pct is None:
        return "Tidak diketahui"
    if pct >= 85:
        return "Penuh"
    if pct >= 60:
        return "Sedang"
    return "Aman"


def handle_capture():
    """Dipanggil saat ESP32 kirim 'CAPTURE'. Ambil gambar, klasifikasi, simpan DB, balas ESP32.

    PENTING: tidak ada fallback/default classification. Kalau YOLO tidak
    menemukan objek dengan confidence >= threshold, backend mengirim
    'UNKNOWN' ke ESP32 (servo TIDAK digerakkan) TANPA menyimpan apapun ke
    database - tidak ada foto tersimpan, tidak ada baris riwayat, tidak ada
    notifikasi. Ini murni supaya sampah tidak salah masuk bin; bukan
    kejadian yang perlu dicatat/di-notif setiap saat karena bisa sangat
    sering terjadi (mis. saat sensor masuk trigger tapi belum ada objek
    stabil di depan kamera).
    """
    logger.info("Menerima sinyal CAPTURE, menjalankan YOLO...")
    with app.app_context():
        try:
            result = detector.classify()
            classification = result["classification"]

            if classification is None:
                logger.info(
                    f"Tidak ada objek dengan confidence memadai "
                    f"(terbaik: {result['confidence']:.2f}), servo tidak digerakkan - tidak dicatat"
                )
                worker.send_result("UNKNOWN")
                return

            image_path = detector.save_capture(result["_annotated_frame"])

            det = Detection(
                classification=classification,
                raw_label=result["raw_label"],
                confidence=result["confidence"],
                image_path=image_path,
            )
            db.session.add(det)
            db.session.commit()

            worker.send_result(classification)
            logger.info(f"Hasil klasifikasi: {classification} (conf={result['confidence']:.2f})")

        except Exception as e:
            logger.exception(f"Error saat capture/klasifikasi: {e}")
            log_event("ERROR", f"Gagal capture/klasifikasi: {e}")
            # Tidak menebak hasil - kirim UNKNOWN supaya ESP32 tidak menggerakkan servo apapun
            worker.send_result("UNKNOWN")


def handle_level(organic_cm, nonorg_cm, full_flag=None):
    with app.app_context():
        latest_state["last_seen"] = datetime.utcnow().isoformat()
        latest_state["esp32_connected"] = True

        if full_flag == "FULL_ORGANIC":
            latest_state["organic_full"] = True
            log_event("WARNING", "Bin organic penuh (<3cm)")
            return
        if full_flag == "FULL_NONORGANIC":
            latest_state["nonorganic_full"] = True
            log_event("WARNING", "Bin non-organic penuh (<3cm)")
            return

        latest_state["organic_cm"] = organic_cm
        latest_state["nonorganic_cm"] = nonorg_cm
        latest_state["organic_full"] = organic_cm is not None and 0 < organic_cm < 3
        latest_state["nonorganic_full"] = nonorg_cm is not None and 0 < nonorg_cm < 3

        level = BinLevel(
            organic_cm=organic_cm,
            nonorganic_cm=nonorg_cm,
            organic_full=latest_state["organic_full"],
            nonorganic_full=latest_state["nonorganic_full"],
        )
        db.session.add(level)
        db.session.commit()


def handle_ack(status):
    logger.info(f"ACK dari ESP32: {status}")
    if status == "TIMEOUT":
        with app.app_context():
            log_event("WARNING", "Servo timeout menunggu hasil klasifikasi")
    elif status == "UNKNOWN":
        logger.info("ESP32 mengonfirmasi: sampah tidak dipindahkan (klasifikasi tidak yakin)")


def handle_ready():
    logger.info("ESP32 mengirim READY")
    latest_state["esp32_connected"] = True
    with app.app_context():
        log_event("INFO", "ESP32 terhubung / restart")


def log_event(level, message):
    ev = SystemEvent(level=level, message=message)
    db.session.add(ev)
    db.session.commit()


worker = SerialWorker(
    port=SERIAL_PORT,
    baudrate=SERIAL_BAUD,
    on_capture=handle_capture,
    on_level=handle_level,
    on_ack=handle_ack,
    on_ready=handle_ready,
)
worker.start()


# ==================== ROUTES: DASHBOARD ====================

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/captures/<path:filename>")
def serve_capture(filename):
    return send_from_directory(os.path.join(BASE_DIR, "captures"), filename)


def _mjpeg_generator():
    while True:
        frame = detector.get_mjpeg_frame()
        if frame is not None:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        time.sleep(0.08)   # ~12 fps ke browser, cukup untuk preview


@app.route("/video_feed")
def video_feed():
    """Live stream YOLO (dengan bounding box) untuk ditampilkan di dashboard."""
    return Response(_mjpeg_generator(),
                     mimetype="multipart/x-mixed-replace; boundary=frame")


# ==================== ROUTES: API ====================

@app.route("/api/status")
def api_status():
    organic_pct = _level_to_percent(latest_state["organic_cm"])
    nonorganic_pct = _level_to_percent(latest_state["nonorganic_cm"])
    pcts = [p for p in (organic_pct, nonorganic_pct) if p is not None]
    total_pct = round(sum(pcts) / len(pcts), 1) if pcts else None

    return jsonify({
        **latest_state,
        "serial_connected": worker.connected,
        "camera": detector.get_status(),
        "organic_pct": organic_pct,
        "nonorganic_pct": nonorganic_pct,
        "total_pct": total_pct,
        "organic_status": _capacity_status(organic_pct),
        "nonorganic_status": _capacity_status(nonorganic_pct),
        "total_status": _capacity_status(total_pct),
    })


@app.route("/api/detections")
def api_detections():
    rows = Detection.query.order_by(Detection.timestamp.desc()).limit(50).all()
    return jsonify([r.to_dict() for r in rows])


@app.route("/api/levels")
def api_levels():
    rows = BinLevel.query.order_by(BinLevel.timestamp.desc()).limit(100).all()
    out = []
    for r in reversed(rows):
        d = r.to_dict()
        d["organic_pct"] = _level_to_percent(r.organic_cm)
        d["nonorganic_pct"] = _level_to_percent(r.nonorganic_cm)
        out.append(d)
    return jsonify(out)


@app.route("/api/events")
def api_events():
    rows = SystemEvent.query.order_by(SystemEvent.timestamp.desc()).limit(50).all()
    return jsonify([r.to_dict() for r in rows])


@app.route("/api/stats/summary")
def api_stats_summary():
    total = db.session.query(func.count(Detection.id)).scalar() or 0
    organic = db.session.query(func.count(Detection.id)).filter(
        Detection.classification == "ORGANIC").scalar() or 0
    non_organic = db.session.query(func.count(Detection.id)).filter(
        Detection.classification == "NON_ORGANIC").scalar() or 0

    since_24h = datetime.utcnow() - timedelta(hours=24)
    last24h = db.session.query(func.count(Detection.id)).filter(
        Detection.timestamp >= since_24h).scalar() or 0

    return jsonify({
        "total": total,
        "organic": organic,
        "non_organic": non_organic,
        "last_24h": last24h,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)