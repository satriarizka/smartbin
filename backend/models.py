from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Detection(db.Model):
    """Riwayat setiap sampah yang terdeteksi & diklasifikasi."""
    __tablename__ = "detections"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    classification = db.Column(db.String(20), nullable=False)   # ORGANIC / NON_ORGANIC / UNKNOWN
    raw_label = db.Column(db.String(50), nullable=True)          # nama class asli dari YOLO (mis. "organic")
    confidence = db.Column(db.Float, nullable=True)
    image_path = db.Column(db.String(255), nullable=True)
    servo_status = db.Column(db.String(20), default="OK")       # OK / TIMEOUT / SKIPPED

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "classification": self.classification,
            "raw_label": self.raw_label,
            "confidence": self.confidence,
            "image_path": self.image_path,
            "servo_status": self.servo_status,
        }


class BinLevel(db.Model):
    """Snapshot level ketinggian sampah tiap kali ESP32 melapor."""
    __tablename__ = "bin_levels"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    organic_cm = db.Column(db.Float, nullable=True)
    nonorganic_cm = db.Column(db.Float, nullable=True)
    organic_full = db.Column(db.Boolean, default=False)
    nonorganic_full = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "organic_cm": self.organic_cm,
            "nonorganic_cm": self.nonorganic_cm,
            "organic_full": self.organic_full,
            "nonorganic_full": self.nonorganic_full,
        }


class SystemEvent(db.Model):
    """Log event umum (koneksi ESP32 putus, error kamera, dll)."""
    __tablename__ = "system_events"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    level = db.Column(db.String(10), default="INFO")  # INFO / WARNING / ERROR
    message = db.Column(db.String(255))

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "message": self.message,
        }