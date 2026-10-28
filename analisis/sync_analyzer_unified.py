"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         UNIFIED SYNCHRONIZATION & LATENCY ANALYZER  v3.2                   ║
║                                                                              ║
║  ARCHIVOS SOPORTADOS:                                                        ║
║    • Eventos      (.txt)              — REQUERIDO                            ║
║    • PCAP         (.pcap)             — opcional                             ║
║    • EEG          (.csv)              — opcional (múltiples)                 ║
║    • ECG          (.csv)              — opcional (múltiples)                 ║
║    • Video        (.mp4/.avi/.mov/.mkv)— opcional (metadatos)               ║
║    • VBox Sync    (.vbo/.csv)         — opcional (compara INICIO/FIN)        ║
║    • VBox Trigger (.vbo)              — opcional (detecta cambios de nivel)  ║
║                                                                              ║
║  SALIDA → carpeta  <sesion>/resultados/                                      ║
║  EJECUTAR:  python sync_analyzer_unified.py                                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import re
import json
import struct
import calendar
import traceback
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
import seaborn as sns

C_INICIO    = '#2E86AB'
C_FIN       = '#A23B72'
C_JITTER    = '#F18F01'
C_EXCELLENT = '#06A77D'
C_GOOD      = '#52B788'
C_WARNING   = '#F77F00'
C_ERROR     = '#D62828'
C_BG_LIGHT  = '#F8F9FA'
C_BG_DARK   = '#212529'
C_ACCENT    = '#6F2DBD'
C_VBOX      = '#0077B6'
C_PCAP      = '#6A4C93'

plt.style.use('seaborn-v0_8-whitegrid')
sns.set_palette("husl")

RESET  = '\033[0m'
BOLD   = '\033[1m'
GREEN  = '\033[92m'
YELLOW = '\033[93m'
RED    = '\033[91m'
CYAN   = '\033[96m'
BLUE   = '\033[94m'
MAGENTA= '\033[95m'
DIM    = '\033[2m'

def _col(text, *codes):
    return ''.join(codes) + str(text) + RESET

def header(text):
    bar = '═' * 70
    print(f'\n{_col(bar, CYAN, BOLD)}')
    print(f'{_col("  " + text, CYAN, BOLD)}')
    print(f'{_col(bar, CYAN, BOLD)}')

def ok(text):    print(f'  {_col("✓", GREEN, BOLD)}  {text}')
def warn(text):  print(f'  {_col("⚠", YELLOW, BOLD)}  {text}')
def err(text):   print(f'  {_col("✗", RED, BOLD)}  {text}')
def info(text):  print(f'  {_col("•", BLUE)}  {text}')
def skip(text):  print(f'  {_col("⊝", DIM)}  {_col(text, DIM)}')

def _ps_run(script: str) -> str:
    result = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True
    )
    return result.stdout.strip()

def _ps_path(p: str) -> str:
    return str(Path(p).resolve()).replace('/', '\\')

def pick_folder(title: str, initial_dir: str = '') -> Optional[str]:
    init = _ps_path(initial_dir) if initial_dir else ''
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = "{title}"
$dlg.UseDescriptionForTitle = $true
$dlg.ShowNewFolderButton = $false
{f'$dlg.SelectedPath = "{init}"' if init else ''}
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
    $dlg.SelectedPath
}}
"""
    p = _ps_run(script)
    return p if p else None

def pick_file(title: str, filters: str = '*.*', initial_dir: str = '') -> Optional[str]:
    init = _ps_path(initial_dir) if initial_dir else ''
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = "{title}"
$dlg.Filter = "{filters}"
$dlg.Multiselect = $false
$dlg.RestoreDirectory = $false
{f'$dlg.InitialDirectory = "{init}"' if init else ''}
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
    $dlg.FileName
}}
"""
    p = _ps_run(script)
    return p if p else None

def pick_files(title: str, filters: str = '*.*', initial_dir: str = '') -> List[str]:
    init = _ps_path(initial_dir) if initial_dir else ''
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = "{title}"
$dlg.Filter = "{filters}"
$dlg.Multiselect = $true
$dlg.RestoreDirectory = $false
{f'$dlg.InitialDirectory = "{init}"' if init else ''}
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
    $dlg.FileNames -join "|"
}}
"""
    raw = _ps_run(script)
    if not raw:
        return []
    return [p for p in raw.split('|') if p.strip()]

def classify_quality(diff_ms: float, jitter_ms: Optional[float] = None):
    a = abs(diff_ms)
    if jitter_ms:
        if a <= jitter_ms:       return C_EXCELLENT, 'PERFECTO'
        if a <= jitter_ms * 2:   return C_GOOD,      'BUENO'
        if a <= jitter_ms * 10:  return C_WARNING,   'ACEPTABLE'
        return C_ERROR, 'DESFASADO'
    if a < 1:    return C_EXCELLENT, 'PERFECTO'
    if a < 10:   return C_GOOD,      'BUENO'
    if a < 100:  return C_WARNING,   'ACEPTABLE'
    return C_ERROR, 'DESFASADO'

def fmt_ms(diff_ms: float) -> str:
    sign = '+' if diff_ms >= 0 else '-'
    a = abs(diff_ms)
    if a < 1:     return f'{sign}{a*1000:.1f} µs'
    if a < 1000:  return f'{sign}{a:.2f} ms'
    return f'{sign}{a/1000:.3f} s'

def read_csv_safe(path: str) -> pd.DataFrame:
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        try:
            return pd.read_csv(path, encoding=enc)
        except Exception:
            pass
    return pd.read_csv(path, sep=None, engine='python',
                       encoding='utf-8', errors='replace', on_bad_lines='skip')

def find_epoch_col(df: pd.DataFrame) -> Optional[str]:
    priority = ('unix_timestamp', 'EpochTime', 'Timestamp', 'timestamp', 'time')
    candidates = [c for c in priority if c in df.columns] + \
                 [c for c in df.columns if c not in priority]
    for col in candidates:
        try:
            v = float(pd.to_numeric(df[col].dropna().iloc[0], errors='coerce'))
            if 1_000_000_000 < v < 10_000_000_000:
                return col
        except Exception:
            pass
    return None

def vbo_hhmmss_to_utc_s(s: str) -> float:
    f = float(s)
    i = int(f)
    frac = f - i
    hh = i // 10000
    mm = (i % 10000) // 100
    ss = i % 100
    return hh * 3600 + mm * 60 + ss + frac

def utc_s_to_epoch(utc_seconds: float, ref_date_utc: str) -> float:
    midnight = datetime.strptime(ref_date_utc, '%Y-%m-%d')
    return calendar.timegm(midnight.timetuple()) + utc_seconds


def ffprobe_video_info(path: str) -> Optional[dict]:
    """
    Lee la duración REAL del video desde los metadatos del contenedor usando
    ffprobe (si está disponible en PATH). A diferencia de OpenCV
    (frames/fps, poco fiable con índices AVI incompletos o VFR), ffprobe lee
    directamente el campo `duration` del contenedor y del stream de video,
    que es la fuente más confiable de la duración real grabada.
    Devuelve None si ffprobe no está instalado o falla.
    """
    import shutil
    if shutil.which('ffprobe') is None:
        return None
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-print_format', 'json',
            '-show_format', '-show_streams',
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not result.stdout:
            return None
        data = json.loads(result.stdout)

        fmt = data.get('format', {}) or {}
        streams = data.get('streams', []) or []
        vstream = next((s for s in streams if s.get('codec_type') == 'video'), None)

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        dur_format = _f(fmt.get('duration'))
        dur_stream = _f(vstream.get('duration')) if vstream else None

        # nb_frames * (1/fps) como tercera referencia cruzada, si están disponibles
        nb_frames = None
        fps = None
        if vstream:
            nb_frames = vstream.get('nb_frames')
            try:
                nb_frames = int(nb_frames) if nb_frames is not None else None
            except (TypeError, ValueError):
                nb_frames = None
            rate_str = vstream.get('avg_frame_rate') or vstream.get('r_frame_rate')
            if rate_str and '/' in rate_str:
                num, den = rate_str.split('/')
                num, den = _f(num), _f(den)
                if num and den:
                    fps = num / den
            elif rate_str:
                fps = _f(rate_str)

        dur_frames = (nb_frames / fps) if (nb_frames and fps and fps > 0) else None

        # Preferimos duración del stream de video; si no, la del contenedor;
        # si no, la derivada de nb_frames/fps.
        duration_real = dur_stream or dur_format or dur_frames
        if duration_real is None:
            return None

        return {
            'duration_s':        duration_real,
            'duration_format_s': dur_format,
            'duration_stream_s': dur_stream,
            'duration_frames_s': dur_frames,
            'fps':               fps,
            'nb_frames':         nb_frames,
            'width':             int(vstream['width'])  if vstream and vstream.get('width')  else None,
            'height':            int(vstream['height']) if vstream and vstream.get('height') else None,
            'codec':             vstream.get('codec_name') if vstream else None,
            'bit_rate':          _f(fmt.get('bit_rate')),
            'format_name':       fmt.get('format_name'),
        }
    except Exception:
        return None


class SyncAnalyzer:

    def __init__(self):
        self.events_file:         Optional[str]       = None
        self.pcap_file:           Optional[str]       = None
        self.eeg_files:           Dict[str, str]      = {}
        self.ecg_files:           Dict[str, str]      = {}
        self.video_files:         Dict[str, str]      = {}
        self.vbox_sync_file:      Optional[str]       = None
        self.vbox_trigger_file:   Optional[str]       = None

        self.events_df:    Optional[pd.DataFrame] = None
        self.events_dict:  Dict[str, float]       = {}
        self.start_time:   Optional[float]        = None
        self.end_time:     Optional[float]        = None
        self.system_jitter: Optional[float]       = None

        self.file_endpoints:  Dict[str, Tuple[float, float]] = {}
        self.differences:     Dict[str, dict]                = {}
        self.latency_details: Dict[str, List[dict]]          = {}
        self.advanced_stats:  Dict[str, dict]                = {}
        self.trigger_transitions: List[dict]                 = []
        self.video_stats:      Dict[str, dict]                = {}

        self.pcap_stats:       dict                          = {}
        self.csv_source_stats: Dict[str, dict]               = {}
        self.vbox_sync_stats:  dict                          = {}
        self.vbox_trigger_stats: dict                        = {}

    @property
    def results_folder(self) -> Optional[str]:
        if self.events_file is None:
            return None
        folder = Path(self.events_file).parent.parent / 'resultados'
        if folder.exists():
            import shutil
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        return str(folder)

    def select_files(self) -> bool:
        header('SELECCIÓN DE ARCHIVOS')

        print(f'\n{_col("[0/7] Carpeta de SESIÓN — REQUERIDA", BOLD)}')
        session_dir = pick_folder('Selecciona la carpeta de sesión')
        if not session_dir:
            err('Cancelado — carpeta de sesión obligatoria.')
            return False
        base = Path(session_dir)
        ok(base.name)

        print(f'\n{_col("[1/7] Archivo de EVENTOS (.txt) — REQUERIDO", BOLD)}')
        events_init = str(base / 'Events') if (base / 'Events').exists() else str(base)
        p = pick_file('Archivo de Eventos (requerido)',
                    'Text files (*.txt)|*.txt|All files (*.*)|*.*',
                    events_init)
        if not p:
            err('Cancelado — archivo de eventos obligatorio.')
            return False
        self.events_file = p
        ok(Path(p).name)

        def _ask(step, label, sub, filters, attr=None):
            candidate = base / sub
            init = str(candidate) if candidate.exists() else str(base)
            chosen = pick_file(f'[{step}] {label} — Cancelar para omitir', filters, init)
            if chosen:
                ok(f'{label}: {Path(chosen).name}')
                if attr:
                    setattr(self, attr, chosen)
            else:
                skip(f'{label} omitido')
            return chosen

        def _ask_multi(step, label, sub, filters):
            candidate = base / sub
            init = str(candidate) if candidate.exists() else str(base)
            chosen = pick_files(f'[{step}] {label} — Cancelar para omitir', filters, init)
            if chosen:
                ok(f'{label}: {len(chosen)} archivo(s)')
            else:
                skip(f'{label} omitido')
            return chosen or []

        print(f'\n{_col("[2/7] PCAP (.pcap) — opcional", BOLD)}')
        _ask('2/7', 'PCAP', 'LIDAR',
            'PCAP files (*.pcap)|*.pcap|All files (*.*)|*.*', 'pcap_file')

        print(f'\n{_col("[3/7] EEG (.csv) — opcional, múltiples", BOLD)}')
        for i, fp in enumerate(_ask_multi('3/7', 'EEG', 'EEG',
                                        'CSV files (*.csv)|*.csv|All files (*.*)|*.*')):
            self.eeg_files[self._csv_key(fp, 'eeg', i)] = fp

        print(f'\n{_col("[4/7] ECG (.csv) — opcional, múltiples", BOLD)}')
        for i, fp in enumerate(_ask_multi('4/7', 'ECG', 'ECG',
                                        'CSV files (*.csv)|*.csv|All files (*.*)|*.*')):
            self.ecg_files[self._csv_key(fp, 'ecg', i)] = fp

        print(f'\n{_col("[5/7] Video (.mp4/.avi/.mov/.mkv) — opcional, múltiples", BOLD)}')
        for i, fp in enumerate(_ask_multi('5/7', 'Video', 'Video',
                                        'Video files (*.mp4;*.avi;*.mov;*.mkv)|*.mp4;*.avi;*.mov;*.mkv|All files (*.*)|*.*')):
            self.video_files[f'video_{i}'] = fp

        print(f'\n{_col("[6/7] VBox SINCRONIZACIÓN (.vbo/.csv) — opcional", BOLD)}')
        _ask('6/7', 'VBox Sync', '.',
            'VBO/CSV files (*.vbo;*.csv)|*.vbo;*.csv|All files (*.*)|*.*', 'vbox_sync_file')

        print(f'\n{_col("[7/7] VBox TRIGGER (.vbo) — opcional", BOLD)}')
        _ask('7/7', 'VBox Trigger', '.',
            'VBO files (*.vbo)|*.vbo|All files (*.*)|*.*', 'vbox_trigger_file')

        return True

    def _csv_key(self, path: str, kind: str, idx: int) -> str:
        name = Path(path).name.lower()
        patterns = {
            'eeg': [('raw','raw'),('motion','motion'),('power','power'),
                    ('facial','facial'),('quality','quality'),('metrics','metrics')],
            'ecg': [('filtered','filtered'),('original','original')]
        }
        for pat, label in patterns.get(kind, []):
            if pat in name:
                hz = re.search(r'_(\d+)hz', name)
                return f'{label}_{hz.group(1)}Hz' if hz else label
        return f'{kind}_{idx}'

    def load_events(self) -> bool:
        header('CARGANDO EVENTOS')
        try:
            df = read_csv_safe(self.events_file)
            df.columns = df.columns.str.strip()
            if 'Event' not in df.columns or 'EpochTime' not in df.columns:
                raise ValueError("Se requieren columnas 'Event' y 'EpochTime'")

            df['EpochTime'] = pd.to_numeric(df['EpochTime'], errors='coerce')
            df = df.dropna(subset=['EpochTime']).sort_values('EpochTime').reset_index(drop=True)
            self.events_df = df

            for _, row in df.iterrows():
                name = str(row['Event']).strip()
                t    = float(row['EpochTime'])
                self.events_dict[name] = t
                ts_str = datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')[:-3]
                print(f'  {_col("•", BLUE)}  {_col(name, BOLD):<26} {_col(f"{t:.3f}", DIM)}  '
                      f'{_col(f"({ts_str})", CYAN)}')

            for k in ('INICIO', 'START', 'start', 'inicio'):
                if k in self.events_dict:
                    self.start_time = self.events_dict[k]; break
            for k in ('FIN', 'END', 'STOP', 'fin', 'end', 'stop'):
                if k in self.events_dict:
                    self.end_time = self.events_dict[k]; break

            if self.start_time is None:  warn('No se encontró INICIO/START')
            if self.end_time   is None:  warn('No se encontró FIN/END')

            if 'INICIO_SETUP' in self.events_dict and self.start_time:
                self.system_jitter = abs(self.start_time - self.events_dict['INICIO_SETUP'])
                print(f'\n  {_col("⏱", YELLOW)}  Jitter del sistema: '
                      f'{_col(f"±{self.system_jitter*1000:.3f} ms", YELLOW, BOLD)}')
            return True
        except Exception as e:
            err(f'Error cargando eventos: {e}')
            traceback.print_exc()
            return False

    def analyze_all(self):
        header('ANALIZANDO FUENTES')
        if self.pcap_file:
            self._pcap()
        for key, path in self.eeg_files.items():
            self._csv_source(path, f'EEG_{key}', critical='raw' in key)
        for key, path in self.ecg_files.items():
            self._csv_source(path, f'ECG_{key}', critical='filtered' in key)
        for key, path in self.video_files.items():
            self._video(path, f'VIDEO_{key}')
        if self.vbox_sync_file:
            self._vbox_sync()
        if self.vbox_trigger_file:
            self._vbox_trigger()
        self._compute_diffs()

    # ── PCAP ─────────────────────────────────────────────────────────────────

    def _pcap(self):
        src = 'PCAP'
        print(f'\n  {_col(f"[{src}]", MAGENTA, BOLD)} {Path(self.pcap_file).name}')
        try:
            fsize_mb = Path(self.pcap_file).stat().st_size / 1_048_576
            fsize_bytes = Path(self.pcap_file).stat().st_size
            with open(self.pcap_file, 'rb') as f:
                magic = struct.unpack('<I', f.read(4))[0]
                if magic not in (0xA1B2C3D4, 0xD4C3B2A1):
                    err(f'Magic inválido: {magic:#010x}'); return
                _vmaj, _vmin, _tz, _sig, snaplen, network = struct.unpack('<HHiIII', f.read(20))
                first = last = None
                n = 0
                total_bytes = 0
                while True:
                    h = f.read(16)
                    if len(h) < 16: break
                    ts_s, ts_us, inc, orig = struct.unpack('<IIII', h)
                    f.read(inc)
                    ts = ts_s + ts_us / 1e6
                    if first is None: first = ts
                    last = ts
                    n += 1
                    total_bytes += orig
            if first is None:
                err('Sin paquetes válidos'); return
            dur  = last - first
            rate = n / dur if dur > 0 else float('nan')
            avg_pkt = total_bytes / n if n > 0 else 0
            link_names = {1: 'Ethernet', 105: 'IEEE 802.11 WiFi',
                          228: 'IPv4 raw', 229: 'IPv6 raw'}
            link_str = link_names.get(network, f'LinkType {network}')
            self.file_endpoints[src] = (first, last)

            ok(f'{n:,} paquetes  |  {dur:.2f} s  |  {fsize_mb:.1f} MB')
            info(f'Primer   : {datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({first:.3f})')
            info(f'Último   : {datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({last:.3f})')
            info(f'Duración : {int(dur//60)} min {dur%60:.2f} s  ({dur:.3f} s total)')
            info(f'Tasa     : {rate:.1f} pkt/s  |  Paquete medio: {avg_pkt:.0f} B')
            info(f'Enlace   : {link_str}  |  Snaplen: {snaplen} B')
            info(f'Tamaño   : {fsize_mb:.2f} MB  ({fsize_bytes:,} bytes)')

            self.pcap_stats = {
                'filename':        Path(self.pcap_file).name,
                'filepath':        self.pcap_file,
                'packets':         n,
                'duration_s':      dur,
                'duration_min':    int(dur // 60),
                'duration_sec':    dur % 60,
                'first_epoch':     first,
                'last_epoch':      last,
                'first_local':     datetime.fromtimestamp(first).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'last_local':      datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'rate_pkt_s':      rate,
                'avg_pkt_bytes':   avg_pkt,
                'total_bytes_cap': total_bytes,
                'link_type':       link_str,
                'link_type_id':    network,
                'snaplen_bytes':   snaplen,
                'file_size_mb':    fsize_mb,
                'file_size_bytes': fsize_bytes,
                'version_major':   _vmaj,
                'version_minor':   _vmin,
            }
        except Exception as e:
            err(str(e))

    # ── CSV (EEG / ECG) ──────────────────────────────────────────────────────

    def _csv_source(self, path: str, label: str, critical: bool = False):
        star = _col(' ★', YELLOW, BOLD) if critical else ''
        print(f'\n  {_col(f"[{label}]", BLUE, BOLD)}{star} {Path(path).name}')
        try:
            df = read_csv_safe(path)
            col = find_epoch_col(df)
            if col is None:
                err('Sin columna de timestamp UNIX'); return

            df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna(subset=[col]).sort_values(col).reset_index(drop=True)
            if df.empty:
                err('Sin datos válidos'); return

            first, last = float(df[col].iloc[0]), float(df[col].iloc[-1])
            self.file_endpoints[label] = (first, last)
            dur  = last - first
            rate = (len(df) - 1) / dur if dur > 0 else float('nan')
            fsize_kb    = Path(path).stat().st_size / 1024
            fsize_bytes = Path(path).stat().st_size
            n_records   = len(df)
            n_cols      = len(df.columns)
            col_names   = df.columns.tolist()

            ok(f'{n_records:,} registros  |  {dur:.2f} s  |  Fs≈{rate:.1f} Hz')
            info(f'Columna ts : {col}')
            info(f'Primer     : {datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({first:.3f})')
            info(f'Último     : {datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({last:.3f})')
            info(f'Duración   : {int(dur//60)} min {dur%60:.2f} s  ({dur:.3f} s total)')
            info(f'Columnas   : {n_cols}  → {", ".join(col_names[:6])}{"…" if n_cols > 6 else ""}')
            info(f'Tamaño     : {fsize_kb:.1f} KB  ({fsize_kb/1024:.2f} MB)')

            stats_entry = {
                'filename':        Path(path).name,
                'filepath':        path,
                'label':           label,
                'critical':        critical,
                'timestamp_col':   col,
                'records':         n_records,
                'duration_s':      dur,
                'duration_min':    int(dur // 60),
                'duration_sec':    dur % 60,
                'sample_rate_hz':  rate,
                'first_epoch':     first,
                'last_epoch':      last,
                'first_local':     datetime.fromtimestamp(first).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'last_local':      datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'n_columns':       n_cols,
                'columns':         col_names,
                'file_size_kb':    fsize_kb,
                'file_size_mb':    fsize_kb / 1024,
                'file_size_bytes': fsize_bytes,
            }

            if self.events_df is not None:
                self.latency_details[label] = [
                    {'event': str(row['Event']),
                     'diff_ms': (float(df.loc[(df[col] - float(row['EpochTime'])).abs().idxmin(), col])
                                 - float(row['EpochTime'])) * 1000}
                    for _, row in self.events_df.iterrows()
                ]

            if critical:
                adv = {}
                if len(df) > 10:
                    jitter_vals = np.diff(df[col].values)
                    jitter_ms   = float(np.std(jitter_vals) * 1000)
                    jitter_mean = float(np.mean(jitter_vals) * 1000)
                    jitter_max  = float(np.max(np.abs(jitter_vals)) * 1000)
                    adv['jitter_muestreo_std_ms']  = jitter_ms
                    adv['jitter_muestreo_mean_ms'] = jitter_mean
                    adv['jitter_muestreo_max_ms']  = jitter_max
                    info(f'Jitter ts  : std={jitter_ms:.3f} ms  |  media={jitter_mean:.3f} ms  |  max={jitter_max:.3f} ms')

                val_col = next((c for c in ('Value','value','amplitude','data')
                                if c in df.columns), None)
                if val_col:
                    sig = df[val_col].dropna().astype(float).values
                    if 'EEG' in label:
                        adv['signal_col'] = val_col
                        adv['media']      = float(np.mean(sig))
                        adv['std']        = float(np.std(sig))
                        adv['min']        = float(np.min(sig))
                        adv['max']        = float(np.max(sig))
                        adv['n_samples']  = len(sig)
                        info(f'Señal      : media={adv["media"]:.3f}  std={adv["std"]:.3f}  '
                             f'min={adv["min"]:.3f}  max={adv["max"]:.3f}')
                    if 'ECG' in label and len(sig) > 0:
                        thr   = np.mean(sig) + 1.5 * np.std(sig)
                        peaks = np.where(sig > thr)[0]
                        adv['signal_col']    = val_col
                        adv['ecg_threshold'] = float(thr)
                        adv['n_peaks']       = len(peaks)
                        if len(peaks) > 1:
                            ivals = np.diff(df[col].values[peaks])
                            mu = np.mean(ivals)
                            if mu > 0:
                                adv['fc_bpm']            = float(60.0 / mu)
                                adv['rr_mean_s']         = float(mu)
                                adv['rr_std_s']          = float(np.std(ivals))
                                adv['rr_min_s']          = float(np.min(ivals))
                                adv['rr_max_s']          = float(np.max(ivals))
                                info(f'FC estimada: {adv["fc_bpm"]:.1f} bpm')
                if adv:
                    self.advanced_stats[label] = adv
                    stats_entry['advanced'] = adv

            self.csv_source_stats[label] = stats_entry

        except Exception as e:
            err(str(e))
            traceback.print_exc()

    # ── Video ─────────────────────────────────────────────────────────────────

    def _video(self, path: str, label: str):
        print(f'\n  {_col(f"[{label}]", GREEN, BOLD)} {Path(path).name}')
        try:
            ct = os.path.getctime(path)
            mt = os.path.getmtime(path)
            dur_fs = mt - ct          # duración filesystem: siempre disponible
            fsize_mb    = Path(path).stat().st_size / 1_048_576
            fsize_bytes = Path(path).stat().st_size

            # ── 1) OpenCV: fps, frames, resolución, y duración frames/fps ──────
            fps = float('nan')
            total_frames = 0
            width = 0
            height = 0
            duration_opencv = None
            cv2_available = False

            try:
                import cv2
                cap = cv2.VideoCapture(path)
                if not cap.isOpened():
                    raise ValueError('No se pudo abrir el video con OpenCV')

                fps            = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                total_frames   = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                width          = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                height         = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                duration_opencv = (total_frames / fps) if fps > 0 else None
                cap.release()
                cv2_available  = True
            except Exception as cv_err:
                info(f'OpenCV      : no disponible o no pudo leer metadatos ({cv_err})')

            # ── 2) ffprobe: duración REAL leída de los metadatos del contenedor ─
            # Esta es la fuente más confiable de la duración real grabada,
            # ya que lee directamente el campo `duration` del stream/contenedor
            # en vez de derivarla de frames/fps (que falla con VFR o índices
            # incompletos) o de fechas de archivo (que fallan si el archivo
            # fue copiado/movido, lo que altera ctime/mtime).
            ffp = ffprobe_video_info(path)
            duration_ffprobe = ffp['duration_s'] if ffp else None
            if ffp:
                if not width  and ffp.get('width'):  width  = ffp['width']
                if not height and ffp.get('height'): height = ffp['height']
                if (np.isnan(fps) or fps == 0) and ffp.get('fps'):
                    fps = ffp['fps']
                if not total_frames and ffp.get('nb_frames'):
                    total_frames = ffp['nb_frames']
            else:
                info('ffprobe     : no disponible o no pudo leer metadatos')

            # ── 3) Duración REAL efectiva ────────────────────────────────────
            # Prioridad: ffprobe (metadata real) > OpenCV (frames/fps) >
            # filesystem (mtime-ctime, solo como último recurso — no es una
            # medida real de duración, es aproximada y se ve afectada por
            # copias/transferencias que actualizan mtime/ctime).
            if duration_ffprobe is not None and duration_ffprobe > 0:
                duration_real   = duration_ffprobe
                duration_source = 'ffprobe (metadatos reales del contenedor)'
            elif duration_opencv is not None and duration_opencv > 0:
                duration_real   = duration_opencv
                duration_source = 'opencv (frames/fps)'
            else:
                duration_real   = dur_fs
                duration_source = 'filesystem (mtime-ctime) — sin metadatos disponibles'

            # Discrepancia notable entre la duración real (metadata) y el
            # tiempo de archivo (fs). Solo se anota como advertencia; ya NO se
            # descarta la duración real por defecto.
            fs_discrepancy = abs(duration_real - dur_fs) > 5.0
            if fs_discrepancy:
                warn(
                    f'Discrepancia metadata vs filesystem: '
                    f'real={duration_real:.2f} s  vs  mtime-ctime={dur_fs:.2f} s  '
                    f'(fuente real: {duration_source})'
                )

            # duration_s = duración usada para el análisis de sincronización.
            # Se usa la duración REAL (metadata) siempre que esté disponible;
            # el endpoint final ahora refleja el fin real del video, no solo
            # el mtime del archivo.
            duration_s = duration_real

            # ── file_endpoints: inicio = ctime, fin = ctime + duración real ────
            end_real = ct + duration_real
            self.file_endpoints[label] = (ct, end_real)

            ok(
                f'Duración REAL  : {int(duration_real//60)} min {duration_real%60:.2f} s '
                f'({duration_real:.3f} s)  |  {fsize_mb:.1f} MB  '
                f'{_col(f"[{duration_source}]", DIM)}'
            )
            if duration_ffprobe is not None:
                info(f'  ffprobe   : {duration_ffprobe:.3f} s'
                     + (f'  (codec={ffp.get("codec")}, '
                        f'bitrate={ffp["bit_rate"]/1000:.0f} kbps)' if ffp.get('bit_rate') else ''))
            if duration_opencv is not None:
                info(f'  OpenCV    : {duration_opencv:.3f} s  (frames/fps)')
            note = _col(f'⚠ discrepancia {abs(duration_real-dur_fs):.1f} s', YELLOW) if fs_discrepancy else 'OK'
            info(f'  Filesystem: {dur_fs:.3f} s  (mtime − ctime)  [{note}]')

            info(f'FPS         : {fps:.3f}' if not np.isnan(fps) and fps else 'FPS         : N/D')
            info(f'Frames      : {total_frames:,}' if total_frames else 'Frames      : N/D')
            info(f'Resolución  : {width}x{height}' if width and height else 'Resolución  : N/D')
            info(f'Creado      : {datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({ct:.3f})')
            info(f'Fin (real)  : {datetime.fromtimestamp(end_real).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({end_real:.3f})')
            info(f'Modificado  : {datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({mt:.3f})')
            info(f'Tamaño      : {fsize_mb:.2f} MB  ({fsize_bytes:,} bytes)')
            info(f'Extensión   : {Path(path).suffix.upper()}')

            self.video_stats[label] = {
                'filename':          Path(path).name,
                'filepath':          path,
                'extension':         Path(path).suffix.upper(),
                'fps':               fps,
                'frames':            total_frames,
                'width':             width,
                'height':            height,
                'resolution':        f'{width}x{height}' if width and height else 'N/D',
                # Duración real leída de metadatos (ffprobe) — la más confiable
                'duration_ffprobe_s': duration_ffprobe,
                # Duración según OpenCV (frames/fps)
                'duration_video_s':  duration_opencv,
                # Duración filesystem mtime-ctime (siempre disponible, aproximada)
                'duration_fs_s':     dur_fs,
                # Duración REAL elegida (prioridad ffprobe > opencv > filesystem)
                'duration_real_s':   duration_real,
                # Alias retrocompatible: duración efectiva usada para sincronización
                'duration_s':        duration_s,
                'duration_source':   duration_source,
                'fs_discrepancy':    fs_discrepancy,
                'fs_discrepancy_s':  abs(duration_real - dur_fs),
                # Retrocompatibilidad con el nombre de campo anterior
                'cv2_truncated':     fs_discrepancy and duration_source.startswith('opencv'),
                'created_epoch':     ct,
                'modified_epoch':    mt,
                'end_real_epoch':    end_real,
                'created_local':     datetime.fromtimestamp(ct).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'modified_local':    datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'end_real_local':    datetime.fromtimestamp(end_real).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'size_mb':           fsize_mb,
                'size_bytes':        fsize_bytes,
                'cv2_used':          cv2_available,
                'ffprobe_used':      ffp is not None,
                'codec':             ffp.get('codec') if ffp else None,
                'bit_rate_bps':      ffp.get('bit_rate') if ffp else None,
                'timestamps_type':   'metadata real (ffprobe/opencv) + filesystem como referencia',
            }
        except Exception as e:
            err(str(e))

    @staticmethod
    def _video_best_duration_label(v: dict) -> str:
        """Etiqueta corta 'X.XXX s [fuente]' para tablas del reporte."""
        real = v.get('duration_real_s', v.get('duration_s'))
        src  = v.get('duration_source', '?')
        tag  = ('ffprobe' if 'ffprobe' in src else
                'opencv'  if 'opencv'  in src else
                'fs')
        return f'{real:.3f} s [{tag}]'

    def _video_report_lines(self, v: dict, lbl: str) -> List[str]:
        """Bloque de líneas de texto para la sección VIDEO del reporte,
        mostrando la duración real (metadata) junto con las demás fuentes."""
        fps_txt = (f'{v["fps"]:.4f}'
                   if isinstance(v.get('fps'), (int, float)) and not np.isnan(v['fps']) and v['fps']
                   else 'N/D')

        lines = [
            f'\n  [{lbl}]',
            f'  Archivo             : {v["filename"]}',
            f'  Ruta                : {v["filepath"]}',
            f'  Extensión           : {v["extension"]}',
            f'  Codec               : {v.get("codec") or "N/D"}',
            f'  Resolución          : {v["resolution"]}',
            f'  FPS                 : {fps_txt}',
            f'  Frames totales      : {v["frames"]:,}' if v["frames"] else '  Frames totales      : N/D',
            '',
            f'  ── Duración (todas las fuentes) ──',
        ]
        if v.get('duration_ffprobe_s') is not None:
            d = v['duration_ffprobe_s']
            lines.append(f'  Duración ffprobe    : {d:.3f} s  ({int(d//60)} min {d%60:.2f} s)  [metadatos reales del contenedor]')
        else:
            lines.append('  Duración ffprobe    : N/D (ffprobe no disponible o sin metadatos)')

        if v.get('duration_video_s') is not None:
            d = v['duration_video_s']
            lines.append(f'  Duración OpenCV     : {d:.3f} s  ({int(d//60)} min {d%60:.2f} s)  [frames/fps]')
        else:
            lines.append('  Duración OpenCV     : N/D')

        dfs = v['duration_fs_s']
        lines.append(f'  Duración filesystem : {dfs:.3f} s  ({int(dfs//60)} min {dfs%60:.2f} s)  [mtime − ctime, aproximada]')

        dreal = v.get('duration_real_s', v.get('duration_s'))
        lines += [
            f'  → Duración REAL usada : {dreal:.3f} s  ({int(dreal//60)} min {dreal%60:.2f} s)',
            f'    Fuente              : {v.get("duration_source", "?")}',
        ]
        if v.get('fs_discrepancy'):
            lines.append(
                f'    ⚠ Discrepancia vs filesystem: {v.get("fs_discrepancy_s", 0):.2f} s '
                f'(puede deberse a copia/transferencia del archivo, que altera mtime/ctime, '
                f'o a un índice de contenedor incompleto)')

        lines += [
            '',
            f'  Creado              : epoch={v["created_epoch"]:.6f}  local={v["created_local"]}',
            f'  Fin real (metadata) : epoch={v["end_real_epoch"]:.6f}  local={v["end_real_local"]}',
            f'  Modificado (fs)     : epoch={v["modified_epoch"]:.6f}  local={v["modified_local"]}',
            f'  Tamaño              : {v["size_mb"]:.3f} MB  ({v["size_bytes"]:,} bytes)',
            f'  Bitrate             : {v["bit_rate_bps"]/1000:.0f} kbps' if v.get('bit_rate_bps') else '  Bitrate             : N/D',
            f'  ffprobe usado       : {"Sí" if v.get("ffprobe_used") else "No"}',
            f'  OpenCV usado        : {"Sí" if v["cv2_used"] else "No"}',
            f'  Nota                : {v["timestamps_type"]}',
        ]
        return lines

    # ── VBox Sync ─────────────────────────────────────────────────────────────

    def _vbox_sync(self):
        label = 'VBOX_SYNC'
        print(f'\n  {_col(f"[{label}]", CYAN, BOLD)} {Path(self.vbox_sync_file).name}')
        try:
            first, last, n = self._vbo_endpoints(self.vbox_sync_file)
            if first is None:
                err('Sin timestamps válidos'); return
            self.file_endpoints[label] = (first, last)
            dur = last - first
            rate = (n - 1) / dur if dur > 0 and n > 1 else float('nan')
            fsize_kb    = Path(self.vbox_sync_file).stat().st_size / 1024
            fsize_bytes = Path(self.vbox_sync_file).stat().st_size

            ok(f'{n:,} registros  |  {dur:.2f} s  |  Fs≈{rate:.1f} Hz')
            info(f'Primer     : {datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({first:.3f})')
            info(f'Último     : {datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}  ({last:.3f})')
            info(f'Duración   : {int(dur//60)} min {dur%60:.2f} s  ({dur:.3f} s total)')
            info(f'Tamaño     : {fsize_kb:.1f} KB  ({fsize_kb/1024:.2f} MB)')

            self.vbox_sync_stats = {
                'filename':        Path(self.vbox_sync_file).name,
                'filepath':        self.vbox_sync_file,
                'records':         n,
                'duration_s':      dur,
                'duration_min':    int(dur // 60),
                'duration_sec':    dur % 60,
                'sample_rate_hz':  rate,
                'first_epoch':     first,
                'last_epoch':      last,
                'first_local':     datetime.fromtimestamp(first).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'last_local':      datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'file_size_kb':    fsize_kb,
                'file_size_mb':    fsize_kb / 1024,
                'file_size_bytes': fsize_bytes,
            }
        except Exception as e:
            err(str(e))
            traceback.print_exc()

    # ── VBox Trigger ──────────────────────────────────────────────────────────

    def _vbox_trigger(self):
        label     = 'VBOX_TRIGGER'
        TRIG_CH   = 'Trigger'
        THRESHOLD = 1.5

        print(f'\n  {_col(f"[{label}]", YELLOW, BOLD)} {Path(self.vbox_trigger_file).name}')
        try:
            text  = self._read_vbo_text(self.vbox_trigger_file)
            lines = text.splitlines()

            headers, in_hdr = [], False
            for l in lines:
                ls = l.strip().lower()
                if ls == '[header]':              in_hdr = True;  continue
                if ls.startswith('[') and in_hdr: break
                if in_hdr and l.strip():          headers.append(l.strip())

            data_start = next((i for i, l in enumerate(lines)
                               if l.strip().lower() == '[data]'), None)
            if data_start is None:
                raise ValueError('No se encontró [data] en el VBO')

            if TRIG_CH not in headers:
                raise ValueError(
                    f"Canal '{TRIG_CH}' no encontrado.\n"
                    f"Disponibles: {', '.join(headers)}")
            trig_col = headers.index(TRIG_CH)

            ref_date = (datetime.utcfromtimestamp(self.start_time).strftime('%Y-%m-%d')
                        if self.start_time else datetime.utcnow().strftime('%Y-%m-%d'))

            samples: List[Tuple[float, float]] = []

            for raw in lines[data_start + 1:]:
                parts = raw.split()
                if len(parts) <= max(1, trig_col):
                    continue
                try:
                    utc_s    = vbo_hhmmss_to_utc_s(parts[1])
                    trig_val = float(parts[trig_col])
                except (ValueError, IndexError):
                    continue
                samples.append((utc_s_to_epoch(utc_s, ref_date), trig_val))

            if not samples:
                err('Sin muestras válidas en el canal Trigger'); return

            n_lines   = len(samples)
            first_ep  = samples[0][0]
            last_ep   = samples[-1][0]
            dur_s     = last_ep - first_ep
            rate_hz   = (n_lines - 1) / dur_s if dur_s > 0 and n_lines > 1 else float('nan')
            fsize_kb  = Path(self.vbox_trigger_file).stat().st_size / 1024
            fsize_bytes = Path(self.vbox_trigger_file).stat().st_size

            utc_ini = datetime.utcfromtimestamp(first_ep).strftime('%H:%M:%S.%f')[:-3]
            utc_fin = datetime.utcfromtimestamp(last_ep).strftime('%H:%M:%S.%f')[:-3]
            loc_ini = datetime.fromtimestamp(first_ep).strftime('%H:%M:%S.%f')[:-3]
            loc_fin = datetime.fromtimestamp(last_ep).strftime('%H:%M:%S.%f')[:-3]

            tz_offset = int(-((datetime.fromtimestamp(first_ep) -
                               datetime.utcfromtimestamp(first_ep)).total_seconds()) // 3600)

            info(f'Canal      : {TRIG_CH}  (col {trig_col})')
            info(f'Umbral     : {THRESHOLD} V  (mayor = HIGH, menor = LOW)')
            info(f'Canales    : {len(headers)}  → {", ".join(headers[:6])}{"…" if len(headers)>6 else ""}')
            info(f'Primer     : {loc_ini} local  /  {utc_ini} UTC  ({first_ep:.3f})')
            info(f'Último     : {loc_fin} local  /  {utc_fin} UTC  ({last_ep:.3f})')
            info(f'Duración   : {int(dur_s//60)} min {dur_s%60:.2f} s  ({dur_s:.3f} s total)')
            info(f'Fs aprox.  : {rate_hz:.1f} Hz')
            info(f'Tamaño     : {fsize_kb:.1f} KB  ({fsize_kb/1024:.2f} MB)')
            info(f'UTC local  : UTC{tz_offset:+d}')

            transitions: List[dict] = []

            first_is_high = samples[0][1] > THRESHOLD
            prev_epoch    = samples[0][0]
            prev_high     = first_is_high

            if not first_is_high:
                transitions.append({
                    'kind':          'HIGH→LOW',
                    'epoch':         samples[0][0],
                    'utc':           datetime.utcfromtimestamp(samples[0][0]).strftime('%H:%M:%S.%f')[:-3],
                    'local':         datetime.fromtimestamp(samples[0][0]).strftime('%H:%M:%S.%f')[:-3],
                    'trig_val':      samples[0][1],
                    'dt_prev_s':     samples[1][0] - samples[0][0] if len(samples) > 1 else 0.0,
                    'inferred':      True,
                    'closest_event': None,
                    'diff_event_ms': None,
                })

            for epoch, trig_val in samples[1:]:
                is_high = trig_val > THRESHOLD

                if   prev_high and not is_high: kind = 'HIGH→LOW'
                elif not prev_high and is_high: kind = 'LOW→HIGH'
                else:
                    prev_high  = is_high
                    prev_epoch = epoch
                    continue

                transitions.append({
                    'kind':          kind,
                    'epoch':         epoch,
                    'utc':           datetime.utcfromtimestamp(epoch).strftime('%H:%M:%S.%f')[:-3],
                    'local':         datetime.fromtimestamp(epoch).strftime('%H:%M:%S.%f')[:-3],
                    'trig_val':      trig_val,
                    'dt_prev_s':     epoch - prev_epoch,
                    'inferred':      False,
                    'closest_event': None,
                    'diff_event_ms': None,
                })
                prev_high  = is_high
                prev_epoch = epoch

            if self.events_df is not None:
                ev_list = [(str(r['Event']), float(r['EpochTime']))
                           for _, r in self.events_df.iterrows()]
                for tr in transitions:
                    diffs = {ev: (tr['epoch'] - et) * 1000 for ev, et in ev_list}
                    best  = min(diffs, key=lambda k: abs(diffs[k]))
                    tr['closest_event'] = best
                    tr['diff_event_ms'] = diffs[best]

            self.trigger_transitions = transitions

            hl = [t for t in transitions if t['kind'] == 'HIGH→LOW']
            lh = [t for t in transitions if t['kind'] == 'LOW→HIGH']

            ok(f'{n_lines:,} líneas  |  {len(transitions)} transiciones')
            sep70 = '─' * 70
            print(f'  {_col(sep70, DIM)}')
            print(f'  Transiciones totales : {_col(len(transitions), BOLD)}')
            print(f'  {_col("HIGH → LOW  (bajadas):", RED)} {len(hl)}')
            print(f'  {_col("LOW  → HIGH (subidas):", GREEN)} {len(lh)}')
            print(f'  {_col(sep70, DIM)}')

            if transitions:
                h_num   = f"{'#':>3}"
                h_tipo  = f"{'Tipo':<18}"
                h_utc   = f"{'Hora UTC':>14}"
                h_local = f"{'Hora LOCAL':>14}"
                h_val   = f"{'Valor (V)':>11}"
                h_dt    = f"{'Dt prev (s)':>12}"
                print(f'  {h_num}  {h_tipo}  {h_utc}  {h_local}  {h_val}  {h_dt}')
                print(f'  {_col(sep70, DIM)}')

                for i, t in enumerate(transitions, 1):
                    if t['kind'] == 'HIGH→LOW':
                        role_lbl  = _col('inicio', RED)
                        kind_str  = _col('HIGH → LOW  ↓', RED, BOLD)
                    else:
                        role_lbl  = _col('fin',  GREEN)
                        kind_str  = _col('LOW  → HIGH ↑', GREEN, BOLD)

                    inferred_mark = _col(' *inferida*', YELLOW) if t.get('inferred') else ''
                    val_str = f"{t['trig_val']:>+10.4f}"
                    dt_str  = f"{t['dt_prev_s']:>11.3f}"

                    print(f"  {i:>3}  {kind_str}{inferred_mark:<2}  "
                          f"{t['utc']:>14}  {t['local']:>14}  "
                          f"{_col(val_str, CYAN)}  {dt_str}  {role_lbl}")

                print(f'  {_col(sep70, DIM)}')

            hl_ep = hl[0]['epoch'] if hl else None
            lh_ep = lh[0]['epoch'] if lh else None

            if hl_ep:
                inf_note = _col('  (inferida — 1ª muestra del archivo)', YELLOW) \
                           if hl[0].get('inferred') else ''
                print(f"\n  {_col('⬇', RED, BOLD)}  Trigger INICIO  (1ª HIGH→LOW): "
                      f"{_col(datetime.fromtimestamp(hl_ep).strftime('%H:%M:%S.%f')[:-3], BOLD)}"
                      f"{inf_note}")
            if lh_ep:
                print(f"  {_col('⬆', GREEN, BOLD)}  Trigger FIN     (1ª LOW→HIGH):  "
                      f"{_col(datetime.fromtimestamp(lh_ep).strftime('%H:%M:%S.%f')[:-3], BOLD)}")

            self.file_endpoints[label] = (first_ep, last_ep)
            ok(f'Archivo VBO completo:  '
               f'inicio={datetime.fromtimestamp(first_ep).strftime("%H:%M:%S.%f")[:-3]}  '
               f'fin={datetime.fromtimestamp(last_ep).strftime("%H:%M:%S.%f")[:-3]}  '
               f'({dur_s:.3f} s)')

            if hl_ep is not None or lh_ep is not None:
                self.trigger_window = (
                    hl_ep if hl_ep is not None else first_ep,
                    lh_ep if lh_ep is not None else last_ep,
                )
                self.file_endpoints[label] = (self.trigger_window[0], self.trigger_window[1])
                ok(f'Trigger activo:        '
                   f'inicio={datetime.fromtimestamp(self.trigger_window[0]).strftime("%H:%M:%S.%f")[:-3]}  '
                   f'fin={datetime.fromtimestamp(self.trigger_window[1]).strftime("%H:%M:%S.%f")[:-3]}  '
                   f'({self.trigger_window[1] - self.trigger_window[0]:.3f} s)  '
                   f'[pre: {self.trigger_window[0] - first_ep:.3f} s  |  '
                   f'post: {last_ep - self.trigger_window[1]:.3f} s]')
            else:
                self.trigger_window = None
                self.file_endpoints[label] = (first_ep, last_ep)

            if hl and self.events_df is not None:
                fhl = hl[0]
                self.latency_details[label] = [
                    {'event': str(r['Event']),
                     'diff_ms': (fhl['epoch'] - float(r['EpochTime'])) * 1000}
                    for _, r in self.events_df.iterrows()
                ]

            self.vbox_trigger_stats = {
                'filename':        Path(self.vbox_trigger_file).name,
                'filepath':        self.vbox_trigger_file,
                'trigger_channel': TRIG_CH,
                'trigger_col_idx': trig_col,
                'threshold_v':     THRESHOLD,
                'all_channels':    headers,
                'n_channels':      len(headers),
                'n_samples':       n_lines,
                'duration_s':      dur_s,
                'duration_min':    int(dur_s // 60),
                'duration_sec':    dur_s % 60,
                'sample_rate_hz':  rate_hz,
                'first_epoch':     first_ep,
                'last_epoch':      last_ep,
                'first_utc':       utc_ini,
                'last_utc':        utc_fin,
                'first_local':     loc_ini,
                'last_local':      loc_fin,
                'utc_offset':      tz_offset,
                'file_size_kb':    fsize_kb,
                'file_size_mb':    fsize_kb / 1024,
                'file_size_bytes': fsize_bytes,
                'ref_date_used':   ref_date,
                'n_transitions':   len(transitions),
                'n_high_to_low':   len(hl),
                'n_low_to_high':   len(lh),
                'trigger_inicio_epoch': hl_ep,
                'trigger_fin_epoch':    lh_ep,
                'trigger_inicio_local': datetime.fromtimestamp(hl_ep).strftime('%H:%M:%S.%f')[:-3] if hl_ep else None,
                'trigger_fin_local':    datetime.fromtimestamp(lh_ep).strftime('%H:%M:%S.%f')[:-3] if lh_ep else None,
            }

        except Exception as e:
            err(str(e))
            traceback.print_exc()

    @staticmethod
    def _read_vbo_text(path: str) -> str:
        with open(path, 'rb') as f:
            raw = f.read()
        try:    return raw.decode('utf-8')
        except: return raw.decode('latin-1')

    def _vbo_endpoints(self, path: str) -> Tuple:
        ref_date = (datetime.utcfromtimestamp(self.start_time).strftime('%Y-%m-%d')
                    if self.start_time else datetime.utcnow().strftime('%Y-%m-%d'))

        if Path(path).suffix.lower() == '.vbo':
            text  = self._read_vbo_text(path)
            lines = text.replace('\r\n','\n').replace('\r','\n').splitlines()
            ds    = next((i for i, l in enumerate(lines)
                          if l.strip().lower() == '[data]'), None)
            if ds is None: return None, None, 0
            epochs = []
            for raw in lines[ds + 1:]:
                parts = raw.split()
                if len(parts) < 2: continue
                try:
                    epochs.append(utc_s_to_epoch(vbo_hhmmss_to_utc_s(parts[1]), ref_date))
                except Exception: pass
            if not epochs: return None, None, 0
            return min(epochs), max(epochs), len(epochs)

        df  = read_csv_safe(path)
        col = find_epoch_col(df)
        if col:
            v = pd.to_numeric(df[col], errors='coerce').dropna()
            return float(v.iloc[0]), float(v.iloc[-1]), len(v)
        for c in df.columns:
            s0 = str(df[c].dropna().iloc[0]) if len(df[c].dropna()) > 0 else ''
            if re.match(r'\d{6}\.\d{1,3}', s0):
                epochs = []
                for v in df[c].dropna():
                    try: epochs.append(utc_s_to_epoch(vbo_hhmmss_to_utc_s(str(v)), ref_date))
                    except: pass
                if epochs: return min(epochs), max(epochs), len(epochs)
        raise ValueError('No se encontró columna de tiempo válida en el CSV VBox')

    def _compute_diffs(self):
        if self.start_time is None and self.end_time is None:
            warn('Sin INICIO/FIN para calcular diferencias'); return

        jms = self.system_jitter * 1000 if self.system_jitter else None

        header('DIFERENCIAS TEMPORALES')
        print(f"  {'Fuente':<32} {'Ref':<8} {'Δ (s)':>13} {'Δ (ms)':>13}  Estado")
        print('  ' + '─'*68)

        status_colors = {
            'PERFECTO':  (GREEN,  '✅'),
            'BUENO':     (GREEN,  '✅'),
            'ACEPTABLE': (YELLOW, '⚠ '),
            'DESFASADO': (RED,    '✗ '),
        }

        for src, (first, last) in self.file_endpoints.items():
            for ts, ref_t, ref_name in ((first, self.start_time, 'INICIO'),
                                        (last,  self.end_time,   'FIN')):
                if ref_t is None: continue
                ds  = ts - ref_t
                dms = ds * 1000
                col, status = classify_quality(dms, jms)
                key = f'{src}_{ref_name}'
                self.differences[key] = dict(
                    diff_s=ds, diff_ms=dms, status=status,
                    color=col, reference=ref_name, source=src)
                sc, ico = status_colors.get(status, ('', '  '))
                print(f'  {key:<32} {ref_name:<8} '
                      f'{ds:>+12.6f} {dms:>+12.3f}  '
                      f'{_col(ico + " " + status, sc)}')
        print('  ' + '─'*68)

    def save_figures(self, out: str):
        if not self.differences:
            warn('Sin diferencias para visualizar'); return

        header('GENERANDO FIGURAS')
        jms = self.system_jitter * 1000 if self.system_jitter else None

        inicio_d = {k: v for k, v in self.differences.items() if v['reference'] == 'INICIO'}
        fin_d    = {k: v for k, v in self.differences.items() if v['reference'] == 'FIN'}

        self._fig_dashboard(jms, out)
        if inicio_d: self._fig_lollipop(inicio_d, 'INICIO', jms, out)
        if fin_d:    self._fig_lollipop(fin_d,    'FIN',    jms, out)
        self._fig_timeline(jms, out)
        if self.latency_details: self._fig_heatmap(out)
        if self.trigger_transitions: self._fig_trigger(out)
        if self.vbox_trigger_file or self.vbox_sync_file:
            self._fig_trigger_signal(out)

        print(f'\n  {_col("✅  Figuras guardadas en:", GREEN, BOLD)} {_col(out, CYAN)}')

    def _fig_dashboard(self, jms, out):
        fig = plt.figure(figsize=(20, 13), facecolor='white')
        self._banner(fig, 'DASHBOARD DE SINCRONIZACIÓN MULTI-MODAL',
                    f'{len(self.file_endpoints)} fuentes  |  {len(self.differences)} comparaciones')

        gs = fig.add_gridspec(3, 4, left=0.05, right=0.97,
                            top=0.90, bottom=0.05, hspace=0.55, wspace=0.35)

        dabs    = [abs(d['diff_ms']) for d in self.differences.values()]
        max_dms = max(dabs) if dabs else 0
        avg_dms = float(np.mean(dabs)) if dabs else 0

        if   max_dms < (jms or 10):              gcolor, gtxt = C_EXCELLENT, 'EXCELENTE'
        elif max_dms < (jms*10 if jms else 100): gcolor, gtxt = C_WARNING,   'ACEPTABLE'
        else:                                    gcolor, gtxt = C_ERROR,     'CRÍTICO'

        kpis = [
            (fig.add_subplot(gs[0, 0]), '★', gtxt,              'Estado global',    gcolor),
            (fig.add_subplot(gs[0, 1]), 'J',
            f'±{jms:.2f} ms' if jms else 'N/D', 'Jitter sistema',
            C_JITTER if jms else '#ADB5BD'),
            (fig.add_subplot(gs[0, 2]), 'MAX', fmt_ms(max_dms), 'Máx desviación',  C_ACCENT),
            (fig.add_subplot(gs[0, 3]), 'AVG', fmt_ms(avg_dms), 'Media desviación', C_INICIO),
        ]
        for ax, icon, val, lbl, color in kpis:
            self._kpi_card(ax, icon, val, lbl, color)

        file_groups: dict[str, dict] = {}
        for key, entry in self.differences.items():
            parts = key.rsplit('_', 1)
            base, tag = (parts[0], parts[1]) if len(parts) == 2 else (key, key)
            file_groups.setdefault(base, {})[tag] = entry

        ordered_bases = sorted(
            file_groups.keys(),
            key=lambda b: max(abs(e['diff_ms']) for e in file_groups[b].values())
        )

        row_labels  = []
        row_vals    = []
        row_colors  = []
        row_refs    = []
        row_tags    = []
        row_bases   = []

        for base in ordered_bases:
            group = file_groups[base]
            for tag in ('INICIO', 'FIN'):
                if tag not in group:
                    continue
                e = group[tag]
                row_labels.append(f'{base}\n({tag})')
                row_vals.append(e['diff_ms'])
                row_colors.append(e['color'])
                row_refs.append(e.get('reference', tag))
                row_tags.append(tag)
                row_bases.append(base)

        ax_main = fig.add_subplot(gs[1:, :3])
        n_rows  = len(row_vals)
        yp      = np.arange(n_rows)

        current_base, band = None, False
        base_to_rows: dict[str, list] = {}
        for i, base in enumerate(row_bases):
            base_to_rows.setdefault(base, []).append(i)

        for base, rows in base_to_rows.items():
            band = not band
            if band:
                ax_main.axhspan(min(rows) - 0.5, max(rows) + 0.5,
                                alpha=0.04, color='#333333', zorder=0)

        if jms:
            ax_main.axvspan(-jms, jms, alpha=0.10, color=C_EXCELLENT,
                            label=f'Zona óptima (±{jms:.2f} ms)', zorder=1)
        ax_main.axvline(0, color=C_BG_DARK, lw=1.8, alpha=0.7, zorder=2)

        mx = max((abs(d) for d in row_vals), default=1)

        for base, rows in base_to_rows.items():
            if len(rows) < 2:
                continue
            i_idx = next((r for r in rows if row_tags[r] == 'INICIO'), None)
            f_idx = next((r for r in rows if row_tags[r] == 'FIN'),    None)
            if i_idx is None or f_idx is None:
                continue
            xi, yi = row_vals[i_idx], i_idx
            xf, yf = row_vals[f_idx], f_idx
            xmid   = (xi + xf) / 2
            ax_main.plot([xi, xmid, xmid, xf], [yi, yi, yf, yf],
                        color='#888888', lw=1.2, ls='--', alpha=0.55,
                        zorder=3, solid_capstyle='round')

        xoff = mx * 0.03
        for i, (d, c, tag) in enumerate(zip(row_vals, row_colors, row_tags)):
            marker = 'o' if tag == 'INICIO' else 's'
            ax_main.scatter([d], [i], s=220, c=c, marker=marker,
                            edgecolors='white', lw=2, zorder=6)
            tx, ha = (d + xoff, 'left') if d >= 0 else (d - xoff, 'right')
            ax_main.text(tx, i, fmt_ms(d), va='center', ha=ha,
                        fontsize=9, fontweight='bold', color=c,
                        bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                                edgecolor=c, lw=1.5, alpha=0.95),
                        zorder=7)

        ytick_labels = []
        for i, (base, tag, ref) in enumerate(zip(row_bases, row_tags, row_refs)):
            ytick_labels.append(f'{base}  [{tag}]')

        ax_main.set_yticks(yp)
        ax_main.set_yticklabels(ytick_labels, fontsize=8.5, fontweight='bold')

        for tick, tag in zip(ax_main.get_yticklabels(), row_tags):
            tick.set_color(C_INICIO if tag == 'INICIO' else C_ACCENT)

        from matplotlib.lines import Line2D
        legend_handles = [
            Line2D([0], [0], marker='o', color='w', markerfacecolor='#555',
                markersize=9, label='INICIO'),
            Line2D([0], [0], marker='s', color='w', markerfacecolor='#555',
                markersize=9, label='FIN'),
        ]
        if jms:
            from matplotlib.patches import Patch
            legend_handles.insert(0, Patch(facecolor=C_EXCELLENT, alpha=0.3,
                                        label=f'Zona óptima (±{jms:.2f} ms)'))

        ax_main.set_xlabel('Diferencia temporal (ms)', fontsize=12, fontweight='bold')
        ax_main.set_title('DESVIACIÓN POR FUENTE  —  agrupada por archivo (● INICIO  ■ FIN)',
                        fontsize=12, fontweight='bold', pad=12)
        ax_main.set_xlim(-mx * 1.3, mx * 1.3)
        ax_main.set_ylim(-0.8, n_rows - 0.2)
        ax_main.grid(True, alpha=0.25, axis='x', ls='--')
        ax_main.set_facecolor(C_BG_LIGHT)
        ax_main.legend(handles=legend_handles, loc='lower right',
                    fontsize=9, framealpha=0.95)

        ax_leg = fig.add_subplot(gs[1:, 3])
        ax_leg.axis('off')
        cats   = {'PERFECTO': C_EXCELLENT, 'BUENO': C_GOOD,
                'ACEPTABLE': C_WARNING,  'DESFASADO': C_ERROR}
        counts = {c: sum(1 for d in self.differences.values() if d['status'] == c)
                for c in cats}
        ax_leg.text(0.5, 0.97, 'CLASIFICACIÓN', transform=ax_leg.transAxes,
                    fontsize=12, fontweight='bold', ha='center', color=C_BG_DARK)
        for i, (cat, col) in enumerate(cats.items()):
            y = 0.82 - i * 0.20
            ax_leg.add_patch(Rectangle((0.05, y - 0.07), 0.13, 0.13,
                                    transform=ax_leg.transAxes,
                                    facecolor=col, edgecolor='white', lw=2))
            ax_leg.text(0.115, y, str(counts[cat]), transform=ax_leg.transAxes,
                        fontsize=17, fontweight='bold', ha='center', va='center',
                        color='white')
            ax_leg.text(0.23, y + 0.02, cat, transform=ax_leg.transAxes,
                        fontsize=10, fontweight='bold', va='center', color=col)

        fig.text(0.5, 0.005,
                f'Generado: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}  |  '
                f'Unified Sync Analyzer v3.2',
                ha='center', fontsize=8, color='#6C757D', style='italic')

        self._save(fig, Path(out) / '01_dashboard.png')

    def _fig_lollipop(self, data, event_name, jms, out):
        fig, ax = plt.subplots(figsize=(16, max(6, len(data)*1.3)), facecolor='white')
        ev_col  = C_INICIO if event_name == 'INICIO' else C_FIN
        self._banner(fig, f'SINCRONIZACIÓN CON EVENTO {event_name}',
                     f'Desviación de cada fuente respecto a {event_name}')

        items = sorted(data.items(), key=lambda x: abs(x[1]['diff_ms']))
        srcs  = [s for s, _ in items]
        dms   = [d['diff_ms'] for _, d in items]
        cols  = [d['color']   for _, d in items]
        ststs = [d['status']  for _, d in items]
        yp    = np.arange(len(srcs))

        if jms:
            ax.axvspan(-jms, jms, alpha=0.12, color=C_EXCELLENT, label=f'±{jms:.2f} ms')
        ax.axvline(0, color=ev_col, lw=3, alpha=0.9, label=f'Evento {event_name}')

        mx = max((abs(d) for d in dms), default=1)
        for i, (d, c, st) in enumerate(zip(dms, cols, ststs)):
            ax.plot([0, d], [i, i], color=c, lw=3, alpha=0.6)
            ax.scatter([d], [i], s=400, c=c, edgecolors='white', lw=3, zorder=5)
            xoff = mx * 0.04
            tx, ha = (d+xoff,'left') if d >= 0 else (d-xoff,'right')
            ax.text(tx, i+0.16, fmt_ms(d), va='center', ha=ha,
                    fontsize=12, fontweight='bold', color=c,
                    bbox=dict(boxstyle='round,pad=0.35', facecolor='white',
                               edgecolor=c, lw=2))
            ax.text(tx, i-0.22, st, va='center', ha=ha,
                    fontsize=9, color=c, style='italic')

        ax.set_yticks(yp)
        ax.set_yticklabels(srcs, fontsize=11, fontweight='bold')
        ax.set_xlabel('Diferencia (ms)', fontsize=12, fontweight='bold')
        ax.set_xlim(-mx*1.35, mx*1.35)
        ax.set_ylim(-0.7, len(srcs)-0.3)
        ax.grid(True, alpha=0.3, axis='x', ls='--')
        ax.set_facecolor(C_BG_LIGHT)
        ax.legend(fontsize=10, framealpha=0.95)

        dabs = [abs(d) for d in dms]
        fig.text(0.5, 0.02,
                 f'Máx: {fmt_ms(max(dabs))}   |   Mín: {fmt_ms(min(dabs))}   |   '
                 f'Media: {fmt_ms(float(np.mean(dabs)))}',
                 ha='center', fontsize=10, fontweight='bold', color=C_BG_DARK,
                 bbox=dict(boxstyle='round,pad=0.5', facecolor=C_BG_LIGHT,
                            edgecolor=ev_col, lw=2))
        plt.subplots_adjust(top=0.88, bottom=0.10, left=0.22, right=0.96)
        self._save(fig, Path(out) / f'02_sync_{event_name.lower()}.png')

    def _fig_timeline(self, jms, out):
        fig = plt.figure(figsize=(20, 10), facecolor='white')
        self._banner(fig, 'TIMELINE DE SINCRONIZACIÓN',
                     'Vista temporal completa — todas las fuentes')
        ax = fig.add_subplot(111)
        fig.subplots_adjust(left=0.09, right=0.96, top=0.88, bottom=0.10)

        t0  = self.start_time or 0
        t1  = self.end_time or t0
        dur = t1 - t0 if t1 > t0 else 1

        ax.barh([0], [dur], left=[0], height=0.45,
                color=C_BG_DARK, alpha=0.9, edgecolor='white', lw=2)
        for x, label, color in ((0,'INICIO',C_INICIO), (dur,'FIN',C_FIN)):
            ax.scatter([x], [0], s=500, c=color, marker='D',
                       edgecolors='white', lw=3, zorder=10)
            ax.text(x, 0.38, label, ha='center', va='bottom',
                    fontsize=10, fontweight='bold', color=color)
        ax.text(dur/2, 0, f'{dur:.2f} s  ({dur/60:.2f} min)',
                ha='center', va='center', fontsize=11, fontweight='bold', color='white')

        cat_colors = {'PCAP':C_PCAP,'EEG':C_INICIO,'ECG':'#E76F51',
                      'VIDEO':'#2A9D8F','VBOX':C_VBOX,'VBOX_SYNC':C_VBOX,
                      'VBOX_TRIGGER':'#E9C46A','TRIGGER':'#E9C46A'}
        y = -1.4
        for src, (first, last) in self.file_endpoints.items():
            r0, r1 = first - t0, last - t0
            cat    = src.split('_')[0]
            col    = cat_colors.get(cat, C_ACCENT)
            ax.barh([y], [r1-r0], left=[r0], height=0.3,
                    color=col, alpha=0.65, edgecolor=C_BG_DARK, lw=1.2)
            ax.text((r0+r1)/2, y, src, ha='center', va='center',
                    fontsize=9, fontweight='bold', color='white')
            for key_sfx, xpos, marker in (('_INICIO',r0,'o'),('_FIN',r1,'s')):
                kk = f'{src}{key_sfx}'
                if kk in self.differences:
                    dv  = self.differences[kk]['diff_ms']
                    dc  = self.differences[kk]['color']
                    ax.scatter([xpos],[y],s=200,c=dc,marker=marker,
                               edgecolors='white',lw=2,zorder=8)
                    ax.text(xpos,y-0.3,fmt_ms(dv),ha='center',va='top',
                            fontsize=9,fontweight='bold',color=dc,
                            bbox=dict(boxstyle='round,pad=0.2',
                                      facecolor='white',edgecolor=dc,lw=1))

            if src == 'VBOX_TRIGGER' and self.trigger_transitions:
                for tr in self.trigger_transitions:
                    rx = tr['epoch'] - t0
                    tc = C_ERROR if tr['kind'] == 'HIGH→LOW' else C_EXCELLENT
                    sym = '↓' if tr['kind'] == 'HIGH→LOW' else '↑'
                    ax.scatter([rx], [0], s=120, c=tc,
                               marker='^' if sym=='↑' else 'v',
                               edgecolors='white', lw=1.5, zorder=12)
                    ax.text(rx, 0.55, sym, ha='center', va='bottom',
                            fontsize=11, fontweight='bold', color=tc)

            y -= 1.1

        ax.set_yticks([])
        ax.set_xlabel('Tiempo relativo desde INICIO (s)', fontsize=12, fontweight='bold')
        ax.set_xlim(-dur*0.08, dur*1.1)
        ax.set_ylim(y+0.5, 0.8)
        ax.grid(True, alpha=0.3, axis='x', ls='--')
        ax.set_facecolor(C_BG_LIGHT)
        if jms:
            ax.text(0.01, 0.98, f'Jitter: ±{jms:.2f} ms',
                    transform=ax.transAxes, fontsize=10, va='top', fontweight='bold',
                    bbox=dict(boxstyle='round,pad=0.4',facecolor='white',
                               edgecolor=C_BG_DARK,lw=1))
        self._save(fig, Path(out)/'03_timeline.png')

    def _fig_heatmap(self, out):
        rows = {src: {d['event']: d['diff_ms'] for d in details}
                for src, details in self.latency_details.items()}
        if not rows: return
        df = pd.DataFrame(rows).T
        if df.empty: return

        fig, ax = plt.subplots(
            figsize=(max(10, len(df.columns)*2), max(5, len(df)*0.9)+2),
            facecolor='white')
        self._banner(fig, 'LATENCIA POR EVENTO Y SEÑAL (ms)',
                     'Positivo = señal llega después del evento')
        sns.heatmap(df, annot=True, fmt='.1f', cmap='RdYlGn_r',
                    center=0, linewidths=0.5, ax=ax,
                    cbar_kws={'label': 'Latencia (ms)'})
        ax.set_xlabel('Evento', fontsize=11, fontweight='bold')
        ax.set_ylabel('Señal',  fontsize=11, fontweight='bold')
        plt.subplots_adjust(top=0.88, bottom=0.15, left=0.18, right=0.96)
        self._save(fig, Path(out)/'04_latency_heatmap.png')

    def _fig_trigger(self, out):
        if not self.trigger_transitions:
            return

        hl = [t for t in self.trigger_transitions if t['kind'] == 'HIGH→LOW']
        lh = [t for t in self.trigger_transitions if t['kind'] == 'LOW→HIGH']

        vt = self.vbox_trigger_stats
        t0_file = vt['first_epoch'] if vt else self.trigger_transitions[0]['epoch']
        t1_file = vt['last_epoch']  if vt else self.trigger_transitions[-1]['epoch']

        trig_ini = hl[0]['epoch'] if hl else None
        trig_fin = lh[0]['epoch'] if lh else None

        vbox_sync_ep = self.file_endpoints.get('VBOX_SYNC')

        C_VBO    = '#0077B6'
        C_PRE    = '#ADB5BD'
        C_TRIG   = '#F18F01'
        C_SYNC   = '#06A77D'
        C_SES    = '#6F2DBD'
        C_DARK   = '#212529'
        C_LIGHT  = '#F8F9FA'

        rows = []
        rows.append(('VBO archivo', t0_file, t1_file, C_VBO, 0.18))
        if trig_ini and trig_ini > t0_file:
            rows.append(('Pre-trigger', t0_file, trig_ini, C_PRE, 0.55))
        if trig_ini and trig_fin:
            rows.append(('Trigger activo', trig_ini, trig_fin, C_TRIG, 0.85))
        elif trig_ini:
            rows.append(('Trigger activo', trig_ini, t1_file, C_TRIG, 0.85))
        if trig_fin and trig_fin < t1_file:
            rows.append(('Post-trigger', trig_fin, t1_file, C_PRE, 0.55))
        if vbox_sync_ep:
            rows.append(('VBox SYNC', vbox_sync_ep[0], vbox_sync_ep[1], C_SYNC, 0.80))
        if self.start_time and self.end_time:
            rows.append(('Sesión (.txt)', self.start_time, self.end_time, C_SES, 0.75))

        n = len(rows)

        fig, ax = plt.subplots(figsize=(18, 3.2 + n * 0.85), facecolor='white')
        self._banner(fig, 'TIMELINE TRIGGER — VBox',
                     f'Trigger activo  ·  HIGH→LOW: {len(hl)}  LOW→HIGH: {len(lh)}')
        fig.subplots_adjust(top=0.82, bottom=0.18, left=0.15, right=0.97)

        t0 = t0_file
        bar_h = 0.52
        y_gap = 1.0

        for i, (label, ts, te, color, alpha) in enumerate(rows):
            y   = (n - 1 - i) * y_gap
            x0  = ts - t0
            dur = te - ts

            ax.barh(y, dur, left=x0, height=bar_h,
                    color=color, alpha=alpha,
                    edgecolor=color, linewidth=1.2,
                    align='center')

            ax.text(-0.5, y, label,
                    ha='right', va='center',
                    fontsize=10, fontweight='bold', color=C_DARK)

            ax.text(x0, y + bar_h * 0.62,
                    datetime.fromtimestamp(ts).strftime('%H:%M:%S.%f')[:-3],
                    ha='left', va='bottom', fontsize=8, color=color, fontweight='bold')

            ax.text(x0 + dur, y + bar_h * 0.62,
                    datetime.fromtimestamp(te).strftime('%H:%M:%S.%f')[:-3],
                    ha='right', va='bottom', fontsize=8, color=color, fontweight='bold')

            ax.text(x0 + dur / 2, y,
                    f'{dur:.3f} s',
                    ha='center', va='center', fontsize=9,
                    fontweight='bold', color='white' if alpha > 0.5 else C_DARK,
                    bbox=dict(boxstyle='round,pad=0.2',
                              facecolor=color, alpha=0.85,
                              edgecolor='none'))

        for ep, ls_, col_, lbl_ in (
            (trig_ini, '--', C_TRIG,  'Trigger ini'),
            (trig_fin, '--', C_TRIG,  'Trigger fin'),
            (t0_file,  '-',  C_VBO,   'VBO ini'),
            (t1_file,  '-',  C_VBO,   'VBO fin'),
        ):
            if ep is None:
                continue
            ax.axvline(ep - t0, color=col_, lw=1.2,
                       linestyle=ls_, alpha=0.55, zorder=0)

        if vbox_sync_ep and trig_ini and trig_fin:
            d_ini_ms = (vbox_sync_ep[0] - trig_ini) * 1000
            d_fin_ms = (vbox_sync_ep[1] - trig_fin) * 1000
            fig.text(0.97, 0.04,
                     f'Δ SYNC vs TRIGGER   inicio {fmt_ms(d_ini_ms)}  |  fin {fmt_ms(d_fin_ms)}',
                     ha='right', fontsize=9, fontweight='bold',
                     color=C_SYNC,
                     bbox=dict(boxstyle='round,pad=0.4',
                               facecolor=C_LIGHT,
                               edgecolor=C_SYNC, lw=1.2))

        x_max = max(r[2] - t0 for r in rows) * 1.02
        ax.set_xlim(-0.5, x_max)
        ax.set_ylim(-y_gap * 0.6, (n - 1) * y_gap + y_gap * 0.6)
        ax.set_xlabel('Tiempo relativo al inicio del archivo VBO (s)',
                      fontsize=11, fontweight='bold')
        ax.set_yticks([])
        ax.set_facecolor(C_LIGHT)
        ax.grid(True, axis='x', alpha=0.25, linestyle='--')

        self._save(fig, Path(out) / '05_trigger.png')

    def _fig_trigger_signal(self, out: str):
        if not self.vbox_trigger_file and not self.vbox_sync_file:
            warn('Sin archivos VBO para graficar señal Trigger'); return

        C_TRIG   = '#F18F01'
        C_SYNC   = '#06A77D'
        C_THRESH = '#D62828'
        C_DARK   = '#212529'
        C_LIGHT  = '#F8F9FA'
        THRESHOLD = 1.5

        def _load_trigger_signal(path: str):
            text  = self._read_vbo_text(path)
            lines = text.replace('\r\n', '\n').replace('\r', '\n').splitlines()

            headers, in_hdr = [], False
            for l in lines:
                ls = l.strip().lower()
                if ls == '[header]':               in_hdr = True;  continue
                if ls.startswith('[') and in_hdr:  break
                if in_hdr and l.strip():           headers.append(l.strip())

            trig_col = None
            if 'Trigger' in headers:
                trig_col = headers.index('Trigger')
            else:
                for idx, h in enumerate(headers):
                    if 'trig' in h.lower():
                        trig_col = idx; break

            if trig_col is None:
                raise ValueError(
                    f"Canal 'Trigger' no encontrado en {Path(path).name}.\n"
                    f"Disponibles: {', '.join(headers)}")

            ref_date = (
                datetime.utcfromtimestamp(self.start_time).strftime('%Y-%m-%d')
                if self.start_time else datetime.utcnow().strftime('%Y-%m-%d')
            )

            ds = next(
                (i for i, l in enumerate(lines) if l.strip().lower() == '[data]'),
                None)
            if ds is None:
                raise ValueError(f"No se encontró [data] en {Path(path).name}")

            epochs, values = [], []
            for raw in lines[ds + 1:]:
                parts = raw.split()
                if len(parts) <= max(1, trig_col): continue
                try:
                    ep  = utc_s_to_epoch(vbo_hhmmss_to_utc_s(parts[1]), ref_date)
                    val = float(parts[trig_col])
                    epochs.append(ep); values.append(val)
                except (ValueError, IndexError):
                    continue

            if not epochs:
                raise ValueError(f"Sin muestras válidas en {Path(path).name}")
            return np.array(epochs), np.array(values)

        def _regions(rel_t, val, threshold):
            regions = []
            if len(rel_t) < 2:
                return regions
            is_high   = val[0] > threshold
            seg_start = rel_t[0]
            for i in range(1, len(rel_t)):
                now_high = val[i] > threshold
                if now_high != is_high:
                    regions.append({
                        'state':    'HIGH' if is_high else 'LOW',
                        't_start':  seg_start,
                        't_end':    rel_t[i],
                        'duration': rel_t[i] - seg_start,
                    })
                    is_high   = now_high
                    seg_start = rel_t[i]
            regions.append({
                'state':    'HIGH' if is_high else 'LOW',
                't_start':  seg_start,
                't_end':    rel_t[-1],
                'duration': rel_t[-1] - seg_start,
            })
            return regions

        def _fmt_dur(s):
            if s < 1:     return f'{s*1000:.1f} ms'
            if s < 60:    return f'{s:.2f} s'
            return f'{int(s//60)}m {s%60:.1f}s'

        def _plot_signal(ax, rel_t, val, color, label, fname):
            y_max = max(val.max(), THRESHOLD) * 1.25
            ax.plot(rel_t, val, color=color, lw=1.6, alpha=0.92,
                    drawstyle='steps-post', label=label, zorder=4)
            ax.axhline(THRESHOLD, color=C_THRESH, lw=1.2, ls='--',
                       alpha=0.75, label=f'Umbral {THRESHOLD} V', zorder=3)
            regs = _regions(rel_t, val, THRESHOLD)
            for r in regs:
                is_high  = r['state'] == 'HIGH'
                fc       = color if is_high else C_DARK
                alpha_f  = 0.20  if is_high else 0.07
                t_c      = (r['t_start'] + r['t_end']) / 2
                dur_txt  = _fmt_dur(r['duration'])
                mask = (rel_t >= r['t_start']) & (rel_t <= r['t_end'])
                xs   = rel_t[mask]
                ys   = val[mask]
                if len(xs) > 1:
                    ax.fill_between(xs, ys, THRESHOLD,
                                    step='post', alpha=alpha_f, color=fc, zorder=2)
                y_lbl  = y_max * 0.88 if is_high else THRESHOLD * 0.45
                t_col  = color if is_high else '#888888'
                bbox_c = dict(boxstyle='round,pad=0.25',
                              facecolor='white', edgecolor=t_col,
                              lw=1.0, alpha=0.88)
                ax.text(t_c, y_lbl, dur_txt,
                        ha='center', va='center',
                        fontsize=8.5, fontweight='bold',
                        color=t_col, bbox=bbox_c, zorder=6,
                        clip_on=True)
            for i in range(1, len(val)):
                if (val[i - 1] > THRESHOLD) != (val[i] > THRESHOLD):
                    ax.scatter([rel_t[i]], [THRESHOLD],
                               s=55, c=C_THRESH, zorder=7,
                               edgecolors='white', lw=1.2)
            if self.start_time:
                ax.axvline(self.start_time - t0_global,
                           color='#2E86AB', lw=1.5, ls=':', alpha=0.8,
                           label='INICIO sesión')
            if self.end_time:
                ax.axvline(self.end_time - t0_global,
                           color='#A23B72', lw=1.5, ls=':', alpha=0.8,
                           label='FIN sesión')
            ax.set_ylabel('Voltaje (V)', fontsize=10, fontweight='bold')
            ax.set_title(f'{label}  —  {fname}',
                         fontsize=11, fontweight='bold', pad=6)
            ax.set_facecolor(C_LIGHT)
            ax.set_ylim(-0.3, y_max)
            ax.set_yticks([0, THRESHOLD, round(val.max(), 1)])
            ax.grid(True, axis='x', alpha=0.25, ls='--')
            ax.legend(fontsize=8, loc='upper right', framealpha=0.95, ncol=3)

        data = {}

        if self.vbox_trigger_file:
            try:
                ep, val = _load_trigger_signal(self.vbox_trigger_file)
                data['TRIGGER'] = (ep, val,
                                   Path(self.vbox_trigger_file).name, C_TRIG)
                ok(f'Trigger signal: {len(ep):,} muestras')
            except Exception as e:
                warn(f'VBox TRIGGER: {e}')

        if self.vbox_sync_file and Path(self.vbox_sync_file).suffix.lower() == '.vbo':
            try:
                ep, val = _load_trigger_signal(self.vbox_sync_file)
                data['SYNC'] = (ep, val,
                                Path(self.vbox_sync_file).name, C_SYNC)
                ok(f'Sync signal: {len(ep):,} muestras')
            except Exception as e:
                warn(f'VBox SYNC: {e}')

        if not data:
            warn('Sin datos de señal Trigger para graficar'); return

        t0_global = min(ep[0] for ep, _, __, ___ in data.values())

        def _rel(ep): return ep - t0_global

        n_src  = len(data)
        n_rows = n_src + 1

        fig, axes = plt.subplots(
            n_rows, 1,
            figsize=(18, 3.8 * n_rows + 1.5),
            facecolor='white',
            sharex=True
        )
        if n_rows == 1:
            axes = [axes]

        self._banner(
            fig,
            'SEÑAL TRIGGER — VBox TRIGGER vs SYNC',
            f'Umbral {THRESHOLD} V  ·  duración de cada zona HIGH / LOW'
        )
        fig.subplots_adjust(
            top=0.91, bottom=0.06,
            left=0.07, right=0.97,
            hspace=0.42
        )

        ax_idx       = 0
        signals_norm = {}

        for key, (ep, val, fname, color) in data.items():
            rel_t = _rel(ep)
            _plot_signal(axes[ax_idx], rel_t, val, color, key, fname)
            v_min, v_max = val.min(), val.max()
            dv = v_max - v_min if v_max != v_min else 1.0
            signals_norm[key] = (rel_t, (val - v_min) / dv, color, fname)
            ax_idx += 1

        ax_ov = axes[ax_idx]

        all_vals = np.concatenate([val for ep, val, _, __ in data.values()])
        v_min_g  = all_vals.min()
        v_max_g  = all_vals.max()
        dv_g     = v_max_g - v_min_g if v_max_g != v_min_g else 1.0
        thr_norm = (THRESHOLD - v_min_g) / dv_g

        _ov_y = {
            'TRIGGER': {'HIGH': 1.22, 'LOW': 0.18},
            'SYNC':    {'HIGH': 1.10, 'LOW': 0.06},
        }
        _ov_y_default = [
            {'HIGH': 1.22, 'LOW': 0.18},
            {'HIGH': 1.10, 'LOW': 0.06},
            {'HIGH': 0.98, 'LOW': -0.06},
        ]

        for sig_idx, (key, (rel_t, val_n, color, fname)) in enumerate(signals_norm.items()):
            ax_ov.plot(rel_t, val_n, color=color, lw=1.8, alpha=0.85,
                       drawstyle='steps-post', label=key)
            ax_ov.fill_between(rel_t, val_n, 0,
                               step='post', alpha=0.10, color=color)
            y_map = _ov_y.get(key, _ov_y_default[min(sig_idx, len(_ov_y_default) - 1)])
            regs = _regions(rel_t, val_n, thr_norm)
            for r in regs:
                is_high = r['state'] == 'HIGH'
                t_c     = (r['t_start'] + r['t_end']) / 2
                y_lbl   = y_map['HIGH'] if is_high else y_map['LOW']
                ax_ov.text(t_c, y_lbl, _fmt_dur(r['duration']),
                           ha='center', va='center',
                           fontsize=7.5, fontweight='bold',
                           color=color, alpha=0.90,
                           bbox=dict(boxstyle='round,pad=0.22',
                                     facecolor='white', edgecolor=color,
                                     lw=1.0, alpha=0.90),
                           zorder=6, clip_on=True)

        ax_ov.axhline(thr_norm, color=C_THRESH, lw=1.2, ls='--',
                      alpha=0.7, label=f'Umbral (norm.)')
        if self.start_time:
            ax_ov.axvline(self.start_time - t0_global,
                          color='#2E86AB', lw=1.5, ls=':', alpha=0.8,
                          label='INICIO sesión')
        if self.end_time:
            ax_ov.axvline(self.end_time - t0_global,
                          color='#A23B72', lw=1.5, ls=':', alpha=0.8,
                          label='FIN sesión')

        ax_ov.set_ylabel('Amplitud\n(normaliz. 0-1)', fontsize=10, fontweight='bold')
        ax_ov.set_title('SUPERPOSICIÓN — TRIGGER vs SYNC (normalizadas)',
                        fontsize=11, fontweight='bold', pad=6)
        ax_ov.set_facecolor(C_LIGHT)
        ax_ov.set_ylim(-0.16, 1.38)
        ax_ov.set_yticks([0, 0.5, 1])
        ax_ov.grid(True, axis='x', alpha=0.25, ls='--')
        ax_ov.legend(fontsize=9, loc='upper right', framealpha=0.95, ncol=4)

        all_rel = np.concatenate([_rel(ep) for ep, _, __, ___ in data.values()])
        x_min, x_max = all_rel.min(), all_rel.max()
        pad = (x_max - x_min) * 0.02
        axes[-1].set_xlim(x_min - pad, x_max + pad)
        axes[-1].set_xlabel(
            'Tiempo relativo al inicio del primer archivo (s)',
            fontsize=11, fontweight='bold')

        ax_top = axes[0].twiny()
        ax_top.set_xlim(axes[0].get_xlim())
        tick_rel = np.linspace(x_min, x_max, 8)
        tick_lbl = [
            datetime.fromtimestamp(t0_global + r).strftime('%H:%M:%S')
            for r in tick_rel
        ]
        ax_top.set_xticks(tick_rel)
        ax_top.set_xticklabels(tick_lbl, fontsize=8, rotation=20, ha='left')
        ax_top.tick_params(axis='x', length=3)

        self._save(fig, Path(out) / '06_trigger_signal.png')

    @staticmethod
    def _banner(fig, title: str, subtitle: str = ''):
        hax = fig.add_axes([0, 0.94, 1, 0.06])
        hax.set_facecolor(C_BG_DARK)
        hax.set_xticks([]); hax.set_yticks([])
        for sp in hax.spines.values(): sp.set_visible(False)
        hax.text(0.5, 0.62, title, transform=hax.transAxes,
                 fontsize=18, fontweight='bold', color='white', ha='center', va='center')
        if subtitle:
            hax.text(0.5, 0.18, subtitle, transform=hax.transAxes,
                     fontsize=10, color='#CED4DA', ha='center', va='center', style='italic')

    @staticmethod
    def _kpi_card(ax, icon, value, label, color):
        ax.set_xlim(0,1); ax.set_ylim(0,1); ax.axis('off')
        ax.add_patch(FancyBboxPatch((0.02,0.05), 0.96, 0.90,
                                    boxstyle='round,pad=0.02',
                                    facecolor=color, edgecolor='none', alpha=0.92))
        ax.text(0.17, 0.68, icon,  transform=ax.transAxes,
                fontsize=24, ha='center', va='center')
        ax.text(0.60, 0.58, value, transform=ax.transAxes,
                fontsize=18, fontweight='bold', ha='center', va='center', color='white')
        ax.text(0.50, 0.22, label, transform=ax.transAxes,
                fontsize=9,  ha='center', va='center', color='white', fontweight='bold')

    @staticmethod
    def _save(fig, path):
        fig.savefig(path, dpi=200, bbox_inches='tight', facecolor='white')
        plt.close(fig)
        ok(Path(path).name)

    # ═══════════════════════════════════════════════════════════════════════════
    #  REPORTE COMPLETO  (TXT + JSON)
    # ═══════════════════════════════════════════════════════════════════════════

    def save_report(self, out: str):
        sep  = '=' * 72
        sep2 = '-' * 72

        def _ts(epoch: float) -> str:
            return datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        def _dur(s: float) -> str:
            return f'{int(s//60)} min {s%60:.3f} s  ({s:.3f} s)'

        lines = [
            sep,
            '  REPORTE UNIFICADO DE SINCRONIZACIÓN  v3.2',
            sep,
            f'  Generado  : {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            f'  Eventos   : {Path(self.events_file).name if self.events_file else "N/A"}',
            f'  Directorio: {Path(self.events_file).parent if self.events_file else "N/A"}',
            '',
        ]

        lines += [sep, '  1. ARCHIVOS CARGADOS', sep]
        lines += [
            f'  Eventos      : {Path(self.events_file).name if self.events_file else "N/A"}',
            f'  PCAP         : {Path(self.pcap_file).name if self.pcap_file else "N/A"}',
            f'  EEG          : {len(self.eeg_files)} archivo(s)  →  '
            f'{", ".join(Path(p).name for p in self.eeg_files.values()) or "—"}',
            f'  ECG          : {len(self.ecg_files)} archivo(s)  →  '
            f'{", ".join(Path(p).name for p in self.ecg_files.values()) or "—"}',
            f'  Video        : {len(self.video_files)} archivo(s)  →  '
            f'{", ".join(Path(p).name for p in self.video_files.values()) or "—"}',
            f'  VBox Sync    : {Path(self.vbox_sync_file).name if self.vbox_sync_file else "N/A"}',
            f'  VBox Trigger : {Path(self.vbox_trigger_file).name if self.vbox_trigger_file else "N/A"}',
            '',
        ]

        lines += [sep, '  2. EVENTOS', sep]
        for ev, t in self.events_dict.items():
            lines.append(f'  {ev:<24} epoch={t:.6f}  local={_ts(t)}')
        if self.start_time:
            lines.append(f'\n  INICIO : epoch={self.start_time:.6f}  local={_ts(self.start_time)}')
        if self.end_time:
            lines.append(f'  FIN    : epoch={self.end_time:.6f}  local={_ts(self.end_time)}')
        if self.start_time and self.end_time:
            dur_ev = self.end_time - self.start_time
            lines.append(f'  Duración sesión : {_dur(dur_ev)}')
        if self.system_jitter is not None:
            lines.append(f'\n  Jitter del sistema : ±{self.system_jitter*1000:.6f} ms  '
                         f'({self.system_jitter:.9f} s)')
        lines.append('')

        if self.pcap_stats:
            p = self.pcap_stats
            lines += [sep, '  3. PCAP', sep]
            lines += [
                f'  Archivo      : {p["filename"]}',
                f'  Ruta         : {p["filepath"]}',
                f'  Versión PCAP : {p["version_major"]}.{p["version_minor"]}',
                f'  Tipo enlace  : {p["link_type"]}  (ID={p["link_type_id"]})',
                f'  Snaplen      : {p["snaplen_bytes"]} B',
                f'  Paquetes     : {p["packets"]:,}',
                f'  Tasa         : {p["rate_pkt_s"]:.2f} pkt/s',
                f'  Tamaño medio : {p["avg_pkt_bytes"]:.1f} B/paquete',
                f'  Bytes captura: {p["total_bytes_cap"]:,} B',
                f'  Primer       : epoch={p["first_epoch"]:.6f}  local={p["first_local"]}',
                f'  Último       : epoch={p["last_epoch"]:.6f}  local={p["last_local"]}',
                f'  Duración     : {_dur(p["duration_s"])}',
                f'  Tamaño arch. : {p["file_size_mb"]:.3f} MB  ({p["file_size_bytes"]:,} bytes)',
                '',
            ]

        eeg_entries = {k: v for k, v in self.csv_source_stats.items() if 'EEG' in k}
        if eeg_entries:
            lines += [sep, '  4. EEG', sep]
            for lbl, s in eeg_entries.items():
                lines.append(f'\n  [{lbl}]  {"★ CRÍTICO" if s["critical"] else ""}')
                lines += [
                    f'  Archivo      : {s["filename"]}',
                    f'  Ruta         : {s["filepath"]}',
                    f'  Columna ts   : {s["timestamp_col"]}',
                    f'  Registros    : {s["records"]:,}',
                    f'  Fs (aprox.)  : {s["sample_rate_hz"]:.4f} Hz',
                    f'  Primer       : epoch={s["first_epoch"]:.6f}  local={s["first_local"]}',
                    f'  Último       : epoch={s["last_epoch"]:.6f}  local={s["last_local"]}',
                    f'  Duración     : {_dur(s["duration_s"])}',
                    f'  Columnas ({s["n_columns"]}): {", ".join(s["columns"][:8])}{"…" if s["n_columns"]>8 else ""}',
                    f'  Tamaño       : {s["file_size_kb"]:.2f} KB  ({s["file_size_mb"]:.3f} MB)',
                ]
                if 'advanced' in s:
                    adv = s['advanced']
                    if 'jitter_muestreo_std_ms' in adv:
                        lines.append(
                            f'  Jitter ts    : std={adv["jitter_muestreo_std_ms"]:.6f} ms  '
                            f'media={adv["jitter_muestreo_mean_ms"]:.6f} ms  '
                            f'max={adv["jitter_muestreo_max_ms"]:.6f} ms')
                    if 'media' in adv:
                        lines.append(
                            f'  Señal        : media={adv["media"]:.6f}  std={adv["std"]:.6f}  '
                            f'min={adv["min"]:.6f}  max={adv["max"]:.6f}  '
                            f'n={adv.get("n_samples","")}')
            lines.append('')

        ecg_entries = {k: v for k, v in self.csv_source_stats.items() if 'ECG' in k}
        if ecg_entries:
            lines += [sep, '  5. ECG', sep]
            for lbl, s in ecg_entries.items():
                lines.append(f'\n  [{lbl}]  {"★ CRÍTICO" if s["critical"] else ""}')
                lines += [
                    f'  Archivo      : {s["filename"]}',
                    f'  Ruta         : {s["filepath"]}',
                    f'  Columna ts   : {s["timestamp_col"]}',
                    f'  Registros    : {s["records"]:,}',
                    f'  Fs (aprox.)  : {s["sample_rate_hz"]:.4f} Hz',
                    f'  Primer       : epoch={s["first_epoch"]:.6f}  local={s["first_local"]}',
                    f'  Último       : epoch={s["last_epoch"]:.6f}  local={s["last_local"]}',
                    f'  Duración     : {_dur(s["duration_s"])}',
                    f'  Columnas ({s["n_columns"]}): {", ".join(s["columns"][:8])}{"…" if s["n_columns"]>8 else ""}',
                    f'  Tamaño       : {s["file_size_kb"]:.2f} KB  ({s["file_size_mb"]:.3f} MB)',
                ]
                if 'advanced' in s:
                    adv = s['advanced']
                    if 'jitter_muestreo_std_ms' in adv:
                        lines.append(
                            f'  Jitter ts    : std={adv["jitter_muestreo_std_ms"]:.6f} ms  '
                            f'media={adv["jitter_muestreo_mean_ms"]:.6f} ms  '
                            f'max={adv["jitter_muestreo_max_ms"]:.6f} ms')
                    if 'fc_bpm' in adv:
                        lines.append(
                            f'  FC estimada  : {adv["fc_bpm"]:.2f} bpm  '
                            f'(RR media={adv["rr_mean_s"]*1000:.2f} ms  '
                            f'std={adv["rr_std_s"]*1000:.2f} ms  '
                            f'min={adv["rr_min_s"]*1000:.2f} ms  '
                            f'max={adv["rr_max_s"]*1000:.2f} ms)')
                    if 'n_peaks' in adv:
                        lines.append(f'  Picos R dete.: {adv["n_peaks"]}  '
                                     f'(umbral={adv["ecg_threshold"]:.4f})')
            lines.append('')

        if self.video_stats:
            lines += [sep, '  6. VIDEO', sep]
            for lbl, v in self.video_stats.items():
                lines += self._video_report_lines(v, lbl)
            lines.append('')

        if self.vbox_sync_stats:
            v = self.vbox_sync_stats
            lines += [sep, '  7. VBOX SYNC', sep]
            lines += [
                f'  Archivo      : {v["filename"]}',
                f'  Ruta         : {v["filepath"]}',
                f'  Registros    : {v["records"]:,}',
                f'  Fs (aprox.)  : {v["sample_rate_hz"]:.4f} Hz',
                f'  Primer       : epoch={v["first_epoch"]:.6f}  local={v["first_local"]}',
                f'  Último       : epoch={v["last_epoch"]:.6f}  local={v["last_local"]}',
                f'  Duración     : {_dur(v["duration_s"])}',
                f'  Tamaño       : {v["file_size_kb"]:.2f} KB  ({v["file_size_mb"]:.4f} MB)',
                '',
            ]

        if self.vbox_trigger_stats:
            v = self.vbox_trigger_stats
            lines += [sep, '  8. VBOX TRIGGER', sep]
            lines += [
                f'  Archivo          : {v["filename"]}',
                f'  Ruta             : {v["filepath"]}',
                f'  Canal trigger    : {v["trigger_channel"]}  (columna {v["trigger_col_idx"]})',
                f'  Umbral           : {v["threshold_v"]} V  (>= HIGH, < LOW)',
                f'  Canales totales  : {v["n_channels"]}  →  {", ".join(v["all_channels"][:8])}{"…" if v["n_channels"]>8 else ""}',
                f'  Muestras         : {v["n_samples"]:,}',
                f'  Fs (aprox.)      : {v["sample_rate_hz"]:.4f} Hz',
                f'  Primer (LOCAL)   : {v["first_local"]}  epoch={v["first_epoch"]:.6f}',
                f'  Primer (UTC)     : {v["first_utc"]}',
                f'  Último (LOCAL)   : {v["last_local"]}  epoch={v["last_epoch"]:.6f}',
                f'  Último (UTC)     : {v["last_utc"]}',
                f'  Duración         : {_dur(v["duration_s"])}',
                f'  Zona horaria     : UTC{v["utc_offset"]:+d}',
                f'  Fecha ref. usada : {v["ref_date_used"]}',
                f'  Transiciones tot.: {v["n_transitions"]}',
                f'  HIGH→LOW (↓)     : {v["n_high_to_low"]}',
                f'  LOW→HIGH (↑)     : {v["n_low_to_high"]}',
            ]
            if v['trigger_inicio_local']:
                lines.append(f'  Trigger INICIO   : {v["trigger_inicio_local"]}  epoch={v["trigger_inicio_epoch"]:.6f}')
            if v['trigger_fin_local']:
                lines.append(f'  Trigger FIN      : {v["trigger_fin_local"]}  epoch={v["trigger_fin_epoch"]:.6f}')
            lines += [
                f'  Tamaño           : {v["file_size_kb"]:.2f} KB  ({v["file_size_mb"]:.4f} MB)',
                '',
            ]

        lines += [sep, '  9. ENDPOINTS POR FUENTE (comparación INICIO / FIN)', sep]
        hdr_ep = f"  {'Fuente':<24} {'Inicio LOCAL':<26} {'Fin LOCAL':<26} {'Duración':>12}"
        lines.append(hdr_ep)
        lines.append('  ' + sep2)
        for src, (first, last) in self.file_endpoints.items():
            dur_s = last - first
            if src in self.video_stats:
                dur_txt = self._video_best_duration_label(self.video_stats[src])
            else:
                dur_txt = f'{dur_s:.3f} s'
            lines.append(
                f'  {src:<24} {_ts(first):<26} {_ts(last):<26} {dur_txt:>18}')
        lines.append('  ' + sep2)
        lines.append('')

        lines += [sep, '  10. DIFERENCIAS TEMPORALES', sep]
        lines.append(f"  {'Fuente':<32} {'Ref':<8} {'Δ (s)':>16} {'Δ (ms)':>14}  {'Δ (µs)':>12}  Estado")
        lines.append('  ' + sep2)
        icons = {'PERFECTO': '✅', 'BUENO': '✅', 'ACEPTABLE': '⚠ ', 'DESFASADO': '✗ '}
        for key, d in self.differences.items():
            dms = d['diff_ms']
            lines.append(
                f"  {key:<32} {d['reference']:<8} "
                f"{d['diff_s']:>+15.9f} {dms:>+13.6f}  "
                f"{dms*1000:>+11.3f}  "
                f"{icons.get(d['status'],'')} {d['status']}")
        lines.append('  ' + sep2)

        if self.differences:
            dabs = [abs(d['diff_ms']) for d in self.differences.values()]
            lines += [
                '',
                f'  Máxima desviación : {max(dabs):.6f} ms  ({max(dabs)*1000:.3f} µs)',
                f'  Mínima desviación : {min(dabs):.6f} ms  ({min(dabs)*1000:.3f} µs)',
                f'  Media desviación  : {float(np.mean(dabs)):.6f} ms',
                f'  Std  desviación   : {float(np.std(dabs)):.6f} ms',
                f'  Mediana           : {float(np.median(dabs)):.6f} ms',
                '',
            ]
            mx = max(dabs)
            if   mx < 1:     lines.append('  ✅  EXCELENTE:   máx < 1 ms')
            elif mx < 10:    lines.append('  ✅  MUY BUENO:   máx < 10 ms')
            elif mx < 100:   lines.append('  ✅  BUENO:       máx < 100 ms')
            elif mx < 500:   lines.append('  ✅  ACEPTABLE:   máx < 500 ms')
            elif mx < 1000:  lines.append('  ⚠   REVISABLE:   máx < 1 s')
            else:            lines.append('  ✗   CRÍTICO:     máx >= 1 s')
            if self.system_jitter:
                jms = self.system_jitter * 1000
                lines.append(f'  Jitter referencia : ±{jms:.6f} ms')
                if mx <= jms:       lines.append('  → DENTRO del jitter del sistema (PERFECTO)')
                elif mx <= jms*2:   lines.append('  → ≤ 2× jitter (BUENO)')
                elif mx <= jms*10:  lines.append('  → ≤ 10× jitter (ACEPTABLE)')
                else:               lines.append('  → > 10× jitter (DESFASADO)')
        lines.append('')

        if self.trigger_transitions:
            hl = [t for t in self.trigger_transitions if t['kind'] == 'HIGH→LOW']
            lh = [t for t in self.trigger_transitions if t['kind'] == 'LOW→HIGH']
            lines += [sep, '  11. TRANSICIONES TRIGGER VBOX', sep]
            lines += [
                f'  Total : {len(self.trigger_transitions)}',
                f'  ↓ HIGH→LOW (bajadas): {len(hl)}',
                f'  ↑ LOW→HIGH (subidas): {len(lh)}',
                '',
                f"  {'#':>3}  {'Tipo':<12}  {'UTC':>14}  {'LOCAL':>14}  "
                f"{'Valor(V)':>10}  {'Dt_prev(s)':>12}  {'Evento cercano':<24}  {'Δ evento(ms)':>13}  Inferida",
                '  ' + sep2,
            ]
            for i, t in enumerate(self.trigger_transitions, 1):
                ev  = t.get('closest_event') or '—'
                dif = f"{t['diff_event_ms']:+.3f}" if t['diff_event_ms'] is not None else '—'
                inf = 'Sí' if t.get('inferred') else 'No'
                lines.append(
                    f"  {i:>3}  {t['kind']:<12}  {t['utc']:>14}  {t['local']:>14}  "
                    f"{t['trig_val']:>+10.4f}  {t['dt_prev_s']:>12.4f}  "
                    f"{ev:<24}  {dif:>13}  {inf}")
            lines.append('  ' + sep2)
            lines.append('')

        if self.latency_details:
            lines += [sep, '  12. LATENCIA DETALLADA POR EVENTO (ms)', sep]
            for src, details in self.latency_details.items():
                dvals = [d['diff_ms'] for d in details]
                lines.append(f'\n  ▸ {src}')
                lines.append(f"    {'Evento':<26} {'Δ (ms)':>14}  {'Δ (µs)':>12}  {'Δ (s)':>12}")
                lines.append('    ' + '─' * 66)
                for d in details:
                    lines.append(
                        f"    {d['event']:<26} {d['diff_ms']:>+13.6f}  "
                        f"{d['diff_ms']*1000:>+11.3f}  {d['diff_ms']/1000:>+11.9f}")
                if dvals:
                    abs_v = [abs(x) for x in dvals]
                    lines += [
                        '    ' + '─' * 66,
                        f'    Media: {float(np.mean(dvals)):>+.6f} ms  '
                        f'Std: {float(np.std(dvals)):.6f} ms  '
                        f'Máx abs: {max(abs_v):.6f} ms  '
                        f'Mín abs: {min(abs_v):.6f} ms',
                    ]
            lines.append('')

        if self.advanced_stats:
            lines += [sep, '  13. ESTADÍSTICAS AVANZADAS DE SEÑAL', sep]
            for src, stats in self.advanced_stats.items():
                lines.append(f'\n  ▸ {src}')
                for k, v in stats.items():
                    if isinstance(v, float):
                        lines.append(f'    {k:<34} : {v:.8f}')
                    else:
                        lines.append(f'    {k:<34} : {v}')
            lines.append('')

        lines.append(sep)
        report = '\n'.join(lines)

        rpath = Path(out) / 'sync_report.txt'
        rpath.write_text(report, encoding='utf-8')
        ok('sync_report.txt')

        jpath = Path(out) / 'sync_report.json'
        json_data = {
            'meta': {
                'generated':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'analyzer':     'Unified Sync Analyzer v3.2',
                'events_file':  self.events_file,
            },
            'events': {
                ev: {
                    'epoch':    t,
                    'local':    _ts(t),
                    'utc':      datetime.utcfromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                }
                for ev, t in self.events_dict.items()
            },
            'session': {
                'start_epoch':    self.start_time,
                'end_epoch':      self.end_time,
                'start_local':    _ts(self.start_time) if self.start_time else None,
                'end_local':      _ts(self.end_time)   if self.end_time   else None,
                'duration_s':     (self.end_time - self.start_time)
                                  if self.start_time and self.end_time else None,
                'system_jitter_s':  self.system_jitter,
                'system_jitter_ms': self.system_jitter * 1000 if self.system_jitter else None,
            },
            'pcap':         self.pcap_stats if self.pcap_stats else None,
            'eeg':          {k: v for k, v in self.csv_source_stats.items() if 'EEG' in k},
            'ecg':          {k: v for k, v in self.csv_source_stats.items() if 'ECG' in k},
            'video':        self.video_stats if self.video_stats else None,
            'vbox_sync':    self.vbox_sync_stats if self.vbox_sync_stats else None,
            'vbox_trigger': self.vbox_trigger_stats if self.vbox_trigger_stats else None,
            'endpoints': {
                src: {
                    'first_epoch':  v[0],
                    'last_epoch':   v[1],
                    'first_local':  _ts(v[0]),
                    'last_local':   _ts(v[1]),
                    'duration_s':   v[1] - v[0],
                }
                for src, v in self.file_endpoints.items()
            },
            'differences': {
                key: {
                    'source':    d['source'],
                    'reference': d['reference'],
                    'diff_s':    d['diff_s'],
                    'diff_ms':   d['diff_ms'],
                    'diff_us':   d['diff_ms'] * 1000,
                    'status':    d['status'],
                    'abs_diff_ms': abs(d['diff_ms']),
                }
                for key, d in self.differences.items()
            },
            'differences_summary': {
                'max_abs_ms':    max(abs(d['diff_ms']) for d in self.differences.values()) if self.differences else None,
                'min_abs_ms':    min(abs(d['diff_ms']) for d in self.differences.values()) if self.differences else None,
                'mean_abs_ms':   float(np.mean([abs(d['diff_ms']) for d in self.differences.values()])) if self.differences else None,
                'std_ms':        float(np.std([abs(d['diff_ms']) for d in self.differences.values()])) if self.differences else None,
                'median_abs_ms': float(np.median([abs(d['diff_ms']) for d in self.differences.values()])) if self.differences else None,
            } if self.differences else {},
            'latency_per_event': {
                src: [
                    {
                        'event':    d['event'],
                        'diff_ms':  d['diff_ms'],
                        'diff_us':  d['diff_ms'] * 1000,
                        'diff_s':   d['diff_ms'] / 1000,
                    }
                    for d in details
                ]
                for src, details in self.latency_details.items()
            },
            'trigger_transitions': [
                {
                    'index':         i + 1,
                    'kind':          t['kind'],
                    'epoch':         t['epoch'],
                    'utc':           t['utc'],
                    'local':         t['local'],
                    'trigger_value_v': t['trig_val'],
                    'dt_prev_s':     t['dt_prev_s'],
                    'inferred':      t.get('inferred', False),
                    'closest_event': t.get('closest_event'),
                    'diff_event_ms': t.get('diff_event_ms'),
                    'diff_event_us': t['diff_event_ms'] * 1000 if t.get('diff_event_ms') is not None else None,
                }
                for i, t in enumerate(self.trigger_transitions)
            ],
            'advanced_stats': self.advanced_stats,
        }

        jpath.write_text(
            json.dumps(json_data, indent=2, default=str, ensure_ascii=False),
            encoding='utf-8')
        ok('sync_report.json')

        return str(rpath)


def main():
    if os.name == 'nt':
        os.system('')

    print(_col("""
╔══════════════════════════════════════════════════════════════════════════════╗
║         UNIFIED SYNCHRONIZATION & LATENCY ANALYZER  v3.2                   ║
║         PCAP · EEG · ECG · Video · VBox Sync · VBox Trigger                ║
╚══════════════════════════════════════════════════════════════════════════════╝
""", CYAN, BOLD))

    az = SyncAnalyzer()

    if not az.select_files():
        print(_col('\n  Selección cancelada. Saliendo.\n', RED))
        sys.exit(0)

    if not az.load_events():
        print(_col('\n  Error crítico en eventos. Saliendo.\n', RED))
        sys.exit(1)

    az.analyze_all()

    out = az.results_folder
    print(f'\n  {_col("📁  Resultados →", BOLD)} {_col(out, CYAN)}')

    az.save_figures(out)
    az.save_report(out)

    header('ANÁLISIS COMPLETADO')
    print(f'\n  {_col("✅", GREEN, BOLD)}  Todos los archivos guardados en:')
    print(f'     {_col(out, CYAN, BOLD)}\n')

    try:
        subprocess.Popen(['explorer', out])
    except Exception:
        pass


if __name__ == '__main__':
    main()