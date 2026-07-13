#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

Adafruit_PWMServoDriver pca = Adafruit_PWMServoDriver(0x40);

// ==================== PIN ====================
#define TRIG_MASUK     5
#define ECHO_MASUK    18
#define TRIG_ORGANIC  23
#define ECHO_ORGANIC  19
#define TRIG_NONORG   25
#define ECHO_NONORG   26

#define SERVO_ORGANIC     0
#define SERVO_NON_ORGANIC 1

#define SERVO_MIN   150
#define SERVO_90    375

// ==================== KONFIG ====================
#define FULL_THRESHOLD_CM   3     // < 3 cm dianggap penuh
#define MASUK_MIN_CM        5
#define MASUK_MAX_CM        30
#define LEVEL_REPORT_MS     1000  // kirim data level tiap 1 detik
#define CAPTURE_COOLDOWN_MS 2000  // jeda antar deteksi sampah masuk

unsigned long lastLevelReport = 0;
unsigned long lastCaptureTime = 0;

// ==================== PROTOTIPE ====================
long readDistance(int trig, int echo);
String waitForClassifyResult();

void setup() {
  Serial.begin(115200);
  pca.begin();
  pca.setPWMFreq(50);

  pinMode(TRIG_MASUK, OUTPUT); pinMode(ECHO_MASUK, INPUT);
  pinMode(TRIG_ORGANIC, OUTPUT); pinMode(ECHO_ORGANIC, INPUT);
  pinMode(TRIG_NONORG, OUTPUT); pinMode(ECHO_NONORG, INPUT);

  closeServo(SERVO_ORGANIC);
  closeServo(SERVO_NON_ORGANIC);

  delay(300);
  Serial.println("READY");   // sinyal handshake ke backend Flask
}

void loop() {
  unsigned long now = millis();

  // ================= LAPORAN LEVEL (periodik) =================
  if (now - lastLevelReport >= LEVEL_REPORT_MS) {
    lastLevelReport = now;

    long levelOrganic = readDistance(TRIG_ORGANIC, ECHO_ORGANIC);
    long levelNonOrg   = readDistance(TRIG_NONORG, ECHO_NONORG);

    // Format terstruktur, gampang di-parse backend: LEVEL,<organic_cm>,<nonorganic_cm>
    Serial.print("LEVEL,");
    Serial.print(levelOrganic);
    Serial.print(",");
    Serial.println(levelNonOrg);

    if (levelOrganic > 0 && levelOrganic < FULL_THRESHOLD_CM) {
      Serial.println("FULL_ORGANIC");
    }
    if (levelNonOrg > 0 && levelNonOrg < FULL_THRESHOLD_CM) {
      Serial.println("FULL_NONORGANIC");
    }
  }

  // ================= DETEKSI SAMPAH MASUK =================
  if (now - lastCaptureTime >= CAPTURE_COOLDOWN_MS) {
    long distMasuk = readDistance(TRIG_MASUK, ECHO_MASUK);

    if (distMasuk > MASUK_MIN_CM && distMasuk < MASUK_MAX_CM) {
      lastCaptureTime = now;

      Serial.println("CAPTURE");   // minta backend capture + klasifikasi

      String result = waitForClassifyResult();

      if (result == "ORGANIC") {
        Serial.println("ACK,ORGANIC");
        openServo(SERVO_ORGANIC);
        delay(2500);
        closeServo(SERVO_ORGANIC);
      } else if (result == "NON_ORGANIC") {
        Serial.println("ACK,NON_ORGANIC");
        openServo(SERVO_NON_ORGANIC);
        delay(2500);
        closeServo(SERVO_NON_ORGANIC);
      } else {
        Serial.println("ACK,TIMEOUT");
      }
    }
  }
}

// ==================== FUNCTION ====================

long readDistance(int trig, int echo) {
  digitalWrite(trig, LOW);
  delayMicroseconds(2);
  digitalWrite(trig, HIGH);
  delayMicroseconds(10);
  digitalWrite(trig, LOW);
  long duration = pulseIn(echo, HIGH, 30000);
  if (duration == 0) return -1; // no echo / out of range
  return duration * 0.0343 / 2;
}

void openServo(int channel) {
  pca.setPWM(channel, 0, SERVO_90);
}

void closeServo(int channel) {
  pca.setPWM(channel, 0, SERVO_MIN);
}

// Tunggu balasan klasifikasi dari backend Flask ("ORGANIC" / "NON_ORGANIC")
// selama loop tetap harus jalan menerima data lain diabaikan.
String waitForClassifyResult() {
  unsigned long startTime = millis();
  while (millis() - startTime < 15000) {   // timeout 15 detik
    if (Serial.available()) {
      String response = Serial.readStringUntil('\n');
      response.trim();
      response.toUpperCase();
      if (response == "ORGANIC" || response == "NON_ORGANIC") {
        return response;
      }
      // baris lain (mis. noise) diabaikan
    }
  }
  return "";
}
