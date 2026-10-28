"""
velodyne_manager_karen.py  v10  (2026-03-06)
=============================================
Driver Velodyne HDL-64E con fórmula de cálculo XYZ exacta del manual Velodyne.
Calibración cargada desde dbkaren.xml (mismo archivo usado por VeloView).

FÓRMULA DE VELODYNE (del manual HDL-64E S2 Appendix F):
    cosRotAngle = cos(azimuth) * cos(rotCorrection) + sin(azimuth) * sin(rotCorrection)
    sinRotAngle = sin(azimuth) * cos(rotCorrection) - cos(azimuth) * sin(rotCorrection)
    xyDistance = distance * cos(vertCorrection)
    X = xyDistance * sinRotAngle - horizOffsetCorrection * cosRotAngle
    Y = xyDistance * cosRotAngle + horizOffsetCorrection * sinRotAngle
    Z = distance * sin(vertCorrection) + vertOffsetCorrection

CAMBIOS v10:
  - Fórmula XYZ exacta del manual Velodyne (idéntica a VeloView/DSR)
  - rotCorrection aplicado al azimut (no solo al ángulo vertical)
  - horizOffsetCorrection aplicado correctamente
  - Valores del XML en grados (convertidos a radianes internamente)
  - horizOffsetCorrection en mm (convertido a metros)

CAMBIOS v11 (parche puntos faltantes vs VeloView):
  - puntos_buffer: maxlen subido de 100_000 a 400_000 (el HDL-64E a 5-10Hz
    genera 130k-260k+ puntos por vuelta en single-return, y el deque viejo
    recortaba silenciosamente los puntos más antiguos de cada vuelta)
  - Contador nuevo stats['puntos_descartados_buffer'] que avisa cuando un
    frame excede el maxlen, para poder detectarlo en vivo
  - Contador nuevo stats['paquetes_cola_llena'] ya existía como drops_queue,
    se expone también en obtener_estadisticas() con nombre más claro
"""

VERSION = "v11-2026-03-06-patch"

import socket, struct, time, os, pathlib
import numpy as np
from threading import Thread, Lock
from collections import deque
from datetime import datetime
from queue import Queue, Full
import xml.etree.ElementTree as ET
import math


# ============================================================
# CALIBRACIÓN DESDE XML
# ============================================================

def load_calibration_from_xml(xml_path: str) -> dict:
    """
    Carga la calibración HDL-64E desde el archivo XML de Velodyne.
    
    El XML (db.xml o dbkaren.xml) contiene:
      - rotCorrection_: corrección rotacional en GRADOS
      - vertCorrection_: ángulo vertical en GRADOS  
      - distCorrection_: corrección de distancia (cm)
      - vertOffsetCorrection_: offset vertical (cm)
      - horizOffsetCorrection_: offset horizontal en MM (±4mm típico)
    
    Returns:
        dict con arrays numpy para cada láser (64 total):
            - rot_deg: rotCorrection en grados [64]
            - vert_deg: vertCorrection en grados [64]
            - rot_rad: rotCorrection en radianes [64]
            - vert_rad: vertCorrection en radianes [64]
            - cos_rot: cos(rotCorrection) [64]
            - sin_rot: sin(rotCorrection) [64]
            - cos_vert: cos(vertCorrection) [64]
            - sin_vert: sin(vertCorrection) [64]
            - dist_corr_m: distCorrection en metros [64]
            - vert_off_m: vertOffsetCorrection en metros [64]
            - horiz_off_m: horizOffsetCorrection en metros [64]
            - dist_lsb: resolución de distancia (0.002 m = 2mm)
    """
    print(f"📂 Cargando calibración desde: {xml_path}")
    
    if not os.path.exists(xml_path):
        raise FileNotFoundError(f"Archivo de calibración no encontrado: {xml_path}")
    
    # Leer y limpiar el XML (puede tener bytes nulos al final)
    with open(xml_path, 'rb') as f:
        content = f.read()
    content = content.rstrip(b'\x00').rstrip()
    xml_content = content.decode('utf-8')
    
    root = ET.fromstring(xml_content)
    db = root.find('.//DB')
    
    if db is None:
        raise ValueError("No se encontró el elemento DB en el XML")
    
    # Obtener distLSB (resolución de distancia)
    dist_lsb_elem = db.find('distLSB_')
    dist_lsb = float(dist_lsb_elem.text) / 100.0 if dist_lsb_elem is not None else 0.002  # cm a m
    
    # Extraer puntos de calibración
    points_elem = db.find('points_')
    if points_elem is None:
        raise ValueError("No se encontró el elemento points_ en el XML")
    
    # Arrays para 64 láseres
    rot_deg = np.zeros(64, dtype=np.float64)
    vert_deg = np.zeros(64, dtype=np.float64)
    dist_corr = np.zeros(64, dtype=np.float64)
    vert_off = np.zeros(64, dtype=np.float64)
    horiz_off = np.zeros(64, dtype=np.float64)
    
    count = 0
    for item in points_elem.findall('item'):
        px = item.find('px')
        if px is not None:
            laser_id = int(px.find('id_').text)
            if 0 <= laser_id < 64:
                rot_deg[laser_id] = float(px.find('rotCorrection_').text)
                vert_deg[laser_id] = float(px.find('vertCorrection_').text)
                dist_corr[laser_id] = float(px.find('distCorrection_').text)
                vert_off[laser_id] = float(px.find('vertOffsetCorrection_').text)
                horiz_off[laser_id] = float(px.find('horizOffsetCorrection_').text)
                count += 1
    
    if count != 64:
        print(f"⚠️ Se esperaban 64 láseres, se encontraron {count}")
    
    # Convertir a radianes
    rot_rad = np.deg2rad(rot_deg)
    vert_rad = np.deg2rad(vert_deg)
    
    # Pre-calcular senos y cosenos
    cos_rot = np.cos(rot_rad)
    sin_rot = np.sin(rot_rad)
    cos_vert = np.cos(vert_rad)
    sin_vert = np.sin(vert_rad)
    
    # Convertir unidades:
    # - distCorrection: cm → m (dividir por 100)
    # - vertOffsetCorrection: cm → m (dividir por 100)  
    # - horizOffsetCorrection: mm → m (dividir por 1000)
    dist_corr_m = dist_corr / 100.0
    vert_off_m = vert_off / 100.0
    horiz_off_m = horiz_off / 1000.0
    
    calib = {
        'rot_deg': rot_deg,
        'vert_deg': vert_deg,
        'rot_rad': rot_rad,
        'vert_rad': vert_rad,
        'cos_rot': cos_rot,
        'sin_rot': sin_rot,
        'cos_vert': cos_vert,
        'sin_vert': sin_vert,
        'dist_corr_m': dist_corr_m,
        'vert_off_m': vert_off_m,
        'horiz_off_m': horiz_off_m,
        'dist_lsb': dist_lsb,
    }
    
    print(f"   ✓ Calibración cargada: {count} láseres")
    print(f"   ✓ Rango vertical: {vert_deg.min():.2f}° a {vert_deg.max():.2f}°")
    print(f"   ✓ Rango rotacional: {rot_deg.min():.2f}° a {rot_deg.max():.2f}°")
    print(f"   ✓ Offset horizontal: {horiz_off.min():.1f}mm a {horiz_off.max():.1f}mm")
    print(f"   ✓ Resolución distancia: {dist_lsb*100:.2f} cm")
    
    return calib


def _build_calibration_fallback() -> dict:
    """
    Calibración de respaldo si no se puede cargar el XML.
    Usa ángulos verticales aproximados del HDL-64E.
    """
    print("⚠️ Usando calibración de respaldo (sin XML)")
    
    # Ángulos verticales aproximados HDL-64E
    # Banco superior (0-31): ~-8.33° a +2°
    # Banco inferior (32-63): ~-24.33° a -8.53°
    vert_deg = np.zeros(64, dtype=np.float64)
    
    # Upper block: lasers 0-31 
    for i in range(32):
        vert_deg[i] = -8.33 + (i / 31.0) * 10.33  # -8.33 to +2
    
    # Lower block: lasers 32-63
    for i in range(32, 64):
        vert_deg[i] = -24.33 + ((i-32) / 31.0) * 15.8  # -24.33 to -8.53
    
    rot_deg = np.zeros(64, dtype=np.float64)
    vert_rad = np.deg2rad(vert_deg)
    rot_rad = np.deg2rad(rot_deg)
    
    return {
        'rot_deg': rot_deg,
        'vert_deg': vert_deg,
        'rot_rad': rot_rad,
        'vert_rad': vert_rad,
        'cos_rot': np.cos(rot_rad),
        'sin_rot': np.sin(rot_rad),
        'cos_vert': np.cos(vert_rad),
        'sin_vert': np.sin(vert_rad),
        'dist_corr_m': np.zeros(64),
        'vert_off_m': np.zeros(64),
        'horiz_off_m': np.zeros(64),
        'dist_lsb': 0.002,
    }


# ============================================================
# CALIBRACIÓN VLP-16 — ángulos nominales
# ============================================================
_VLP16_VERT_DEG = np.array(
    [-15, 1, -13, 3, -11, 5, -9, 7, -7, 9, -5, 11, -3, 13, -1, 15],
    dtype=np.float64
)
_VLP16_VERT_RAD = np.deg2rad(_VLP16_VERT_DEG)
_VLP16_COS_V    = np.cos(_VLP16_VERT_RAD)
_VLP16_SIN_V    = np.sin(_VLP16_VERT_RAD)
_VLP16_RES      = 0.002
_VLP16_DIST_MIN = 0.5
_VLP16_DIST_MAX = 100.0


# ============================================================
# PRODUCT IDs Velodyne
# ============================================================
_PRODUCT_ID_MAP = {
    0x21: 'HDL-32E',
    0x22: 'VLP-16',
    0x24: 'VLP-32C',
    0x28: 'VLP-16-HiRes',
    0x31: 'VLP-32A',
    0x32: 'VLP-32B',
    0x37: 'HDL-64E',
    0x38: 'HDL-64E-S2',
    0x39: 'HDL-64E-S2.1',
    0x40: 'HDL-64E-S3',
    0x43: 'VLS-128',
    0x46: 'VLP-16-Puck-Lite',
    0x62: 'VLP-16-Puck',
}
_VLP16_IDS = {0x22, 0x28, 0x46, 0x62}
_HDL64_IDS = {0x37, 0x38, 0x39, 0x40}


# ============================================================
# ESCRITOR PCAP
# ============================================================
class PcapWriter:
    MAGIC      = 0xA1B2C3D4
    VMAJ, VMIN = 2, 4
    SNAPLEN    = 65535
    NETWORK    = 1

    def __init__(self, ruta, src_ip='192.168.1.200', dst_ip='255.255.255.255',
                 t_ini=None, t_fin=None):
        self._f       = open(ruta, 'wb')
        self._src_ip  = src_ip
        self._dst_ip  = dst_ip
        self._cerrado = False
        self._t_ini   = t_ini
        self._t_fin   = t_fin
        self._escritos    = 0
        self._descartados = 0
        self._f.write(struct.pack('<IHHiIII',
            self.MAGIC, self.VMAJ, self.VMIN, 0, 0, self.SNAPLEN, self.NETWORK))
        self._eth = bytes([
            0xff,0xff,0xff,0xff,0xff,0xff,
            0x60,0x76,0x88,0x00,0x00,0x00,
            0x08,0x00])

    def _cksum(self, hdr):
        if len(hdr) % 2: hdr += b'\x00'
        s = sum((hdr[i]<<8)+hdr[i+1] for i in range(0,len(hdr),2))
        while s>>16: s=(s&0xFFFF)+(s>>16)
        return ~s & 0xFFFF

    def _frame(self, pay, sp=2368, dp=2368):
        ulen = 8+len(pay)
        udp  = struct.pack('>HHHH',sp,dp,ulen,0)
        ilen = 20+ulen
        src  = bytes(map(int, self._src_ip.split('.')))
        dst  = bytes(map(int, self._dst_ip.split('.')))
        pre  = (bytes([0x45,0x00])+struct.pack('>H',ilen)+
                bytes([0,0,0x40,0,0xff,0x11,0,0])+src+dst)
        ck   = self._cksum(pre)
        ip   = (bytes([0x45,0x00])+struct.pack('>H',ilen)+
                bytes([0,0,0x40,0,0xff,0x11])+
                struct.pack('>H',ck)+src+dst)
        return self._eth+ip+udp+pay

    def escribir_paquete(self, payload, ts):
        if self._cerrado: return
        if self._t_ini is not None and ts < self._t_ini:
            self._descartados += 1; return
        if self._t_fin is not None and ts > self._t_fin:
            self._descartados += 1; return
        if   len(payload)==1248: payload=payload[42:]
        elif len(payload)!=1206: return
        frm = self._frame(payload)
        s,u = int(ts), int((ts%1)*1_000_000)
        self._f.write(struct.pack('<IIII',s,u,len(frm),len(frm)))
        self._f.write(frm)
        self._escritos += 1

    def cerrar(self):
        if self._f and not self._f.closed:
            self._cerrado=True; self._f.flush(); self._f.close()
            print(f"     📦 PCAP cerrado | escritos={self._escritos:,} "
                  f"descartados={self._descartados:,}")


# ============================================================
# GESTOR VELODYNE HDL-64E
# ============================================================
class VelodyneHDL64E:
    """
    Driver Velodyne HDL-64E con fórmula de cálculo XYZ del manual oficial.
    
    La fórmula es idéntica a la usada por VeloView/DSR:
        cosRotAngle = cos(azimuth)*cos(rotCorrection) + sin(azimuth)*sin(rotCorrection)
        sinRotAngle = sin(azimuth)*cos(rotCorrection) - cos(azimuth)*sin(rotCorrection)
        xyDistance = distance * cos(vertCorrection)
        X = xyDistance * sinRotAngle - horizOffset * cosRotAngle
        Y = xyDistance * cosRotAngle + horizOffset * sinRotAngle
        Z = distance * sin(vertCorrection) + vertOffset
    """
    T1206 = 1206; T1248 = 1248; ETH = 42
    NB = 12; TB = 100
    NL_VLP16 = 16

    _SO_RCVBUF_TARGET = 16 * 1024 * 1024
    _SO_RCVBUFFORCE   = 33

    # Ruta por defecto al archivo de calibración
    DEFAULT_CALIB_PATH = r"C:\MCSS_MAESTRIA\dbkaren.xml"

    # Headers de bloque HDL-64E
    HDR_UPPER = 0xEEFF
    HDR_LOWER = 0xDDFF

    # Tamaño del buffer de puntos (por vuelta). El HDL-64E en single-return
    # genera entre ~130k (10Hz) y ~260k (5Hz) puntos por vuelta; dual-return
    # puede duplicar esa cifra. 100_000 era insuficiente y recortaba puntos
    # silenciosamente. Se deja margen amplio.
    PUNTOS_BUFFER_MAXLEN = 400_000

    def __init__(self, puerto=2368, parent=None, modelo='AUTO', calib_xml_path=None):
        """
        Inicializa el driver Velodyne.
        
        Args:
            puerto: Puerto UDP para recibir datos (default: 2368)
            parent: Objeto padre (para integración con GUI)
            modelo: 'AUTO' para detectar, o 'HDL-64E', 'VLP-16', etc.
            calib_xml_path: Ruta al archivo XML de calibración
        """
        self.puerto  = puerto
        self.parent  = parent
        self.socket  = None
        self.activo  = False

        self.modelo          = modelo
        self._modelo_detectado = (modelo != 'AUTO')
        self._es_vlp16       = False

        self.puntos_buffer = deque(maxlen=self.PUNTOS_BUFFER_MAXLEN)
        self.lock = Lock()

        self.grabando               = False
        self.timestamp_inicio       = None
        self.acquisition_start_time = None
        self.acquisition_end_time   = None
        self._pcap_writer           = None
        self._pcap_ruta             = None
        self.carpeta_grabacion      = None
        self.nombre_participante    = None
        self._toggle_bloqueado      = False

        self.stats = {
            'paquetes_totales'  : 0,
            'paquetes_grabados' : 0,
            'errores'           : 0,
            'drops_queue'       : 0,
            'puntos_descartados_buffer': 0,
            'ultimo_paquete'    : time.time(),
        }

        self._raw_queue = None
        self._pcap_lock = Lock()

        # ── Cargar calibración ──────────────────────────────────────────────
        self._calib_xml_path = calib_xml_path or self.DEFAULT_CALIB_PATH
        try:
            self._calib = load_calibration_from_xml(self._calib_xml_path)
            self._calib_loaded = True
        except Exception as e:
            print(f"⚠️ No se pudo cargar XML ({e})")
            self._calib = _build_calibration_fallback()
            self._calib_loaded = False

        # Estado para timestamps por azimuth
        self._ts_anchor   = 0.0
        self._az_anchor   = 0.0
        self._rot_rate    = 3600.0
        self._rot_t0      = 0.0
        self._rot_az_prev = None

        print(f"🔧 Velodyne {modelo} | Puerto {puerto} | {VERSION}")
        print(f"     Buffer puntos/vuelta → maxlen={self.PUNTOS_BUFFER_MAXLEN:,}")
        if self._modelo_detectado:
            self._print_modelo_info()

    def _detectar_modelo(self, payload: bytes):
        """Lee product_id del byte 1205 y configura el driver."""
        if len(payload) < 1206:
            return
        pid = payload[1205]
        nombre = _PRODUCT_ID_MAP.get(pid, f'UNKNOWN(0x{pid:02X})')
        self.modelo           = nombre
        self._modelo_detectado = True
        self._es_vlp16        = (pid in _VLP16_IDS)
        print(f"🔍 Sensor detectado: {nombre} (product_id=0x{pid:02X})")
        self._print_modelo_info()

    def _print_modelo_info(self):
        if self._es_vlp16:
            print(f"     ↕  Elevación VLP-16: -15° a +15° (16 láseres)")
        else:
            v = self._calib['vert_deg']
            print(f"     ↕  HDL-64E: {v.min():.2f}° a {v.max():.2f}° (64 láseres)")
            if self._calib_loaded:
                print(f"     ✓  Calibración: {self._calib_xml_path}")

    # ── Captura ──────────────────────────────────────────────
    def iniciar_captura(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._configurar_buffer_so()
            self.socket.bind(('', self.puerto))
            self.socket.settimeout(1.0)

            self._raw_queue = Queue(maxsize=80000)
            self.activo = True

            self.hilo_rx   = Thread(target=self._loop_rx,   daemon=True, name="velo-rx")
            self.hilo_proc = Thread(target=self._loop_proc, daemon=True, name="velo-proc")
            self.hilo_mon  = Thread(target=self._monitor,   daemon=True, name="velo-mon")
            self.hilo_rx.start()
            self.hilo_proc.start()
            self.hilo_mon.start()

            print(f"✅ Captura LiDAR iniciada puerto {self.puerto}")
            return True
        except Exception as e:
            print(f"❌ Error captura LiDAR: {e}")
            return False

    def _configurar_buffer_so(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                               self._SO_RCVBUF_TARGET)
        real = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        if real < self._SO_RCVBUF_TARGET:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       self._SO_RCVBUFFORCE,
                                       self._SO_RCVBUF_TARGET)
                real = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                print(f"     Buffer SO forzado  → {real/1024/1024:.1f} MB")
            except (PermissionError, OSError):
                print(f"     Buffer SO limitado → {real/1024:.0f} KB")
        else:
            print(f"     Buffer SO          → {real/1024/1024:.1f} MB ✅")

    # ── Hilo RX ──────────────────────────────────────────────
    def _loop_rx(self):
        _count = 0
        while self.activo:
            try:
                raw, _ = self.socket.recvfrom(2048)
                ts_sys  = time.time()
                _count += 1
                if _count <= 5:
                    print(f"📡 RX paquete #{_count} len={len(raw)} ts={ts_sys:.3f}")

                n = len(raw)
                if   n == self.T1206: payload = raw
                elif n == self.T1248: payload = raw[self.ETH:]
                else:                 payload = None

                if payload is not None:
                    if not self._modelo_detectado and _count <= 3:
                        self._detectar_modelo(payload)

                    ts = self._ts_por_azimuth(payload, ts_sys)

                    with self._pcap_lock:
                        w = self._pcap_writer
                        if w is not None and not w._cerrado and self.grabando:
                            try:
                                w.escribir_paquete(payload, ts)
                                self.stats['paquetes_grabados'] += 1
                            except Exception as e:
                                print(f"PCAP write error: {e}")

                try:
                    self._raw_queue.put_nowait((raw, ts_sys))
                except Full:
                    self.stats['drops_queue'] += 1

            except socket.timeout:
                if _count == 0:
                    print("⏱️  socket timeout — sin datos del LiDAR")
                continue
            except Exception as e:
                print(f"❌ RX error: {e}")
                self.stats['errores'] += 1

    # ── Hilo procesamiento ───────────────────────────────────
    def _loop_proc(self):
        ultimo_az    = None
        frame_actual = []
        _diag        = 0

        while self.activo or not self._raw_queue.empty():
            try:
                raw, ts_sys = self._raw_queue.get(timeout=0.5)
            except Exception:
                continue

            n = len(raw)
            if   n == self.T1206: dat = raw
            elif n == self.T1248: dat = raw[self.ETH:]
            else:                 continue

            if _diag < 2:
                print(f"🔍 modelo={self.modelo} ts_sys={ts_sys:.3f} grabando={self.grabando}")
                _diag += 1

            az_raw = struct.unpack('<H', dat[2:4])[0] / 100.0
            self.stats['paquetes_totales'] += 1
            self.stats['ultimo_paquete']    = ts_sys

            # Decodificar puntos
            if self._es_vlp16:
                pts = self._decode_vlp16(dat)
            else:
                pts = self._decode_hdl64e(dat)

            frame_actual.extend(pts)

            if ultimo_az is not None and az_raw < ultimo_az - 180:
                n_frame = len(frame_actual)
                if n_frame > self.PUNTOS_BUFFER_MAXLEN:
                    perdidos = n_frame - self.PUNTOS_BUFFER_MAXLEN
                    self.stats['puntos_descartados_buffer'] += perdidos
                    print(f"⚠️  Frame con {n_frame:,} pts excede buffer "
                          f"({self.PUNTOS_BUFFER_MAXLEN:,}) — se perdieron {perdidos:,}")
                with self.lock:
                    self.puntos_buffer.clear()
                    self.puntos_buffer.extend(frame_actual)
                frame_actual = []
            ultimo_az = az_raw

    # ── Monitor ──────────────────────────────────────────────
    def _monitor(self):
        while self.activo:
            time.sleep(5)
            q     = self._raw_queue.qsize() if self._raw_queue else 0
            drops = self.stats['drops_queue']
            errs  = self.stats['errores']
            perdidos_buf = self.stats['puntos_descartados_buffer']
            if q > 500 or drops > 0 or errs > 0 or perdidos_buf > 0:
                print(f"⚠️  LiDAR monitor | queue={q}/80000 "
                      f"drops={drops} errores={errs} "
                      f"puntos_perdidos_buffer={perdidos_buf:,}")

    # ── Timestamp ────────────────────────────────────────────
    def _ts_por_azimuth(self, payload: bytes, ts_sys: float) -> float:
        """Genera timestamp PCap único por paquete interpolando el azimuth."""
        if len(payload) < 4:
            return ts_sys

        az = struct.unpack('<H', payload[2:4])[0] / 100.0

        if self._rot_az_prev is not None:
            if az < self._rot_az_prev - 180:
                if self._rot_t0 > 0:
                    dt = ts_sys - self._rot_t0
                    if 0.05 < dt < 0.5:
                        self._rot_rate = 360.0 / dt
                self._rot_t0 = ts_sys
        self._rot_az_prev = az

        if ts_sys != self._ts_anchor:
            self._ts_anchor = ts_sys
            self._az_anchor = az
            return ts_sys

        delta_az = az - self._az_anchor
        if delta_az < -180: delta_az += 360
        if delta_az < 0:    delta_az  = 0
        return self._ts_anchor + delta_az / self._rot_rate

    # ============================================================
    # DECODIFICADOR HDL-64E - FÓRMULA OFICIAL VELODYNE
    # ============================================================
    
    def _decode_hdl64e(self, dat: bytes) -> list:
        """
        Decodifica un paquete HDL-64E usando la fórmula oficial de Velodyne.
        
        Fórmula del manual HDL-64E S2 (Appendix F):
            cosRotAngle = cos(azimuth)*cos(rotCorrection) + sin(azimuth)*sin(rotCorrection)
            sinRotAngle = sin(azimuth)*cos(rotCorrection) - cos(azimuth)*sin(rotCorrection)
            xyDistance = distance * cos(vertCorrection)
            X = xyDistance * sinRotAngle - horizOffsetCorrection * cosRotAngle
            Y = xyDistance * cosRotAngle + horizOffsetCorrection * sinRotAngle
            Z = distance * sin(vertCorrection) + vertOffsetCorrection
        """
        pts = []
        
        # Obtener calibración
        cos_rot = self._calib['cos_rot']      # cos(rotCorrection) para cada láser
        sin_rot = self._calib['sin_rot']      # sin(rotCorrection) para cada láser
        cos_vert = self._calib['cos_vert']    # cos(vertCorrection) para cada láser
        sin_vert = self._calib['sin_vert']    # sin(vertCorrection) para cada láser
        horiz_off = self._calib['horiz_off_m'] # horizOffsetCorrection en metros
        vert_off = self._calib['vert_off_m']   # vertOffsetCorrection en metros
        dist_lsb = self._calib['dist_lsb']     # resolución de distancia (0.002 m)
        
        DIST_MIN = 0.9   # ~3 feet mínimo según manual
        DIST_MAX = 120.0  # 120m máximo
        
        for b in range(12):  # 12 bloques por paquete
            off = b * 100
            hdr = struct.unpack('<H', dat[off:off+2])[0]
            
            # Determinar si es bloque superior o inferior
            if hdr == self.HDR_UPPER:
                is_upper = True
                laser_base = 0   # Láseres 0-31 (según el XML, id 0-31 son upper)
            elif hdr == self.HDR_LOWER:
                is_upper = False
                laser_base = 32  # Láseres 32-63 (según el XML, id 32-63 son lower)
            else:
                continue  # Header desconocido
            
            # Azimuth del bloque (en centésimas de grado)
            az_raw = struct.unpack('<H', dat[off+2:off+4])[0]
            az_rad = math.radians(az_raw / 100.0)
            
            # Pre-calcular sin/cos del azimuth
            cos_az = math.cos(az_rad)
            sin_az = math.sin(az_rad)
            
            for ch in range(32):  # 32 canales por bloque
                ol = off + 4 + ch * 3
                raw_dist = struct.unpack('<H', dat[ol:ol+2])[0]
                
                if raw_dist == 0:
                    continue
                
                # Distancia en metros
                distance = raw_dist * dist_lsb
                
                if not (DIST_MIN <= distance <= DIST_MAX):
                    continue
                
                # ID del láser (0-63)
                laser_id = laser_base + ch
                
                if laser_id >= 64:
                    continue
                
                # ═══════════════════════════════════════════════════════════
                # FÓRMULA OFICIAL VELODYNE (del manual HDL-64E S2 Appendix F)
                # ═══════════════════════════════════════════════════════════
                
                # Combinar azimuth con rotCorrection del láser
                # cosRotAngle = cos(azimuth)*cos(rotCorrection) + sin(azimuth)*sin(rotCorrection)
                # sinRotAngle = sin(azimuth)*cos(rotCorrection) - cos(azimuth)*sin(rotCorrection)
                cos_rot_angle = cos_az * cos_rot[laser_id] + sin_az * sin_rot[laser_id]
                sin_rot_angle = sin_az * cos_rot[laser_id] - cos_az * sin_rot[laser_id]
                
                # Distancia en el plano XY
                xy_distance = distance * cos_vert[laser_id]
                
                # Coordenadas XYZ
                # X = xyDistance * sinRotAngle - horizOffset * cosRotAngle
                # Y = xyDistance * cosRotAngle + horizOffset * sinRotAngle
                # Z = distance * sinVertAngle + vertOffset
                x = xy_distance * sin_rot_angle - horiz_off[laser_id] * cos_rot_angle
                y = xy_distance * cos_rot_angle + horiz_off[laser_id] * sin_rot_angle
                z = distance * sin_vert[laser_id] + vert_off[laser_id]
                
                # Intensidad normalizada
                intensity = dat[ol+2] / 255.0
                
                pts.append([x, y, z, intensity])
        
        return pts

    # ============================================================
    # DECODIFICADOR VLP-16
    # ============================================================
    
    def _decode_vlp16(self, dat: bytes) -> list:
        """Decodifica un paquete VLP-16."""
        pts = []
        for b in range(self.NB):
            off = b * self.TB
            az_deg = struct.unpack('<H', dat[off+2:off+4])[0] / 100.0
            az_rad = np.deg2rad(az_deg)
            cos_az = np.cos(az_rad)
            sin_az = np.sin(az_rad)

            for l in range(self.NL_VLP16):
                ol = off + 4 + l * 3
                raw_dist = struct.unpack('<H', dat[ol:ol+2])[0]
                if raw_dist == 0:
                    continue

                dist = raw_dist * _VLP16_RES
                if not (_VLP16_DIST_MIN <= dist <= _VLP16_DIST_MAX):
                    continue

                cos_v = _VLP16_COS_V[l]
                sin_v = _VLP16_SIN_V[l]

                x = dist * cos_v * cos_az
                y = dist * cos_v * sin_az
                z = dist * sin_v

                intensity = dat[ol+2] / 255.0
                pts.append([x, y, z, float(intensity)])

        return pts

    # ── API pública ──────────────────────────────────────────
    def obtener_puntos(self):
        with self.lock:
            return (np.array(list(self.puntos_buffer))
                    if self.puntos_buffer else np.zeros((0, 4)))

    def limpiar_buffer(self):
        with self.lock:
            self.puntos_buffer.clear()

    def iniciar_grabacion(self, carpeta_base=None, nombre_participante=None,
                          start_time=None):
        if self.grabando:
            print("LiDAR: ya grabando - ignorado."); return False
        self._toggle_bloqueado      = True
        self.grabando               = True
        self.timestamp_inicio       = time.time()
        self.acquisition_start_time = float(start_time) if start_time is not None \
                                      else self.timestamp_inicio
        self.acquisition_end_time   = None
        self.carpeta_grabacion      = carpeta_base
        self.nombre_participante    = nombre_participante

        carp = self._carpeta_lidar(carpeta_base)
        carp.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        pref = nombre_participante or f"velodyne_{self.modelo.lower().replace(' ','_')}"
        self._pcap_ruta = carp / f"{pref}_lidar_{ts}.pcap"

        self._ts_anchor = 0.0

        with self._pcap_lock:
            self._pcap_writer = PcapWriter(self._pcap_ruta,
                                           t_ini=self.acquisition_start_time)
        print(f"GRABANDO LiDAR | {nombre_participante} | sensor={self.modelo}")
        print(f"     Inicio: "
              f"{datetime.fromtimestamp(self.acquisition_start_time).strftime('%H:%M:%S.%f')[:-3]}")
        print(f"     PCAP -> {self._pcap_ruta}")
        return True

    def detener_grabacion(self, end_time=None):
        if not self.grabando: return
        self.grabando             = False
        self.acquisition_end_time = float(end_time) if end_time is not None \
                                    else time.time()
        with self._pcap_lock:
            if self._pcap_writer and not self._pcap_writer._cerrado:
                self._pcap_writer._t_fin = self.acquisition_end_time
        time.sleep(0.15)
        hora = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        dur  = time.time() - self.timestamp_inicio
        with self._pcap_lock:
            ref = self._pcap_writer
            self._pcap_writer = None
        if ref: ref.cerrar()
        self._toggle_bloqueado = False
        t0 = datetime.fromtimestamp(self.acquisition_start_time).strftime('%H:%M:%S.%f')[:-3]
        t1 = datetime.fromtimestamp(self.acquisition_end_time).strftime('%H:%M:%S.%f')[:-3]
        print(f"⏸️  LiDAR DETENIDO | {hora} | {dur:.1f}s | ventana {t0} → {t1}")
        if self._pcap_ruta and os.path.exists(self._pcap_ruta):
            mb = os.path.getsize(self._pcap_ruta) / (1024*1024)
            print(f"     📦 PCAP: {mb:.1f} MB → {self._pcap_ruta}")
        if self.stats['puntos_descartados_buffer'] > 0:
            print(f"     ⚠️  Puntos perdidos por overflow de buffer durante la sesión: "
                  f"{self.stats['puntos_descartados_buffer']:,}")

    def _carpeta_lidar(self, base=None):
        return pathlib.Path(base or self.carpeta_grabacion or ".") / "LIDAR"

    def estado_grabacion(self):
        extra = ""
        if self._pcap_ruta and os.path.exists(self._pcap_ruta):
            extra = f" | {os.path.getsize(self._pcap_ruta)/(1024*1024):.1f} MB"
        drops = self.stats['drops_queue']
        drop_str = f" | drops_viz={drops}" if drops > 0 else ""
        sensor_str = f" | {self.modelo}" if self._modelo_detectado else ""
        calib_str = " ✓xml" if self._calib_loaded else ""
        perdidos = self.stats['puntos_descartados_buffer']
        perdidos_str = f" | pts_perdidos={perdidos:,}" if perdidos > 0 else ""
        return (f"🔴 Grabando | pkts={self.stats['paquetes_grabados']:,}"
                f"{extra}{drop_str}{sensor_str}{calib_str}{perdidos_str}"
                if self.grabando else f"⚪ Listo{extra}{sensor_str}{calib_str}{perdidos_str}")

    def obtener_estadisticas(self):
        pb = (os.path.getsize(self._pcap_ruta)
              if self._pcap_ruta and os.path.exists(self._pcap_ruta) else 0)
        return {
            **self.stats,
            'pcap_bytes' : pb,
            'buffer_size': len(self.puntos_buffer),
            'buffer_maxlen': self.PUNTOS_BUFFER_MAXLEN,
            'modelo'     : self.modelo,
            'queue_size' : self._raw_queue.qsize() if self._raw_queue else 0,
            'calib_loaded': self._calib_loaded,
        }

    def esta_recibiendo_datos(self):
        return (time.time() - self.stats['ultimo_paquete']) < 2.0

    @property
    def toggle_habilitado(self):
        return not self._toggle_bloqueado

    def detener(self):
        self.activo = False
        if self.grabando:
            self.detener_grabacion()
        for nombre in ('hilo_rx', 'hilo_proc', 'hilo_mon'):
            hilo = getattr(self, nombre, None)
            if hilo and hilo.is_alive():
                hilo.join(timeout=2.0)
        if self.socket:
            self.socket.close()
            self.socket = None
        print(f"🔴 LiDAR {self.modelo} detenido")


# ============================================================
# DEMO
# ============================================================
if __name__ == "__main__":
    v = VelodyneHDL64E(puerto=2368, modelo='AUTO')
    if v.iniciar_captura():
        v.iniciar_grabacion("./test_v10", "demo", start_time=time.time())
        print("⏳ 10 s...")
        time.sleep(10)
        v.detener_grabacion(end_time=time.time())
        s = v.obtener_estadisticas()
        print(f"modelo={s['modelo']}  pkts={s['paquetes_totales']:,}  "
              f"grabados={s['paquetes_grabados']:,}  "
              f"drops={s['drops_queue']}  PCAP={s['pcap_bytes']//1024} KB  "
              f"calib={s['calib_loaded']}  "
              f"puntos_perdidos_buffer={s['puntos_descartados_buffer']:,}")
    v.detener()
