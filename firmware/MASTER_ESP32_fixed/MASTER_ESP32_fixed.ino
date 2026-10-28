const int INPUT_PIN = 10;   // ahora es únicamente SALIDA TTL hacia el VBOX

volatile bool     event_pending    = false;
volatile uint32_t event_micros     = 0;
volatile bool     event_is_inicio  = true;
volatile uint32_t last_isr_micros  = 0;
const    uint32_t DEBOUNCE_US      = 50000;  // 50 ms: ya no hay rebote mecánico,
                                              // solo protege de ruido eléctrico

void IRAM_ATTR onPinChange() {
    uint32_t now = micros();
    if ((now - last_isr_micros) < DEBOUNCE_US) return;
    last_isr_micros = now;
    event_micros    = now;
    // El nivel actual del pin nos dice sin ambigüedad qué evento es:
    // LOW = INICIO (TTL activo), HIGH = FIN (TTL inactivo/idle)
    event_is_inicio = (digitalRead(INPUT_PIN) == LOW);
    event_pending   = true;
}

void setup() {
    pinMode(INPUT_PIN,   OUTPUT);
    digitalWrite(INPUT_PIN, HIGH);   // estado idle = FIN / sin grabar
    pinMode(LED_BUILTIN, OUTPUT);
    Serial.begin(115200);
    delay(500);
    attachInterrupt(digitalPinToInterrupt(INPUT_PIN), onPinChange, CHANGE);
    digitalWrite(LED_BUILTIN, HIGH);
    Serial.println("MASTER_ESP32");

    Serial.print("SYNC,");
    Serial.println(micros());
}

unsigned long last_sync = 0;
String serial_buffer = "";

void loop() {
    // ── Comandos desde Python (GUI: botones Iniciar / Detener) ──
    while (Serial.available() > 0) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            serial_buffer.trim();
            if (serial_buffer.length() > 0) {

                if (serial_buffer == "INICIO") {
                    digitalWrite(INPUT_PIN, LOW);   // TTL activo → dispara el ISR

                } else if (serial_buffer == "FIN") {
                    digitalWrite(INPUT_PIN, HIGH);  // TTL idle → dispara el ISR

                } else if (serial_buffer == "RESET") {
                    Serial.println("MASTER_ESP32");
                }
            }
            serial_buffer = "";
        } else {
            serial_buffer += c;
        }
    }

    if (millis() - last_sync >= 1000) {
        Serial.print("SYNC,");
        Serial.println(micros());
        last_sync = millis();
    }

    if (event_pending) {
        noInterrupts();
        uint32_t ts        = event_micros;
        bool     is_inicio = event_is_inicio;
        event_pending      = false;
        interrupts();

        // SYNC antes del evento
        Serial.print("SYNC,");
        Serial.println(micros());

        // Evento
        if (is_inicio) {
            Serial.print("INICIO,");
            digitalWrite(LED_BUILTIN, LOW);
        } else {
            Serial.print("FIN,");
            digitalWrite(LED_BUILTIN, HIGH);
        }
        Serial.println(ts);

        // SYNC después del evento
        Serial.print("SYNC,");
        Serial.println(micros());

        last_sync = millis();
    }

    delay(1);
}
