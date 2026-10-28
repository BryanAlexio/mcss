/*
 * ESP32-S3 ESCLAVO con MQTT + ESP-NOW
 * IDENTIFICACIÓN DE ELECTRODOS ECG
 * VERSION: IP dinámica por ESP-NOW + envío MQTT optimizado (anti-saturación)
 *
 * Flujo: la Raspberry (Mestrodatos) interpreta IP e indicadores del servidor web y los
 * manda al ESP32 maestro; este los reenvía por ESP-NOW. Los esclavos reciben las
 * indicaciones por ESP-NOW y envían datos al broker MQTT (Raspberry), que los pasa
 * al servidor para su visualización.
 *
 * Cambios respecto a la versión anterior:
 *  - Payload compacto opcional (COMPACT_PAYLOAD) y tamaño de paquete variable (50..200)
 *  - Sin busy-wait ni prints pesados en la ruta TDM
 *  - PubSubClient protegido con mutex; nada de MQTT dentro del callback de ESP-NOW
 *  - WiFi sin ahorro de energía, TCP sin Nagle, reintentos MQTT con espera
 *  - sampleNum a 32 bits (antes se desbordaba a los 65 s)
 */
#include <Arduino.h>
#include <SD_MMC.h>
#include <SPI.h>
#include <FastLED.h>
#include <esp_now.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <driver/adc.h>
#include <esp_adc_cal.h>
#include <esp_wifi.h>
#include <ArduinoJson.h>

// ========== CONFIGURACION WIFI ==========
#define WIFI_CHANNEL 6  // Canal de tu router "US_CICATA"

// ========== CONFIGURACION ELECTRODO ECG ==========
#define SLAVE_ID 2
#if SLAVE_ID == 1
  #define ELECTRODE_NAME "RA"
  #define ELECTRODE_POSITION "Brazo Derecho"
  #define ELECTRODE_COLOR 0xFF0000  // Rojo
#elif SLAVE_ID == 2
  #define ELECTRODE_NAME "LA"
  #define ELECTRODE_POSITION "Brazo Izquierdo"
  #define ELECTRODE_COLOR 0xFFFF00  // Amarillo
#elif SLAVE_ID == 3
  #define ELECTRODE_NAME "LL"
  #define ELECTRODE_POSITION "Pierna Izquierda"
  #define ELECTRODE_COLOR 0x00FF00  // Verde
#elif SLAVE_ID == 4
  #define ELECTRODE_NAME "RL"
  #define ELECTRODE_POSITION "Pierna Derecha (Tierra)"
  #define ELECTRODE_COLOR 0x0000FF  // Azul
#elif SLAVE_ID == 5
  #define ELECTRODE_NAME "V1"
  #define ELECTRODE_POSITION "Precordial 1"
  #define ELECTRODE_COLOR 0xFF00FF  // Magenta
#elif SLAVE_ID == 6
  #define ELECTRODE_NAME "V2"
  #define ELECTRODE_POSITION "Precordial 2"
  #define ELECTRODE_COLOR 0x00FFFF  // Cian
#elif SLAVE_ID == 7
  #define ELECTRODE_NAME "V3"
  #define ELECTRODE_POSITION "Precordial 3"
  #define ELECTRODE_COLOR 0xFFA500  // Naranja
#elif SLAVE_ID == 8
  #define ELECTRODE_NAME "V4"
  #define ELECTRODE_POSITION "Precordial 4"
  #define ELECTRODE_COLOR 0x800080  // Púrpura
#elif SLAVE_ID == 9
  #define ELECTRODE_NAME "V5"
  #define ELECTRODE_POSITION "Precordial 5"
  #define ELECTRODE_COLOR 0xFFC0CB  // Rosa
#else
  #define ELECTRODE_NAME "V6"
  #define ELECTRODE_POSITION "Precordial 6"
  #define ELECTRODE_COLOR 0x808080  // Gris
#endif

// ========== WIFI ==========
const char* ssid = "TU_WIFI_SSID";          // <-- reemplazar con su red WiFi
const char* password = "TU_WIFI_PASSWORD";  // <-- reemplazar con su contraseña

// ========== CONFIGURACIÓN SERVIDOR MQTT (RECIBIDA POR ESP-NOW) ==========
String mqtt_server_ip = "";
bool server_ip_received = false;
const int mqtt_port = 1883;

#define MQTT_TOPIC_DATA     "esp32/slave/data"
#define MQTT_TOPIC_STATUS   "esp32/slave/status"
#define MQTT_TOPIC_REGISTER "esp32/slave/register"
#define MQTT_TOPIC_ACK      "esp32/slave/ack"
#define MQTT_TOPIC_REQUEST  "esp32/master/request/"

// ==================== CONFIGURACIÓN SD ====================
#define SD_RETRY_ATTEMPTS 3
#define SD_RETRY_DELAY_MS 500
#define SD_MIN_FREE_SPACE_MB 10

// ========== LED ==========
#define LED_PIN     3
#define NUM_LEDS    1
#define BRIGHTNESS  50
#define LED_TYPE    WS2812B
#define COLOR_ORDER GRB

CRGB leds[NUM_LEDS];

// Pines para SD_MMC
#define PIN_CLK 15
#define PIN_CMD 7
#define PIN_D0  16
#define PIN_D1  10
#define PIN_D2  21
#define PIN_D3  14

// ========== ADC ==========
#define ADC_PIN 2
#define ADC_CHANNEL ADC1_CHANNEL_1
#define ADC_ATTEN ADC_ATTEN_DB_11
#define ADC_WIDTH ADC_WIDTH_BIT_12

esp_adc_cal_characteristics_t adc_chars;

// ========== ADQUISICION ==========
#define ACQ_FREQ_HZ 1000
#define ACQ_INTERVAL_MS 1

// ========== BUFFER CIRCULAR Y ENVÍO ==========
#define CIRCULAR_BUFFER_SIZE 10000
#define MQTT_BATCH_SIZE      50     // mínimo deseado por paquete (50 ms de datos)
#define TDM_MAX_SAMPLES      400    // máximo por solicitud (para ponerse al día si hay atraso)
#define TDM_DEADLINE_MS      90     // tiempo que da el maestro para responder
#define SD_BATCH_SIZE        50
#define MQTT_PAYLOAD_SIZE    4096   // 400 muestras compactas ≈ 2.2 KB

// 0 = formato original  "samples":[[ts,num,adc],...]   (tope ~121 muestras por paquete)
// 1 = formato compacto  "n0","t0","t1","adc":[...]     (5x menos tráfico; requiere el parche en la Raspberry)
#define COMPACT_PAYLOAD      1

#define TDM_VERBOSE          0      // 1 = imprime un resumen por paquete

struct SampleData {
  uint64_t timestamp_us;
  uint32_t sampleNum;   // antes uint16_t: se desbordaba a los 65 s
  uint16_t adcValue;
};

SampleData circularBuffer[CIRCULAR_BUFFER_SIZE];
volatile uint32_t writeIndex = 0;
volatile uint32_t readIndexMQTT = 0;
volatile uint32_t readIndexSD = 0;
volatile bool bufferOverflow = false;

// Mutex recursivo: PubSubClient no es thread-safe
SemaphoreHandle_t mqttMutex;
#define MQTT_LOCK()   xSemaphoreTakeRecursive(mqttMutex, portMAX_DELAY)
#define MQTT_UNLOCK() xSemaphoreGiveRecursive(mqttMutex)

// ========== ESP-NOW ==========
#define CMD_PING      1
#define CMD_PONG      2
#define CMD_START     3
#define CMD_STOP      4
#define CMD_RESET     5
#define CMD_SET_IP    6

typedef struct __attribute__((packed)) {
  uint8_t comando;
  uint8_t datos[10];
} mensaje_simple_t;

typedef struct __attribute__((packed)) {
  uint8_t comando;
  char ip[16];
} mensaje_ip_t;

// ========== VARIABLES GLOBALES ==========
volatile bool startAcquisition = false;
volatile bool acquisitionActive = false;
volatile bool acquisitionFinished = false;
volatile bool systemReset = false;
volatile uint32_t total_samples = 0;

bool ping_recibido = false;
bool pong_enviado = false;
volatile bool mqtt_registrado = false;
unsigned long tiempo_registro_mqtt = 0;

WiFiClient espClient;
PubSubClient mqttClient(espClient);
volatile bool mqtt_connected = false;
uint32_t mqtt_packet_id = 0;
uint32_t mqtt_sent_count = 0;
uint32_t mqtt_error_count = 0;

char filename[32];
uint32_t sd_written_count = 0;

uint8_t direccionMaestro[] = {0x94, 0x54, 0xC5, 0xE7, 0x1B, 0x30};
bool master_connected = false;

#define ESTADO_IDLE         0
#define ESTADO_DISCOVERY    1
#define ESTADO_REGISTRANDO  2
#define ESTADO_LISTO        3
#define ESTADO_ADQUIRIENDO  4
#define ESTADO_COMPLETADO   5
#define ESTADO_RESET        6

volatile uint8_t estado_actual = ESTADO_IDLE;

// ========== VARIABLES TDM ==========
volatile uint32_t requested_packet_id = 0;
volatile bool packet_request_pending = false;

// ========== FUNCIONES AUXILIARES BUFFER ==========
inline uint32_t nextIndex(uint32_t index) {
  return (index + 1) % CIRCULAR_BUFFER_SIZE;
}

inline uint32_t samplesAvailableMQTT() {
  uint32_t w = writeIndex, r = readIndexMQTT;
  return (w >= r) ? (w - r) : (CIRCULAR_BUFFER_SIZE - r + w);
}

inline uint32_t samplesAvailableSD() {
  uint32_t w = writeIndex, r = readIndexSD;
  return (w >= r) ? (w - r) : (CIRCULAR_BUFFER_SIZE - r + w);
}

inline bool isBufferFull() {
  uint32_t nextWrite = nextIndex(writeIndex);
  return (nextWrite == readIndexMQTT) || (nextWrite == readIndexSD);
}

// ========== WIFI ==========
void setupWiFi() {
  Serial.println("\n[WiFi] Configurando modo híbrido...");
  WiFi.mode(WIFI_STA);

  while (true) {
    Serial.println("[WiFi] Intentando conectar al canal " + String(WIFI_CHANNEL) + "...");

    esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
    WiFi.begin(ssid, password, WIFI_CHANNEL);

    uint8_t attempts = 0;
    Serial.print("Conectando");

    while (WiFi.status() != WL_CONNECTED && attempts < 30) {
      delay(500);
      Serial.print(".");
      attempts++;

      leds[0] = (attempts % 2 == 0) ? CRGB::Blue : CRGB::Black;
      FastLED.show();
    }

    if (WiFi.status() == WL_CONNECTED) {
      uint8_t actualChannel = WiFi.channel();

      if (actualChannel == WIFI_CHANNEL) {
        Serial.println(" OK!");
        Serial.print("[WiFi] IP: ");
        Serial.println(WiFi.localIP());
        Serial.print("[WiFi] Canal confirmado: ");
        Serial.println(actualChannel);
        Serial.print("[WiFi] RSSI: ");
        Serial.print(WiFi.RSSI());
        Serial.println(" dBm");

        leds[0] = CRGB::Green;
        FastLED.show();
        delay(1000);
        break;
      } else {
        Serial.println(" CANAL INCORRECTO!");
        Serial.println("[WiFi] Esperado: " + String(WIFI_CHANNEL) +
                       ", Obtenido: " + String(actualChannel));
        Serial.println("[WiFi] Desconectando y reiniciando...");

        WiFi.disconnect(true);
        delay(1000);

        leds[0] = CRGB::Orange;
        FastLED.show();
        delay(2000);

        WiFi.mode(WIFI_OFF);
        delay(100);
        WiFi.mode(WIFI_STA);
        delay(100);
      }
    } else {
      Serial.println(" FALLO!");
      Serial.println("[WiFi] Reintentando en 3 segundos...");

      leds[0] = CRGB::Red;
      FastLED.show();
      delay(3000);
    }
  }

  WiFi.setSleep(false);   // sin ahorro de energía: menos latencia en MQTT y ESP-NOW
}

// ========== CALLBACK MQTT CON SOPORTE TDM ==========
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String topic_str(topic);

  // 1) SOLICITUD TDM DEL MAESTRO (MÁXIMA PRIORIDAD)
  if (topic_str.indexOf("master/request") != -1) {
    int lastSlash = topic_str.lastIndexOf('/');
    if (lastSlash > 0) {
      int slave_in_topic = topic_str.substring(lastSlash + 1).toInt();
      if (slave_in_topic != SLAVE_ID) return;   // no es para mí
    }

    uint32_t packet_id = 0;
    bool parsed = false;

    DynamicJsonDocument doc(256);
    DeserializationError error = deserializeJson(doc, payload, length);

    if (!error && doc.containsKey("get_packet")) {
      packet_id = doc["get_packet"].as<uint32_t>();
      parsed = true;
    } else {
      // Parseo manual de respaldo
      String payload_str;
      payload_str.reserve(length);
      for (unsigned int i = 0; i < length; i++) payload_str += (char)payload[i];

      int get_packet_pos = payload_str.indexOf("get_packet");
      if (get_packet_pos >= 0) {
        int colon_pos = payload_str.indexOf(":", get_packet_pos);
        if (colon_pos > 0) {
          int end_pos = payload_str.length();
          int p;
          if ((p = payload_str.indexOf("}", colon_pos)) > 0 && p < end_pos) end_pos = p;
          if ((p = payload_str.indexOf("]", colon_pos)) > 0 && p < end_pos) end_pos = p;
          if ((p = payload_str.indexOf(",", colon_pos)) > 0 && p < end_pos) end_pos = p;

          String packet_str = payload_str.substring(colon_pos + 1, end_pos);
          packet_str.trim();
          uint32_t id = packet_str.toInt();
          if (id > 0) {
            packet_id = id;
            parsed = true;
          }
        }
      }
    }

    if (parsed) {
      noInterrupts();
      requested_packet_id = packet_id;
      packet_request_pending = true;
      interrupts();
#if TDM_VERBOSE
      Serial.printf("[TDM] Solicitud recibida: paquete #%lu\n", packet_id);
#endif
    } else {
      Serial.println("[TDM] ⚠️  No se pudo interpretar la solicitud");
    }
    return;
  }

  // 2) ACK
  if (topic_str.indexOf("ack") != -1) return;

  // 3) CONTROL
  if (topic_str.indexOf("control") != -1) {
    Serial.printf("[MQTT] Comando de control: %s\n", topic);
    return;
  }
}

// ========== EMPAQUETADO ==========
// Construye el JSON de un paquete con 'n' muestras desde readIndexMQTT.
// NO avanza readIndexMQTT: el llamador lo avanza solo si publicó con éxito.
// Devuelve en 'packed' cuántas muestras entraron.
static void buildPacket(char* buf, size_t cap, uint32_t pktId, uint32_t n,
                        bool tdm, bool isFinal, uint32_t waitMs, uint32_t &packed) {
#if COMPACT_PAYLOAD
  const uint32_t maxFit = (cap - 220) / 6;    // ~5 caracteres por muestra
#else
  const uint32_t maxFit = (cap - 220) / 32;   // ~29 caracteres por muestra
#endif
  if (n > maxFit) n = maxFit;
  packed = n;
  if (n == 0) { buf[0] = '\0'; return; }

  uint32_t idx = readIndexMQTT;
  const SampleData first = circularBuffer[idx];
  const SampleData last  = circularBuffer[(idx + n - 1) % CIRCULAR_BUFFER_SIZE];

#if COMPACT_PAYLOAD
  // Muestra i tiene sampleNum = n0 + i; tiempo interpolable entre t0 y t1.
  int len = snprintf(buf, cap,
    "{\"id\":%lu,\"slave\":%d,\"tdm\":%s,\"final\":%s,\"count\":%lu,\"wait_ms\":%lu,"
    "\"n0\":%lu,\"t0\":%llu,\"t1\":%llu,\"adc\":[",
    (unsigned long)pktId, SLAVE_ID, tdm ? "true" : "false", isFinal ? "true" : "false",
    (unsigned long)n, (unsigned long)waitMs,
    (unsigned long)first.sampleNum, first.timestamp_us, last.timestamp_us);
  for (uint32_t i = 0; i < n; i++) {
    len += snprintf(buf + len, cap - len, "%s%u", i ? "," : "", circularBuffer[idx].adcValue);
    idx = nextIndex(idx);
  }
#else
  int len = snprintf(buf, cap,
    "{\"id\":%lu,\"slave\":%d,\"tdm\":%s,\"final\":%s,\"count\":%lu,\"wait_ms\":%lu,\"samples\":[",
    (unsigned long)pktId, SLAVE_ID, tdm ? "true" : "false", isFinal ? "true" : "false",
    (unsigned long)n, (unsigned long)waitMs);
  for (uint32_t i = 0; i < n; i++) {
    const SampleData &s = circularBuffer[idx];
    len += snprintf(buf + len, cap - len, "%s[%llu,%lu,%u]", i ? "," : "",
                    s.timestamp_us, (unsigned long)s.sampleNum, s.adcValue);
    idx = nextIndex(idx);
  }
#endif
  snprintf(buf + len, cap - len, "]}");
}

// ========== MQTT ==========
void reconnectMQTT() {
  if (!server_ip_received) return;   // no intentar con IP vacía

  MQTT_LOCK();
  if (!mqttClient.connected()) {
    Serial.print("[MQTT] Conectando a broker ");
    Serial.print(mqtt_server_ip);
    Serial.print("...");

    String clientId = "ESP32_" + String(ELECTRODE_NAME) + "_" + String(SLAVE_ID);

    mqttClient.setServer(mqtt_server_ip.c_str(), mqtt_port);
    mqttClient.setCallback(mqttCallback);

    if (mqttClient.connect(clientId.c_str())) {
      Serial.println(" OK");
      mqtt_connected = true;

      String tdm_topic = String(MQTT_TOPIC_REQUEST) + String(SLAVE_ID);
      mqttClient.subscribe(tdm_topic.c_str());

      String ackTopic = String(MQTT_TOPIC_ACK) + "/" + String(SLAVE_ID);
      mqttClient.subscribe(ackTopic.c_str());

      Serial.printf("[MQTT] ✓ Conectado a servidor: %s:%d\n", mqtt_server_ip.c_str(), mqtt_port);
    } else {
      Serial.printf(" FALLO (rc=%d)\n", mqttClient.state());
      mqtt_connected = false;
    }
  }
  MQTT_UNLOCK();
}

void registrarEnMQTT() {
  if (!server_ip_received) {
    Serial.println("[MQTT] ⚠️  Esperando IP del servidor...");
    return;
  }

  MQTT_LOCK();

  if (!mqtt_connected || !mqttClient.connected()) {
    reconnectMQTT();
    if (!mqtt_connected) {
      Serial.println("[MQTT] No se pudo conectar para registro");
      MQTT_UNLOCK();
      return;
    }
  }

  char registro[512];
  snprintf(registro, sizeof(registro),
           "{"
           "\"event\":\"slave_connected\","
           "\"slave_id\":%d,"
           "\"electrode_name\":\"%s\","
           "\"electrode_position\":\"%s\","
           "\"electrode_color\":\"#%06X\","
           "\"mac\":\"%s\","
           "\"ip\":\"%s\","
           "\"rssi\":%d,"
           "\"server_ip\":\"%s\","
           "\"timestamp\":%lu"
           "}",
           SLAVE_ID,
           ELECTRODE_NAME,
           ELECTRODE_POSITION,
           ELECTRODE_COLOR,
           WiFi.macAddress().c_str(),
           WiFi.localIP().toString().c_str(),
           WiFi.RSSI(),
           mqtt_server_ip.c_str(),
           millis());

  String registerTopic = String(MQTT_TOPIC_REGISTER) + "/" + String(SLAVE_ID);

  if (mqttClient.publish(registerTopic.c_str(), registro)) {
    Serial.println("[MQTT] ✓ Registro enviado al broker");
    Serial.printf("[ELECTRODO] ID: %d | Nombre: %s | Posición: %s\n",
                  SLAVE_ID, ELECTRODE_NAME, ELECTRODE_POSITION);
    Serial.printf("[SERVER] IP: %s:%d | RSSI: %d dBm\n",
                  mqtt_server_ip.c_str(), mqtt_port, WiFi.RSSI());

    mqtt_registrado = true;

    String statusTopic = String(MQTT_TOPIC_STATUS) + "/" + String(SLAVE_ID);
    mqttClient.publish(statusTopic.c_str(), "online");

    String lastRegTopic = String(MQTT_TOPIC_STATUS) + "/" + String(SLAVE_ID) + "/last_register";
    mqttClient.publish(lastRegTopic.c_str(), String(millis()).c_str());
  } else {
    Serial.println("[MQTT] ✗ ERROR: No se pudo enviar registro");
    mqtt_registrado = false;
  }

  MQTT_UNLOCK();
}

// ========== ADC ==========
void setupADC() {
  adc1_config_width(ADC_WIDTH);
  adc1_config_channel_atten(ADC_CHANNEL, ADC_ATTEN);
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN, ADC_WIDTH, 1100, &adc_chars);
  Serial.println("[ADC] Configurado: 12 bits, 1000 Hz");
}

// ========== LED ==========
void actualizarLED() {
  static unsigned long ultimo_parpadeo = 0;

  switch (estado_actual) {
    case ESTADO_IDLE:
      leds[0] = CRGB::White;
      break;

    case ESTADO_DISCOVERY:
      if (millis() - ultimo_parpadeo > 500) {
        static bool led_on = false;
        leds[0] = led_on ? CRGB::Blue : CRGB::Black;
        led_on = !led_on;
        ultimo_parpadeo = millis();
      }
      break;

    case ESTADO_REGISTRANDO:
      if (millis() - ultimo_parpadeo > 120) {
        static bool led_on = false;
        leds[0] = led_on ? CRGB::Yellow : CRGB::Black;
        led_on = !led_on;
        ultimo_parpadeo = millis();
      }
      break;

    case ESTADO_LISTO:
      leds[0] = CRGB(ELECTRODE_COLOR);
      break;

    case ESTADO_ADQUIRIENDO:
      if (mqtt_connected) {
        uint8_t brightness = 128 + (sin(millis() * 0.01) * 127);
        leds[0] = CRGB(ELECTRODE_COLOR);
        leds[0].fadeToBlackBy(255 - brightness);
      } else {
        leds[0] = CRGB::Orange;
      }
      break;

    case ESTADO_COMPLETADO:
      leds[0] = CRGB::Cyan;
      break;

    case ESTADO_RESET:
      leds[0] = CRGB::Red;
      break;
  }
  FastLED.show();
}

// ========== ARCHIVO SD ==========
String generateSessionFilename() {
  for (int i = 1; i < 1000; i++) {
    sprintf(filename, "/%s_%03d_%03d.csv", ELECTRODE_NAME, SLAVE_ID, i);
    if (!SD_MMC.exists(filename)) {
      return String(filename);
    }
  }
  sprintf(filename, "/%s_data.csv", ELECTRODE_NAME);
  return String(filename);
}

// ========== RESET ==========
void ejecutarReset() {
  Serial.println("\n========================================");
  Serial.printf("=== [CMD] RESET RECIBIDO - %s ===\n", ELECTRODE_NAME);
  Serial.println("========================================");

  estado_actual = ESTADO_RESET;

  if (acquisitionActive) {
    Serial.println("Deteniendo adquisición activa...");
    startAcquisition = false;
    delay(500);
  }

  if (mqtt_connected) {
    MQTT_LOCK();
    String statusTopic = String(MQTT_TOPIC_STATUS) + "/" + String(SLAVE_ID);
    mqttClient.publish(statusTopic.c_str(), "resetting");
    MQTT_UNLOCK();
    delay(100);
  }

  leds[0] = CRGB::Red;
  FastLED.show();
  delay(500);

  Serial.println(">>> REINICIANDO AHORA <<<\n");
  Serial.flush();

  ESP.restart();
}

// ========== ESP-NOW ==========
void OnDataSent(const esp_now_send_info_t *info, esp_now_send_status_t status) {
  if (pong_enviado && !mqtt_registrado && status == ESP_NOW_SEND_SUCCESS) {
    Serial.printf("[ESP-NOW] PONG enviado exitosamente - %s (ID:%d)\n",
                  ELECTRODE_NAME, SLAVE_ID);
  }
}

void enviarPong() {
  mensaje_simple_t msg;
  msg.comando = CMD_PONG;
  msg.datos[0] = SLAVE_ID;
  memset(&msg.datos[1], 0, sizeof(msg.datos) - 1);

  esp_err_t result = esp_now_send(direccionMaestro, (uint8_t *)&msg, sizeof(msg));

  if (result == ESP_OK) {
    pong_enviado = true;
    Serial.printf("[ESP-NOW] PONG enviado - %s (ID:%d)\n", ELECTRODE_NAME, SLAVE_ID);
  } else {
    Serial.println("[ESP-NOW] Error enviando PONG");
  }
}

// NOTA: este callback corre en la tarea WiFi. No se usa MQTT aquí;
// solo se cambian banderas y las tareas se encargan del resto.
void OnDataRecv(const esp_now_recv_info_t *recv_info, const uint8_t *incomingData, int len) {

  // ========== MENSAJE DE IP ==========
  if (len == sizeof(mensaje_ip_t)) {
    mensaje_ip_t *ip_msg = (mensaje_ip_t *)incomingData;

    if (ip_msg->comando == CMD_SET_IP) {

      if (acquisitionActive) {
        Serial.println("\n[IP] ⚠️  Adquisición activa - Ignorando actualización de IP");
        return;
      }

      char ipbuf[17];
      memcpy(ipbuf, ip_msg->ip, 16);
      ipbuf[16] = '\0';
      String nueva_ip = String(ipbuf);
      nueva_ip.trim();

      if (nueva_ip.length() > 0) {
        bool ip_changed = (mqtt_server_ip != nueva_ip);

        mqtt_server_ip = nueva_ip;
        server_ip_received = true;

        Serial.printf("[IP] Servidor recibido: %s (%s)\n", mqtt_server_ip.c_str(),
                      ip_changed ? "cambió" : "sin cambios");

        // Si la IP cambió, forzar reconexión desde la tarea MQTT
        if (ip_changed) {
          mqtt_connected = false;
          mqtt_registrado = false;
          mqttClient.setServer(mqtt_server_ip.c_str(), mqtt_port);
        }

        if (ping_recibido && !mqtt_registrado && !acquisitionActive) {
          estado_actual = ESTADO_REGISTRANDO;
        }

        leds[0] = CRGB::Cyan;
        FastLED.show();
      }
      return;
    }
  }

  // ========== MENSAJES NORMALES ==========
  if (len < (int)sizeof(mensaje_simple_t)) return;

  mensaje_simple_t msg;
  memcpy(&msg, incomingData, sizeof(mensaje_simple_t));

  if (!master_connected) {
    memcpy(direccionMaestro, recv_info->src_addr, 6);
    master_connected = true;

    esp_now_peer_info_t peerInfo{};
    memcpy(peerInfo.peer_addr, direccionMaestro, 6);
    peerInfo.channel = WIFI_CHANNEL;
    peerInfo.encrypt = false;
    esp_now_add_peer(&peerInfo);

    Serial.printf("[ESP-NOW] Maestro conectado: %02X:%02X:%02X:%02X:%02X:%02X\n",
                 direccionMaestro[0], direccionMaestro[1], direccionMaestro[2],
                 direccionMaestro[3], direccionMaestro[4], direccionMaestro[5]);
  }

  switch (msg.comando) {
    case CMD_PING:
      Serial.printf("\n>>> [ESP-NOW] PING RECIBIDO - %s (IP recibida: %s) <<<\n",
                    ELECTRODE_NAME, server_ip_received ? "SI" : "NO");

      if (!acquisitionActive) {
        ping_recibido = true;
        enviarPong();

        // registrationTask se encarga de (re)registrar en MQTT
        mqtt_registrado = false;
        estado_actual = ESTADO_REGISTRANDO;
        tiempo_registro_mqtt = millis();
      } else {
        Serial.println("[ESP-NOW] PING ignorado: adquisición activa");
      }
      break;

    case CMD_START:
      Serial.printf("\n=== CMD_START RECIBIDO - %s ===\n", ELECTRODE_NAME);

      if (!server_ip_received) {
        Serial.println("[ERROR] No se puede iniciar: falta IP del servidor");
        break;
      }

      if (!acquisitionActive && estado_actual == ESTADO_LISTO) {
        writeIndex = 0;
        readIndexMQTT = 0;
        readIndexSD = 0;
        bufferOverflow = false;
        mqtt_packet_id = 0;
        mqtt_sent_count = 0;
        mqtt_error_count = 0;
        sd_written_count = 0;

        startAcquisition = true;
        acquisitionFinished = false;
        estado_actual = ESTADO_ADQUIRIENDO;
        Serial.println("[START] ✓ Adquisición solicitada");
      } else {
        Serial.printf("[START] ✗ Condiciones NO cumplidas (activa=%d, estado=%d, requerido=%d)\n",
                      acquisitionActive, estado_actual, ESTADO_LISTO);
      }
      break;

    case CMD_STOP:
      Serial.printf("\n=== [ESP-NOW] STOP recibido - %s ===\n", ELECTRODE_NAME);
      startAcquisition = false;
      break;

    case CMD_RESET:
      ejecutarReset();
      break;

    default:
      Serial.printf("[ESP-NOW] Comando desconocido: %d\n", msg.comando);
      break;
  }
}

// ========== TAREAS ==========
void acquisitionTask(void *parameter) {
  TickType_t xLastWakeTime = xTaskGetTickCount();
  const TickType_t xFrequency = pdMS_TO_TICKS(ACQ_INTERVAL_MS);

  uint64_t start_us = 0;
  uint32_t overflow_count = 0;
  unsigned long last_progress = 0;

  for (;;) {
    if (systemReset) {
      vTaskDelay(50 / portTICK_PERIOD_MS);
      continue;
    }

    // Iniciar adquisición
    if (startAcquisition && !acquisitionActive) {
      acquisitionActive = true;
      acquisitionFinished = false;
      estado_actual = ESTADO_ADQUIRIENDO;

      start_us = esp_timer_get_time();
      xLastWakeTime = xTaskGetTickCount();
      total_samples = 0;
      overflow_count = 0;
      bufferOverflow = false;

      readIndexMQTT = writeIndex;
      readIndexSD = writeIndex;

      Serial.println("\n>>> ADQUISICION INICIADA <<<");
      Serial.printf("Frecuencia: %d Hz | Buffer: %d muestras\n",
                    ACQ_FREQ_HZ, CIRCULAR_BUFFER_SIZE);
      last_progress = millis();
    }

    if (acquisitionActive) {
      uint16_t adc_val = adc1_get_raw(ADC_CHANNEL);
      uint64_t timestamp = esp_timer_get_time() - start_us;

      if (!isBufferFull()) {
        circularBuffer[writeIndex].timestamp_us = timestamp;
        circularBuffer[writeIndex].sampleNum = total_samples;
        circularBuffer[writeIndex].adcValue = adc_val;
        writeIndex = nextIndex(writeIndex);
      } else {
        if (!bufferOverflow) {
          bufferOverflow = true;
          Serial.println("[WARN] Buffer circular lleno!");
        }
        overflow_count++;
      }

      total_samples++;

      if (millis() - last_progress >= 5000) {
        Serial.printf("[ADC] Muestras: %lu | MQTT pend: %lu | SD pend: %lu | Overflow: %lu\n",
                      total_samples, samplesAvailableMQTT(), samplesAvailableSD(), overflow_count);
        last_progress = millis();
      }

      xTaskDelayUntil(&xLastWakeTime, xFrequency);

      if (!startAcquisition) {
        acquisitionActive = false;
        acquisitionFinished = true;
        estado_actual = ESTADO_COMPLETADO;

        uint64_t elapsed_us = esp_timer_get_time() - start_us;
        Serial.println("\n>>> ADQUISICION DETENIDA <<<");
        Serial.printf("Total: %lu muestras | Tiempo: %.2f s | Frecuencia real: %.1f Hz\n",
                      total_samples,
                      elapsed_us / 1000000.0,
                      total_samples / (elapsed_us / 1000000.0));
      }
    } else {
      vTaskDelay(20 / portTICK_PERIOD_MS);
    }
  }
}

void sdWriterTask(void *parameter) {
  File dataFile;
  bool fileOpen = false;
  bool sd_finished = false;

  for (;;) {
    if (systemReset) {
      if (fileOpen) {
        dataFile.close();
        fileOpen = false;
      }
      sd_finished = false;
      vTaskDelay(100 / portTICK_PERIOD_MS);
      continue;
    }

    if (startAcquisition && !fileOpen) {
      String sessionFilename = generateSessionFilename();
      strcpy(filename, sessionFilename.c_str());

      dataFile = SD_MMC.open(filename, FILE_WRITE);
      if (dataFile) {
        fileOpen = true;
        sd_finished = false;
        Serial.printf("[SD] Archivo: %s\n", filename);
      } else {
        Serial.println("[SD] ERROR al abrir archivo");
      }
    }

    if (fileOpen) {
      char writeBuffer[SD_BATCH_SIZE][48];
      uint16_t bufferIndex = 0;
      bool keepWriting = true;

      while (keepWriting && fileOpen && !systemReset) {
        while (samplesAvailableSD() > 0 && bufferIndex < SD_BATCH_SIZE) {
          SampleData s = circularBuffer[readIndexSD];
          readIndexSD = nextIndex(readIndexSD);

          sprintf(writeBuffer[bufferIndex], "%llu,%lu,%u\n",
                  s.timestamp_us,
                  (unsigned long)s.sampleNum,
                  s.adcValue);

          bufferIndex++;
        }

        if (bufferIndex >= SD_BATCH_SIZE || (acquisitionFinished && bufferIndex > 0)) {
          for (uint16_t i = 0; i < bufferIndex; i++) {
            dataFile.print(writeBuffer[i]);
          }
          dataFile.flush();
          sd_written_count += bufferIndex;
          bufferIndex = 0;
        }

        if (acquisitionFinished && samplesAvailableSD() == 0) {
          keepWriting = false;
        }

        if (samplesAvailableSD() == 0 && !acquisitionFinished) {
          vTaskDelay(10 / portTICK_PERIOD_MS);
        }
      }

      if (!keepWriting && fileOpen) {
        dataFile.flush();
        long fileSize = dataFile.size();
        dataFile.close();
        fileOpen = false;
        sd_finished = true;

        Serial.println("\n========================================");
        Serial.println("[SD] ESCRITURA FINALIZADA");
        Serial.printf("Archivo: %s | Tamaño: %ld bytes\n", filename, fileSize);
        Serial.printf("Escritas: %lu | Total: %lu\n", sd_written_count, total_samples);
        Serial.println("========================================\n");

        sd_written_count = 0;

        if (!systemReset) {
          estado_actual = ESTADO_COMPLETADO;
          vTaskDelay(2000 / portTICK_PERIOD_MS);

          if (!startAcquisition && !systemReset) {
            estado_actual = ESTADO_LISTO;
            Serial.println("\n>>> SISTEMA LISTO PARA NUEVA ADQUISICION <<<\n");
          }
        }
      }
    } else {
      if (startAcquisition && sd_finished) {
        sd_finished = false;
      }
    }

    vTaskDelay(20 / portTICK_PERIOD_MS);
  }
}

void mqttWriterTask(void *parameter) {
  static char mqttPayload[MQTT_PAYLOAD_SIZE];
  bool mqtt_finished = false;
  uint32_t lastReconnect = 0;

  Serial.println("[MQTT-Writer] Iniciado (TDM, tamaño variable)");

  for (;;) {
    if (systemReset) {
      mqtt_finished = false;
      packet_request_pending = false;
      vTaskDelay(100 / portTICK_PERIOD_MS);
      continue;
    }

    // Mantener conexión (con espera de 2 s entre reintentos)
    MQTT_LOCK();
    if (WiFi.status() == WL_CONNECTED && server_ip_received) {
      if (!mqttClient.connected()) {
        mqtt_connected = false;
        if (millis() - lastReconnect > 2000) {
          lastReconnect = millis();
          reconnectMQTT();
        }
      }
      mqttClient.loop();
    }
    MQTT_UNLOCK();

    // ===== SOLICITUD TDM =====
    if (packet_request_pending) {
      noInterrupts();
      uint32_t packet_id = requested_packet_id;
      packet_request_pending = false;
      interrupts();

      const uint32_t t_req = millis();
      const uint32_t deadline = t_req + TDM_DEADLINE_MS - 10;   // margen para publicar

      // Esperar el mínimo cediendo CPU (sin busy-wait ni prints)
      while (samplesAvailableMQTT() < MQTT_BATCH_SIZE && millis() < deadline) {
        vTaskDelay(1);
      }

      uint32_t avail = samplesAvailableMQTT();
      uint32_t wait_ms = millis() - t_req;

      if (avail == 0 || !mqtt_connected) {
        Serial.printf("[TDM] ❌ Paquete #%lu sin datos/MQTT (avail=%lu, mqtt=%d)\n",
                      packet_id, avail, (int)mqtt_connected);
        mqtt_error_count++;
        continue;
      }

      // Envía lo disponible (hasta TDM_MAX_SAMPLES): si hay atraso, se pone al día
      uint32_t n = (avail > TDM_MAX_SAMPLES) ? TDM_MAX_SAMPLES : avail;
      uint32_t packed = 0;

      MQTT_LOCK();
      buildPacket(mqttPayload, sizeof(mqttPayload), packet_id, n, true, false, wait_ms, packed);
      bool ok = (packed > 0) && mqttClient.publish(MQTT_TOPIC_DATA, mqttPayload);
      if (ok) {
        readIndexMQTT = (readIndexMQTT + packed) % CIRCULAR_BUFFER_SIZE;
        mqtt_sent_count += packed;
      } else {
        mqtt_error_count++;   // no se avanza el índice: se reintenta en la próxima solicitud
      }
      MQTT_UNLOCK();

#if TDM_VERBOSE
      Serial.printf("[TDM] #%lu %s n=%lu espera=%lums total=%lums backlog=%lu\n",
                    packet_id, ok ? "OK" : "FALLO", packed, wait_ms,
                    millis() - t_req, samplesAvailableMQTT());
#else
      if (!ok) {
        Serial.printf("[TDM] ❌ Fallo publicación #%lu\n", packet_id);
      } else if (samplesAvailableMQTT() > CIRCULAR_BUFFER_SIZE / 2) {
        Serial.printf("[TDM] ⚠️ Backlog alto: %lu muestras\n", samplesAvailableMQTT());
      }
#endif
      continue;
    }

    // ===== VACIADO FINAL =====
    if (acquisitionFinished && samplesAvailableMQTT() > 0 && !mqtt_finished) {
      Serial.println("\n[MQTT] 📤 Vaciado final del buffer...");

      if (mqtt_connected) {
        while (samplesAvailableMQTT() > 0 && !systemReset) {
          uint32_t remaining = samplesAvailableMQTT();
          uint32_t n = (remaining > TDM_MAX_SAMPLES) ? TDM_MAX_SAMPLES : remaining;
          uint32_t packed = 0;

          mqtt_packet_id++;
          MQTT_LOCK();
          buildPacket(mqttPayload, sizeof(mqttPayload), mqtt_packet_id, n, false,
                      false, 0, packed);
          // 'final' se marca solo si lo que queda cabe completo en este paquete
          bool is_final = (remaining <= packed);
          if (is_final) {
            // reconstruir con final=true (mismo contenido)
            buildPacket(mqttPayload, sizeof(mqttPayload), mqtt_packet_id, n, false,
                        true, 0, packed);
          }
          bool ok = (packed > 0) && mqttClient.publish(MQTT_TOPIC_DATA, mqttPayload);
          if (ok) {
            readIndexMQTT = (readIndexMQTT + packed) % CIRCULAR_BUFFER_SIZE;
            mqtt_sent_count += packed;
          }
          mqttClient.loop();
          MQTT_UNLOCK();

          if (!ok) {
            mqtt_packet_id--;
            Serial.println("[MQTT] ⚠️ Error en vaciado, reintentando...");
            vTaskDelay(50 / portTICK_PERIOD_MS);
            if (!mqtt_connected) break;
            continue;
          }
          vTaskDelay(15 / portTICK_PERIOD_MS);   // ritmo suave para el broker
        }

        mqtt_finished = true;
        Serial.printf("[MQTT] ✓ Transferencia completa | Enviadas: %lu | Errores: %lu | Total: %lu\n",
                      mqtt_sent_count, mqtt_error_count, total_samples);
      }
    }

    if (startAcquisition && mqtt_finished) {
      mqtt_finished = false;
      mqtt_sent_count = 0;
      mqtt_error_count = 0;
      mqtt_packet_id = 0;
      Serial.println("[MQTT] 🔄 Preparado para nueva adquisición");
    }

    vTaskDelay(5 / portTICK_PERIOD_MS);   // antes 50 ms: respondía tarde a las solicitudes TDM
  }
}

void registrationTask(void *parameter) {
  for (;;) {
    if (systemReset) {
      vTaskDelay(100 / portTICK_PERIOD_MS);
      continue;
    }

    if (ping_recibido && !mqtt_registrado && !acquisitionActive) {
      if (!server_ip_received) {
        vTaskDelay(500 / portTICK_PERIOD_MS);
        continue;
      }

      Serial.println("[REG] Intentando registro MQTT...");
      registrarEnMQTT();

      if (mqtt_registrado) {
        estado_actual = ESTADO_LISTO;
        Serial.println("[REG] Registro completado. Estado: LISTO");
      } else {
        Serial.println("[REG] Registro falló, reintentando en 2s...");
        vTaskDelay(2000 / portTICK_PERIOD_MS);
      }
    }

    vTaskDelay(200 / portTICK_PERIOD_MS);
  }
}

void ledTask(void *parameter) {
  for (;;) {
    actualizarLED();
    vTaskDelay(50 / portTICK_PERIOD_MS);
  }
}

// ========== ERROR CRÍTICO DE SD (no retorna) ==========
void sdFatalError() {
  Serial.println();
  Serial.println("❌❌❌ ERROR CRÍTICO: TARJETA SD NO DISPONIBLE ❌❌❌");
  Serial.println("⚠️  EL SISTEMA NO PUEDE CONTINUAR SIN SD");
  Serial.println();
  Serial.println("[SOLUCIONES]");
  Serial.println("  1. Verifique que la tarjeta SD esté INSERTADA");
  Serial.println("  2. Verifique el FORMATO (FAT32 recomendado)");
  Serial.println("  3. Verifique las CONEXIONES físicas:");
  Serial.printf("       CLK: GPIO %d | CMD: GPIO %d | D0: GPIO %d\n", PIN_CLK, PIN_CMD, PIN_D0);
  Serial.println("  4. Verifique que tenga espacio libre (>10 MB)");
  Serial.println("  5. Pruebe REFORMATEAR la tarjeta");
  Serial.println("  6. Pruebe con OTRA tarjeta SD");
  Serial.println();
  Serial.println("[ESTADO] Sistema DETENIDO - LED parpadeando en ROJO");

  unsigned long lastMessage = 0;
  while (true) {
    for (int i = 0; i < 3; i++) {
      leds[0] = CRGB::Red;
      FastLED.show();
      delay(100);
      leds[0] = CRGB::Black;
      FastLED.show();
      delay(100);
    }
    delay(1000);

    if (millis() - lastMessage > 10000) {
      Serial.println("[ERROR] ⚠️  Sistema detenido - SD no disponible");
      Serial.println("[INFO] Inserte/corrija SD y presione RESET");
      lastMessage = millis();
    }
  }
}

bool initializeSD() {
  Serial.println("  INICIALIZANDO TARJETA SD (SD_MMC 1-BIT) - CRÍTICO");
  Serial.printf("  CLK: %d | CMD: %d | D0: %d\n", PIN_CLK, PIN_CMD, PIN_D0);

  if (!SD_MMC.setPins(PIN_CLK, PIN_CMD, PIN_D0)) {
    Serial.println("[SD] ❌ ERROR: No se pudieron configurar los pines");
    sdFatalError();
  }

  bool sd_ok = false;

  for (uint8_t attempt = 1; attempt <= SD_RETRY_ATTEMPTS && !sd_ok; attempt++) {
    Serial.printf("[SD] Intento %d/%d...", attempt, SD_RETRY_ATTEMPTS);

    if (SD_MMC.begin("/sdcard", true)) {

      // VALIDACIÓN 1: TIPO DE TARJETA
      uint8_t cardType = SD_MMC.cardType();
      if (cardType == CARD_NONE) {
        Serial.println(" ❌ Tarjeta detectada pero tipo INVÁLIDO");
        SD_MMC.end();
        continue;
      }

      Serial.println(" ✅ OK");
      Serial.println("[SD] ✓ Inicializada en modo 1-BIT");

      Serial.print("[SD] Tipo: ");
      switch (cardType) {
        case CARD_MMC:  Serial.println("MMC");  break;
        case CARD_SD:   Serial.println("SDSC"); break;
        case CARD_SDHC: Serial.println("SDHC"); break;
        default:        Serial.println("UNKNOWN");
      }

      // VALIDACIÓN 2: CAPACIDAD Y ESPACIO
      uint64_t cardSize = SD_MMC.cardSize() / (1024 * 1024);
      uint64_t totalBytes = SD_MMC.totalBytes() / (1024 * 1024);
      uint64_t usedBytes = SD_MMC.usedBytes() / (1024 * 1024);
      uint64_t freeBytes = totalBytes - usedBytes;

      Serial.printf("[SD] Capacidad total: %llu MB\n", cardSize);
      Serial.printf("[SD] Espacio usado: %llu MB\n", usedBytes);
      Serial.printf("[SD] Espacio libre: %llu MB\n", freeBytes);

      if (freeBytes < SD_MIN_FREE_SPACE_MB) {
        Serial.printf("[SD] ❌ ERROR: Espacio insuficiente (<%d MB)\n", SD_MIN_FREE_SPACE_MB);
        SD_MMC.end();
        continue;
      }

      // VALIDACIÓN 3: TEST DE ESCRITURA/LECTURA
      Serial.print("[SD] Test de escritura...");
      File testFile = SD_MMC.open("/sd_test_write.txt", FILE_WRITE);
      if (!testFile) {
        Serial.println(" ❌ FALLO - No se puede crear archivo");
        SD_MMC.end();
        continue;
      }

      testFile.print("ESP32 SD Test - Electrodo ");
      testFile.println(ELECTRODE_NAME);
      testFile.flush();
      testFile.close();
      Serial.println(" ✅ OK");

      Serial.print("[SD] Test de lectura...");
      testFile = SD_MMC.open("/sd_test_write.txt", FILE_READ);
      if (!testFile) {
        Serial.println(" ❌ FALLO - No se puede leer archivo");
        SD_MMC.end();
        continue;
      }

      String readData = testFile.readString();
      testFile.close();

      if (readData.indexOf(ELECTRODE_NAME) == -1) {
        Serial.println(" ❌ FALLO - Datos corruptos");
        SD_MMC.end();
        continue;
      }
      Serial.println(" ✅ OK");
      SD_MMC.remove("/sd_test_write.txt");

      // VALIDACIÓN 4: TEST DE VELOCIDAD
      Serial.print("[SD] Test de velocidad de escritura...");
      File speedTest = SD_MMC.open("/speed_test.bin", FILE_WRITE);
      if (!speedTest) {
        Serial.println(" ❌ FALLO");
        SD_MMC.end();
        continue;
      }

      uint8_t testBuffer[512];
      memset(testBuffer, 0xAA, sizeof(testBuffer));

      uint32_t writeStart = millis();
      for (int i = 0; i < 20; i++) {  // 10 KB
        speedTest.write(testBuffer, sizeof(testBuffer));
      }
      speedTest.flush();
      uint32_t writeTime = millis() - writeStart;
      if (writeTime == 0) writeTime = 1;
      speedTest.close();
      SD_MMC.remove("/speed_test.bin");

      float writeSpeed = (10.0 * 1024.0) / (writeTime / 1000.0);  // bytes/s
      Serial.printf(" ✅ OK (%.1f KB/s)\n", writeSpeed / 1024.0);

      if (writeSpeed < 50000) {
        Serial.println("[SD] ⚠️  ADVERTENCIA: Velocidad de escritura baja");
      }

      sd_ok = true;

    } else {
      Serial.println(" ❌ FALLO");
      if (attempt < SD_RETRY_ATTEMPTS) {
        Serial.printf("  Esperando %d ms antes de reintentar...\n", SD_RETRY_DELAY_MS);
        delay(SD_RETRY_DELAY_MS);
      }
    }
  }

  if (!sd_ok) {
    sdFatalError();
  }

  Serial.println("✅ TARJETA SD VALIDADA Y LISTA (SD_MMC 1-BIT)");

  leds[0] = CRGB::Green;
  FastLED.show();
  delay(500);

  return true;
}

// ========== SETUP ==========
void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println("\n========================================");
  Serial.println("  ESP32-S3 ESCLAVO - ECG ELECTRODO");
  Serial.println("  VERSION: IP dinámica por ESP-NOW + MQTT optimizado");
  Serial.println("========================================");
  Serial.printf("  Electrodo: %s (ID: %d)\n", ELECTRODE_NAME, SLAVE_ID);
  Serial.printf("  Posición: %s\n", ELECTRODE_POSITION);
  Serial.printf("  Color: #%06X\n", ELECTRODE_COLOR);
  Serial.printf("  Payload: %s | Lote: %d..%d muestras\n",
                COMPACT_PAYLOAD ? "compacto" : "original", MQTT_BATCH_SIZE, TDM_MAX_SAMPLES);
  Serial.println("========================================\n");

  mqttMutex = xSemaphoreCreateRecursiveMutex();

  // LED
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS);
  FastLED.setBrightness(BRIGHTNESS);
  leds[0] = CRGB(ELECTRODE_COLOR);
  FastLED.show();

  setupADC();
  setupWiFi();

  // El servidor MQTT se configura cuando llegue la IP por ESP-NOW
  espClient.setNoDelay(true);   // sin Nagle: cada paquete sale al instante
  mqttClient.setCallback(mqttCallback);
  mqttClient.setBufferSize(MQTT_PAYLOAD_SIZE + 128);
  mqttClient.setKeepAlive(15);
  mqttClient.setSocketTimeout(2);

  Serial.print("[ESP-NOW] MAC: ");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("[ERROR] ESP-NOW init falló");
    while (true) delay(1000);
  }

  esp_now_register_send_cb(OnDataSent);
  esp_now_register_recv_cb(OnDataRecv);

  esp_now_peer_info_t peerInfo{};
  memcpy(peerInfo.peer_addr, direccionMaestro, 6);
  peerInfo.channel = WIFI_CHANNEL;
  peerInfo.encrypt = false;
  esp_now_add_peer(&peerInfo);

  // SD
  initializeSD();

  // Tareas
  Serial.println("\n[TASKS] Creando tareas...");
  xTaskCreatePinnedToCore(acquisitionTask, "Acquisition", 4096, NULL, 3, NULL, 0);
  delay(50);
  xTaskCreatePinnedToCore(registrationTask, "Registration", 4096, NULL, 2, NULL, 1);
  delay(50);
  xTaskCreatePinnedToCore(mqttWriterTask, "MQTT_Writer", 8192, NULL, 2, NULL, 1);
  delay(50);
  xTaskCreatePinnedToCore(sdWriterTask, "SD_Writer", 8192, NULL, 2, NULL, 1);
  delay(50);
  xTaskCreatePinnedToCore(ledTask, "LED", 2048, NULL, 1, NULL, 1);

  Serial.println("\n========================================");
  Serial.printf("Electrodo: %s | ID: %d | Freq: %d Hz\n",
                ELECTRODE_NAME, SLAVE_ID, ACQ_FREQ_HZ);
  Serial.println("Esperando IP del servidor por ESP-NOW...");
  Serial.println("========================================\n");

  estado_actual = ESTADO_DISCOVERY;
}

void loop() {
  vTaskDelay(1000 / portTICK_PERIOD_MS);
}
