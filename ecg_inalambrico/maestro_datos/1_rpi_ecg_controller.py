"""
Windows PC - Control TTL via Serial + MQTT + WebSocket + TDM (Time Division Multiplexing)
VERSION: Maestro con Multiplexeo TDM controlado por PC Windows
ADAPTADO PARA: Thonny Python IDE

IMPORTANTE: Antes de ejecutar, instalar dependencias:
    pip install paho-mqtt websocket-client pyserial
"""

import time
from datetime import datetime
import atexit
import paho.mqtt.client as mqtt
import json
import csv
import os
from threading import Thread, Lock, Event
import websocket
import re
from pathlib import Path
import serial
import serial.tools.list_ports
import socket
import sys

# ==================== CONFIGURACIÓN PARA THONNY ====================
# Obtener directorio del script actual
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
print(f"[THONNY] Directorio del script: {SCRIPT_DIR}")

# Agregar directorio al path para imports
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# ==================== IMPORTS DE MÓDULOS LOCALES ====================
# Intentar importar módulos locales con manejo de errores
try:
    from ecg_derivations_calculator import ECGRealtimeDerivationsCalculator
    print("[THONNY] ✓ ECG Calculator importado")
except ImportError as e:
    print(f"[THONNY] ⚠️  Error importando ECG Calculator: {e}")
    print("[THONNY] El sistema funcionará sin cálculo de derivaciones ECG")
    ECGRealtimeDerivationsCalculator = None

try:
    from rpi_user_manager import UserManager, init_user_manager
    print("[THONNY] ✓ User Manager importado")
except ImportError as e:
    print(f"[THONNY] ⚠️  Error importando User Manager: {e}")
    print("[THONNY] El sistema funcionará sin gestión de usuarios")
    UserManager = None
    init_user_manager = None

# ==================== CONFIGURACIÓN SERVIDOR WEBSOCKET ====================
# El servidor WebSocket debe iniciarse manualmente por el usuario
WEBSOCKET_AUTO_START = False

# Mapeo de nombres descriptivos a posiciones estándar de ECG
ELECTRODE_POSITION_MAPPING = {
    # Nombres en español (descriptivos)
    'Brazo Derecho': 'RA',
    'Brazo Izquierdo': 'LA',
    'Pierna Izquierda': 'LL',
    'Pierna Derecha': 'RL',  # Reference (no se usa en derivaciones)
    
    # Precordiales (por si vienen con nombres descriptivos)
    'V1': 'V1',
    'V2': 'V2',
    'V3': 'V3',
    'V4': 'V4',
    'V5': 'V5',
    'V6': 'V6',
    
    # Nombres estándar (por si ya vienen correctos)
    'RA': 'RA',
    'LA': 'LA',
    'LL': 'LL',
    'RL': 'RL',
    
    # Variantes comunes
    'Right Arm': 'RA',
    'Left Arm': 'LA',
    'Left Leg': 'LL',
    'Right Leg': 'RL',
    
    # Mayúsculas/minúsculas
    'brazo derecho': 'RA',
    'brazo izquierdo': 'LA',
    'pierna izquierda': 'LL',
    'pierna derecha': 'RL',
}

def normalize_electrode_position(position_raw):
    """
    Convierte nombres descriptivos de electrodos a posiciones estándar
    
    Args:
        position_raw: Posición como viene del ESP32 ("Brazo Derecho", "RA", etc.)
    
    Returns:
        str: Posición estándar ("RA", "LA", "LL", etc.) o None si es inválida
    """
    if position_raw is None:
        return None
    
    # Buscar en el mapeo
    position_normalized = ELECTRODE_POSITION_MAPPING.get(position_raw)
    
    if position_normalized:
        return position_normalized
    
    # Si no está en el mapeo, intentar limpiar el string
    position_clean = str(position_raw).strip()
    
    # Buscar en el mapeo con el string limpio
    position_normalized = ELECTRODE_POSITION_MAPPING.get(position_clean)
    
    if position_normalized:
        return position_normalized
    
    # Si no se encuentra, retornar None
    print(f"[WARN] Posición de electrodo desconocida: '{position_raw}'")
    print(f"[WARN] Posiciones válidas: {list(ELECTRODE_POSITION_MAPPING.keys())}")
    return None

# ==================== DETECCIÓN AUTOMÁTICA DE IP DEL SERVIDOR ====================

def get_local_ip():
    """
    Obtiene la IP local de la PC en la red
    Retorna la IP que se usa para conectarse a Internet
    """
    try:
        # Crear socket temporal para determinar IP local
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        # No necesita conectarse realmente, solo determina qué interfaz usaría
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        print(f"[IP] No se pudo detectar IP automáticamente: {e}")
        return "192.168.1.100"  # Fallback

def find_server_ip():
    """
    Busca automáticamente la IP del servidor en la red local
    Asume que el servidor está en la misma subred que esta PC
    """
    # ========== CONFIGURACIÓN MANUAL (OPCIONAL) ==========
    # Si la detección automática no funciona, descomente y configure aquí:
    MANUAL_SERVER_IP = None  # Ejemplo: "192.168.1.100"
    
    if MANUAL_SERVER_IP:
        print(f"\n[IP] Usando IP configurada manualmente: {MANUAL_SERVER_IP}")
        return MANUAL_SERVER_IP
    # ======================================================
    
    local_ip = get_local_ip()
    
    # Extraer los primeros 3 octetos (subred)
    # Ejemplo: 192.168.1.100 -> 192.168.1
    subnet = '.'.join(local_ip.split('.')[:-1])
    
    print(f"\n[IP] Detectando servidor en la red...")
    print(f"[IP] IP local de esta PC: {local_ip}")
    print(f"[IP] Buscando servidor en subred: {subnet}.x")
    
    # Estrategia 1: Usar la misma IP para todo (asumiendo servidor en esta PC)
    # Esto es común en desarrollo
    if local_ip:
        print(f"[IP] ✓ Usando IP local como servidor: {local_ip}")
        return local_ip
    
    # Estrategia 2: IPs comunes de servidor
    common_server_ips = [
        f"{subnet}.1",    # Router (común para servidores)
        f"{subnet}.100",  # IP común de servidor
        f"{subnet}.10",   # Otra IP común
        local_ip,         # Esta misma PC
    ]
    
    return common_server_ips[0]  # Retornar la más probable

# Detectar IP del servidor automáticamente
SERVER_IP = find_server_ip()

# ==================== CONSTANTES DE CONVERSIÓN ADC ====================
ADC_MAX_VALUE = 4095  # 12 bits = 2^12 - 1
VREF = 3.3  # Voltaje de referencia en voltios
ADC_TO_VOLTAGE_FACTOR = VREF / ADC_MAX_VALUE  # Factor de conversión

def adc_to_voltage(adc_value):
    """Convierte un valor ADC de 12 bits a voltaje"""
    try:
        adc_int = int(adc_value)
        if adc_int < 0:
            adc_int = 0
        elif adc_int > ADC_MAX_VALUE:
            adc_int = ADC_MAX_VALUE
        
        voltage = adc_int * ADC_TO_VOLTAGE_FACTOR
        return round(voltage, 6)
    except:
        return 0.0

def voltage_to_adc(voltage):
    """Convierte voltaje a valor ADC (útil para debug)"""
    try:
        v = float(voltage)
        if v < 0:
            v = 0
        elif v > VREF:
            v = VREF
        
        adc = int(v / ADC_TO_VOLTAGE_FACTOR)
        return adc
    except:
        return 0

# ==================== CONSTANTES TDM ====================
TDM_CYCLE_DURATION_MS = 1500      # Ciclo completo de 1000 ms
MAX_SLAVES = 10                    # Máximo de esclavos (10 slots de 100 ms cada uno)
TDM_SLOT_DURATION_MS = 150         # Duración de cada slot
TDM_SLOT_TIMEOUT_MS = 140           # Timeout dentro del slot
TDM_WAIT_BEFORE_FIRST_REQUEST_MS = 1500  # Esperar antes de la primera solicitud

# Configuración Serial
# Si la auto-detección no funciona, configure manualmente aquí:
SERIAL_PORT = None    # ← CAMBIE AQUÍ si su ESP32 está en otro puerto
SERIAL_BAUDRATE = 115200
SERIAL_TIMEOUT = 1

# Comandos seriales (equivalentes a los mensajes TTL)
SERIAL_COMMANDS = {
    'ENCONTRAR': 'CMD:ENCONTRAR\n',
    'INICIAR': 'CMD:INICIAR\n',
    'FINALIZAR': 'CMD:FINALIZAR\n',
    'RESET': 'CMD:RESET\n'
}

# Configuración MQTT - USA IP AUTOMÁTICA
MQTT_BROKER = SERVER_IP  # Detectado automáticamente
MQTT_PORT = 1883
MQTT_TOPIC_DATA = "esp32/+/data"
MQTT_TOPIC_STATUS = "esp32/+/status"
MQTT_TOPIC_REGISTER_WILDCARD = "esp32/slave/register/#"
MQTT_TOPIC_REQUEST = "esp32/master/request/{}"  # Topic para solicitudes
WS_IP = "192.168.5.68"
# Configuración WebSocket - USA IP AUTOMÁTICA
WEBSOCKET_SERVER = f"ws://{WS_IP}:8080/raspberryControl"  # Detectado automáticamente

TIMEOUT_FINALIZACION = 20

# Variables globales
cleanup_ejecutado = False
mqtt_client = None
ws_client = None
serial_connection = None
adquisicion_activa = False
finalizacion_enviada = False
file_lock = Lock()
csv_files = {}
session_folder = None
packets_received = {}
samples_received = {}
esclavos_finalizados = set()
esclavos_activos = set()
ultimo_paquete_tiempo = {}
command_queue = []
command_lock = Lock()
electrodos_registrados = {}
ecg_realtime_calculator = None  # Calculador en tiempo real
discovery_count = 0

# Variables para gestión de usuarios
user_manager = None
current_user = None
current_user_dir = None
current_session_dir = None
current_session_num = None

# ==================== VARIABLES TDM ====================
tdm_active = False
tdm_thread = None
tdm_stop_event = Event()
tdm_lock = Lock()
tdm_current_packet_id = 0
tdm_slot_index = 0
tdm_slaves_list = []
tdm_slot_responses = {}  # {slave_id: {'packet_id': X, 'received': bool, 'timestamp': T}}
tdm_packets_in_cycle = 1  # Cuantos paquetes se solicitan en cada ciclo completo

# ==================== COMUNICACIÓN SERIAL ====================

def find_esp32_port():
    """Busca automáticamente el puerto COM del ESP32"""
    print("\n[SERIAL] Buscando puerto del ESP32...")
    
    ports = serial.tools.list_ports.comports()
    
    esp32_identifiers = [
        "ECGESP32",
        "CH340",
        "CP210",
        "USB-SERIAL",
        "UART",
        "USB UART",
        "USB Serial"
    ]
    
    for port in ports:
        print(f"[SERIAL] Encontrado: {port.device} - {port.description}")
        
        for identifier in esp32_identifiers:
            if identifier.upper() in port.description.upper():
                print(f"[SERIAL] ✓ ESP32 detectado en {port.device} (identificador: {identifier})")
                return port.device
    
    print("[SERIAL] No se pudo detectar automáticamente el ESP32")
    print("[SERIAL] Puertos disponibles:")
    for port in ports:
        print(f"  - {port.device}: {port.description}")
    
    for port in ports:
        if "CH340" in port.description or port.device == "COM5":
            print(f"\n[SERIAL] SUGERENCIA: Probablemente sea {port.device}")
            print(f"[SERIAL] Para usar automáticamente, configure:")
            print(f'         SERIAL_PORT = "{port.device}"')
            return port.device
    
    return None

def enviar_ip_servidor():
    """Envía la IP del servidor al ESP32 después de establecer conexión"""
    global serial_connection, SERVER_IP
    
    if serial_connection is None or not serial_connection.is_open:
        print("[SERIAL ERROR] No se puede enviar IP - puerto no abierto")
        return False
    
    try:
        print(f"\n{'='*60}")
        print(f"[{get_timestamp()}] ENVIANDO IP DEL SERVIDOR AL ESP32")
        print(f"{'='*60}")
        print(f"  [IP] Servidor: {SERVER_IP}")
        
        ip_command = f"IP:{SERVER_IP}\n"
        
        serial_connection.reset_input_buffer()
        
        bytes_written = serial_connection.write(ip_command.encode('utf-8'))
        serial_connection.flush()
        
        print(f"  [SERIAL TX] {bytes_written} bytes enviados: {ip_command.strip()}")
        
        print(f"  [SERIAL] Esperando confirmación...")
        start_time = time.time()
        timeout = 3
        
        while (time.time() - start_time) < timeout:
            if serial_connection.in_waiting > 0:
                response = serial_connection.readline().decode('utf-8').strip()
                print(f"  [SERIAL RX] {response}")
                
                if "IP:OK" in response or "RESP:IP:OK" in response:
                    print(f"  [OK] ✓ ESP32 confirmó recepción de IP")
                    print(f"{'='*60}\n")
                    return True
                break
            time.sleep(0.01)
        
        print(f"  [WARN] No se recibió confirmación (timeout)")
        print(f"{'='*60}\n")
        return True
        
    except Exception as e:
        print(f"[SERIAL ERROR] Error al enviar IP: {e}")
        return False

def setup_serial():
    """Inicializa conexión serial con el ESP32"""
    global serial_connection, SERIAL_PORT
    
    if SERIAL_PORT is None:
        SERIAL_PORT = find_esp32_port()
    
    if SERIAL_PORT is None:
        print("[SERIAL ERROR] No se pudo detectar el puerto del ESP32")
        print("[SERIAL] Por favor, configure SERIAL_PORT manualmente en el código")
        return False
    
    try:
        print(f"\n[SERIAL] Conectando a {SERIAL_PORT} @ {SERIAL_BAUDRATE} baud...")
        
        serial_connection = serial.Serial(
            port=SERIAL_PORT,
            baudrate=SERIAL_BAUDRATE,
            timeout=SERIAL_TIMEOUT,
            write_timeout=2
        )
        
        time.sleep(2)
        
        serial_connection.reset_input_buffer()
        serial_connection.reset_output_buffer()
        
        print("[SERIAL] ✓ Conexión establecida")
        
        test_response = send_serial_command("PING", expect_response=True)
        if test_response:
            print(f"[SERIAL] ✓ ESP32 respondió: {test_response}")
        
        print("\n[SERIAL] Configurando ESP32 con IP del servidor...")
        if enviar_ip_servidor():
            print("[SERIAL] ✓ IP del servidor enviada correctamente")
        else:
            print("[SERIAL] ⚠️  No se pudo confirmar envío de IP")
        
        start_ip_sender()

        return True
    
    except serial.SerialException as e:
        print(f"[SERIAL ERROR] No se pudo abrir puerto {SERIAL_PORT}: {e}")
        return False
    except Exception as e:
        print(f"[SERIAL ERROR] Error inesperado: {e}")
        return False

def send_serial_command(command, expect_response=False, timeout=2):
    """Envía un comando por serial al ESP32"""
    global serial_connection
    
    if serial_connection is None or not serial_connection.is_open:
        print("[SERIAL ERROR] Puerto serial no está abierto")
        return None
    
    try:
        if command in SERIAL_COMMANDS:
            serial_cmd = SERIAL_COMMANDS[command]
        else:
            serial_cmd = f"CMD:{command}\n"
        
        print(f"\n{'='*60}")
        print(f"[{get_timestamp()}] ENVIANDO COMANDO SERIAL: {command}")
        print(f"{'='*60}")
        
        serial_connection.reset_input_buffer()
        
        bytes_written = serial_connection.write(serial_cmd.encode('utf-8'))
        serial_connection.flush()
        
        print(f"  [SERIAL TX] {bytes_written} bytes enviados: {serial_cmd.strip()}")
        
        response = None
        if expect_response:
            print(f"  [SERIAL] Esperando respuesta (timeout: {timeout}s)...")
            start_time = time.time()
            
            while (time.time() - start_time) < timeout:
                if serial_connection.in_waiting > 0:
                    response = serial_connection.readline().decode('utf-8').strip()
                    print(f"  [SERIAL RX] {response}")
                    break
                time.sleep(0.01)
            
            if response is None:
                print("  [SERIAL] No se recibió respuesta (timeout)")
        
        print(f"[{get_timestamp()}] Comando completado")
        print(f"{'='*60}\n")
        
        return response
    
    except serial.SerialException as e:
        print(f"[SERIAL ERROR] Error de comunicación: {e}")
        return None
    except Exception as e:
        print(f"[SERIAL ERROR] Error inesperado: {e}")
        return None

def get_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def enviar_reset():
    """Envía comando RESET por serial"""
    print("\n" + "="*60)
    print(f"[{get_timestamp()}] ENVIANDO RESET")
    print("="*60)
    
    response = send_serial_command('RESET', expect_response=True, timeout=3)
    
    if response and "OK" in response:
        print("  [OK] Reset confirmado por ESP32")
    else:
        print("  [WARN] Reset enviado pero sin confirmación")
    
    print("  [OK] Esperando reinicio de sistema...")
    time.sleep(2.5)
    
    print(f"[{get_timestamp()}] Sistema reiniciado")
    print("="*60 + "\n")
    time.sleep(5)
    
    print("\n[SERIAL] Configurando ESP32 con IP del servidor...")
    if enviar_ip_servidor():
        print("[SERIAL] ✓ IP del servidor enviada correctamente")
    else:
        print("[SERIAL] ⚠️  No se pudo confirmar envío de IP")
    send_ws_status()

def enviar_pulso(mensaje):
    """Envía comando TTL equivalente por serial"""
    if mensaje not in SERIAL_COMMANDS:
        print(f"[SERIAL ERROR] Mensaje inválido: {mensaje}")
        return False
    
    response = send_serial_command(mensaje, expect_response=True, timeout=2)
    
    if response and "OK" in response:
        print(f"  [OK] Comando {mensaje} confirmado por ESP32")
        return True
    else:
        print(f"  [WARN] Comando {mensaje} enviado pero sin confirmación")
        return True

# ==================== ENVÍO RECURRENTE DE IP ====================

ip_sender_thread = None
ip_sender_active = False
ip_sender_stop_event = Event()
IP_SEND_INTERVAL_SECONDS = 60

def enviar_ip_servidor_recurrente():
    """Función que se ejecuta en un hilo separado para enviar la IP periódicamente"""
    global ip_sender_active, SERVER_IP
    
    print(f"\n[IP_SENDER] Hilo iniciado - Enviando IP cada {IP_SEND_INTERVAL_SECONDS} segundos")
    
    while ip_sender_active and not ip_sender_stop_event.is_set():
        try:
            if serial_connection is None or not serial_connection.is_open:
                print("[IP_SENDER] Puerto serial no disponible, esperando...")
                time.sleep(5)
                continue
            
            success = enviar_ip_servidor()
            
            if success:
                print(f"[IP_SENDER] ✓ IP enviada correctamente: {SERVER_IP}")
            else:
                print(f"[IP_SENDER] ⚠️ Fallo al enviar IP, reintentando en {IP_SEND_INTERVAL_SECONDS}s")
            
            ip_sender_stop_event.wait(timeout=IP_SEND_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"[IP_SENDER ERROR] Error en hilo de envío: {e}")
            time.sleep(5)
    
    print("[IP_SENDER] Hilo detenido")

def start_ip_sender():
    """Inicia el hilo de envío recurrente de IP"""
    global ip_sender_thread, ip_sender_active, ip_sender_stop_event
    
    if ip_sender_active:
        print("[IP_SENDER] Ya está activo")
        return False
    
    print(f"\n[IP_SENDER] Iniciando envío recurrente de IP cada {IP_SEND_INTERVAL_SECONDS}s...")
    
    ip_sender_active = True
    ip_sender_stop_event.clear()
    
    ip_sender_thread = Thread(target=enviar_ip_servidor_recurrente, daemon=True)
    ip_sender_thread.start()
    
    print("[IP_SENDER] ✓ Hilo iniciado")
    return True

def stop_ip_sender():
    """Detiene el hilo de envío recurrente de IP"""
    global ip_sender_active, ip_sender_thread, ip_sender_stop_event
    
    if not ip_sender_active:
        return
    
    print("\n[IP_SENDER] Deteniendo hilo de envío...")
    
    ip_sender_active = False
    ip_sender_stop_event.set()
    
    if ip_sender_thread and ip_sender_thread.is_alive():
        ip_sender_thread.join(timeout=3)
        print("[IP_SENDER] Hilo detenido")

# ==================== GESTIÓN DE ARCHIVOS CON VOLTAJE ====================

def crear_sesion():
    """Crea carpeta para nueva sesión en el directorio del usuario"""
    global session_folder, current_session_dir, current_session_num
    global ecg_realtime_calculator
    
    if current_user is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_folder = os.path.join(SCRIPT_DIR, f"data_session_{timestamp}")
        if not os.path.exists(session_folder):
            os.makedirs(session_folder)
        print(f"\n[SESION] Carpeta creada (sin usuario): {session_folder}")
        return session_folder
    
    result = user_manager.create_new_session(current_user)
    
    if result['success']:
        session_folder = result['electrodos_dir']
        current_session_dir = result['session_dir']
        current_session_num = result['session_num']
        
        print("\n" + "="*60)
        print("[SESION] Nueva sesión creada para usuario")
        print(f"  Usuario: {current_user.get('nombreCompleto', 'Unknown')}")
        print(f"  Sesión: {current_session_num}")
        print(f"  Directorio: {session_folder}")
        print("="*60 + "\n")
        
        print("[ECG_RT] Verificando electrodos registrados...")
        print(f"[ECG_RT] Total electrodos: {len(electrodos_registrados)}")
        
        if len(electrodos_registrados) == 0:
            print("[ECG_RT] ⚠️  NO HAY ELECTRODOS REGISTRADOS")
            print("[ECG_RT] Ejecute comando ENCONTRAR primero")
            return session_folder
        
        for slave_id, info in electrodos_registrados.items():
            pos = info.get('electrode_position', 'Unknown')
            pos_raw = info.get('electrode_position_raw', pos)
            name = info.get('electrode_name', 'Unknown')
            print(f"[ECG_RT]   - Slave {slave_id}: {name} ('{pos_raw}' → '{pos}')")
        
        if ECGRealtimeDerivationsCalculator is not None:
            try:
                electrodes_mapping = {}
                invalid_count = 0
                
                for slave_id, info in electrodos_registrados.items():
                    position = info.get('electrode_position', 'Unknown')
                    position_raw = info.get('electrode_position_raw', position)
                    
                    if position.startswith('INVALID_'):
                        print(f"[ECG_RT]   ✗ Electrodo {slave_id} tiene posición inválida: '{position_raw}'")
                        invalid_count += 1
                        continue
                    
                    if position in ECGRealtimeDerivationsCalculator.ELECTRODE_POSITIONS:
                        electrodes_mapping[slave_id] = position
                        print(f"[ECG_RT]   ✓ Electrodo {slave_id} ('{position_raw}' → '{position}') agregado al mapeo")
                    else:
                        print(f"[ECG_RT]   ✗ Electrodo {slave_id} posición no reconocida: '{position}'")
                        invalid_count += 1
                
                if invalid_count > 0:
                    print(f"[ECG_RT] ⚠️  {invalid_count} electrodo(s) con posiciones inválidas (ignorados)")
                
                if not electrodes_mapping:
                    print("[ECG_RT] ⚠️  NO HAY ELECTRODOS CON POSICIONES VÁLIDAS")
                    print(f"[ECG_RT] Posiciones válidas: {ECGRealtimeDerivationsCalculator.ELECTRODE_POSITIONS}")
                    print("[ECG_RT] Asegúrese de que los ESP32 envíen:")
                    print("[ECG_RT]   - 'Brazo Derecho' (o 'RA') para brazo derecho")
                    print("[ECG_RT]   - 'Brazo Izquierdo' (o 'LA') para brazo izquierdo")
                    print("[ECG_RT]   - 'Pierna Izquierda' (o 'LL') para pierna izquierda")
                    return session_folder
                
                print(f"[ECG_RT] Creando calculador con {len(electrodes_mapping)} electrodos...")
                print(f"[ECG_RT] Mapeo final: {electrodes_mapping}")
                
                ecg_realtime_calculator = ECGRealtimeDerivationsCalculator(
                    session_folder=session_folder,
                    electrodes_mapping=electrodes_mapping,
                    ws_callback=send_ws_message
                )
                
                print("[ECG_RT] ✓ Calculador creado exitosamente")
                print("[ECG_RT] ✓ Callback WebSocket configurado")
                print("[ECG_RT] Esperando comando INICIAR para comenzar procesamiento...")
            
            except Exception as e:
                print(f"[ECG_RT ERROR] Error creando calculador: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("[ECG_RT] ⚠️  ECG Calculator no disponible (no se importó el módulo)")
    
    else:
        print(f"[ERROR] No se pudo crear sesión: {result.get('error')}")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_folder = os.path.join(SCRIPT_DIR, f"data_session_{timestamp}")
        os.makedirs(session_folder, exist_ok=True)
    
    return session_folder

def abrir_archivo_esclavo(slave_id):
    """Abre archivo CSV para un esclavo - AHORA CON VOLTAJE"""
    global csv_files
    
    if slave_id in csv_files:
        return csv_files[slave_id]
    
    if session_folder is None:
        print(f"[INFO] Esperando inicio de sesión para crear archivo de esclavo {slave_id}")
        return None
    
    electrode_name = "slave"
    if slave_id in electrodos_registrados:
        electrode_name = electrodos_registrados[slave_id].get('electrode_name', 'slave')
    
    filename = os.path.join(session_folder, f"{electrode_name}_{slave_id}_data.csv")
    
    file_handle = open(filename, 'w', newline='', encoding='utf-8')
    csv_writer = csv.writer(file_handle)
    
    csv_writer.writerow(['timestamp_us', 'sample_num', 'adc_value', 'voltage'])
    
    csv_files[slave_id] = (file_handle, csv_writer)
    esclavos_activos.add(slave_id)
    
    print(f"[CSV] Archivo abierto con conversión a voltaje: {filename}")
    
    return csv_files[slave_id]

def cerrar_archivos_seguros():
    """Cierra archivos CSV SOLO CUANDO ES SEGURO"""
    global csv_files, ecg_realtime_calculator
    
    print("\n[ARCHIVOS] Iniciando cierre seguro de archivos...")
    
    # Esperar a que el calculador ECG termine
    if ecg_realtime_calculator is not None:
        try:
            stats = ecg_realtime_calculator.get_stats()
            
            if stats.get('running', False):
                print("[ARCHIVOS] ⚠️  Calculador ECG aún está activo")
                print("[ARCHIVOS] Esperando finalización del calculador...")
                
                timeout = 30
                start_time = time.time()
                
                while time.time() - start_time < timeout:
                    stats = ecg_realtime_calculator.get_stats()
                    
                    if not stats.get('running', False):
                        print("[ARCHIVOS] ✓ Calculador ECG ha finalizado")
                        break
                    
                    threads = stats.get('active_threads', {})
                    readers = threads.get('readers', 0)
                    processors = threads.get('processors', 0)
                    
                    if readers == 0 and processors == 0:
                        print("[ARCHIVOS] ✓ Todos los hilos del calculador han terminado")
                        break
                    
                    print(f"[ARCHIVOS] Esperando... (lectores: {readers}, procesadores: {processors})")
                    time.sleep(1)
                
                if hasattr(ecg_realtime_calculator, 'shutdown_complete'):
                    print("[ARCHIVOS] Esperando señal de cierre completo...")
                    shutdown_ok = ecg_realtime_calculator.shutdown_complete.wait(timeout=10)
                    
                    if shutdown_ok:
                        print("[ARCHIVOS] ✓ Calculador confirmó cierre completo")
                    else:
                        print("[ARCHIVOS] ⚠️  Timeout esperando cierre completo")
        
        except Exception as e:
            print(f"[ARCHIVOS] Error verificando calculador: {e}")
    
    # Ahora sí cerrar archivos
    print("\n[ARCHIVOS] Procediendo a cerrar archivos CSV...")
    
    with file_lock:
        archivos_cerrados = 0
        
        for slave_id, (file_handle, _) in csv_files.items():
            try:
                if not file_handle.closed:
                    file_handle.flush()
                    file_handle.close()
                    archivos_cerrados += 1
                    
                    electrode_name = electrodos_registrados.get(slave_id, {}).get('electrode_name', str(slave_id))
                    print(f"[ARCHIVOS] ✓ Cerrado: {electrode_name} (ID: {slave_id})")
            except Exception as e:
                print(f"[ARCHIVOS] Error cerrando archivo {slave_id}: {e}")
        
        csv_files.clear()
        print(f"\n[ARCHIVOS] Total archivos cerrados: {archivos_cerrados}")

def cerrar_archivos():
    """Cierra todos los archivos CSV - con verificación de calculador"""
    global csv_files, ecg_realtime_calculator
    
    # Si hay calculador activo, usar versión segura
    if ecg_realtime_calculator is not None:
        try:
            stats = ecg_realtime_calculator.get_stats()
            if stats.get('running', False):
                print("[ARCHIVOS] Usando cierre seguro por calculador activo...")
                cerrar_archivos_seguros()
                return
        except:
            pass
    
    # Si no hay calculador, cerrar normalmente
    with file_lock:
        for slave_id, (file_handle, _) in csv_files.items():
            try:
                file_handle.flush()
                file_handle.close()
                print(f"[CSV] Cerrado archivo de esclavo {slave_id}")
            except:
                pass
        
        csv_files = {}

# ==================== UTILIDADES ====================

def safe_int_from_string(s, default=None):
    if s is None:
        return default
    m = re.search(r'(\d+)$', str(s))
    if m:
        try:
            return int(m.group(1))
        except:
            return default
    return default

def extract_slave_id_from_topic(topic):
    parts = topic.split('/')
    if len(parts) >= 4:
        last = parts[-1]
        sid = safe_int_from_string(last)
        if sid is not None:
            return sid
    if len(parts) >= 2:
        second = parts[1]
        sid = safe_int_from_string(second)
        if sid is not None:
            return sid
    return None

def leer_datos_csv(filepath):
    """Lee y ordena datos de un archivo CSV - AHORA CON VOLTAJE"""
    try:
        datos = []
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    datos.append({
                        'timestamp_us': int(row['timestamp_us']),
                        'sample_num': int(row['sample_num']),
                        'adc_value': int(row['adc_value']),
                        'voltage': float(row.get('voltage', 0.0))
                    })
                except (ValueError, KeyError):
                    continue
        
        datos.sort(key=lambda x: x['sample_num'])
        return datos
    except Exception as e:
        print(f"[ERROR] Leyendo CSV {filepath}: {e}")
        return []

def enviar_datos_grafica(slave_id):
    """Envía datos del CSV al servidor para graficar - INCLUYE VOLTAJE"""
    if session_folder is None:
        print("[WARN] No hay sesión activa")
        return
    
    electrode_name = "slave"
    if slave_id in electrodos_registrados:
        electrode_name = electrodos_registrados[slave_id].get('electrode_name', 'slave')
    
    filename = os.path.join(session_folder, f"{electrode_name}_{slave_id}_data.csv")
    
    if not os.path.exists(filename):
        print(f"[WARN] Archivo no existe: {filename}")
        return
    
    print(f"[GRAFICA] Leyendo datos de {filename}...")
    datos = leer_datos_csv(filename)
    
    if not datos:
        print("[WARN] No hay datos para graficar")
        return
    
    print(f"[GRAFICA] Datos leídos: {len(datos)} muestras")
    
    BATCH_SIZE = 1000
    total_batches = (len(datos) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for i in range(0, len(datos), BATCH_SIZE):
        batch = datos[i:i+BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        
        send_ws_message('graph_data_batch', {
            'slave_id': slave_id,
            'electrode_name': electrode_name,
            'batch_num': batch_num,
            'total_batches': total_batches,
            'data': batch
        })
        
        print(f"[GRAFICA] Enviado batch {batch_num}/{total_batches} ({len(batch)} muestras)")
        
        time.sleep(0.05)
    
    print(f"[GRAFICA] ✓ Envío completado para electrodo {electrode_name}")

# ==================== TDM CONTROLLER ====================

def tdm_controller_thread():
    """Hilo dedicado para controlar el multiplexeo TDM"""
    global tdm_active, tdm_current_packet_id, tdm_slot_index, tdm_slaves_list
    global tdm_slot_responses, adquisicion_activa
    
    print("\n[TDM] ========================================")
    print("[TDM] CONTROLLER INICIADO")
    print(f"[TDM] Ciclo: {TDM_CYCLE_DURATION_MS} ms | Slot: {TDM_SLOT_DURATION_MS} ms | Esclavos: {len(tdm_slaves_list)}")
    print("[TDM] ========================================\n")
    
    print(f"[TDM] Esperando {TDM_WAIT_BEFORE_FIRST_REQUEST_MS} ms para que esclavos preparen datos...")
    time.sleep(TDM_WAIT_BEFORE_FIRST_REQUEST_MS / 1000.0)
    
    cycle_count = 0
    
    while tdm_active and not tdm_stop_event.is_set():
        try:
            if not adquisicion_activa:
                time.sleep(0.1)
                continue
            
            cycle_count += 1
            tdm_current_packet_id = cycle_count
            
            print(f"\n[TDM] CICLO {cycle_count} - Solicitando PAQUETE {tdm_current_packet_id}")
            
            with tdm_lock:
                tdm_slot_responses.clear()
            
            for slot_idx, slave_id in enumerate(tdm_slaves_list):
                if not adquisicion_activa or tdm_stop_event.is_set():
                    break
                
                slot_start_time = time.time()
                
                request_topic = MQTT_TOPIC_REQUEST.format(slave_id)
                request_payload = json.dumps({
                    "get_packet": tdm_current_packet_id
                })
                
                try:
                    mqtt_client.publish(request_topic, request_payload, qos=1)
                    print(f"[TDM][Slot {slot_idx + 1}] Solicitando a esclavo {slave_id} (PAQ {tdm_current_packet_id})")
                except Exception as e:
                    print(f"[TDM][Slot {slot_idx + 1}] Error publicando solicitud: {e}")
                
                with tdm_lock:
                    tdm_slot_responses[slave_id] = {
                        'packet_id': tdm_current_packet_id,
                        'received': False,
                        'timestamp': time.time()
                    }
                
                elapsed = (time.time() - slot_start_time) * 1000
                remaining = TDM_SLOT_TIMEOUT_MS - elapsed
                
                if remaining > 0:
                    time.sleep(remaining / 1000.0)
                
                with tdm_lock:
                    if slave_id in tdm_slot_responses:
                        if tdm_slot_responses[slave_id]['received']:
                            electrode_name = electrodos_registrados.get(slave_id, {}).get('electrode_name', str(slave_id))
                            print(f"[TDM][Slot {slot_idx + 1}] ✓ Respuesta OK - {electrode_name} (200 muestras)")
                        else:
                            print(f"[TDM][Slot {slot_idx + 1}] ✗ Timeout esperando a esclavo {slave_id}")
        
        except Exception as e:
            print(f"[TDM][ERROR] Error en ciclo TDM: {e}")
            time.sleep(0.5)

def start_tdm_controller():
    """Inicia el hilo controlador de TDM"""
    global tdm_thread, tdm_active, tdm_stop_event, tdm_slaves_list
    
    if not adquisicion_activa:
        print("[TDM] No se puede iniciar TDM sin adquisición activa")
        return False
    
    if len(electrodos_registrados) == 0:
        print("[TDM] No hay esclavos registrados")
        return False
    
    with tdm_lock:
        tdm_slaves_list = sorted(list(electrodos_registrados.keys()))
        tdm_active = True
        tdm_stop_event.clear()
    
    print(f"\n[TDM] Esclavos detectados: {tdm_slaves_list}")
    
    tdm_thread = Thread(target=tdm_controller_thread, daemon=False)
    tdm_thread.start()
    
    return True

def stop_tdm_controller():
    """Detiene el hilo controlador de TDM"""
    global tdm_active, tdm_thread, tdm_stop_event
    
    print("\n[TDM] Deteniendo controlador TDM...")
    
    with tdm_lock:
        tdm_active = False
        tdm_stop_event.set()
    
    if tdm_thread and tdm_thread.is_alive():
        tdm_thread.join(timeout=2)
        print("[TDM] Controlador TDM detenido")

# ==================== MQTT CON CONVERSIÓN ADC Y TDM ====================

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("\n[MQTT] Conectado al broker!")
        client.subscribe(MQTT_TOPIC_DATA)
        client.subscribe(MQTT_TOPIC_STATUS)
        client.subscribe(MQTT_TOPIC_REGISTER_WILDCARD)
        print("[MQTT] Suscrito a topics: data, status, register")
        print(f"[MQTT] Conversión ADC habilitada: 12 bits -> {VREF} V")
    else:
        print(f"[MQTT ERROR] Conexión fallida (rc={rc})")

def on_message(client, userdata, msg):
    """Callback de mensaje MQTT - CON CONVERSIÓN A VOLTAJE Y DETECCIÓN TDM"""
    global packets_received, samples_received, ultimo_paquete_tiempo, electrodos_registrados
    global tdm_slot_responses
    
    topic = msg.topic
    payload_raw = msg.payload
    payload_text = None
    try:
        payload_text = payload_raw.decode('utf-8')
    except:
        payload_text = str(payload_raw)
    
    slave_id = extract_slave_id_from_topic(topic)
    
    payload_json = None
    if 'data' in topic or 'register' in topic or 'status' in topic:
        try:
            payload_json = json.loads(payload_text)
        except:
            payload_json = None
    
    if slave_id is None and payload_json:
        if 'slave' in payload_json:
            try:
                slave_id = int(payload_json['slave'])
            except:
                slave_id = safe_int_from_string(payload_json.get('slave'))
        elif 'slave_id' in payload_json:
            try:
                slave_id = int(payload_json['slave_id'])
            except:
                slave_id = safe_int_from_string(payload_json.get('slave_id'))
    
    # CASO: Registro
    if 'register' in topic:
        if slave_id is None and payload_json:
            slave_id = payload_json.get('slave_id') or payload_json.get('slave')
            try:
                slave_id = int(slave_id)
            except:
                slave_id = safe_int_from_string(slave_id)
        if slave_id is None:
            print("[MQTT][REGISTER] Registro sin slave_id")
            return
        
        electrode_name = payload_json.get('electrode_name', 'Unknown') if payload_json else 'Unknown'
        electrode_position_raw = payload_json.get('electrode_position', 'Unknown') if payload_json else 'Unknown'
        electrode_color = payload_json.get('electrode_color', '#666666') if payload_json else '#666666'
        mac = payload_json.get('mac') if payload_json else None
        ip = payload_json.get('ip') if payload_json else None
        rssi = payload_json.get('rssi') if payload_json else None
        
        electrode_position = normalize_electrode_position(electrode_position_raw)
        
        if electrode_position is None:
            print(f"[MQTT][REGISTER] ⚠️  Posición inválida para electrodo {slave_id}")
            print(f"[MQTT][REGISTER] Posición recibida: '{electrode_position_raw}'")
            print(f"[MQTT][REGISTER] Electrodo NO será agregado al sistema ECG")
            electrode_position = f"INVALID_{electrode_position_raw}"
        else:
            print(f"[MQTT][REGISTER] Posición normalizada: '{electrode_position_raw}' → '{electrode_position}'")
        
        if slave_id in electrodos_registrados:
            print(f"[MQTT][REGISTER] Electrodo RE-REGISTRADO (Discovery #{discovery_count})")
            print(f"  ID: {slave_id} | Nombre: {electrode_name} (YA CONOCIDO)")
            electrodos_registrados[slave_id].update({
                'electrode_position': electrode_position,
                'electrode_position_raw': electrode_position_raw,
                'ip': ip,
                'rssi': rssi,
                'last_seen': time.time(),
                'discovery_count': discovery_count,
                'seen_in_current_discovery': True
            })
        else:
            print(f"[MQTT][REGISTER] Electrodo NUEVO (Discovery #{discovery_count})")
            print(f"  ID: {slave_id} | Nombre: {electrode_name} | Posición: {electrode_position_raw} → {electrode_position}")
            print(f"  MAC: {mac} | IP: {ip} | RSSI: {rssi} | Color: {electrode_color}")
            
            electrodos_registrados[slave_id] = {
                'slave_id': slave_id,
                'electrode_name': electrode_name,
                'electrode_position': electrode_position,
                'electrode_position_raw': electrode_position_raw,
                'electrode_color': electrode_color,
                'mac': mac,
                'ip': ip,
                'rssi': rssi,
                'registered_at': time.time(),
                'last_seen': time.time(),
                'discovery_count': discovery_count,
                'seen_in_current_discovery': True
            }
        
        print(f"[MQTT][REGISTER] Electrodos totales registrados: {len(electrodos_registrados)}")
        
        registered_positions = [
            info.get('electrode_position') 
            for info in electrodos_registrados.values()
            if not info.get('electrode_position', '').startswith('INVALID_')
        ]
        
        has_ra = 'RA' in registered_positions
        has_la = 'LA' in registered_positions
        has_ll = 'LL' in registered_positions
        
        print(f"[MQTT][REGISTER] Posiciones ECG: RA={has_ra}, LA={has_la}, LL={has_ll}")
        
        if has_ra and has_la:
            print("[MQTT][REGISTER] ✓ Electrodos mínimos para ECG presentes (RA + LA)")
            if has_ll:
                print("[MQTT][REGISTER] ✓ LL presente: se pueden calcular 6 derivaciones (I, II, III, aVR, aVL, aVF)")
            else:
                print("[MQTT][REGISTER] ℹ️  Sin LL: solo se calculará derivación I")
        else:
            faltantes = []
            if not has_ra:
                faltantes.append("RA (Brazo Derecho)")
            if not has_la:
                faltantes.append("LA (Brazo Izquierdo)")
            print(f"[MQTT][REGISTER] ⚠️  Faltan electrodos: {', '.join(faltantes)}")

        send_ws_message('slave_register', {
            'slave_id': slave_id,
            'electrode_name': electrode_name,
            'electrode_position': electrode_position,
            'electrode_position_raw': electrode_position_raw,
            'electrode_color': electrode_color,
            'mac': mac,
            'ip': ip,
            'rssi': rssi,
            'discovery_count': discovery_count,
            'is_rediscovery': slave_id in electrodos_registrados
        })
        return
    
    # CASO: Status
    if 'status' in topic:
        if slave_id is None:
            slave_id = extract_slave_id_from_topic(topic)
        if slave_id is None:
            print("[MQTT][STATUS] Status sin slave_id")
            return
        
        electrode_name = electrodos_registrados.get(slave_id, {}).get('electrode_name', str(slave_id))
        print(f"[MQTT][STATUS] Electrodo {electrode_name} ({slave_id}) -> {payload_text}")
        
        send_ws_message('slave_status', {
            'slave_id': slave_id,
            'electrode_name': electrode_name,
            'status': payload_text
        })
        return

    # CASO: Data - CONVERSIÓN A VOLTAJE + VALIDACIÓN TDM
    if 'data' in topic:
        if slave_id is None and payload_json:
            if 'slave' in payload_json:
                try:
                    slave_id = int(payload_json['slave'])
                except:
                    slave_id = safe_int_from_string(payload_json['slave'])
            elif 'slave_id' in payload_json:
                try:
                    slave_id = int(payload_json['slave_id'])
                except:
                    slave_id = safe_int_from_string(payload_json['slave_id'])
        
        if slave_id is None:
            print("[MQTT ERROR] Datos sin slave_id")
            return
        
        packet_id = payload_json.get('id', 0) if payload_json else 0
        is_final = payload_json.get('final', False) if payload_json else False
        
        with tdm_lock:
            if tdm_active and slave_id in tdm_slot_responses:
                expected_packet = tdm_slot_responses[slave_id]['packet_id']
                if packet_id == expected_packet:
                    tdm_slot_responses[slave_id]['received'] = True
                    print(f"[TDM] Validación OK - Esclavo {slave_id} envió paquete {packet_id}")
                else:
                    print(f"[TDM] AVISO - Esclavo {slave_id} envió paquete {packet_id} (esperado {expected_packet})")
        
        if slave_id not in esclavos_activos:
            esclavos_activos.add(slave_id)
            electrode_name = electrodos_registrados.get(slave_id, {}).get('electrode_name', str(slave_id))
            print(f"[MQTT] Nuevo electrodo detectado: {electrode_name} ({slave_id})")
        
        with file_lock:
            result = abrir_archivo_esclavo(slave_id)
            if result is None:
                return
            file_handle, csv_writer = result
        
        try:
            samples = payload_json.get('samples', []) if payload_json else []
        except Exception as e:
            print(f"[MQTT ERROR] Error parseando payload: {e}")
            return
        
        if slave_id not in packets_received:
            packets_received[slave_id] = 0
            samples_received[slave_id] = 0
        
        packets_received[slave_id] += 1
        samples_received[slave_id] += len(samples)
        ultimo_paquete_tiempo[slave_id] = time.time()
        
        if len(samples) > 0:
            electrode_name = electrodos_registrados.get(slave_id, {}).get('electrode_name', str(slave_id))
            
            if packet_id % 5 == 0 or is_final:
                samples_formatted = []
                for sample in samples:
                    try:
                        if isinstance(sample, list) or isinstance(sample, tuple):
                            timestamp_us, sample_num, adc_value = sample
                        elif isinstance(sample, dict):
                            timestamp_us = sample.get('timestamp_us', 0)
                            sample_num = sample.get('sampleNum', sample.get('sample_num', 0))
                            adc_value = sample.get('adcValue', sample.get('adc_value', 0))
                        else:
                            continue
                        
                        voltage = adc_to_voltage(adc_value)
                        
                        samples_formatted.append({
                            'timestamp_us': timestamp_us,
                            'sample_num': sample_num,
                            'adc_value': adc_value,
                            'voltage': voltage
                        })
                    except:
                        continue
                
                if samples_formatted:
                    send_ws_message('mqtt_data_sample', {
                        'slave_id': slave_id,
                        'electrode_name': electrode_name,
                        'samples': samples_formatted,
                        'packet_id': packet_id
                    })

        # Escribir al CSV con voltaje
        for sample in samples:
            try:
                timestamp_us, sample_num, adc_value = sample
            except:
                if isinstance(sample, dict):
                    timestamp_us = sample.get('timestamp_us', 0)
                    sample_num = sample.get('sampleNum', sample.get('sample_num', 0))
                    adc_value = sample.get('adcValue', sample.get('adc_value', 0))
                else:
                    continue
            
            voltage = adc_to_voltage(adc_value)
            csv_writer.writerow([timestamp_us, sample_num, adc_value, voltage])
        
        file_handle.flush()
        
        if packets_received[slave_id] % 20 == 0:
            electrode_name = electrodos_registrados.get(slave_id, {}).get('electrode_name', str(slave_id))
            print(f"[MQTT] Electrodo {electrode_name} ({slave_id}) | Paquetes: {packets_received[slave_id]} | Muestras: {samples_received[slave_id]}")
            
            send_ws_message('data_progress', {
                'slave_id': slave_id,
                'electrode_name': electrode_name,
                'packets': packets_received[slave_id],
                'samples': samples_received[slave_id]
            })
        
        if is_final:
            esclavos_finalizados.add(slave_id)
            electrode_name = electrodos_registrados.get(slave_id, {}).get('electrode_name', str(slave_id))
            print("\n" + "="*60)
            print(f"[MQTT] ELECTRODO {electrode_name} ({slave_id}) FINALIZADO")
            print(f"  Total muestras: {samples_received[slave_id]}")
            print(f"  Total paquetes: {packets_received[slave_id]}")
            print("="*60 + "\n")
            
            send_ws_message('slave_finished', {
                'slave_id': slave_id,
                'electrode_name': electrode_name,
                'total_packets': packets_received[slave_id],
                'total_samples': samples_received[slave_id]
            })
            
            if finalizacion_enviada and esclavos_finalizados == esclavos_activos:
                print("\n" + "="*60)
                print("  TODOS LOS ELECTRODOS HAN FINALIZADO")
                print("="*60 + "\n")
                send_ws_status()
        return

def on_disconnect(client, userdata, rc):
    if rc != 0:
        print(f"[MQTT] Desconectado inesperadamente (rc={rc})")

def setup_mqtt():
    global mqtt_client
    
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.on_disconnect = on_disconnect
    
    try:
        print(f"\n[MQTT] Conectando a {MQTT_BROKER}:{MQTT_PORT}...")
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
        time.sleep(2)
        return True
    except Exception as e:
        print(f"[MQTT ERROR] No se pudo conectar: {e}")
        return False

# ==================== WEBSOCKET ====================

def on_ws_message(ws, message):
    global command_queue, current_user, current_user_dir
    
    try:
        data = json.loads(message)
        print(f"[WS] Mensaje recibido: {data.get('type', 'unknown')}")
        
        if data.get('type') == 'user_authentication':
            user_data = data.get('user_data')
            if user_data:
                handle_user_authentication(user_data)
                send_ws_message('user_auth_response', {
                    'success': True,
                    'username': current_user.get('nombreCompleto') if current_user else None
                })
            return
        
        if data.get('type') == 'request_users_list':
            if user_manager:
                users = user_manager.list_all_users()
                send_ws_message('users_list', {'users': users})
            return
        
        if data.get('type') == 'raspberry_command':
            command = data.get('command')
            params = data.get('params', {})
            
            with command_lock:
                command_queue.append((command, params))
            
            print(f"[WS] Comando agregado a cola: {command}")
        
        if data.get('type') == 'request_graph_data':
            slave_id = data.get('slave_id')
            if slave_id is not None:
                Thread(target=enviar_datos_grafica, args=(slave_id,), daemon=True).start()
    
    except Exception as e:
        print(f"[WS ERROR] Error procesando mensaje: {e}")

def on_ws_error(ws, error):
    print(f"[WS ERROR] {error}")

def on_ws_close(ws, close_status_code, close_msg):
    print("[WS] Conexión cerrada")

def on_ws_open(ws):
    print("[WS] Conexión establecida con servidor")
    send_ws_status()
    
    if user_manager:
        users = user_manager.list_all_users()
        send_ws_message('users_list', {'users': users})

def send_ws_message(message_type, data):
    global ws_client
    
    if ws_client and ws_client.sock and ws_client.sock.connected:
        try:
            message = {
                'type': message_type,
                'timestamp': time.time()
            }
            message.update(data)
            
            ws_client.send(json.dumps(message))
        except Exception as e:
            print(f"[WS ERROR] Error enviando mensaje: {e}")

def send_ws_status():
    """Envía estado actual al servidor"""
    status = {
        'connected': True,
        'acquiring': adquisicion_activa,
        'tdm_active': tdm_active,
        'slaves_active': list(esclavos_activos),
        'slaves_finished': list(esclavos_finalizados),
        'session_folder': session_folder,
        'electrodos_registrados': electrodos_registrados,
        'discovery_count': discovery_count,
        'timestamp': time.time(),
        'serial_port': SERIAL_PORT,
        'serial_connected': serial_connection is not None and serial_connection.is_open if serial_connection else False,
        'current_user': {
            'nombre': current_user.get('nombreCompleto') if current_user else None,
            'username': user_manager.sanitize_username(current_user.get('nombreCompleto', '')) if (current_user and user_manager) else None,
            'session_num': current_session_num
        } if current_user else None
    }
    
    send_ws_message('raspberry_status', {'status': status})

def send_ws_command_response(command, success, message=""):
    send_ws_message('command_response', {
        'command': command,
        'success': success,
        'message': message
    })

def handle_user_authentication(user_data):
    """Maneja la autenticación de un usuario"""
    global current_user, current_user_dir, user_manager
    
    if user_manager is None:
        print("[USER ERROR] User Manager no disponible")
        return {'success': False, 'error': 'User Manager no disponible'}
    
    print("\n" + "="*60)
    print("[USER] Autenticando usuario")
    print("="*60)
    
    exists = user_manager.user_exists(user_data)
    
    if exists:
        print(f"[USER] Usuario existente: {user_data.get('nombreCompleto')}")
        user_manager.update_last_session_log(user_data)
        result = {
            'success': True,
            'existed': True,
            'user_dir': str(user_manager.get_user_directory(user_data))
        }
    else:
        print("[USER] Nuevo usuario, creando estructura...")
        result = user_manager.create_user_structure(user_data)
    
    if result['success']:
        current_user = user_data
        current_user_dir = result['user_dir']
        
        print("[USER] ✓ Usuario autenticado")
        print(f"  Nombre: {user_data.get('nombreCompleto')}")
        print(f"  Directorio: {current_user_dir}")
        print(f"  Estado: {'Existente' if exists else 'Nuevo'}")
        print("="*60 + "\n")
    else:
        print(f"[USER ERROR] Error en autenticación: {result.get('error')}")
        print("="*60 + "\n")
    
    send_ws_status()
    return result

def setup_websocket():
    global ws_client
    
    print(f"\n[WS] Conectando a {WEBSOCKET_SERVER}...")
    
    def run_websocket():
        global ws_client
        while True:
            try:
                ws_client = websocket.WebSocketApp(
                    WEBSOCKET_SERVER,
                    on_open=on_ws_open,
                    on_message=on_ws_message,
                    on_error=on_ws_error,
                    on_close=on_ws_close
                )
                ws_client.run_forever()
            except Exception as e:
                print(f"[WS ERROR] Error en conexión: {e}")
            
            print("[WS] Reconectando en 5 segundos...")
            time.sleep(5)
    
    ws_thread = Thread(target=run_websocket)
    ws_thread.daemon = True
    ws_thread.start()
    
    time.sleep(2)
    return True

# ==================== PROCESAMIENTO DE COMANDOS ====================

def esperar_finalizacion_completa():
    """Espera a que todos los esclavos terminen de enviar datos"""
    global finalizacion_enviada
    
    if not esclavos_activos:
        print("[INFO] No hay electrodos activos para esperar")
        return
    
    print("\n" + "="*60)
    print("  ESPERANDO RECEPCIÓN COMPLETA DE DATOS")
    print("="*60)
    print(f"  Electrodos activos: {sorted(list(esclavos_activos))}")
    print(f"  Electrodos finalizados: {sorted(list(esclavos_finalizados))}")
    print(f"  Timeout: {TIMEOUT_FINALIZACION} segundos")
    print("="*60 + "\n")
    
    tiempo_inicio = time.time()
    ultimo_reporte = tiempo_inicio
    
    while time.time() - tiempo_inicio < TIMEOUT_FINALIZACION:
        if esclavos_finalizados == esclavos_activos:
            print("\n[OK] Todos los electrodos completaron el envío!")
            break
        
        if time.time() - ultimo_reporte >= 5.0:
            pendientes = esclavos_activos - esclavos_finalizados
            print(f"[INFO] Esperando electrodos: {sorted(list(pendientes))} - Tiempo: {time.time() - tiempo_inicio:.1f}s")
            ultimo_reporte = time.time()
        
        time.sleep(0.5)
    
    if esclavos_finalizados != esclavos_activos:
        pendientes = esclavos_activos - esclavos_finalizados
        print(f"\n[WARN] Timeout alcanzado. Electrodos sin finalizar: {sorted(list(pendientes))}")

def reset_estado_adquisicion():
    """Resetea variables de estado de adquisición"""
    global adquisicion_activa, finalizacion_enviada, session_folder
    
    cerrar_archivos()
    packets_received.clear()
    samples_received.clear()
    esclavos_finalizados.clear()
    esclavos_activos.clear()
    ultimo_paquete_tiempo.clear()
    adquisicion_activa = False
    finalizacion_enviada = False
    session_folder = None

def procesar_comando(command, params):
    """Procesa un comando recibido via WebSocket"""
    global adquisicion_activa, finalizacion_enviada, discovery_count, electrodos_registrados
    global current_user
    
    try:
        if command == 'ENCONTRAR':
            if adquisicion_activa:
                send_ws_command_response(command, False, "Hay adquisición activa")
                return
            
            if current_user is None:
                send_ws_command_response(command, False, "No hay usuario autenticado")
                return
            
            discovery_count += 1
            print("\n" + "="*60)
            print(f">>> DISCOVERY #{discovery_count} INICIADO <<<")
            print(f"Usuario: {current_user.get('nombreCompleto')}")
            print(f"Electrodos conocidos: {len(electrodos_registrados)}")
            print("="*60 + "\n")
            
            for slave_id in electrodos_registrados:
                electrodos_registrados[slave_id]['seen_in_current_discovery'] = False
            
            enviar_pulso('ENCONTRAR')
            
            def verificar_discovery_timeout():
                time.sleep(6)
                
                print("\n" + "="*60)
                print(f">>> VERIFICANDO ELECTRODOS DESPUÉS DE DISCOVERY #{discovery_count} <<<")
                print("="*60)
                
                electrodos_desconectados = []
                electrodos_activos_disc = []
                
                for slave_id, info in list(electrodos_registrados.items()):
                    if not info.get('seen_in_current_discovery', False):
                        electrode_name = info.get('electrode_name', str(slave_id))
                        print(f"[WARN] Electrodo {electrode_name} (ID:{slave_id}) NO RESPONDIÓ")
                        electrodos_desconectados.append(slave_id)
                        
                        send_ws_message('slave_disconnected', {
                            'slave_id': slave_id,
                            'electrode_name': electrode_name,
                            'discovery_count': discovery_count,
                            'reason': 'no_response_in_discovery'
                        })
                    else:
                        electrode_name = info.get('electrode_name', str(slave_id))
                        print(f"[OK] Electrodo {electrode_name} (ID:{slave_id}) ACTIVO")
                        electrodos_activos_disc.append(slave_id)
                
                print("="*60)
                print(f"Resumen Discovery #{discovery_count}:")
                print(f"  Activos: {len(electrodos_activos_disc)} - {electrodos_activos_disc}")
                print(f"  Desconectados: {len(electrodos_desconectados)} - {electrodos_desconectados}")
                print("="*60 + "\n")
                
                send_ws_status()
            
            timeout_thread = Thread(target=verificar_discovery_timeout)
            timeout_thread.daemon = True
            timeout_thread.start()
            
            send_ws_command_response(command, True, 
                f"Comando ENCONTRAR enviado (Discovery #{discovery_count})")
            send_ws_status()
        
        elif command == 'INICIAR':
            if adquisicion_activa:
                send_ws_command_response(command, False, "Ya hay adquisición activa")
                return
            
            if current_user is None:
                send_ws_command_response(command, False, "No hay usuario autenticado")
                return
            
            if len(electrodos_registrados) == 0:
                print("[ERROR] No hay electrodos registrados")
                print("[INFO] Ejecute comando ENCONTRAR primero")
                send_ws_command_response(command, False, 
                    "No hay electrodos registrados. Ejecute ENCONTRAR primero")
                return
            
            print(f"\n[INFO] Iniciando adquisición con {len(electrodos_registrados)} electrodos:")
            for slave_id, info in electrodos_registrados.items():
                name = info.get('electrode_name', 'Unknown')
                pos = info.get('electrode_position', 'Unknown')
                print(f"  - Slave {slave_id}: {name} ({pos})")
            
            reset_estado_adquisicion()
            crear_sesion()
            
            enviar_pulso('INICIAR')
            adquisicion_activa = True
            finalizacion_enviada = False
            
            print("\n[INFO] Adquisición iniciada")
            print(f"  Usuario: {current_user.get('nombreCompleto')}")
            print(f"  Sesión: {current_session_num}")
            print(f"  Electrodos: {len(electrodos_registrados)}")
            print(f"  Conversión ADC->V: 12 bits -> {VREF} V")
            print("  Modo TDM: Habilitado")
            
            if start_tdm_controller():
                print("[INFO] Controlador TDM iniciado correctamente")
            else:
                print("[WARN] No se pudo iniciar TDM controller")
            
            if ecg_realtime_calculator:
                try:
                    print("\n[ECG_RT] Iniciando procesamiento en tiempo real...")
                    print(f"[ECG_RT] Electrodos configurados: {ecg_realtime_calculator.position_to_slave}")
                    
                    if ecg_realtime_calculator.start():
                        print("[ECG_RT] ✓ Procesamiento en tiempo real ACTIVO")
                        print("[ECG_RT] Los archivos de derivaciones se crearán en:")
                        print(f"[ECG_RT]   {ecg_realtime_calculator.derivations_csv_folder}")
                    else:
                        print("[ECG_RT] ⚠️  Calculador ya estaba ejecutándose")
                
                except Exception as e:
                    print(f"[ECG_RT ERROR] Error iniciando calculador: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print("[ECG_RT] ⚠️  NO HAY CALCULADOR INICIALIZADO")
                print("[ECG_RT] Esto puede deberse a:")
                print("[ECG_RT]   1. No se ejecutó ENCONTRAR antes de INICIAR")
                print("[ECG_RT]   2. Los electrodos no tienen posiciones válidas")
                print("[ECG_RT]   3. Error en la creación del calculador")
            
            send_ws_command_response(command, True, 
                f"Adquisición iniciada - Sesión: {current_session_num} (TDM + ECG RT {'ACTIVO' if ecg_realtime_calculator else 'INACTIVO'})")
            send_ws_status()
        
        elif command == 'FINALIZAR':
            if not adquisicion_activa:
                send_ws_command_response(command, False, "No hay adquisición activa")
                return
            
            print("\n" + "="*60)
            print(">>> INICIANDO SECUENCIA DE FINALIZACIÓN <<<")
            print("="*60)
            
            # PASO 1: Detener TDM
            print("\n[PASO 1] Deteniendo controlador TDM...")
            stop_tdm_controller()
            
            # PASO 2: Enviar comando FINALIZAR
            print("\n[PASO 2] Enviando comando FINALIZAR...")
            enviar_pulso('FINALIZAR')
            finalizacion_enviada = True
            
            # PASO 3: Esperar buffers
            print("\n[PASO 3] Esperando recepción completa...")
            esperar_finalizacion_completa()
            
            # PASO 4: Esperar escritura
            print("\n[PASO 4] Esperando escritura de últimos datos (2s)...")
            time.sleep(2)
            
            # PASO 5: Detener calculador ECG (SIN cerrar archivos aún)
            print("\n[PASO 5] Deteniendo calculador ECG...")
            if ecg_realtime_calculator:
                try:
                    ecg_realtime_calculator.stop()
                    
                    if hasattr(ecg_realtime_calculator, 'shutdown_complete'):
                        shutdown_ok = ecg_realtime_calculator.shutdown_complete.wait(timeout=30)
                        
                        if shutdown_ok:
                            print("[PASO 5] ✓ Calculador finalizó correctamente")
                        
                        # Estadísticas finales
                        stats = ecg_realtime_calculator.get_stats()
                        if 'error' not in stats:
                            total_samples = sum(
                                ds.get('total_calculated', 0) 
                                for ds in stats.get('derivation_stats', {}).values()
                            )
                            print(f"[PASO 5] Total muestras procesadas: {total_samples}")
                
                except Exception as e:
                    print(f"[PASO 5] ERROR: {e}")
            
            # PASO 6: AHORA SÍ cerrar archivos
            print("\n[PASO 6] Cerrando archivos de forma segura...")
            cerrar_archivos_seguros()  # <-- USA LA NUEVA FUNCIÓN
            
            adquisicion_activa = False
            
            print("\n" + "="*60)
            print(">>> FINALIZACIÓN COMPLETA <<<")
            print("="*60)
            
            send_ws_command_response(command, True, "Adquisición finalizada correctamente")
            send_ws_status()
        
        elif command == 'RESET':
            if adquisicion_activa:
                stop_tdm_controller()
                
                if ecg_realtime_calculator:
                    try:
                        ecg_realtime_calculator.stop()
                    except:
                        pass
                
                enviar_pulso('FINALIZAR')
                finalizacion_enviada = True
                time.sleep(1)
                esperar_finalizacion_completa()
                cerrar_archivos()
            
            enviar_reset()
            reset_estado_adquisicion()
            
            print(f"[RESET] Limpiando {len(electrodos_registrados)} electrodos registrados")
            electrodos_registrados.clear()
            
            discovery_count = 0
            
            send_ws_command_response(command, True, "Sistema reiniciado")
            send_ws_status()
        
        elif command == 'STATUS':
            print("\n[STATUS] Estado del sistema")
            print(f"  Usuario actual: {current_user.get('nombreCompleto') if current_user else 'Ninguno'}")
            print(f"  Sesión actual: {current_session_num or 'Ninguna'}")
            print(f"  Electrodos registrados: {len(electrodos_registrados)}")
            
            if electrodos_registrados:
                print("  Posiciones de electrodos:")
                for slave_id, info in electrodos_registrados.items():
                    name = info.get('electrode_name', 'Unknown')
                    pos = info.get('electrode_position', 'Unknown')
                    print(f"    [{slave_id}] {name} - Posición: {pos}")
            
            print(f"  Puerto Serial: {SERIAL_PORT} ({'Conectado' if serial_connection and serial_connection.is_open else 'Desconectado'})")
            print(f"  Conversión ADC: 12 bits -> {VREF} V (factor: {ADC_TO_VOLTAGE_FACTOR:.8f})")
            print(f"  TDM Activo: {tdm_active}")
            print(f"  Adquisición Activa: {adquisicion_activa}")
            
            if ecg_realtime_calculator:
                try:
                    stats = ecg_realtime_calculator.get_stats()
                    
                    if 'error' not in stats:
                        print(f"  ECG Calculator:")
                        print(f"    - Activo: {stats.get('running', False)}")
                        print(f"    - Electrodos: {stats.get('available_electrodes', [])}")
                        print(f"    - Derivaciones válidas: {stats.get('valid_derivations', [])}")
                        
                        deriv_stats = stats.get('derivation_stats', {})
                        if deriv_stats:
                            print(f"    - Estadísticas por derivación:")
                            for deriv, ds in sorted(deriv_stats.items()):
                                print(f"      {deriv}: {ds.get('total_calculated', 0)} muestras (último: {ds.get('last_processed', -1)})")
                        
                        threads = stats.get('active_threads', {})
                        print(f"    - Hilos activos: {threads.get('readers', 0)} lectores, {threads.get('processors', 0)} procesadores")
                        
                        if 'derivations_folder' in stats:
                            print(f"    - Carpeta: {stats['derivations_folder']}")
                    else:
                        print(f"  ECG Calculator: Error - {stats.get('error', 'Unknown')}")
                
                except Exception as e:
                    print(f"  ECG Calculator: Error obteniendo stats - {e}")
            else:
                print("  ECG Calculator: No inicializado")
                if len(electrodos_registrados) == 0:
                    print("    Razón: No hay electrodos registrados (ejecute ENCONTRAR)")
                else:
                    print("    Razón: No se ha iniciado sesión (ejecute INICIAR)")
            
            send_ws_status()
            send_ws_command_response(command, True, "Estado enviado")
        
        elif command == 'GET_GRAPH_DATA':
            slave_id = params.get('slave_id')
            if slave_id is not None:
                Thread(target=enviar_datos_grafica, args=(slave_id,), daemon=True).start()
                send_ws_command_response(command, True, "Enviando datos de gráfica")
            else:
                send_ws_command_response(command, False, "slave_id no especificado")
        
        else:
            send_ws_command_response(command, False, "Comando no reconocido")
    
    except Exception as e:
        print(f"[ERROR] Error procesando comando {command}: {e}")
        import traceback
        traceback.print_exc()
        send_ws_command_response(command, False, str(e))

def command_processor_loop():
    """Loop que procesa comandos de la cola"""
    global command_queue
    
    print("\n[THONNY] ✓ Loop de comandos iniciado")
    print("[THONNY] Esperando comandos via WebSocket...")
    
    while True:
        try:
            with command_lock:
                if command_queue:
                    command, params = command_queue.pop(0)
                    procesar_comando(command, params)
            
            time.sleep(0.1)
        
        except Exception as e:
            print(f"[ERROR] Error en loop de comandos: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(1)

# ==================== CLEANUP ====================

def cleanup():
    """Limpieza final"""
    global cleanup_ejecutado, serial_connection, ecg_realtime_calculator
    
    if cleanup_ejecutado:
        return
    
    cleanup_ejecutado = True
    
    try:
        print("\n[CLEANUP] Limpiando sistema...")

        stop_ip_sender()
        
        if tdm_active:
            stop_tdm_controller()
        
        if ecg_realtime_calculator:
            try:
                print("[CLEANUP] Deteniendo calculador ECG...")
                ecg_realtime_calculator.stop()
            except Exception as e:
                print(f"[CLEANUP] Error deteniendo ECG: {e}")
        
        cerrar_archivos()
        
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        
        if ws_client:
            ws_client.close()
        
        if serial_connection and serial_connection.is_open:
            try:
                serial_connection.close()
                print("[SERIAL] Puerto serial cerrado")
            except:
                pass

        enviar_reset()
        reset_estado_adquisicion()

        print("[OK] Cleanup completado. ¡Adiós!")
    
    except Exception as e:
        print(f"[ERROR] Error en cleanup: {e}")

# Registrar cleanup al salir
atexit.register(cleanup)

# ==================== MAIN ====================

if __name__ == "__main__":
    try:
        print("\n" + "="*60)
        print("  WINDOWS PC - CONTROL ECG (THONNY)")
        print("  VERSION: TDM (Time Division Multiplexing)")
        print("  SERIAL + MQTT + WEBSOCKET + USUARIOS + ADC->V + TDM")
        print("="*60)
        print(f"[{get_timestamp()}] Iniciando...")
        print(f"[THONNY] Directorio de trabajo: {SCRIPT_DIR}")
        print("="*60)
        
        print("\n[RED] Configuración de red:")
        print(f"  IP del servidor detectada: {SERVER_IP}")
        print(f"  MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
        print(f"  WebSocket Server: {WEBSOCKET_SERVER}")
        print("="*60)
        
        print("\n[WEBSOCKET] Nota: El servidor WebSocket debe iniciarse manualmente")
        print("[WEBSOCKET] Asegúrese de que el servidor esté ejecutándose antes de continuar")
        print("="*60)
        
        print("\n[ADC] Configuración de conversión:")
        print(f"  Resolución: 12 bits (0-{ADC_MAX_VALUE})")
        print(f"  Vref: {VREF} V")
        print(f"  Factor de conversión: {ADC_TO_VOLTAGE_FACTOR:.8f} V/bit")
        print("="*60)
        
        print("\n[TDM] Configuración de multiplexeo:")
        print(f"  Ciclo completo: {TDM_CYCLE_DURATION_MS} ms")
        print(f"  Duración por slot: {TDM_SLOT_DURATION_MS} ms")
        print(f"  Máximo de esclavos: {MAX_SLAVES}")
        print(f"  Timeout por slot: {TDM_SLOT_TIMEOUT_MS} ms")
        print("="*60)
        
        if init_user_manager:
            print("\n[USER_MGR] Inicializando gestor de usuarios...")
            user_manager = init_user_manager()
            print("[USER_MGR] Gestor de usuarios listo")
            users = user_manager.list_all_users()
            print(f"[USER_MGR] Usuarios registrados: {len(users)}")
        else:
            print("\n[USER_MGR] ⚠️  Gestor de usuarios no disponible")
        
        print("\n[ECG_CALC] Calculador de derivaciones disponible" if ECGRealtimeDerivationsCalculator else "[ECG_CALC] ⚠️  Calculador no disponible")
        
        if not setup_serial():
            print("\n[ERROR CRÍTICO] No se pudo inicializar puerto serial")
            print("[INFO] El sistema continuará pero los comandos no se enviarán al ESP32")
            print("[INFO] Verifique que el ESP32 esté conectado")
            response = input("\n¿Desea continuar de todas formas? (s/n): ")
            if response.lower() != 's':
                exit(1)
        
        if not setup_mqtt():
            print("[ERROR] No se pudo iniciar MQTT, continuando sin él...")
        
        if not setup_websocket():
            print("[ERROR] No se pudo conectar al servidor WebSocket")
        send_ws_status()
        print("\n[OK] Sistema estable\n")
        print("[INFO] Características TDM:")
        print("  - Multiplexeo TDM con control maestro")
        print("  - Ciclo de 1000 ms distribuido en slots de 100 ms")
        print("  - Maestro solicita paquetes a esclavos")
        print("  - Validación de paquetes esperados por slot")
        print("  - Logs detallados de ciclos, slots y timeouts")
        print("  - Conversión automática ADC (12 bits) a Voltaje (3.3V)")
        print("  - CSV con columnas: timestamp_us, sample_num, adc_value, voltage")
        print("  - Comunicación Serial con ESP32")
        print(f"\n[INFO] Puerto Serial: {SERIAL_PORT if SERIAL_PORT else 'NO DETECTADO'}")
        print("\n[INFO] Esperando autenticación de usuario...\n")
        print("\n[THONNY] Sistema listo para recibir comandos\n")

        command_processor_loop()

    except KeyboardInterrupt:
        print("\n[OK] Interrupción detectada (Ctrl+C)")

    finally:
        if not cleanup_ejecutado:
            cleanup()