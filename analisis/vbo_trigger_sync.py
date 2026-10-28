"""
VBO Trigger Sync - Corta VBO por transiciones del Trigger y corrige el reloj
=============================================================================
Ejecutar: python vbo_trigger_sync.py

FLUJO:
  1. Selecciona el VBO original
  2. Selecciona el archivo de eventos
  3. Detecta las transiciones HIGH→LOW y LOW→HIGH del canal Trigger en el VBO
  4. Corta el VBO entre esas dos transiciones
     CASO ESPECIAL: si no hay LOW→HIGH, corta hasta el último sample del archivo
  5. Corrige el reloj: el primer sample queda en el INICIO del archivo de eventos
  6. Guarda  original_trigSync.vbo

FORMATO DE EVENTOS ACEPTADO:
  Columnas mínimas requeridas: Event, EpochTime
  Columnas opcionales ignoradas: Timestamp, Details, cualquier otra
  Ejemplo:
    Timestamp,EpochTime,Event,Details
    2026-03-06 09:51:10.205,1772812270.2055,INICIO,comentario opcional
    2026-03-06 10:12:37.179,1772813557.1790,FIN,comentario opcional

CRUCE DE MEDIANOCHE (v2):
  - Si la sesión cruza las 00:00 UTC (p.ej. 23:51 → 00:08) los tiempos se
    "desenrollan" (+24 h) para que duraciones y comparaciones sean correctas.
  - El flanco LOW→HIGH final se elige como el que mejor coincide con la
    duración INICIO→FIN del archivo de eventos.
  - La hora escrita en el VBO se vuelve a envolver a 00:00–24:00.

PRECISIÓN:
  - EpochTime se lee con toda la precisión del float (hasta ~15 dígitos)
  - Tiempos internos y displays se muestran con 6 decimales
  - La columna 'time' del VBO de salida se escribe con 6 decimales (HHMMSS.mmmmmm)
"""

import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
import datetime
import io
import os
import csv

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

TRIGGER_THRESHOLD = 1.5
TRIGGER_CHANNEL   = "Trigger"


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades de tiempo  (6 decimales en todo)
# ─────────────────────────────────────────────────────────────────────────────

def vbo_time_to_seconds(vbo_str: str) -> float:
    """HHMMSS.mmmmmm  →  segundos desde medianoche UTC (full precision)."""
    t     = float(vbo_str)
    total = int(t)
    frac  = t - total
    hh    = total // 10000
    mm    = (total % 10000) // 100
    ss    = total % 100
    return hh * 3600 + mm * 60 + ss + frac


def _split_us(secs: float):
    """Segundos (cualquier valor) → (hh, mm, ss, us) envueltos a 24 h.
    Se redondea a microsegundos ENTEROS para evitar '60.000000'."""
    us = int(round(secs * 1_000_000)) % (86400 * 1_000_000)
    s, f = divmod(us, 1_000_000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return h, m, s, f


def seconds_to_vbo_time(secs: float) -> str:
    """Segundos UTC  →  HHMMSS.mmmmmm  (6 decimales, envuelve a 24 h)."""
    h, m, s, f = _split_us(secs)
    return f"{h:02d}{m:02d}{s:02d}.{f:06d}"


def seconds_to_hms(secs: float) -> str:
    """Segundos  →  HH:MM:SS.mmmmmm  (display, 6 decimales, envuelve a 24 h)."""
    h, m, s, f = _split_us(secs)
    return f"{h:02d}:{m:02d}:{s:02d}.{f:06d}"


def norm_half_day(x: float) -> float:
    """Normaliza una diferencia de horas al rango [-12 h, +12 h)."""
    return (x + 43200.0) % 86400.0 - 43200.0


def epoch_to_utc_seconds(epoch: float) -> float:
    """Unix Epoch  →  segundos desde medianoche UTC (full precision)."""
    return epoch % 86400.0


def epoch_to_utc_str(epoch: float) -> str:
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime('%H:%M:%S.') + f"{dt.microsecond:06d}"


def epoch_to_local_str(epoch: float) -> str:
    dt = datetime.datetime.fromtimestamp(epoch)
    return dt.strftime('%H:%M:%S.') + f"{dt.microsecond:06d}"


def local_utc_offset_hours() -> int:
    import time as _t
    now  = _t.time()
    diff = (datetime.datetime.fromtimestamp(now) -
            datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).replace(tzinfo=None)).total_seconds()
    return round(diff / 3600)


# ─────────────────────────────────────────────────────────────────────────────
# Selector de archivo
# ─────────────────────────────────────────────────────────────────────────────

def select_file(title: str, filetypes: list, initial_dir: str = None) -> Path | None:
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    kwargs = dict(title=title, filetypes=filetypes)
    if initial_dir:
        kwargs['initialdir'] = initial_dir
    fp = filedialog.askopenfilename(**kwargs)
    root.destroy()
    return Path(fp) if fp else None


# ─────────────────────────────────────────────────────────────────────────────
# Carga de eventos  (acepta Timestamp,EpochTime,Event,Details o Event,EpochTime)
# ─────────────────────────────────────────────────────────────────────────────

def load_events_file(path: Path) -> dict:
    """
    Lee un archivo de eventos y devuelve { nombre_evento: epoch_float }.
    Requiere columnas 'Event' y 'EpochTime'. Las demás se ignoran.
    """
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('latin-1')
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    result = {}
    if HAS_PANDAS:
        df = pd.read_csv(io.StringIO(text))
        df.columns = df.columns.str.strip()
        if 'Event' not in df.columns or 'EpochTime' not in df.columns:
            raise ValueError(
                f"El archivo debe tener columnas 'Event' y 'EpochTime'.\n"
                f"Columnas encontradas: {list(df.columns)}")
        for _, row in df.iterrows():
            name = str(row['Event']).strip()
            if name and name != 'nan':
                result[name] = float(row['EpochTime'])
    else:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            name = row.get('Event', '').strip()
            ep   = row.get('EpochTime', '').strip()
            if name and ep:
                result[name] = float(ep)
    return result


def print_events_table(events: dict, label: str):
    offset_h = local_utc_offset_hours()
    print(f"\n{'='*72}")
    print(f"  EVENTOS  —  {label}")
    print(f"{'='*72}")
    print(f"  Zona local: UTC{offset_h:+d}")
    print(f"  {'Evento':<22}  {'EpochTime':>18}  {'Hora LOCAL':>16}  {'Hora UTC':>16}")
    print(f"  {'─'*70}")
    for name, ep in events.items():
        print(f"  {name:<22}  {ep:>18.6f}  {epoch_to_local_str(ep):>16}  {epoch_to_utc_str(ep):>16}")


# ─────────────────────────────────────────────────────────────────────────────
# Parseo del VBO
# ─────────────────────────────────────────────────────────────────────────────

def load_vbo(vbo_path: Path) -> str:
    # latin-1 es lossless (byte→char 1:1): el encabezado se reescribe idéntico
    with open(vbo_path, 'rb') as f:
        raw = f.read()
    return raw.decode('latin-1')


def find_data_start(lines: list) -> int:
    for i, l in enumerate(lines):
        if l.strip().lower() == '[data]':
            return i
    return None


def find_trigger_column(lines: list) -> int:
    for i, l in enumerate(lines):
        if l.strip().lower() == '[column names]':
            if i + 1 < len(lines):
                parts = lines[i + 1].strip().split()
                for j, p in enumerate(parts):
                    if p.lower() == TRIGGER_CHANNEL.lower():
                        return j
            break
    # fallback: [header]
    headers = []
    in_hdr  = False
    for l in lines:
        s = l.strip()
        if s.lower() == '[header]':
            in_hdr = True; continue
        if s.startswith('[') and in_hdr:
            break
        if in_hdr and s:
            headers.append(s)
    for j, h in enumerate(headers):
        if h.lower() == TRIGGER_CHANNEL.lower():
            return j
    raise ValueError(f"Canal '{TRIGGER_CHANNEL}' no encontrado en el VBO")


# ─────────────────────────────────────────────────────────────────────────────
# Detección de transiciones del Trigger
# ─────────────────────────────────────────────────────────────────────────────

def find_trigger_transitions(data_lines: list, trig_col: int) -> list:
    transitions = []
    prev_high   = None
    prev_s      = None      # último tiempo desenrollado
    day         = 0         # nº de cruces de medianoche
    for i, line in enumerate(data_lines):
        parts = line.split()
        if len(parts) <= trig_col:
            continue
        try:
            tval   = float(parts[trig_col])
            time_s = vbo_time_to_seconds(parts[1])
        except (ValueError, IndexError):
            continue
        # Desenrollar cruce de medianoche (la hora retrocede > 12 h)
        if prev_s is not None and (time_s + day * 86400) < prev_s - 43200:
            day += 1
        time_s += day * 86400
        prev_s  = time_s
        is_high = tval > TRIGGER_THRESHOLD
        if prev_high is None:
            prev_high = is_high; continue
        if prev_high and not is_high:
            transitions.append(('HIGH_LOW', i, time_s, tval))
        elif not prev_high and is_high:
            transitions.append(('LOW_HIGH', i, time_s, tval))
        prev_high = is_high
    return transitions


# ─────────────────────────────────────────────────────────────────────────────
# Corte + corrección de reloj
# ─────────────────────────────────────────────────────────────────────────────

def cut_and_correct(
    header_block: str,
    data_lines: list,
    cut_start_idx: int,
    cut_end_idx: int,
    vbo_trigger_s: float,
    inicio_utc_s: float,
    output_path: Path,
) -> dict:
    offset_s    = norm_half_day(inicio_utc_s - vbo_trigger_s)
    slice_lines = data_lines[cut_start_idx : cut_end_idx + 1]
    corrected   = []
    errors      = 0

    print(f"\n  Offset de reloj: {offset_s:+.6f}s")
    print(f"    Trigger VBO:    {seconds_to_hms(vbo_trigger_s)}")
    print(f"    INICIO evento:  {seconds_to_hms(inicio_utc_s)}")

    for line in slice_lines:
        parts = line.split()
        if len(parts) < 2:
            corrected.append(line); continue
        try:
            new_s    = vbo_time_to_seconds(parts[1]) + offset_s
            parts[1] = seconds_to_vbo_time(new_s)
            corrected.append(' '.join(parts))
        except (ValueError, IndexError):
            corrected.append(line); errors += 1

    with open(output_path, 'w', encoding='latin-1', newline='') as f:
        f.write((header_block + '\n'.join(corrected) + '\n').replace('\n', '\r\n'))

    # Tiempos de salida desenrollados (para que la duración no salga negativa)
    first_t = last_t = None
    prev_t, day = None, 0
    for l in corrected:
        p = l.split()
        if len(p) >= 2:
            try:
                t = vbo_time_to_seconds(p[1])
            except ValueError:
                continue
            if prev_t is not None and (t + day * 86400) < prev_t - 43200:
                day += 1
            t += day * 86400
            prev_t = t
            if first_t is None: first_t = t
            last_t = t

    return {
        'offset_s':    offset_s,
        'total_lines': len(slice_lines),
        'errors':      errors,
        'first_time':  first_t,
        'last_time':   last_t,
    }


def _msgbox_error(msg: str):
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    messagebox.showerror("VBO Trigger Sync — Error", msg, parent=root)
    root.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Proceso principal
# ─────────────────────────────────────────────────────────────────────────────

def run():
    print("\n" + "=" * 72)
    print("    VBO TRIGGER SYNC")
    print("    Corta VBO por Trigger + corrige reloj  (6 decimales)")
    print("=" * 72)

    # ── 1. VBO ───────────────────────────────────────────────────────────
    print("\nPaso 1: Selecciona el archivo VBO...")
    vbo_path = select_file("Seleccionar VBO", [("VBO files", "*.vbo"), ("Todos", "*.*")])
    if not vbo_path:
        print("  Cancelado."); return
    print(f"  VBO: {vbo_path.name}")

    # ── 2. Eventos ───────────────────────────────────────────────────────
    print("\nPaso 2: Selecciona el archivo de eventos...")
    ev_dir  = vbo_path.parent.parent / 'Events'
    ini_dir = str(ev_dir) if ev_dir.exists() else str(vbo_path.parent)
    ev_path = select_file("Seleccionar archivo de eventos",
                          [("Text/CSV", "*.txt *.csv"), ("Todos", "*.*")],
                          initial_dir=ini_dir)
    if not ev_path:
        print("  Cancelado."); return
    print(f"  Eventos: {ev_path.name}")

    # ── 3. Cargar eventos ─────────────────────────────────────────────────
    try:
        events = load_events_file(ev_path)
    except Exception as e:
        _msgbox_error(f"Error leyendo eventos:\n{e}"); return

    print_events_table(events, ev_path.name)

    if 'INICIO' not in events:
        _msgbox_error("Evento 'INICIO' no encontrado."); return
    if 'FIN' not in events:
        _msgbox_error("Evento 'FIN' no encontrado."); return

    epoch_inicio = events['INICIO']
    epoch_fin    = events['FIN']
    inicio_utc_s = epoch_to_utc_seconds(epoch_inicio)
    # FIN desenrollado: inicio + (epoch_fin - epoch_inicio)  → nunca negativo
    fin_utc_s    = inicio_utc_s + (epoch_fin - epoch_inicio)

    print(f"\n  INICIO: {epoch_to_utc_str(epoch_inicio)} UTC  ({inicio_utc_s:.6f}s)")
    print(f"  FIN:    {epoch_to_utc_str(epoch_fin)} UTC  ({fin_utc_s % 86400:.6f}s)"
          + ("  [+1 día: cruza medianoche]" if fin_utc_s >= 86400 else ""))
    print(f"  Dur.:   {fin_utc_s - inicio_utc_s:.6f}s  "
          f"({(fin_utc_s - inicio_utc_s)/60:.6f} min)")

    # ── 4. Cargar VBO ─────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  ANALIZANDO VBO  ({vbo_path.name})")
    print(f"{'='*72}")
    text  = load_vbo(vbo_path).replace('\r\n', '\n').replace('\r', '\n')
    lines = text.splitlines()

    data_idx = find_data_start(lines)
    if data_idx is None:
        _msgbox_error("No se encontró [data] en el VBO."); return

    header_block = '\n'.join(lines[:data_idx + 1]) + '\n'
    data_lines   = [l for l in lines[data_idx + 1:] if l.strip()]
    print(f"  Líneas de datos: {len(data_lines):,}")
    print(f"  Rango VBO:  {data_lines[0].split()[1]}  →  {data_lines[-1].split()[1]}")

    # ── 5. Trigger ───────────────────────────────────────────────────────
    try:
        trig_col = find_trigger_column(lines)
    except ValueError as e:
        _msgbox_error(str(e)); return
    print(f"  Canal Trigger: columna {trig_col}")

    print(f"  Detectando transiciones (umbral {TRIGGER_THRESHOLD}V)...")
    transitions = find_trigger_transitions(data_lines, trig_col)

    offset_h = local_utc_offset_hours()
    print(f"\n{'─'*72}")
    print(f"  {'#':>3}  {'Tipo':<16}  {'Hora UTC':>16}  {'Hora LOCAL':>16}  {'Valor (V)':>10}")
    print(f"  {'─'*68}")
    for i, (kind, idx, ts, val) in enumerate(transitions, 1):
        tipo  = "HIGH → LOW ↓" if kind == 'HIGH_LOW' else "LOW  → HIGH ↑"
        local = seconds_to_hms((ts + offset_h * 3600) % 86400)
        print(f"  {i:>3}  {tipo:<16}  {seconds_to_hms(ts):>16}  {local:>16}  {val:>+10.6f}")
    print(f"{'─'*72}")

    if not transitions:
        _msgbox_error("No se encontraron transiciones del Trigger."); return

    first_hl = next((t for t in transitions if t[0] == 'HIGH_LOW'), None)
    dur_ev = fin_utc_s - inicio_utc_s
    # LOW→HIGH posterior al HIGH→LOW cuya duración mejor coincide con eventos
    cand_lh = [t for t in transitions
               if t[0] == 'LOW_HIGH' and first_hl and t[2] > first_hl[2]]
    first_lh = (min(cand_lh, key=lambda t: abs((t[2] - first_hl[2]) - dur_ev))
                if cand_lh else None)

    if first_hl is None:
        _msgbox_error("No se encontró HIGH→LOW."); return

    # ── Caso especial: sin LOW→HIGH → cortar hasta el fin del archivo ─────
    end_of_file_fallback = False
    if first_lh is None:
        end_of_file_fallback = True
        # Construir una transición sintética apuntando al último sample válido
        last_idx = len(data_lines) - 1
        last_parts = data_lines[last_idx].split()
        try:
            last_time_s = vbo_time_to_seconds(last_parts[1])
            while last_time_s < first_hl[2]:      # cruce de medianoche
                last_time_s += 86400
        except (ValueError, IndexError):
            last_time_s = first_hl[2]
        first_lh = ('EOF', last_idx, last_time_s, 0.0)
        print(f"\n  ⚠  LOW→HIGH no encontrado.")
        print(f"     Se usará el FIN del archivo como límite de corte.")
        print(f"     Último sample: línea {last_idx:,}   {seconds_to_hms(last_time_s)}")

    vbo_trigger_s = first_hl[2]
    dur_trigger   = first_lh[2] - first_hl[2]
    dur_eventos   = fin_utc_s   - inicio_utc_s

    end_label = "FIN archivo" if end_of_file_fallback else "LOW→HIGH   "

    print(f"\n  Rango de corte:")
    print(f"    HIGH→LOW:    línea {first_hl[1]:,}   {seconds_to_hms(first_hl[2])}")
    print(f"    {end_label}: línea {first_lh[1]:,}   {seconds_to_hms(first_lh[2])}")
    print(f"    Duración Trigger/EOF: {dur_trigger:.6f}s")
    print(f"    Duración Eventos:     {dur_eventos:.6f}s")
    print(f"    Diferencia:           {dur_trigger - dur_eventos:+.6f}s")

    if not end_of_file_fallback and abs(dur_trigger - dur_eventos) > 0.5:
        print("  ⚠  La duración del Trigger difiere >0.5 s de la de los eventos.")

    # ── 6. Confirmación ───────────────────────────────────────────────────
    extra_warning = (
        "\n⚠ LOW→HIGH no encontrado.\n   Se corta hasta el FIN del archivo.\n"
        if end_of_file_fallback else ""
    )
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    ok = messagebox.askyesno(
        "VBO Trigger Sync",
        f"VBO: {vbo_path.name}\n\n"
        f"HIGH→LOW:    {seconds_to_hms(first_hl[2])}  (línea {first_hl[1]:,})\n"
        f"{end_label}: {seconds_to_hms(first_lh[2])}  (línea {first_lh[1]:,})\n"
        f"Duración:    {dur_trigger:.6f}s\n\n"
        f"INICIO evento: {epoch_to_utc_str(epoch_inicio)} UTC\n"
        f"Corrección:    {inicio_utc_s - vbo_trigger_s:+.6f}s\n"
        f"{extra_warning}\n"
        "¿Continuar?",
        parent=root)
    root.destroy()
    if not ok:
        print("  Cancelado."); return

    # ── 7. Output path ────────────────────────────────────────────────────
    output_vbo = vbo_path.parent / f"{vbo_path.stem}_trigSync.vbo"
    if output_vbo.exists():
        root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
        over = messagebox.askyesno("Sobrescribir",
                                   f"{output_vbo.name}\n¿Sobrescribir?", parent=root)
        root.destroy()
        if not over:
            print("  Cancelado."); return

    # ── 8. Cortar y corregir ──────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  CORTANDO Y CORRIGIENDO RELOJ")
    print(f"{'='*72}")
    stats = cut_and_correct(
        header_block  = header_block,
        data_lines    = data_lines,
        cut_start_idx = first_hl[1],
        cut_end_idx   = first_lh[1],
        vbo_trigger_s = vbo_trigger_s,
        inicio_utc_s  = inicio_utc_s,
        output_path   = output_vbo,
    )

    # ── 9. Resumen ────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  RESULTADO")
    print(f"{'='*72}")
    print(f"  Líneas copiadas:  {stats['total_lines']:,}")
    print(f"  Errores de parse: {stats['errors']}")
    if stats['first_time'] is not None:
        dur_out     = stats['last_time'] - stats['first_time']
        delta_start = norm_half_day(stats['first_time'] - inicio_utc_s)
        delta_end   = norm_half_day(stats['last_time']  - fin_utc_s)
        print(f"  Inicio corregido: {seconds_to_hms(stats['first_time'])}")
        print(f"  Fin    corregido: {seconds_to_hms(stats['last_time'])}")
        print(f"  Duración:         {dur_out:.6f}s  ({dur_out/60:.6f} min)")
        print(f"\n  Verificación:")
        print(f"    Δ inicio: {delta_start:+.6f}s")
        if end_of_file_fallback:
            print(f"    Δ fin:    {delta_end:+.6f}s  (referencia: evento FIN)")
        else:
            print(f"    Δ fin:    {delta_end:+.6f}s")

    size_in  = os.path.getsize(vbo_path)    / (1024 * 1024)
    size_out = os.path.getsize(output_vbo)  / (1024 * 1024)
    print(f"\n  Entrada:   {size_in:.3f} MB")
    print(f"  Salida:    {size_out:.3f} MB  ({(1-size_out/size_in)*100:.1f}% reducción)")
    print(f"\n  Archivo: {output_vbo}")

    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    messagebox.showinfo("VBO Trigger Sync — Listo",
        f"Completado.\n\n{output_vbo.name}\n\n"
        f"Líneas: {stats['total_lines']:,}\n"
        f"Corrección: {stats['offset_s']:+.6f}s"
        + ("\n\n⚠ Sin LOW→HIGH — cortado hasta fin de archivo." if end_of_file_fallback else ""),
        parent=root)
    root.destroy()

    print(f"\n{'='*72}")
    print(f"    PROCESO COMPLETADO")
    print(f"{'='*72}")


if __name__ == "__main__":
    run()
    input("\nPresiona ENTER para cerrar...")