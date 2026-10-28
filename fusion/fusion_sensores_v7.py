"""Fusión multi-sensor: ECG + EEG + VBOX + LiDAR HDL-64E + facial + carril
a una base de tiempo única (unix_epoch UTC). Alineación por ZOH causal.
Nivel 1: CSV maestro. Nivel 2: nubes del LiDAR (PCAP crudo + CSV calibrado).

OPTIMIZACIONES aplicadas:
  - _iter_pcap_fast : mmap en lugar de read() por paquete (2–4× I/O lectura)
  - leer_lidar      : ProcessPoolExecutor por barrido (3–8× en multi-core)
  - leer_lidar      : escritura ZIP en hilo separado (I/O no bloquea CPU)
  - alinear()       : fancy-indexing vectorizado sin loop por columna
  - to_csv()        : float_format en lugar de round() previo (sin copia extra)
"""

import os
import re
import sys
import math
import mmap
import queue
import struct
import threading
import zipfile
import datetime as dt
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

# ============================ CONFIGURACIÓN ============================

F_REJILLA     = "auto"
F_REJILLA_MAX = 2000.0
VENTANA       = "interseccion"
TOL_HUECO     = 3.0
DECIMALES     = 6
ZONA_LOCAL    = "America/Mexico_City"
METODO        = "zoh"

CALIB_XML_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcss", "dbkaren.xml")

MODO_LIDAR           = "nubes"
DESKEW               = True
ROI_FRONTAL_DEG      = 30.0
LIDAR_DIST_MIN       = 0.9
LIDAR_DIST_MAX       = 120.0
HORIZ_OFF_UNIDAD     = "cm"
APLICAR_CORR_DISTANCIA  = True
APLICAR_CORR_INTENSIDAD = True
EGO_RADIO_M   = 0.0
CSV_DECIMALES = 4
ZIP_NIVEL     = 6

# Nº de procesos para decode paralelo (None = os.cpu_count()-1)
N_WORKERS_LIDAR = None

HDR_UPPER, HDR_LOWER = 0xEEFF, 0xDDFF
_C_XX_MIN, _C_XX_MAX = 240.0, 2504.0
_C_YY_MIN, _C_YY_MAX = 193.8, 2504.0

# ============================ NIVEL 2: ESCRITORES ============================

_ETH = bytes([0xff] * 6 + [0x60, 0x76, 0x88, 0x00, 0x00, 0x00, 0x08, 0x00])


def _cksum(h):
    if len(h) % 2:
        h += b"\x00"
    s = sum((h[i] << 8) + h[i + 1] for i in range(0, len(h), 2))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def _frame_eth(payload, src="192.168.1.200", dst="255.255.255.255"):
    ulen = 8 + len(payload)
    udp  = struct.pack(">HHHH", 2368, 2368, ulen, 0)
    ilen = 20 + ulen
    s = bytes(map(int, src.split(".")))
    d = bytes(map(int, dst.split(".")))
    pre = (bytes([0x45, 0x00]) + struct.pack(">H", ilen)
           + bytes([0, 0, 0x40, 0, 0xff, 0x11, 0, 0]) + s + d)
    ip  = (bytes([0x45, 0x00]) + struct.pack(">H", ilen)
           + bytes([0, 0, 0x40, 0, 0xff, 0x11])
           + struct.pack(">H", _cksum(pre)) + s + d)
    return _ETH + ip + udp + payload


def _serializar_pcap(payloads, t_pkts):
    """Serializa un barrido como bytes PCAP (sin escribir a disco)."""
    import io
    buf = io.BytesIO()
    buf.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for pay, ts in zip(payloads, t_pkts):
        frm = _frame_eth(pay)
        buf.write(struct.pack("<IIII",
                              int(ts), int((ts % 1) * 1_000_000),
                              len(frm), len(frm)))
        buf.write(frm)
    return buf.getvalue()


def escribir_pcap_frame(path, payloads, t_pkts):
    """Escribe directamente a disco (solo usado fuera del modo paralelo)."""
    with open(path, "wb") as f:
        f.write(_serializar_pcap(payloads, t_pkts))


def csv_nube_bytes(pts, t_pto, extra):
    df = pd.DataFrame({
        "x":           pts[:, 0],
        "y":           pts[:, 1],
        "z":           pts[:, 2],
        "intensity":   pts[:, 3],
        "laser_id":    extra["laser_id"],
        "azimuth_deg": extra["azimuth_deg"],
        "distance_m":  extra["distance_m"],
        "t_punto":     t_pto,
    })
    return df.to_csv(index=False,
                     float_format=f"%.{CSV_DECIMALES}f").encode("utf-8")

# ============================ CALIBRACIÓN VELODYNE ============================

def _campo(px, nombre, defecto=None):
    e = px.find(nombre)
    if e is None or e.text is None:
        return defecto
    try:
        return float(e.text)
    except ValueError:
        return defecto


def cargar_calib(xml_path):
    with open(xml_path, "rb") as f:
        raw = f.read().rstrip(b"\x00").rstrip()
    db = ET.fromstring(raw.decode("utf-8")).find(".//DB")
    if db is None:
        raise ValueError("XML sin elemento DB")

    e = db.find("distLSB_")
    lsb_cm = float(e.text) if e is not None else 0.2

    n = 64
    rot   = np.zeros(n); vert  = np.zeros(n)
    dc    = np.zeros(n); dcx   = np.zeros(n); dcy  = np.zeros(n)
    voff  = np.zeros(n); hoff  = np.zeros(n)
    fdist = np.zeros(n); fslope= np.zeros(n)
    imin  = np.zeros(n); imax  = np.full(n, 255.0)

    tiene_xy = tiene_focal = False
    leidos = 0
    for it in db.find("points_").findall("item"):
        px = it.find("px")
        if px is None:
            continue
        i = int(px.find("id_").text)
        if not (0 <= i < n):
            continue
        rot[i]  = _campo(px, "rotCorrection_",    0.0)
        vert[i] = _campo(px, "vertCorrection_",   0.0)
        dc[i]   = _campo(px, "distCorrection_",   0.0)
        voff[i] = _campo(px, "vertOffsetCorrection_", 0.0)
        hoff[i] = _campo(px, "horizOffsetCorrection_", 0.0)
        vx = _campo(px, "distCorrectionX_")
        vy = _campo(px, "distCorrectionY_")
        if vx is not None and vy is not None:
            dcx[i], dcy[i] = vx, vy
            tiene_xy = True
        else:
            dcx[i] = dcy[i] = dc[i]
        fd = _campo(px, "focalDistance_")
        fs = _campo(px, "focalSlope_")
        if fd is not None and fs is not None:
            fdist[i], fslope[i] = fd, fs
            tiene_focal = True
        imin[i] = _campo(px, "minIntensity_",  0.0)
        imax[i] = _campo(px, "maxIntensity_", 255.0)
        leidos += 1

    if not APLICAR_CORR_DISTANCIA:
        nivel, desc = 1, "geometria (forzado)"
    elif tiene_xy:
        nivel, desc = 3, "completo (dos puntos, S2)"
    elif np.any(dc != 0):
        nivel, desc = 2, "simple (distCorrection)"
    else:
        nivel, desc = 1, "geometria (sin corr. rango)"
    usa_focal = APLICAR_CORR_INTENSIDAD and tiene_focal

    print(f"    calibracion: {leidos}/64 laseres | nivel {nivel} ({desc})")
    print(f"      distLSB={lsb_cm:.3f}cm  vert[{vert.min():.2f},{vert.max():.2f}]  "
          f"distCorr[{dc.min():.2f},{dc.max():.2f}]cm  "
          f"horizOff[{hoff.min():.2f},{hoff.max():.2f}]{HORIZ_OFF_UNIDAD}  "
          f"focal={'si' if usa_focal else 'no'}")
    if abs(hoff).max() > 0:
        val_cm = abs(hoff).max() * (1.0 if HORIZ_OFF_UNIDAD == "cm" else 0.1)
        if not (0.8 < val_cm < 7.8):
            print(f"      AVISO: horizOff={val_cm:.2f}cm; en HDL-64E se espera ~2.6cm. "
                  f"Prueba HORIZ_OFF_UNIDAD='{'mm' if HORIZ_OFF_UNIDAD=='cm' else 'cm'}'.")

    div_h = 1.0 if HORIZ_OFF_UNIDAD == "cm" else 10.0
    rr, vr = np.deg2rad(rot), np.deg2rad(vert)
    return dict(nivel=nivel, usa_focal=usa_focal,
                cos_rot=np.cos(rr),  sin_rot=np.sin(rr),
                cos_vert=np.cos(vr), sin_vert=np.sin(vr),
                lsb_cm=lsb_cm, dc_cm=dc, dcx_cm=dcx, dcy_cm=dcy,
                vert_off_cm=voff, horiz_off_cm=hoff / div_h,
                focal_dist=fdist, focal_slope=fslope,
                i_min=imin, i_max=imax)


def calib_respaldo():
    print("    aviso: sin XML -> respaldo SIN correccion de rango")
    vert = np.zeros(64)
    vert[:32] = -8.33  + np.arange(32) / 31.0 * 10.33
    vert[32:] = -24.33 + np.arange(32) / 31.0 * 15.8
    vr = np.deg2rad(vert)
    z  = np.zeros(64)
    return dict(nivel=1, usa_focal=False,
                cos_rot=np.ones(64),  sin_rot=z.copy(),
                cos_vert=np.cos(vr),  sin_vert=np.sin(vr),
                lsb_cm=0.2, dc_cm=z.copy(), dcx_cm=z.copy(), dcy_cm=z.copy(),
                vert_off_cm=z.copy(), horiz_off_cm=z.copy(),
                focal_dist=z.copy(), focal_slope=z.copy(),
                i_min=z.copy(), i_max=np.full(64, 255.0))

# ============================ LIDAR: PCAP Y DECODE ============================

# ── OPTIMIZACIÓN 1: mmap en lugar de read() por paquete ──────────────────────
def _iter_pcap(path):
    """Itera paquetes VLP usando mmap (sin copias innecesarias al heap)."""
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            magic = struct.unpack_from("<I", mm, 0)[0]
            if magic == 0xA1B2C3D4:
                end, nano = "<", False
            elif magic == 0xA1B23C4D:
                end, nano = "<", True
            elif magic == 0xD4C3B2A1:
                end, nano = ">", False
            else:
                raise ValueError(f"No es PCAP clasico (magic=0x{magic:08X})")
            if struct.unpack_from(end + "I", mm, 20)[0] != 1:
                raise ValueError("linktype != Ethernet")
            off = 24
            sz  = len(mm)
            while off + 16 <= sz:
                s, u, caplen, _ = struct.unpack_from(end + "IIII", mm, off)
                off += 16
                if off + caplen > sz:
                    break
                # slice de mmap: sin copia hasta que se convierte a bytes
                pay = mm[off + 42 : off + caplen]
                if len(pay) == 1206:
                    yield s + (u / 1e9 if nano else u / 1e6), bytes(pay)
                off += caplen
        finally:
            mm.close()


def detectar_modelo(payloads):
    hdrs = set(); up = lo = 0
    for p in payloads[:50]:
        for b in range(12):
            h = struct.unpack_from("<H", p, b * 100)[0]
            hdrs.add(h)
            if h == HDR_UPPER:
                up += 1
            elif h == HDR_LOWER:
                lo += 1
    es64 = HDR_LOWER in hdrs
    print(f"    modelo: {'HDL-64E' if es64 else 'NO HDL-64E'} (upper:lower={up}:{lo})")
    if not es64:
        raise ValueError("Decodificador solo para HDL-64E (falta 0xDDFF)")


def decodificar_frame(payloads, t_pkts, cal, T_rot_ref):
    P   = len(payloads)
    raw = np.frombuffer(b"".join(payloads), dtype=np.uint8).reshape(P, 1206)
    blk = raw[:, :1200].reshape(P, 12, 100)

    hdr = blk[:, :, 0].astype(np.uint16) | (blk[:, :, 1].astype(np.uint16) << 8)
    az  = (blk[:, :, 2].astype(np.uint16) | (blk[:, :, 3].astype(np.uint16) << 8)) / 100.0
    az_u = np.unwrap(np.deg2rad(az.ravel())).reshape(P, 12)

    az_pkt = az_u[:, 0]
    A = np.column_stack([np.ones(P), az_pkt])
    try:
        a, b = np.linalg.lstsq(A, np.asarray(t_pkts, dtype=np.float64), rcond=None)[0]
    except np.linalg.LinAlgError:
        a, b = float(t_pkts[0]), T_rot_ref / (2 * np.pi)
    T_giro = b * 2 * np.pi
    if not (0.25 * T_rot_ref < T_giro < 4.0 * T_rot_ref) or b <= 0:
        b      = T_rot_ref / (2 * np.pi)
        a      = float(t_pkts[0]) - b * az_pkt[0]
        T_giro = T_rot_ref

    body  = blk[:, :, 4:100].reshape(P, 12, 32, 3)
    d_raw = body[..., 0].astype(np.uint16) | (body[..., 1].astype(np.uint16) << 8)
    i_raw = body[..., 2].astype(np.float64)

    base     = np.where(hdr == HDR_LOWER, 32, 0)[..., None]
    lid      = (base + np.arange(32)[None, None, :]).astype(np.int64)
    bloque_ok = ((hdr == HDR_UPPER) | (hdr == HDR_LOWER))[..., None]
    d0_cm   = d_raw.astype(np.float64) * cal["lsb_cm"]
    d0_m    = d0_cm / 100.0
    ok = bloque_ok & (d_raw != 0) & (d0_m >= LIDAR_DIST_MIN) & (d0_m <= LIDAR_DIST_MAX)
    if not ok.any():
        return np.zeros((0, 4), np.float32), np.zeros(0), T_giro, {}

    azp     = np.repeat(az_u[..., None], 32, axis=2)
    i       = lid[ok]; d0 = d0_cm[ok]; a_pt = azp[ok]
    draw    = d_raw[ok].astype(np.float64); inten = i_raw[ok]
    t_punto = a + b * a_pt

    ca, sa = np.cos(a_pt), np.sin(a_pt)
    cra = ca * cal["cos_rot"][i] + sa * cal["sin_rot"][i]
    sra = sa * cal["cos_rot"][i] - ca * cal["sin_rot"][i]
    cv  = cal["cos_vert"][i]; sv = cal["sin_vert"][i]; ho = cal["horiz_off_cm"][i]

    if cal["nivel"] == 3:
        xyD   = d0 * cv
        xx    = np.abs(xyD * sra - ho * cra)
        yy    = np.abs(xyD * cra + ho * sra)
        dc, dcx, dcy = cal["dc_cm"][i], cal["dcx_cm"][i], cal["dcy_cm"][i]
        corr_x = (dc - dcx) * (xx - _C_XX_MIN) / (_C_XX_MAX - _C_XX_MIN) + dcx
        corr_y = (dc - dcy) * (yy - _C_YY_MIN) / (_C_YY_MAX - _C_YY_MIN) + dcy
        dX = d0 + corr_x; dY = d0 + corr_y
    elif cal["nivel"] == 2:
        dX = dY = d0 + cal["dc_cm"][i]
    else:
        dX = dY = d0

    x_cm = (dX * cv) * sra - ho * cra
    y_cm = (dY * cv) * cra + ho * sra
    z_cm = dY * sv + cal["vert_off_cm"][i]
    x    = (x_cm / 100.0).astype(np.float32)
    y    = (y_cm / 100.0).astype(np.float32)
    z    = (z_cm / 100.0).astype(np.float32)

    if cal["usa_focal"]:
        fd, fs = cal["focal_dist"][i], cal["focal_slope"][i]
        off_   = 256.0 * (1.0 - fd / 13100.0) ** 2
        val_   = 256.0 * (1.0 - draw / 65535.0) ** 2
        inten  = np.clip(inten + fs * np.abs(off_ - val_),
                         cal["i_min"][i], cal["i_max"][i])
    inten = (inten / 255.0).astype(np.float32)

    pts   = np.column_stack([x, y, z, inten])
    extra = {"laser_id":    i.astype(np.int16),
             "azimuth_deg": (np.degrees(a_pt) % 360.0).astype(np.float32),
             "distance_m":  (dY / 100.0).astype(np.float32)}

    if EGO_RADIO_M > 0:
        fuera = np.hypot(pts[:, 0], pts[:, 1]) > EGO_RADIO_M
        pts, t_punto = pts[fuera], t_punto[fuera]
        extra = {k: v[fuera] for k, v in extra.items()}
    return pts, t_punto, T_giro, extra

# ============================ POSE / DE-SKEW ============================

def construir_pose(df_vbox):
    t   = df_vbox.index.values.astype(float)
    lat = df_vbox["vbox_lat_deg"].values
    lon = df_vbox["vbox_long_deg"].values
    alt = (df_vbox["vbox_height"].values if "vbox_height" in df_vbox
           else np.zeros_like(lat))
    hcol = "vbox_True_Head" if "vbox_True_Head" in df_vbox else "vbox_heading"
    yaw  = np.unwrap(np.deg2rad(df_vbox[hcol].values))
    lat0, lon0 = np.nanmedian(lat), np.nanmedian(lon)
    este  = (lon - lon0) * 111320.0 * math.cos(math.radians(lat0))
    norte = (lat - lat0) * 110540.0
    print(f"    pose: heading='{hcol}' origen=({lat0:.6f},{lon0:.6f})")
    return dict(t=t, e=este, n=norte, u=alt, yaw=yaw)


def deskew(pts, t_pto, pose, t_ref):
    ep  = np.interp(t_pto, pose["t"], pose["e"])
    npo = np.interp(t_pto, pose["t"], pose["n"])
    up  = np.interp(t_pto, pose["t"], pose["u"])
    yp  = np.interp(t_pto, pose["t"], pose["yaw"])
    er  = float(np.interp(t_ref, pose["t"], pose["e"]))
    nr  = float(np.interp(t_ref, pose["t"], pose["n"]))
    ur  = float(np.interp(t_ref, pose["t"], pose["u"]))
    yr  = float(np.interp(t_ref, pose["t"], pose["yaw"]))
    x   = pts[:, 0].astype(np.float64)
    y   = pts[:, 1].astype(np.float64)
    z   = pts[:, 2].astype(np.float64)
    cp, sp  = np.cos(yp), np.sin(yp)
    e_w  = x * cp + y * sp + ep
    n_w  = -x * sp + y * cp + npo
    u_w  = z + up
    de, dn, du = e_w - er, n_w - nr, u_w - ur
    cr, sr  = math.cos(yr), math.sin(yr)
    out     = pts.copy()
    out[:, 0] = (de * cr - dn * sr).astype(np.float32)
    out[:, 1] = (de * sr + dn * cr).astype(np.float32)
    out[:, 2] = du.astype(np.float32)
    return out

# ============================ WORKER PARA PROCESO PARALELO ===================

def _worker_barrido(args):
    """
    Función ejecutada en cada proceso worker.
    Recibe: (k, t_start, payloads, t_pkts, cal, T_ref, pose_dict, modo)
    Devuelve: (k, fila_dict, T_giro, pcap_bytes_or_None, csv_bytes_or_None)

    Nota: pose_dict se pasa como dict plain (serializable con pickle)
    porque los arrays numpy dentro de él sí son picklables.
    """
    (k, t_start, payloads, t_pkts, cal, T_ref, pose, modo) = args

    fila = {"epoch": t_start, "frame_id": k, "n_paquetes": len(payloads)}
    pcap_bytes = None
    csv_bytes  = None

    if modo == "indice":
        fila["giro_ms"] = T_ref * 1000.0
        return k, fila, T_ref, None, None

    pts, t_pto, T_giro, extra = decodificar_frame(payloads, t_pkts, cal, T_ref)
    fila["n_puntos"] = len(pts)
    fila["giro_ms"]  = T_giro * 1000.0

    if len(pts):
        if pose is not None:
            pts = deskew(pts, t_pto, pose, t_start)
        ang = np.degrees(np.arctan2(pts[:, 0], pts[:, 1]))
        roi = np.abs(ang) <= ROI_FRONTAL_DEG
        r   = np.hypot(pts[roi, 0], pts[roi, 1])
        fila["dist_min_frontal_m"] = float(r.min()) if r.size else float("nan")
        fila["z_medio_m"]          = float(pts[:, 2].mean())

        if modo == "nubes":
            # Serializar en el worker: evita transferir arrays grandes de vuelta
            pcap_bytes = _serializar_pcap(payloads, t_pkts)
            csv_bytes  = csv_nube_bytes(pts, t_pto, extra)

    return k, fila, T_giro, pcap_bytes, csv_bytes


# ── OPTIMIZACIÓN 2: hilo escritor de ZIP ─────────────────────────────────────
def _zip_writer_thread(zf, q):
    """Consume la cola y escribe en el ZIP sin bloquear el proceso principal."""
    while True:
        item = q.get()
        if item is None:          # señal de cierre
            q.task_done()
            break
        name, data = item
        zf.writestr(name, data)
        q.task_done()

# ============================ LECTOR LIDAR ============================

def leer_lidar(path, calib=None, modo="nubes", dir_salida=None, pose=None,
               participante="sesion"):
    cal = None
    if modo != "indice":
        cal = cargar_calib(calib) if calib and os.path.exists(calib) else calib_respaldo()

    dir_pcap = zf = write_q = writer_th = None
    if modo == "nubes":
        dir_pcap = os.path.join(dir_salida, "LIDAR_PCAP")
        os.makedirs(dir_pcap, exist_ok=True)
        zip_path = os.path.join(dir_salida, f"LIDAR_CSV_{participante}.zip")
        zf       = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                                   compresslevel=ZIP_NIVEL, allowZip64=True)
        # Cola con backpressure para no saturar RAM
        write_q  = queue.Queue(maxsize=256)
        writer_th = threading.Thread(target=_zip_writer_thread,
                                     args=(zf, write_q), daemon=True)
        writer_th.start()
        print(f"    nivel 2a -> {dir_pcap}")
        print(f"    nivel 2b -> {zip_path}")

    print("    leyendo PCAP...")
    frames   = []
    az_prev, buf, tbuf, t0 = None, [], [], None
    muestra  = []

    for ts, pay in _iter_pcap(path):          # <-- usa mmap
        if len(muestra) < 50:
            muestra.append(pay)
        az = struct.unpack_from("<H", pay, 2)[0] / 100.0
        if az_prev is not None and az < az_prev - 180:
            frames.append((t0, buf, tbuf))
            buf, tbuf, t0 = [], [], ts
        if t0 is None:
            t0 = ts
        az_prev = az
        buf.append(pay)
        tbuf.append(ts)
    if buf:
        frames.append((t0, buf, tbuf))
    if not frames:
        if zf:
            write_q.put(None); writer_th.join(); zf.close()
        raise ValueError("PCAP sin barridos")

    detectar_modelo(muestra)

    t_ini    = np.array([t for t, _, _ in frames])
    dt_entre = np.diff(t_ini)
    npkt     = np.array([len(b) for _, b, _ in frames])

    semillas = []
    for _t0, pay_, tp_ in frames[:min(200, len(frames))]:
        if len(pay_) < 20:
            continue
        azp = np.unwrap([np.deg2rad(struct.unpack_from("<H", p, 2)[0] / 100.0)
                         for p in pay_])
        if azp[-1] - azp[0] < 0.5:
            continue
        b_ = np.polyfit(azp, np.asarray(tp_, dtype=np.float64), 1)[0]
        if b_ > 0:
            semillas.append(b_ * 2 * np.pi)
    T_ref = (float(np.median(semillas)) if semillas
             else float(np.median(dt_entre)) if len(dt_entre) else 0.0655)
    print(f"    giro ref: {T_ref*1000:.2f} ms ({1/T_ref:.2f} Hz)")

    if DESKEW and pose is None and modo != "indice":
        print("    aviso: DESKEW sin VBOX -> se omite")

    nf      = len(frames)
    n_work  = max(1, (os.cpu_count() or 2) - 1) if N_WORKERS_LIDAR is None else N_WORKERS_LIDAR

    # ── OPTIMIZACIÓN 3: ProcessPoolExecutor por barrido ───────────────────────
    # Preparar args (listas plain de bytes, picklables)
    args_list = [
        (k, t_start,
         list(payloads),    # list[bytes]
         list(t_pkts),      # list[float]
         cal, T_ref, pose, modo)
        for k, (t_start, payloads, t_pkts) in enumerate(frames)
    ]

    filas_dict = {}   # k -> fila
    T_giros    = {}   # k -> T_giro
    pcap_pendientes = {}   # k -> bytes (escritura diferida)

    print(f"    procesando {nf:,} barridos con {n_work} worker(s)...")

    try:
        ctx = ProcessPoolExecutor(max_workers=n_work)
        futuros = {ctx.submit(_worker_barrido, a): a[0] for a in args_list}

        completados = 0
        for fut in as_completed(futuros):
            k, fila, T_giro, pcap_bytes, csv_bytes = fut.result()
            filas_dict[k] = fila
            T_giros[k]    = T_giro
            completados  += 1

            # Escritura I/O: PCAP a disco, CSV a la cola del hilo ZIP
            if pcap_bytes:
                pcap_path = os.path.join(dir_pcap, f"frame_{k:06d}.pcap")
                pcap_pendientes[pcap_path] = pcap_bytes
            if csv_bytes and write_q is not None:
                write_q.put((f"frame_{k:06d}.csv", csv_bytes))

            if completados % 500 == 0 or completados == nf:
                print(f"      barrido {completados:,}/{nf:,}", end="\r", flush=True)

        ctx.shutdown(wait=False)
        print()

        # Escribir PCAPs en paralelo con hilos (I/O puro, sin GIL)
        def _escribe_pcap(item):
            path_, data_ = item
            with open(path_, "wb") as fout:
                fout.write(data_)

        if pcap_pendientes:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as tp:
                list(tp.map(_escribe_pcap, pcap_pendientes.items()))

    finally:
        if write_q is not None:
            write_q.put(None)    # señal de cierre al hilo escritor
            write_q.join()       # espera a que vacie la cola
        if writer_th is not None:
            writer_th.join()
        if zf is not None:
            zf.close()

    # Reensamblar en orden
    filas   = [filas_dict[k]  for k in range(nf)]
    Tg_arr  = np.array([T_giros[k] for k in range(nf)])

    # ── Estadísticas (igual que versión original) ─────────────────────────────
    print("\n    --- ROTACION ---")
    print(f"    periodo mediana {np.median(Tg_arr)*1000:.2f} ms ({1/np.median(Tg_arr):.2f} Hz)")
    print(f"    rango {Tg_arr.min()*1000:.1f}..{Tg_arr.max()*1000:.1f} ms "
          f"({1/Tg_arr.max():.2f}..{1/Tg_arr.min():.2f} Hz)")
    cv = Tg_arr.std() / np.median(Tg_arr)
    print(f"    giro {'VARIABLE' if cv > 0.05 else 'CONSTANTE'} (CV={cv*100:.1f}%)")

    ok = (Tg_arr > 0) & (npkt > 20)
    if ok.sum() > 10:
        tasa = npkt[ok] / Tg_arr[ok]
        cvt  = tasa.std() / tasa.mean()
        print(f"    tasa disparo {np.median(tasa):,.0f} pkt/s (CV={cvt*100:.1f}%)")
        if npkt[ok].std() >= 1e-9:
            r2 = np.corrcoef(Tg_arr[ok], npkt[ok])[0, 1]
            if r2 > 0.9 and cvt < 0.15:
                print("    -> giro VARIABLE real (no falta de datos)")

    Tg_prev = Tg_arr[:-1]
    hueco   = dt_entre > 1.5 * Tg_prev
    t_perd  = float(np.sum(dt_entre[hueco] - Tg_prev[hueco])) if hueco.any() else 0.0
    total   = float(t_ini[-1] - t_ini[0]) if len(t_ini) > 1 else 1.0
    print("\n    --- HUECOS ---")
    print(f"    barridos {len(frames):,} | intervalos con hueco {int(hueco.sum()):,}")
    print(f"    tiempo sin datos {t_perd:.1f}s de {total:.1f}s ({t_perd/total*100:.1f}%)")
    print(f"    paquetes/barrido mediana {np.median(npkt):.0f} [{npkt.min()},{npkt.max()}]")
    if t_perd / total > 0.05:
        print(f"    !! {t_perd/total*100:.0f}% sin datos -> perdida en captura")

    df = pd.DataFrame(filas)
    if "dist_min_frontal_m" in df.columns and df["dist_min_frontal_m"].notna().any():
        s = df["dist_min_frontal_m"]
        print(f"\n    dist frontal: media {s.mean():.2f} std {s.std():.3f} "
              f"[{s.min():.2f},{s.max():.2f}] m")
        if s.std() < 0.15:
            print(f"    !! ve algo FIJO a ~{s.median():.2f}m (ego-vehiculo). "
                  f"Sube EGO_RADIO_M > {s.median():.1f}")

    disc = [c for c in ("frame_id", "n_paquetes", "n_puntos", "giro_ms",
                        "dist_min_frontal_m", "z_medio_m") if c in df.columns]
    return _preparar(df, "lidar", [], disc)

# ============================ LECTORES ECG/EEG/VBOX ============================

_COLS_TIEMPO     = ["unix_timestamp", "unix_epoch", "epoch", "epochtime",
                    "unixtime", "timestamp_unix", "epochtime"]
_COLS_TEXTO_FECHA = ["timestamp", "time", "datetime", "fecha", "hora"]


def _detectar_col_tiempo(cols):
    norm = {c: re.sub(r"[\s_\-]", "", c.lower()) for c in cols}
    for cand in _COLS_TIEMPO:
        objetivo = cand.replace("_", "")
        for c, n in norm.items():
            if n == objetivo:
                return c
    return None


def _clasificar_columnas(df, epoch_col):
    cont, disc = [], []
    n = max(len(df), 1)
    for c in df.columns:
        if c == epoch_col:
            continue
        norm = re.sub(r"[\s_\-]", "", c.lower())
        if norm in [t.replace("_", "") for t in _COLS_TEXTO_FECHA]:
            continue

        s   = df[c].astype(str).str.replace(r"[\[\]\s]", "", regex=True)
        s   = s.replace("", np.nan)
        num = pd.to_numeric(s, errors="coerce")
        frac_num = num.notna().mean()

        if frac_num < 0.5:
            disc.append(c)
            continue

        df[c] = num
        nun      = num.nunique(dropna=True)
        es_entero = np.allclose(num.dropna() % 1, 0) if num.notna().any() else True
        if nun <= 1 or (es_entero and nun <= max(20, int(0.005 * n))):
            disc.append(c)
        else:
            cont.append(c)
    return cont, disc


def leer_ecg(path):
    df   = pd.read_csv(path)
    tcol = _detectar_col_tiempo(df.columns)
    if tcol is None:
        raise ValueError(f"ECG: no encuentro columna de tiempo en {list(df.columns)}")
    df = df.rename(columns={tcol: "epoch"})
    df["epoch"] = pd.to_numeric(df["epoch"], errors="coerce")
    cont, disc = _clasificar_columnas(df, "epoch")
    print(f"    ECG: tiempo='{tcol}' | {len(cont)} señales + {len(disc)} discretas")
    return _preparar(df[["epoch"] + cont + disc].copy(), "ecg", cont, disc)


def leer_eeg(path):
    # ── PASO 1: detectar desfase entre header y datos (formato EMOTIV) ──
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        hdr_line  = f.readline().strip()
        data_line = f.readline().strip()

    hdr_cols  = hdr_line.split(",")
    data_cols = data_line.split(",")
    n_hdr     = len(hdr_cols)
    n_data    = len(data_cols)

    if n_data == n_hdr + 1:
        # EMOTIV exporta MARKER_HARDWARE como dos campos separados
        # (el valor numérico y la lista de marcadores '[]') pero solo
        # declara un nombre en el header → agregar nombre extra AL FINAL
        nombres = hdr_cols + ["MARKER_LIST"]
        df = pd.read_csv(path, names=nombres, header=0,
                         encoding="utf-8-sig", low_memory=False)
    elif n_data == n_hdr - 1:
        # Caso inverso: columna extra al inicio sin nombre en header
        nombres = ["Timestamp_str"] + hdr_cols
        df = pd.read_csv(path, names=nombres, header=0,
                         encoding="utf-8-sig", low_memory=False)
    else:
        df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)

    # ── PASO 2: detectar columna de tiempo ──
    if "EpochTime" in df.columns:
        tcol = "EpochTime"
    else:
        tcol = _detectar_col_tiempo(df.columns)
        if tcol is None:
            raise ValueError(f"EEG: no encuentro columna de tiempo en {list(df.columns)}")

    df = df.rename(columns={tcol: "epoch"})
    df["epoch"] = pd.to_numeric(df["epoch"], errors="coerce")

    # desambiguar nombres repetidos que pandas marca con .1
    df.columns = [c.replace(".1", "_b") if c != "epoch" else c for c in df.columns]
    cont, disc = _clasificar_columnas(df, "epoch")
    print(f"    EEG: tiempo='{tcol}' | {len(cont)} canales + {len(disc)} discretas")
    return _preparar(df[["epoch"] + cont + disc].copy(), "eeg", cont, disc)

def leer_vbo(path):
    fecha = None; hora_hdr = None
    header_cols, cn_cols, filas = [], [], []
    st = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            s = ln.strip()
            if fecha is None:
                m = re.search(r"created on\s+(\d{2})/(\d{2})/(\d{4})", s)
                if m:
                    d, mo, y = map(int, m.groups())
                    fecha    = dt.datetime(y, mo, d, tzinfo=dt.timezone.utc)
                    mh = re.search(r"@\s*(\d{1,2}):(\d{2})", s)
                    if mh:
                        hora_hdr = int(mh.group(1)) + int(mh.group(2)) / 60.0
            if s.startswith("["):
                st = ("h" if s.startswith("[header]")
                      else "c" if s.startswith("[column names]")
                      else "d" if s.startswith("[data]")
                      else None)
                continue
            if not s:
                continue
            if st == "h":
                header_cols.append(s)
            elif st == "c":
                cn_cols = s.split()
            elif st == "d":
                filas.append(s.split())
    if fecha is None:
        raise ValueError("VBO sin fecha en encabezado")
    if not filas:
        raise ValueError("VBO sin datos")

    ncols = max(len(r) for r in filas)
    cand  = [c for c in (header_cols, cn_cols) if c]
    cols  = min(cand, key=lambda c: abs(len(c) - ncols)) if cand else []
    if len(cols) < ncols:
        cols = list(cols) + [f"col_{i}" for i in range(len(cols), ncols)]
    elif len(cols) > ncols:
        cols = cols[:ncols]
    vistos = {}; unicas = []
    for c in cols:
        if c in vistos:
            vistos[c] += 1; unicas.append(f"{c}_{vistos[c]}")
        else:
            vistos[c] = 0; unicas.append(c)
    cols  = unicas
    filas = [r + [""] * (ncols - len(r)) if len(r) < ncols else r[:ncols]
             for r in filas]

    df = pd.DataFrame(filas, columns=cols).apply(pd.to_numeric, errors="coerce")

    def buscar(exactos, contiene):
        norm = {c: c.lower().replace("_", "").replace(".", "").replace(" ", "")
                for c in df.columns}
        for objetivo in exactos:
            for c, n in norm.items():
                if n == objetivo:
                    return c
        for objetivo in contiene:
            for c, n in norm.items():
                if objetivo in n:
                    return c
        return None

    c_time = buscar(["time"], ["time"]) or df.columns[min(1, len(df.columns) - 1)]
    c_lat  = buscar(["latitude", "lat"],  ["latitude"])
    c_long = buscar(["longitude", "long", "lng"], ["longitude"])

    t   = df[c_time].astype(float)
    seg = (t // 10000) * 3600 + ((t % 10000) // 100) * 60 + (t % 100)

    base      = fecha.timestamp()
    hora_dato0 = float(seg.iloc[0]) / 3600.0
    if hora_hdr is not None and (hora_hdr - hora_dato0) > 12.0:
        base += 86400.0
    df["epoch"] = base + seg

    if c_lat:
        df["lat_deg"]  = df[c_lat]  / 60.0
    if c_long:
        df["long_deg"] = -df[c_long] / 60.0

    excl = {c_time, "epoch", "lat_deg", "long_deg"}
    if c_lat:
        excl.add(c_lat)
    if c_long:
        excl.add(c_long)
    tmp  = df.drop(columns=[c for c in (c_lat, c_long) if c], errors="ignore")
    cont, disc = _clasificar_columnas(tmp, "epoch")
    cont = [c for c in cont if c not in excl]
    disc = [c for c in disc if c not in excl]
    if "long_deg" in df.columns:
        cont = ["long_deg"] + cont
    if "lat_deg"  in df.columns:
        cont = ["lat_deg"]  + cont
    seen = set()
    cont = [c for c in cont if not (c in seen or seen.add(c))]
    seen = set()
    disc = [c for c in disc if not (c in seen or seen.add(c))]

    print(f"    VBOX: {len(cols)} columnas | time='{c_time}' "
          f"lat='{c_lat}' long='{c_long}' | {len(cont)} cont + {len(disc)} disc")
    return _preparar(df[["epoch"] + cont + disc].copy(), "vbox", cont, disc)

# ============================ LECTORES VIDEO ============================

def leer_facial(path):
    df = pd.read_csv(path).rename(columns={"EpochTime": "epoch"})
    if "epoch" not in df.columns:
        raise ValueError("CSV facial sin EpochTime")
    df["epoch"] = pd.to_numeric(df["epoch"], errors="coerce")
    if "Face_Detected" in df.columns:
        fd = pd.to_numeric(df["Face_Detected"], errors="coerce").fillna(0)
        df = df[fd > 0].copy()
    cont = [c for c in ["Blink_Rate", "Head_Yaw", "Head_Pitch", "Head_Roll",
                        "EAR_Value", "MAR_Value", "Mouth_Area"] if c in df.columns]
    disc = [c for c in ["Face_Detected", "Blink_Count", "Yawn_Count",
                        "Attention_Level", "Head_Direction", "Drowsy", "Yawning",
                        "Is_Speaking", "Fatigue_Level", "Blinks_Last_5min",
                        "Yawns_Last_5min", "Is_Calibrated", "Pose_Calibrated"]
            if c in df.columns]
    for c in cont:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["Blink_Count", "Yawn_Count", "Face_Detected", "Drowsy", "Yawning",
              "Is_Speaking", "Is_Calibrated", "Pose_Calibrated",
              "Blinks_Last_5min", "Yawns_Last_5min"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return _preparar(df[["epoch"] + cont + disc].copy(), "face", cont, disc)


def leer_carril(path):
    df = pd.read_csv(path).rename(columns={"EpochTime": "epoch"})
    if "epoch" not in df.columns:
        raise ValueError("CSV carril sin EpochTime")
    df["epoch"] = pd.to_numeric(df["epoch"], errors="coerce")
    cont = [c for c in ["Min_Left_Dist_px", "Min_Right_Dist_px",
                        "Left_Lines_Count", "Right_Lines_Count"] if c in df.columns]
    disc = [c for c in ["Lane_State", "Center_Crossings", "Lane_Change_Active",
                        "Lane_Change_Direction", "Lane_Departure_Warning",
                        "Scenario_Label"] if c in df.columns]
    for c in cont:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return _preparar(df[["epoch"] + cont + disc].copy(), "lane", cont, disc)

# ============================ ALINEACIÓN ============================

def _preparar(df, pref, cont, disc):
    df  = df.dropna(subset=["epoch"]).sort_values("epoch")
    if df["epoch"].duplicated().any():
        agg = {c: "mean"  for c in cont}
        agg.update({c: "first" for c in disc})
        df = df.groupby("epoch", as_index=False).agg(agg)
    d   = np.diff(df["epoch"].values)
    per = float(np.median(d)) if len(d) else 1.0
    ren = {c: f"{pref}_{c}" for c in cont + disc}
    df  = df.rename(columns=ren)
    return (df.set_index("epoch"),
            [ren[c] for c in cont],
            [ren[c] for c in disc],
            per)


# ── OPTIMIZACIÓN 4: alinear() completamente vectorizado ──────────────────────
def alinear(df, cont, disc, per, grid, pref):
    x        = df.index.values.astype(float)
    prev     = np.searchsorted(x, grid, side="right") - 1
    valido_p = prev >= 0
    pi       = np.clip(prev, 0, len(x) - 1)
    edad     = np.where(valido_p, grid - x[pi], np.inf)
    mala     = (~valido_p) | (edad > TOL_HUECO * per) | (grid > x[-1] + TOL_HUECO * per)

    # ── continuas: fancy-index vectorizado (todas de golpe) ──────────────────
    if cont:
        vals_c  = df[cont].values.astype(float)   # (N_sensor, n_cont)
        out_c   = vals_c[pi]                       # (N_grid,   n_cont)
        out_c[mala] = np.nan
    else:
        out_c = np.empty((len(grid), 0))

    # ── discretas: fancy-index vectorizado ───────────────────────────────────
    if disc:
        vals_d  = df[disc].values                  # object array
        out_d   = vals_d[pi]
        out_d[mala] = np.nan
    else:
        out_d = np.empty((len(grid), 0), dtype=object)

    data = {}
    for j, c in enumerate(cont):
        data[c] = out_c[:, j]
    for j, c in enumerate(disc):
        data[c] = out_d[:, j]
    data[f"{pref}_valido"] = (~mala).astype(np.int8)

    return pd.DataFrame(data, index=grid)


def validar_sesion(sensores):
    print("\n    ventana temporal:")
    for n, (df, *_) in sensores.items():
        a, b = df.index[0], df.index[-1]
        fa = dt.datetime.fromtimestamp(a, dt.timezone.utc)
        fb = dt.datetime.fromtimestamp(b, dt.timezone.utc)
        print(f"      {n:6s} {fa:%H:%M:%S}..{fb:%H:%M:%S} UTC ({b-a:.1f}s)")
    ini = max(df.index[0] for df, *_ in sensores.values())
    fin = min(df.index[-1] for df, *_ in sensores.values())
    if fin <= ini:
        print("\n" + "!" * 60)
        print("ERROR: los sensores NO SE SOLAPAN. ¿Participantes distintos?")
        print("!" * 60)
        sys.exit(1)
    print(f"    solape comun: {fin - ini:.1f}s")
    return ini, fin

# ============================ SELECTOR ============================

def seleccionar_archivos():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
    except Exception as e:
        print(f"Sin GUI ({e}) -> linea de comandos.")
        return _cli()
    print("\n" + "=" * 60)
    print("SELECCION (Cancelar = omitir). Todos del MISMO participante.")
    print("=" * 60)
    rutas = {}
    for k, titulo, tipos in [
        ("ecg",    "CSV de ECG",    [("CSV",  "*.csv")]),
        ("eeg",    "CSV de EEG",    [("CSV",  "*.csv")]),
        ("vbox",   "Archivo VBOX",  [("VBOX", "*.vbo")]),
        ("lidar",  "PCAP del LiDAR",[("PCAP", "*.pcap")]),
        ("facial", "CSV facial",    [("CSV",  "*.csv")]),
        ("carril", "CSV de carril", [("CSV",  "*.csv")]),
    ]:
        p = filedialog.askopenfilename(title=f"{titulo} (Cancelar=omitir)",
                                       filetypes=tipos + [("Todos", "*.*")])
        if p:
            rutas[k] = p; print(f"  [USAR]   {k:6s} {os.path.basename(p)}")
        else:
            print(f"  [OMITIR] {k:6s}")
    if "lidar" in rutas and MODO_LIDAR != "indice":
        if os.path.exists(CALIB_XML_DEFAULT):
            rutas["calib"] = CALIB_XML_DEFAULT
            print(f"  [CALIB]  {CALIB_XML_DEFAULT}")
        else:
            p = filedialog.askopenfilename(title="Calibracion dbkaren.xml",
                                           filetypes=[("XML", "*.xml"), ("Todos", "*.*")])
            if p:
                rutas["calib"] = p; print(f"  [USAR]   calib {os.path.basename(p)}")
            else:
                print("  [OMITIR] calib -> respaldo sin corr. rango")
    rutas["salida"] = filedialog.askdirectory(title="Carpeta de salida") or os.getcwd()
    print(f"  [SALIDA] {rutas['salida']}")
    if not any(k in rutas for k in ("ecg", "eeg", "vbox", "lidar", "facial", "carril")):
        messagebox.showerror("Fusion", "No seleccionaste ningun sensor.")
        sys.exit(1)
    root.destroy()
    return rutas


def _cli():
    import argparse
    ap = argparse.ArgumentParser()
    for a in ("ecg", "eeg", "vbox", "lidar", "calib", "facial", "carril"):
        ap.add_argument(f"--{a}")
    ap.add_argument("--salida", default=os.getcwd())
    a = ap.parse_args()
    r = {k: v for k, v in vars(a).items() if v}
    if "lidar" in r and "calib" not in r and os.path.exists(CALIB_XML_DEFAULT):
        r["calib"] = CALIB_XML_DEFAULT
    if not any(k in r for k in ("ecg", "eeg", "vbox", "lidar", "facial", "carril")):
        ap.error("Indica al menos un sensor")
    return r

# ============================ PIPELINE ============================

def main():
    rutas = seleccionar_archivos()
    out   = rutas["salida"]
    os.makedirs(out, exist_ok=True)
    base  = os.path.basename(rutas.get("ecg") or rutas.get("lidar") or "sesion")
    part  = re.split(r"[_.]", base)[0]

    print("\n" + "=" * 60 + f"\nFUSION MULTI-SENSOR (participante: {part})\n" + "=" * 60)
    sensores = {}
    if "ecg"    in rutas: sensores["ecg"]  = leer_ecg(rutas["ecg"])
    if "eeg"    in rutas: sensores["eeg"]  = leer_eeg(rutas["eeg"])
    if "vbox"   in rutas: sensores["vbox"] = leer_vbo(rutas["vbox"])
    if "facial" in rutas: sensores["face"] = leer_facial(rutas["facial"])
    if "carril" in rutas: sensores["lane"] = leer_carril(rutas["carril"])

    pose = None
    if "vbox" in sensores and DESKEW:
        print("\nPose del vehiculo (VBOX)...")
        pose = construir_pose(sensores["vbox"][0])

    if "lidar" in rutas:
        print(f"\nLiDAR (modo={MODO_LIDAR}, deskew={DESKEW and pose is not None})")
        sensores["lidar"] = leer_lidar(rutas["lidar"], rutas.get("calib"),
                                       MODO_LIDAR, out, pose, part)

    print("\nValidando sesion...")
    t_ini, t_fin = validar_sesion(sensores)
    if VENTANA == "union":
        t_ini = min(df.index[0] for df, *_ in sensores.values())
        t_fin = max(df.index[-1] for df, *_ in sensores.values())

    if isinstance(F_REJILLA, str) and F_REJILLA.lower() == "auto":
        f_por_sensor = {nom: 1.0 / per for nom, (_, _, _, per) in sensores.items()}
        nom_rapido   = max(f_por_sensor, key=f_por_sensor.get)
        f_rej = min(round(f_por_sensor[nom_rapido]), F_REJILLA_MAX)
        print(f"\nRejilla AUTO = {f_rej:g} Hz (sensor más rápido: {nom_rapido} "
              f"@ {f_por_sensor[nom_rapido]:.1f} Hz)")
    else:
        f_rej = float(F_REJILLA)
    dt_grid = 1.0 / f_rej

    n    = int(np.floor((t_fin - t_ini) / dt_grid)) + 1
    grid = t_ini + np.arange(n) * dt_grid
    print(f"Rejilla @ {f_rej:g} Hz ({VENTANA}): {n:,} filas, {t_fin-t_ini:.1f}s")
    print(f"metodo continuas: {'ZOH causal' if METODO=='zoh' else 'interp lineal'} "
          f"| discretas: ZOH causal")

    partes = []
    for nom, (df, cont, disc, per) in sensores.items():
        print(f"    {nom:6s} {1/per:8.2f} Hz -> {len(cont)} cont + {len(disc)} disc")
        partes.append(alinear(df, cont, disc, per, grid, nom))

    fus = pd.concat(partes, axis=1)
    fus.insert(0, "unix_epoch",       grid)
    ts = pd.to_datetime(grid, unit="s", utc=True)
    fus.insert(1, "timestamp_utc",
               ts.strftime("%Y-%m-%dT%H:%M:%S.%f").str[:-3] + "Z")
    fus.insert(2, "timestamp_local",
               ts.tz_convert(ZONA_LOCAL).strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3])
    fus.insert(3, "t_rel_s", np.round(grid - grid[0], 6))
    fus = fus.reset_index(drop=True)

    print("\nValidacion:")
    for nom in sensores:
        cols = [c for c in fus.columns
                if c.startswith(nom + "_") and not c.endswith("_valido")]
        print(f"    {nom:6s} NaN={fus[cols].isna().mean().mean()*100:5.2f}% "
              f"cobertura={fus[nom+'_valido'].mean()*100:6.2f}%")

    muertos = [c for c in fus.columns
               if not c.endswith("_valido") and c != "timestamp_local"
               and fus[c].isna().all()]
    if muertos:
        print(f"    !! {len(muertos)} canal(es) 100% vacio(s): {', '.join(muertos)}")

    csv = os.path.join(out, f"fusion_{part}_{int(round(f_rej))}Hz_{VENTANA}.csv")

    # ── OPTIMIZACIÓN 5: float_format en lugar de round() + to_csv ────────────
    fus.to_csv(csv, index=False, float_format=f"%.{DECIMALES}f")

    print(f"\n    nivel 1 -> {csv}")
    print(f"              {len(fus):,} filas x {len(fus.columns)} col "
          f"({os.path.getsize(csv)/1e6:.1f} MB)")

    dp = os.path.join(out, "LIDAR_PCAP")
    if os.path.isdir(dp):
        fs = [f for f in os.listdir(dp) if f.endswith(".pcap")]
        gb = sum(os.path.getsize(os.path.join(dp, f)) for f in fs) / 1e9
        print(f"    nivel 2a -> {dp} ({len(fs):,} pcap, {gb:.2f} GB)")
    zp = os.path.join(out, f"LIDAR_CSV_{part}.zip")
    if os.path.exists(zp):
        print(f"    nivel 2b -> {zp} ({os.path.getsize(zp)/1e9:.2f} GB)")

    with open(csv.replace(".csv", ".README.txt"), "w", encoding="utf-8") as f:
        f.write(f"Fusion multi-sensor | participante {part}\n")
        f.write(f"Generado {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n")
        f.write(f"Rejilla {f_rej:g} Hz | ventana {VENTANA} | metodo {METODO}\n")
        f.write(f"Epoch {t_ini:.4f}..{t_fin:.4f} | {len(fus):,} filas\n")
        f.write(f"LiDAR {MODO_LIDAR} | deskew {DESKEW and pose is not None} | "
                f"corr_dist {APLICAR_CORR_DISTANCIA} | corr_int {APLICAR_CORR_INTENSIDAD}\n\n")
        for k in ("ecg", "eeg", "vbox", "lidar", "calib", "facial", "carril"):
            f.write(f"  {k:6s}: {rutas.get(k, '(omitido)')}\n")
        f.write("\nPCAP crudo (VeloView, cargar dbkaren.xml) != CSV calibrado+deskew.\n")
        f.write("lidar_frame_id = clave foranea al nivel 2.\n")
        f.write("<sensor>_valido: 1 dato real / 0 hueco (NaN).\n")
    print("\nListo.\n")
    return fus


# ── IMPORTANTE en Windows: el guard evita que ProcessPoolExecutor
#    spawne procesos recursivos al importar el módulo ─────────────────────────
if __name__ == "__main__":
    main()