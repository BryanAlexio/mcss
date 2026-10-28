
/*
    El sistema de adquisición de señales (EEG/ECG/Video/LiDAR/VBOX) 
    depende de los mensajes enviados a través del puerto serie (por ejemplo, "INICIO" o "FIN") 
    para marcar las etapas del ciclo de datos que está capturando.
    PC calcula epoch real vía offset SYNC
    
    El VBOX depende de los pulsos de estado bajo (LOW) que recibe del pin PIN_SENSOR 
    para marcar el inicio y fin de los ciclos de datos. 
    Al recibir estos pulsos, el VBOX sabe cuándo comienza y termina un ciclo de adquisición de datos, 
    sincronizando sus propios registros con los del sistema de señales fisiológicas.


    De este modo, ambos sistemas están sincronizados, 
    a pesar de que se basan en diferentes mecanismos para captar los eventos 
    (mensajes por el puerto serie para el sistema de señales y pulsos de estado bajo para el VBOX). 
    Los pulsos de estado bajo son las señales que permiten que ambos sistemas mantengan un marco de referencia temporal 
    común para sus respectivos conjuntos de datos.

    Permitiendo una correlación precisa de los datos capturados, 
    incluso si los dispositivos están operando a diferentes tasas de muestreo.
    */

const int INPUT_PIN = 10;
volatile bool     event_pending    = false;
volatile uint32_t event_micros     = 0;
volatile bool     event_is_inicio  = true;
volatile uint32_t last_isr_micros  = 0;
const    uint32_t DEBOUNCE_US      = 200000;

void IRAM_ATTR onPinChange() {
    uint32_t now = micros();
    if ((now - last_isr_micros) < DEBOUNCE_US) return;
    last_isr_micros = now;
    event_micros    = now;
    event_pending   = true;
}

void setup() {
    pinMode(INPUT_PIN,   INPUT_PULLUP);
    pinMode(LED_BUILTIN, OUTPUT);
    Serial.begin(115200);
    delay(500);
    attachInterrupt(digitalPinToInterrupt(INPUT_PIN), onPinChange, CHANGE);
    digitalWrite(LED_BUILTIN, HIGH);
    Serial.println("MASTER_ESP32");

    // Enviar referencia temporal inmediatamente al conectar
    Serial.print("SYNC,");
    Serial.println(micros());
}

unsigned long last_sync = 0;

void loop() {
    // Heartbeat de sincronización cada 1 segundo
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
        event_is_inicio    = !event_is_inicio;
        interrupts();

        if (is_inicio) {
            Serial.print("INICIO,");
            digitalWrite(LED_BUILTIN, LOW);
        } else {
            Serial.print("FIN,");
            digitalWrite(LED_BUILTIN, HIGH);
        }
        Serial.println(ts);
    }
    delay(1);
}