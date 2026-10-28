/*
 * ESP32 MAESTRO CYD (Cheap Yellow Display ESP32-2432S028R)
 * Control SERIAL a 10 electrodos ECG via ESP-NOW
 * VERSION: Control Serial + Indicadores visuales + BOTON RESET TACTIL
 */

#include <esp_now.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <TFT_eSPI.h>
#include <XPT2046_Touchscreen.h>

// ========== CONFIGURACION TOUCH CYD ==========
#define XPT2046_IRQ 36
#define XPT2046_MOSI 32
#define XPT2046_MISO 39
#define XPT2046_CLK 25
#define XPT2046_CS 33

SPIClass touchscreenSPI = SPIClass(VSPI);
XPT2046_Touchscreen touchscreen(XPT2046_CS, XPT2046_IRQ);

// Calibración del touch (ajustar según tu pantalla)
#define TS_MINX 370
#define TS_MINY 470
#define TS_MAXX 3700
#define TS_MAXY 3400

// ========== DEFINICION DE COLORES RGB565 VERDADEROS ==========
// Formato RGB565: RRRRR GGGGGG BBBBB (5-6-5 bits)
#define COLOR_NEGRO           0x0000
#define COLOR_BLANCO          0xFFFF
#define COLOR_ROJO            0xF800
#define COLOR_VERDE           0x07E0
#define COLOR_AZUL            0x001F
#define COLOR_AMARILLO        0xFFE0
#define COLOR_CIAN            0x07FF
#define COLOR_MAGENTA         0xF81F
#define COLOR_ROJO_OSCURO     0x8800
#define COLOR_VERDE_OSCURO    0x03E0
#define COLOR_AZUL_OSCURO     0x000F
#define COLOR_GRIS_OSCURO     0x4208
#define COLOR_GRIS_MEDIO      0x8410
#define COLOR_GRIS_CLARO      0xD69A
#define COLOR_ROSA            0xFE19
#define COLOR_NARANJA         0xFD20
#define COLOR_NARANJA_OSCURO  0xFC00
#define COLOR_AZUL_BOTON      0x0019
#define COLOR_VERDE_ACTIVO    0x07E0
#define COLOR_ROJO_ALERTA     0xF800
#define COLOR_AMARILLO_AVISO  0xFFE0
#define COLOR_CIAN_INFO       0x07FF
#define COLOR_FONDO_OSCURO    0x2104
#define COLOR_FONDO_MEDIO     0x4208
#define COLOR_FONDO_CLARO     0x8410

// ========== CONFIGURACION PANTALLA CYD ==========
TFT_eSPI tft = TFT_eSPI();

// Dimensiones pantalla
#define SCREEN_WIDTH 320
#define SCREEN_HEIGHT 240

// ========== CONFIGURACION BOTON RESET ==========
#define RESET_BTN_X 240
#define RESET_BTN_Y 155
#define RESET_BTN_W 70
#define RESET_BTN_H 30
#define DEBOUNCE_TIME 300  // ms para evitar múltiples toques

unsigned long lastTouchTime = 0;

// ========== CONFIGURACION LDR ==========
#define LDR_PIN 34
#define BRIGHTNESS_MIN 20
#define BRIGHTNESS_MAX 255
#define LDR_SAMPLES 10
#define LDR_UPDATE_INTERVAL 500  // ms

int ldr_value = 0;
int screen_brightness = 128;
unsigned long last_ldr_read = 0;

// ========== CONFIGURACION RED ==========
String server_ip = "No configurada";  // Se recibirá por serial
bool ip_received = false;

// ========== CONFIGURACION WIFI ==========
#define WIFI_CHANNEL 6

// ========== MACs AUTORIZADAS DE LOS 10 ELECTRODOS ECG ==========
typedef struct {
  uint8_t mac[6];
  const char* nombre;
  const char* nombre_corto;
  uint8_t id;
  bool autorizado;
} electrodo_autorizado_t;

electrodo_autorizado_t electrodos_autorizados[10] = {
  {{0xB4, 0x3A, 0x45, 0xF3, 0xDB, 0x54}, "RA (Brazo Derecho)",           "RA",  1, true},
  {{0xB4, 0x3A, 0x45, 0xF3, 0xEF, 0xF4}, "LA (Brazo Izquierdo)",         "LA",  2, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x54}, "LL (Pierna Izquierda)",        "LL",  3, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x58}, "RL (Pierna Derecha - Tierra)", "RL",  4, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x5C}, "V1 (Precordial)",              "V1",  5, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x60}, "V2 (Precordial)",              "V2",  6, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x64}, "V3 (Precordial)",              "V3",  7, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x68}, "V4 (Precordial)",              "V4",  8, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x6C}, "V5 (Precordial)",              "V5",  9, true},
  {{0xB4, 0x3A, 0x45, 0xFB, 0x3E, 0x70}, "V6 (Precordial)",              "V6", 10, true}
};

// ========== MENSAJES ESP-NOW ==========
#define CMD_PING      1
#define CMD_PONG      2
#define CMD_START     3
#define CMD_STOP      4
#define CMD_RESET     5
#define CMD_SET_IP    6  // NUEVO: Comando para enviar IP

typedef struct __attribute__((packed)) {
  uint8_t comando;
  uint8_t datos[10];
} mensaje_simple_t;

// Nuevo: Estructura para enviar IP a los esclavos
typedef struct __attribute__((packed)) {
  uint8_t comando;
  char ip[16];  // IP en formato string "xxx.xxx.xxx.xxx"
} mensaje_ip_t;

// ========== ESCLAVOS ==========
#define MAX_SLAVES 10
#define DISCOVERY_TIMEOUT_MS 5000
#define PING_INTERVAL_MS 500

typedef struct {
  uint8_t mac[6];
  bool activo;
  bool respondio_discovery_actual;
  unsigned long ultimo_contacto;
  uint8_t id;
  const char* nombre;
  const char* nombre_corto;
} esclavo_t;

esclavo_t esclavos[MAX_SLAVES];
uint8_t num_esclavos = 0;

#define ESTADO_IDLE         0
#define ESTADO_DISCOVERY    1
#define ESTADO_ACTIVO       2

uint8_t estado_sistema = ESTADO_IDLE;
unsigned long tiempo_inicio_discovery = 0;
unsigned long tiempo_ultimo_ping = 0;
bool discovery_completo = false;
uint8_t discovery_count = 0;
bool dataAcquisitionActive = false;

// ========== INTERFAZ ==========
#define INDICATOR_START_Y 90
#define INDICATOR_SIZE 14
#define INDICATOR_SPACING_X 32
#define INDICATOR_SPACING_Y 30

bool necesita_redibujar = true;

// ========== BUFFER SERIAL ==========
String serialBuffer = "";

// ========== FUNCIONES AUXILIARES ==========
int leerLDR() {
  long suma = 0;
  for (int i = 0; i < LDR_SAMPLES; i++) {
    suma += analogRead(LDR_PIN);
    delay(5);
  }
  return suma / LDR_SAMPLES;
}

void ajustarBrilloPantalla() {
  unsigned long ahora = millis();
  
  if (ahora - last_ldr_read < LDR_UPDATE_INTERVAL) {
    return;
  }
  
  last_ldr_read = ahora;
  ldr_value = leerLDR();
  
  screen_brightness = map(ldr_value, 0, 4095, BRIGHTNESS_MIN, BRIGHTNESS_MAX);
  screen_brightness = constrain(screen_brightness, BRIGHTNESS_MIN, BRIGHTNESS_MAX);
  
  #if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcWrite(TFT_BL, screen_brightness);
  #else
    ledcWrite(0, screen_brightness);
  #endif
  
  static unsigned long last_debug = 0;
  if (ahora - last_debug > 5000) {
    Serial.printf("[LDR] Valor: %d | Brillo: %d/255\n", ldr_value, screen_brightness);
    last_debug = ahora;
  }
}

bool compararMAC(const uint8_t *mac1, const uint8_t *mac2) {
  for (int i = 0; i < 6; i++) {
    if (mac1[i] != mac2[i]) return false;
  }
  return true;
}

int buscarMACAutorizada(const uint8_t *mac) {
  for (int i = 0; i < 10; i++) {
    if (compararMAC(electrodos_autorizados[i].mac, mac) && 
        electrodos_autorizados[i].autorizado) {
      return i;
    }
  }
  return -1;
}

int buscarEsclavo(const uint8_t *mac) {
  for (int i = 0; i < num_esclavos; i++) {
    if (compararMAC(esclavos[i].mac, mac)) {
      return i;
    }
  }
  return -1;
}

void agregarEsclavo(const uint8_t *mac, uint8_t slave_id) {
  int idx_autorizado = buscarMACAutorizada(mac);
  if (idx_autorizado < 0) {
    Serial.printf("\n[SEGURIDAD] ⚠️  MAC NO AUTORIZADA RECHAZADA\n");
    return;
  }
  
  if (num_esclavos >= MAX_SLAVES) {
    Serial.println("[Discovery] Límite de esclavos alcanzado");
    return;
  }
  
  int idx = buscarEsclavo(mac);
  if (idx >= 0) {
    if (!esclavos[idx].respondio_discovery_actual) {
      esclavos[idx].respondio_discovery_actual = true;
      esclavos[idx].ultimo_contacto = millis();
      esclavos[idx].activo = true;
      Serial.printf("[Discovery] ✓ %s RE-DETECTADO\n", esclavos[idx].nombre);
      necesita_redibujar = true;
    }
    return;
  }
  
  memcpy(esclavos[num_esclavos].mac, mac, 6);
  esclavos[num_esclavos].activo = true;
  esclavos[num_esclavos].respondio_discovery_actual = true;
  esclavos[num_esclavos].ultimo_contacto = millis();
  esclavos[num_esclavos].id = electrodos_autorizados[idx_autorizado].id;
  esclavos[num_esclavos].nombre = electrodos_autorizados[idx_autorizado].nombre;
  esclavos[num_esclavos].nombre_corto = electrodos_autorizados[idx_autorizado].nombre_corto;
  
  Serial.printf("\n[Discovery] ✓ NUEVO: %s (ID:%d)\n", 
                esclavos[num_esclavos].nombre, esclavos[num_esclavos].id);
  
  esp_now_peer_info_t peerInfo;
  memset(&peerInfo, 0, sizeof(peerInfo));
  memcpy(peerInfo.peer_addr, mac, 6);
  peerInfo.channel = WIFI_CHANNEL;
  peerInfo.encrypt = false;
  
  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.printf("[ERROR] No se pudo agregar peer\n");
  }
  
  num_esclavos++;
  necesita_redibujar = true;
}

// ========== CALLBACKS ESP-NOW ==========
void OnDataSent(const esp_now_send_info_t *info, esp_now_send_status_t status) {
  // Silencioso
}

void OnDataRecv(const esp_now_recv_info_t *recv_info, const uint8_t *incomingData, int len) {
  if (len < sizeof(mensaje_simple_t)) return;
  
  mensaje_simple_t msg;
  memcpy(&msg, incomingData, sizeof(mensaje_simple_t));
  
  if (msg.comando == CMD_PONG && estado_sistema == ESTADO_DISCOVERY) {
    uint8_t slave_id = msg.datos[0];
    agregarEsclavo(recv_info->src_addr, slave_id);
  }
}

// ========== FUNCIONES DE ENVIO ==========
void enviarBroadcast(uint8_t comando) {
  mensaje_simple_t msg;
  msg.comando = comando;
  memset(msg.datos, 0, sizeof(msg.datos));
  
  uint8_t broadcastAddr[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  esp_now_send(broadcastAddr, (uint8_t *)&msg, sizeof(msg));
}

void enviarATodos(uint8_t comando) {
  mensaje_simple_t msg;
  msg.comando = comando;
  memset(msg.datos, 0, sizeof(msg.datos));
  
  Serial.printf("\n[ESP-NOW] Enviando comando %d a %d electrodos\n", comando, num_esclavos);
  
  for (int i = 0; i < num_esclavos; i++) {
    if (esclavos[i].activo) {
      esp_now_send(esclavos[i].mac, (uint8_t *)&msg, sizeof(msg));
      delay(50);
    }
  }
}

// ========== INTERFAZ GRAFICA ==========
void dibujarBotonReset() {
  // Dibujar botón con borde
  tft.fillRoundRect(RESET_BTN_X, RESET_BTN_Y, RESET_BTN_W, RESET_BTN_H, 5, COLOR_ROJO_ALERTA);
  tft.drawRoundRect(RESET_BTN_X, RESET_BTN_Y, RESET_BTN_W, RESET_BTN_H, 5, COLOR_BLANCO);
  
  // Texto del botón
  tft.setTextColor(COLOR_BLANCO);
  tft.setTextSize(1);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("RESET", RESET_BTN_X + RESET_BTN_W/2, RESET_BTN_Y + RESET_BTN_H/2);
}

void dibujarIndicadores() {
  tft.fillRect(0, 50, SCREEN_WIDTH, 95, COLOR_NEGRO);
  
  tft.setTextColor(COLOR_CIAN_INFO);
  tft.setTextSize(1);
  tft.setTextDatum(TL_DATUM);
  tft.drawString("ELECTRODOS ECG", 5, 55);
  
  tft.setTextColor(COLOR_BLANCO);
  char buffer[20];
  sprintf(buffer, "%d/10", num_esclavos);
  tft.setTextDatum(TR_DATUM);
  tft.drawString(buffer, SCREEN_WIDTH - 5, 55);
  
  for (int i = 0; i < 10; i++) {
    int row = i / 5;
    int col = i % 5;
    int x = 10 + col * INDICATOR_SPACING_X;
    int y = INDICATOR_START_Y + row * INDICATOR_SPACING_Y;
    
    bool conectado = false;
    const char* nombre = electrodos_autorizados[i].nombre_corto;
    
    for (int j = 0; j < num_esclavos; j++) {
      if (esclavos[j].id == (i + 1) && esclavos[j].activo) {
        conectado = true;
        nombre = esclavos[j].nombre_corto;
        break;
      }
    }
    
    uint16_t color = conectado ? COLOR_VERDE_ACTIVO : COLOR_ROJO_ALERTA;
    tft.fillCircle(x, y, INDICATOR_SIZE/2, color);
    tft.drawCircle(x, y, INDICATOR_SIZE/2, COLOR_BLANCO);
    
    tft.setTextColor(COLOR_BLANCO);
    tft.setTextSize(1);
    tft.setTextDatum(TC_DATUM);
    tft.drawString(nombre, x, y + INDICATOR_SIZE/2 + 3);
  }
}


void dibujarEstado() {
  tft.fillRect(0, 10, SCREEN_WIDTH, 35, COLOR_GRIS_OSCURO);
  
  tft.setTextColor(COLOR_BLANCO);
  tft.setTextSize(2);
  tft.setTextDatum(MC_DATUM);
  
  const char* texto_estado = "";
  uint16_t color_estado = COLOR_BLANCO;
  
  switch (estado_sistema) {
    case ESTADO_IDLE:
      texto_estado = dataAcquisitionActive ? "ADQUISICION ACTIVA" : "ESPERANDO COMANDO";
      color_estado = dataAcquisitionActive ? COLOR_VERDE_ACTIVO : COLOR_AMARILLO_AVISO;
      break;
    case ESTADO_DISCOVERY:
      texto_estado = "BUSCANDO...";
      color_estado = COLOR_CIAN_INFO;
      break;
    case ESTADO_ACTIVO:
      texto_estado = "ADQUISICION";
      color_estado = COLOR_VERDE_ACTIVO;
      break;
  }
  
  tft.setTextColor(color_estado);
  tft.drawString(texto_estado, SCREEN_WIDTH/2, 27);
}

void dibujarInfo() {
  tft.fillRect(0, 155, SCREEN_WIDTH, 85, COLOR_NEGRO);
  
  tft.setTextColor(COLOR_GRIS_CLARO);
  tft.setTextSize(1);
  tft.setTextDatum(TL_DATUM);
  
  char buffer[50];
  sprintf(buffer, "MAC: %s", WiFi.macAddress().c_str());
  tft.drawString(buffer, 5, 160);
  
  sprintf(buffer, "Canal: %d", WIFI_CHANNEL);
  tft.drawString(buffer, 5, 175);
  
  sprintf(buffer, "Discoveries: %d", discovery_count);
  tft.drawString(buffer, 5, 190);
  
  // ========== MOSTRAR IP DEL SERVIDOR ==========
  tft.setTextColor(ip_received ? COLOR_VERDE_ACTIVO : COLOR_ROJO_ALERTA);
  String displayText = "Server: " + server_ip;
  tft.drawString(displayText, 5, 205);
  
  tft.setTextColor(COLOR_CIAN_INFO);
  tft.setTextDatum(TC_DATUM);
  tft.drawString("Control SERIAL", SCREEN_WIDTH/2, 225);
  
  // Dibujar botón RESET
  dibujarBotonReset();
}

void dibujarPantallaCompleta() {
  tft.fillScreen(COLOR_NEGRO);
  dibujarEstado();
  dibujarIndicadores();
  dibujarInfo();
}

// ========== FUNCIONES DE RESPUESTA SERIAL ==========
void sendSerialResponse(const char* command, bool success, const char* errorCode = "") {
  Serial.print("RESP:");
  Serial.print(command);
  Serial.print(":");
  Serial.print(success ? "OK" : "ERROR");
  if (!success && strlen(errorCode) > 0) {
    Serial.print(":");
    Serial.print(errorCode);
  }
  Serial.println();
}

// ========== FUNCIONES DE ENVIO CON IP ==========
void enviarIPAEsclavo(const uint8_t *mac) {
  if (!ip_received || server_ip.length() == 0) {
    return;  // No enviar si no tenemos IP
  }
  
  mensaje_ip_t msg;
  msg.comando = CMD_SET_IP;
  
  // Copiar IP al mensaje (máximo 15 caracteres + null terminator)
  strncpy(msg.ip, server_ip.c_str(), 15);
  msg.ip[15] = '\0';
  
  esp_now_send(mac, (uint8_t *)&msg, sizeof(msg));
}

void enviarIPATodos() {
  if (!ip_received || server_ip.length() == 0) {
    Serial.println("[IP] No se puede enviar IP - no configurada");
    return;
  }
  
  Serial.printf("\n[IP] Enviando IP del servidor (%s) a %d electrodos\n", 
                server_ip.c_str(), num_esclavos);
  
  for (int i = 0; i < num_esclavos; i++) {
    if (esclavos[i].activo) {
      Serial.printf("  [IP] -> %s\n", esclavos[i].nombre_corto);
      enviarIPAEsclavo(esclavos[i].mac);
      delay(100);  // Delay entre envíos para evitar congestión
    }
  }
  
  Serial.println("[IP] ✓ IP enviada a todos los electrodos");
}

// ========== ACCIONES ==========
void executeEncontrar() {
  Serial.println("\n>>> COMANDO ENCONTRAR <<<");
  
  for (int i = 0; i < num_esclavos; i++) {
    esclavos[i].respondio_discovery_actual = false;
  }
  
  discovery_count++;
  discovery_completo = false;
  estado_sistema = ESTADO_DISCOVERY;
  tiempo_inicio_discovery = millis();
  tiempo_ultimo_ping = 0;
  
  Serial.printf("Discovery #%d iniciado\n", discovery_count);
  enviarBroadcast(CMD_PING);
  necesita_redibujar = true;
}

void executeIniciar() {
  Serial.println("\n>>> COMANDO INICIAR <<<");
  
  int esclavos_activos = 0;
  for (int i = 0; i < num_esclavos; i++) {
    if (esclavos[i].activo) esclavos_activos++;
  }
  
  if (esclavos_activos == 0) {
    Serial.println("[ERROR] No hay electrodos conectados");
    sendSerialResponse("INICIAR", false, "NO_ELECTRODOS");
    
    tft.fillRect(0, 10, SCREEN_WIDTH, 35, COLOR_ROJO_ALERTA);
    tft.setTextColor(COLOR_BLANCO);
    tft.setTextSize(1);
    tft.setTextDatum(MC_DATUM);
    tft.drawString("ERROR: Sin electrodos", SCREEN_WIDTH/2, 27);
    delay(2000);
    necesita_redibujar = true;
    return;
  }
  
  // Verificar que tengamos IP del servidor
  if (!ip_received) {
    Serial.println("[WARN] No se ha recibido IP del servidor");
    sendSerialResponse("INICIAR", false, "NO_IP");
    
    tft.fillRect(0, 10, SCREEN_WIDTH, 35, COLOR_ROJO_ALERTA);
    tft.setTextColor(COLOR_BLANCO);
    tft.setTextSize(1);
    tft.setTextDatum(MC_DATUM);
    tft.drawString("ERROR: Sin IP servidor", SCREEN_WIDTH/2, 27);
    delay(2000);
    necesita_redibujar = true;
    return;
  }
  
  Serial.printf("Iniciando con %d/10 electrodos\n", esclavos_activos);
  estado_sistema = ESTADO_ACTIVO;
  dataAcquisitionActive = true;
  enviarATodos(CMD_START);
  
  sendSerialResponse("INICIAR", true);
  necesita_redibujar = true;
}

void executeFinalizar() {
  Serial.println("\n>>> COMANDO FINALIZAR <<<");
  
  if (dataAcquisitionActive) {
    enviarATodos(CMD_STOP);
    estado_sistema = ESTADO_IDLE;
    dataAcquisitionActive = false;
    Serial.println("Proceso finalizado");
  } else {
    Serial.println("No hay proceso activo");
  }
  necesita_redibujar = true;
}

void executeReset() {
  Serial.println("\n" + String('=', 60));
  Serial.println(">>> RESET COMPLETO DEL SISTEMA <<<");
  Serial.println(String('=', 60));
  
  tft.fillScreen(COLOR_NEGRO);
  tft.setTextColor(COLOR_ROJO_ALERTA);
  tft.setTextSize(2);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("! RESET !", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 - 30);
  tft.setTextSize(1);
  tft.setTextColor(COLOR_AMARILLO_AVISO);
  tft.drawString("Reiniciando sistema", SCREEN_WIDTH/2, SCREEN_HEIGHT/2);
  
  for (int i = 0; i < 3; i++) {
    tft.fillCircle(SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 40, 10, COLOR_ROJO_ALERTA);
    delay(300);
    tft.fillCircle(SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 40, 10, COLOR_NEGRO);
    delay(300);
  }
  
  if (dataAcquisitionActive) {
    Serial.println("[1/6] Deteniendo adquisición activa...");
    enviarATodos(CMD_STOP);
    delay(500);
    Serial.println("      ✓ Adquisición detenida");
  }
  
  Serial.printf("[2/6] Enviando RESET a %d electrodos conectados...\n", num_esclavos);
  if (num_esclavos > 0) {
    enviarATodos(CMD_RESET);
    delay(2000);
    Serial.println("      ✓ Comando RESET enviado");
  }
  
  Serial.println("[3/6] Esperando 3 segundos...");
  for (int i = 3; i > 0; i--) {
    Serial.printf("      %d...\n", i);
    delay(1000);
  }
  
  Serial.println("[4/6] Limpiando memoria del maestro...");
  num_esclavos = 0;
  memset(esclavos, 0, sizeof(esclavos));
  discovery_completo = false;
  discovery_count = 0;
  estado_sistema = ESTADO_IDLE;
  dataAcquisitionActive = false;
  // NO resetear server_ip ni ip_received para mantener la configuración
  Serial.println("      ✓ Memoria limpiada");
  
  Serial.println("[5/6] IP del servidor mantenida");
  Serial.printf("      Server: %s\n", server_ip.c_str());
  
  Serial.println("[6/6] REINICIANDO ESP32 MAESTRO...");
  
  tft.fillScreen(COLOR_NEGRO);
  #if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcWrite(TFT_BL, 0);
  #else
    ledcWrite(0, 0);
  #endif
  
  Serial.flush();
  delay(100);
  ESP.restart();
}

// ========== PROCESAMIENTO DE COMANDOS SERIAL ==========
void processSerialCommand(String command) {
  command.trim();
  
  Serial.print("[SERIAL] Comando recibido: '");
  Serial.print(command);
  Serial.println("'");
  
  // COMANDO: IP (Configurar IP del servidor)
  if (command.startsWith("IP:")) {
    String ip = command.substring(3);
    ip.trim();
    
    if (ip.length() > 0 && ip.length() <= 15) {
      server_ip = ip;
      ip_received = true;
      
      Serial.print("[CONFIG] ✓ IP del servidor configurada: ");
      Serial.println(server_ip);
      
      sendSerialResponse("IP", true);
      
      // Actualizar pantalla inmediatamente
      necesita_redibujar = true;
    } else {
      sendSerialResponse("IP", false, "IP_INVALIDA");
    }
    return;
  }

  // COMANDO: PING
  if (command == "CMD:PING") {
    sendSerialResponse("PING", true);
    return;
  }
  
  // COMANDO: ENCONTRAR
  if (command == "CMD:ENCONTRAR") {
    if (dataAcquisitionActive) {
      sendSerialResponse("ENCONTRAR", false, "ADQUISICION_ACTIVA");
      return;
    }
    executeEncontrar();
    sendSerialResponse("ENCONTRAR", true);
    return;
  }
  
  // COMANDO: INICIAR
  if (command == "CMD:INICIAR") {
    if (dataAcquisitionActive) {
      sendSerialResponse("INICIAR", false, "YA_INICIADO");
      return;
    }
    executeIniciar();
    return;  // executeIniciar ya envía su propia respuesta
  }
  
  // COMANDO: FINALIZAR
  if (command == "CMD:FINALIZAR") {
    if (!dataAcquisitionActive) {
      sendSerialResponse("FINALIZAR", false, "NO_ACTIVO");
      return;
    }
    executeFinalizar();
    sendSerialResponse("FINALIZAR", true);
    return;
  }
  
  // COMANDO: RESET
  if (command == "CMD:RESET") {
    sendSerialResponse("RESET", true);
    executeReset();
    return;
  }
  
  // COMANDO: STATUS
  if (command == "CMD:STATUS") {
    Serial.print("STATUS:");
    Serial.print(dataAcquisitionActive ? "ACQUIRING" : "IDLE");
    Serial.print(",ELECTRODOS:");
    Serial.print(num_esclavos);
    Serial.print(",DISCOVERY:");
    Serial.print(discovery_count);
    Serial.print(",IP:");
    Serial.println(server_ip);
    return;
  }
  
  // Comando no reconocido
  sendSerialResponse("UNKNOWN", false, "COMANDO_NO_RECONOCIDO");
}

void procesarDiscovery() {
  unsigned long tiempo_transcurrido = millis() - tiempo_inicio_discovery;
  
  if (!discovery_completo && 
      tiempo_transcurrido < DISCOVERY_TIMEOUT_MS &&
      millis() - tiempo_ultimo_ping > PING_INTERVAL_MS) {
    
    Serial.printf("[Discovery] PING (%lu/%d ms)\n", tiempo_transcurrido, DISCOVERY_TIMEOUT_MS);
    enviarBroadcast(CMD_PING);
    tiempo_ultimo_ping = millis();
  }
  
  if (!discovery_completo && tiempo_transcurrido >= DISCOVERY_TIMEOUT_MS) {
    discovery_completo = true;
    Serial.printf("\nDiscovery completado: %d/10 electrodos\n", num_esclavos);
    
    // ========== ENVIAR IP A LOS ELECTRODOS ENCONTRADOS ==========
    if (ip_received && num_esclavos > 0) {
      Serial.println("[Discovery] Enviando IP del servidor a electrodos encontrados...");
      enviarIPATodos();
    }
    // ============================================================
    
    estado_sistema = ESTADO_IDLE;
    necesita_redibujar = true;
  }
}

// ========== FUNCIONES DE TOUCH ==========
void manejarTouch() {
  if (!touchscreen.tirqTouched() || !touchscreen.touched()) {
    return;
  }
  
  unsigned long ahora = millis();
  if (ahora - lastTouchTime < DEBOUNCE_TIME) {
    return;  // Ignorar toques muy seguidos (debounce)
  }
  
  TS_Point p = touchscreen.getPoint();
  
  // Mapear coordenadas touch a pantalla (orientación 1)
  int x = map(p.x, TS_MINX, TS_MAXX, 0, SCREEN_WIDTH);
  int y = map(p.y, TS_MINY, TS_MAXY, 0, SCREEN_HEIGHT);
  
  // Limitar a los bordes de la pantalla
  x = constrain(x, 0, SCREEN_WIDTH - 1);
  y = constrain(y, 0, SCREEN_HEIGHT - 1);
  
  Serial.printf("[TOUCH] x=%d, y=%d (raw: %d, %d)\n", x, y, p.x, p.y);
  
  // Verificar si el toque está dentro del botón RESET
  if (x >= RESET_BTN_X && x <= (RESET_BTN_X + RESET_BTN_W) &&
      y >= RESET_BTN_Y && y <= (RESET_BTN_Y + RESET_BTN_H)) {
    
    Serial.println("[TOUCH] ¡Botón RESET presionado!");
    
    // Feedback visual - botón presionado
    tft.fillRoundRect(RESET_BTN_X, RESET_BTN_Y, RESET_BTN_W, RESET_BTN_H, 5, COLOR_ROJO_OSCURO);
    tft.drawRoundRect(RESET_BTN_X, RESET_BTN_Y, RESET_BTN_W, RESET_BTN_H, 5, COLOR_AMARILLO_AVISO);
    tft.setTextColor(COLOR_AMARILLO_AVISO);
    tft.setTextSize(1);
    tft.setTextDatum(MC_DATUM);
    tft.drawString("RESET", RESET_BTN_X + RESET_BTN_W/2, RESET_BTN_Y + RESET_BTN_H/2);
    
    delay(200);  // Pausa para que se vea el feedback
    
    // Ejecutar RESET
    executeReset();
    
    lastTouchTime = ahora;
  }
}

// ========== SETUP ==========
void setup() {
  Serial.begin(115200);
  delay(500);
  
  pinMode(LDR_PIN, INPUT);
  pinMode(TFT_BL, OUTPUT);
  
  #if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcAttach(TFT_BL, 5000, 8);
    ledcWrite(TFT_BL, 128);
  #else
    ledcSetup(0, 5000, 8);
    ledcAttachPin(TFT_BL, 0);
    ledcWrite(0, 128);
  #endif
  
  Serial.println("\n[LDR] Sensor de luz configurado en GPIO 34");
  Serial.println("[Backlight] Control PWM configurado");
  
  // Inicializar touchscreen
  touchscreenSPI.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
  touchscreen.begin(touchscreenSPI);
  touchscreen.setRotation(1);
  Serial.println("[TOUCH] Pantalla táctil configurada");
  
  tft.init();
  tft.setRotation(1);
  tft.fillScreen(COLOR_NEGRO);
  
  tft.setTextColor(COLOR_CIAN_INFO);
  tft.setTextSize(2);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("ESP32 MAESTRO", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 - 30);
  tft.setTextSize(1);
  tft.setTextColor(COLOR_BLANCO);
  tft.drawString("Sistema ECG 10 Electrodos", SCREEN_WIDTH/2, SCREEN_HEIGHT/2);
  tft.setTextColor(COLOR_AMARILLO_AVISO);
  tft.drawString("Control SERIAL + TACTIL", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 20);
  delay(2000);
  
  WiFi.mode(WIFI_STA);
  Serial.printf("\n[WiFi] MAC: %s\n", WiFi.macAddress().c_str());
  
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);
  
  if (esp_now_init() != ESP_OK) {
    Serial.println("[ERROR] ESP-NOW init falló");
    tft.fillScreen(COLOR_ROJO_ALERTA);
    tft.setTextColor(COLOR_BLANCO);
    tft.drawString("ERROR ESP-NOW", SCREEN_WIDTH/2, SCREEN_HEIGHT/2);
    while (1);
  }
  
  esp_now_register_send_cb(OnDataSent);
  esp_now_register_recv_cb(OnDataRecv);
  
  esp_now_peer_info_t peerInfo;
  memset(&peerInfo, 0, sizeof(peerInfo));
  uint8_t broadcastAddr[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  memcpy(peerInfo.peer_addr, broadcastAddr, 6);
  peerInfo.channel = WIFI_CHANNEL;
  peerInfo.encrypt = false;
  esp_now_add_peer(&peerInfo);
  
  Serial.println("\n========================================");
  Serial.println("  ESP32 MAESTRO CYD - INICIADO");
  Serial.println("========================================");
  Serial.printf("Canal: %d | Electrodos: 0/10\n", WIFI_CHANNEL);
  Serial.println("Control: SERIAL + TACTIL");
  Serial.println("\nComandos disponibles:");
  Serial.println("  IP:xxx.xxx.xxx.xxx - Configurar IP servidor");
  Serial.println("  CMD:PING           - Verificar comunicación");
  Serial.println("  CMD:ENCONTRAR      - Buscar electrodos");
  Serial.println("  CMD:INICIAR        - Iniciar adquisición");
  Serial.println("  CMD:FINALIZAR      - Detener adquisición");
  Serial.println("  CMD:RESET          - Reiniciar sistema");
  Serial.println("  CMD:STATUS         - Obtener estado");
  Serial.println("\nBotón RESET táctil en pantalla habilitado\n");
  
  ldr_value = leerLDR();
  Serial.printf("[LDR] Lectura inicial: %d\n\n", ldr_value);
  
  Serial.println("ECGESP32");
  Serial.println("[ESP32] Esperando comandos seriales o toque en pantalla...");
  Serial.println();
  
  // Enviar READY
  Serial.println("STATUS:READY");

  dibujarPantallaCompleta();
}

// ========== LOOP ==========
void loop() {
  ajustarBrilloPantalla();
  
  // Manejar eventos touch
  manejarTouch();
  
  if (estado_sistema == ESTADO_DISCOVERY && !discovery_completo) {
    procesarDiscovery();
  }
  
  if (necesita_redibujar) {
    dibujarPantallaCompleta();
    necesita_redibujar = false;
  }
  
  // Leer comandos seriales
  while (Serial.available() > 0) {
    char c = Serial.read();
    
    if (c == '\n' || c == '\r') {
      if (serialBuffer.length() > 0) {
        processSerialCommand(serialBuffer);
        serialBuffer = "";
      }
    } else {
      serialBuffer += c;
    }
  }
  
  delay(10);
}
