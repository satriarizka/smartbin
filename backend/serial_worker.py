import time
import threading
import logging

import serial

logger = logging.getLogger("serial_worker")


class SerialWorker(threading.Thread):
    """Membaca serial ESP32 terus-menerus di background thread.

    Protokol yang dipahami (satu baris = satu pesan, dikirim ESP32):
      READY                        -> handshake awal
      LEVEL,<organic_cm>,<nonorganic_cm>
      FULL_ORGANIC / FULL_NONORGANIC
      CAPTURE                      -> minta backend capture + klasifikasi,
                                        backend WAJIB balas "ORGANIC" atau
                                        "NON_ORGANIC" via serial
      ACK,ORGANIC / ACK,NON_ORGANIC / ACK,TIMEOUT
    """

    def __init__(self, port, baudrate, on_capture, on_level, on_ack, on_ready=None,
                 reconnect_delay=3):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.on_capture = on_capture   # callback() -> None (harus panggil self.send_result sendiri)
        self.on_level = on_level       # callback(organic_cm, nonorg_cm)
        self.on_ack = on_ack           # callback(status_str)
        self.on_ready = on_ready       # callback()
        self.reconnect_delay = reconnect_delay

        self._ser = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self.connected = False

    def connect(self):
        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=1)
            time.sleep(2)  # tunggu ESP32 reset setelah port dibuka
            self.connected = True
            logger.info(f"Terhubung ke ESP32 di {self.port}")
        except serial.SerialException as e:
            self.connected = False
            logger.warning(f"Gagal konek ke {self.port}: {e}")

    def run(self):
        while not self._stop_event.is_set():
            if not self.connected:
                self.connect()
                if not self.connected:
                    time.sleep(self.reconnect_delay)
                    continue

            try:
                line = self._ser.readline().decode("utf-8", errors="ignore").strip()
            except (serial.SerialException, OSError) as e:
                logger.error(f"Serial error: {e}, mencoba reconnect...")
                self.connected = False
                try:
                    self._ser.close()
                except Exception:
                    pass
                time.sleep(self.reconnect_delay)
                continue

            if not line:
                continue

            self._handle_line(line)

    def _handle_line(self, line: str):
        logger.debug(f"RX: {line}")

        if line == "READY":
            if self.on_ready:
                self.on_ready()

        elif line.startswith("LEVEL,"):
            parts = line.split(",")
            if len(parts) == 3:
                try:
                    organic_cm = float(parts[1])
                    nonorg_cm = float(parts[2])
                    self.on_level(organic_cm, nonorg_cm)
                except ValueError:
                    logger.warning(f"Format LEVEL tidak valid: {line}")

        elif line in ("FULL_ORGANIC", "FULL_NONORGANIC"):
            self.on_level(None, None, full_flag=line)

        elif line == "CAPTURE":
            # Jalankan di thread terpisah supaya tidak blocking pembacaan serial lain
            threading.Thread(target=self.on_capture, daemon=True).start()

        elif line.startswith("ACK,"):
            status = line.split(",", 1)[1]
            self.on_ack(status)

        else:
            logger.debug(f"Baris tidak dikenali: {line}")

    def send_result(self, classification: str):
        """Kirim hasil klasifikasi balik ke ESP32: 'ORGANIC' atau 'NON_ORGANIC'."""
        if not self.connected or self._ser is None:
            logger.error("Tidak bisa kirim hasil, serial belum terkoneksi")
            return
        with self._write_lock:
            try:
                self._ser.write((classification + "\n").encode("utf-8"))
                logger.info(f"TX: {classification}")
            except serial.SerialException as e:
                logger.error(f"Gagal kirim ke ESP32: {e}")

    def stop(self):
        self._stop_event.set()
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
