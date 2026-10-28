"""
ECG Derivations Calculator - VERSIÓN MULTI-HILO POR DERIVACIÓN
Cada derivación válida se procesa en su propio hilo independiente
Solo se procesan derivaciones con electrodos disponibles

CAMBIO: lectura incremental de los CSV de electrodos (posición en bytes),
en lugar de reabrir y reparsear todo el archivo cada 10 ms.
"""

import time
import csv
from pathlib import Path
from threading import Thread, Lock, Event
from collections import defaultdict


class ECGRealtimeDerivationsCalculator:
    """
    Calcula derivaciones del ECG en tiempo real con procesamiento multi-hilo.
    Cada derivación válida tiene su propio hilo de procesamiento.
    """

    # Mapeo de derivaciones a electrodos requeridos
    DERIVATION_REQUIREMENTS = {
        'I': ['RA', 'LA'],
        'II': ['RA', 'LL'],
        'III': ['LA', 'LL'],
        'aVR': ['RA', 'LA', 'LL'],
        'aVL': ['RA', 'LA', 'LL'],
        'aVF': ['RA', 'LA', 'LL'],
        'V1': ['RA', 'LA', 'LL', 'V1'],
        'V2': ['RA', 'LA', 'LL', 'V2'],
        'V3': ['RA', 'LA', 'LL', 'V3'],
        'V4': ['RA', 'LA', 'LL', 'V4'],
        'V5': ['RA', 'LA', 'LL', 'V5'],
        'V6': ['RA', 'LA', 'LL', 'V6']
    }

    ELECTRODE_POSITIONS = ['RA', 'LA', 'LL', 'V1', 'V2', 'V3', 'V4', 'V5', 'V6']

    GAP_TOLERANCE = 400         # muestras que se espera por un hueco antes de darlo por perdido
    CSV_FLUSH_INTERVAL = 0.5    # s entre flush() de los CSV de derivaciones
    WS_MAX_MESSAGE = 500        # muestras máximas por mensaje WebSocket

    def __init__(self, session_folder, electrodes_mapping, ws_callback=None):
        """
        Inicializa el calculador

        Args:
            session_folder: Carpeta de la sesión (donde están los CSVs de electrodos)
            electrodes_mapping: Dict {slave_id: 'electrode_position'} ej: {1: 'RA', 2: 'LA', 3: 'LL'}
            ws_callback: Función callback para enviar datos por WebSocket
        """
        self.session_folder = Path(session_folder)
        self.electrodes_mapping = electrodes_mapping
        self.position_to_slave = {pos: sid for sid, pos in electrodes_mapping.items()}

        # Control de estado
        self.running = False
        self.stop_event = Event()
        self.electrode_reader_threads = {}
        self.derivation_processor_threads = {}

        # Determinar derivaciones válidas
        self.available_electrodes = set(self.position_to_slave.keys())
        self.valid_derivations = self._get_valid_derivations()

        print(f"\n[ECG_RT] ========================================")
        print(f"[ECG_RT] INICIALIZACIÓN")
        print(f"[ECG_RT] ========================================")
        print(f"[ECG_RT] Sesión: {session_folder}")
        print(f"[ECG_RT] Electrodos disponibles: {sorted(self.available_electrodes)}")
        print(f"[ECG_RT] Derivaciones VÁLIDAS: {sorted(self.valid_derivations)}")
        print(f"[ECG_RT] Total derivaciones a procesar: {len(self.valid_derivations)}")

        # Archivos CSV de electrodos
        self.electrode_files = {}

        # Buffer de datos por posición de electrodo
        self.electrode_data = defaultdict(dict)
        self.electrode_locks = {pos: Lock() for pos in self.available_electrodes}

        # Archivos CSV de derivaciones
        parent_folder = self.session_folder.parent
        self.derivations_csv_folder = parent_folder / "derivaciones"
        self.derivations_csv_folder.mkdir(exist_ok=True)

        # WebSocket batching
        self.ws_callback = ws_callback
        self.ws_batch_size = 200          # se envía al juntar 200 muestras...
        self.ws_flush_interval = 0.2      # ...o cada 200 ms, lo que ocurra primero

        # Mayor muestra vista por electrodo (la escribe solo su hilo lector)
        self.electrode_max = {pos: -1 for pos in self.available_electrodes}

        # Estado por derivación
        self.derivation_stats = {deriv: {
            'last_processed_sample': -1,
            'total_calculated': 0,
            'ws_batch_buffer': [],
            'ws_last_flush': time.time(),
            'ws_sent': 0,
            'lost': 0,
            'csv_last_flush': time.time(),
            'csv_writer': None,
            'csv_file': None
        } for deriv in self.valid_derivations}

        self._init_electrode_files()
        self._init_derivation_csvs()

    def _get_valid_derivations(self):
        """Determina qué derivaciones se pueden calcular con los electrodos disponibles"""
        valid = []

        for deriv, required in self.DERIVATION_REQUIREMENTS.items():
            if all(electrode in self.available_electrodes for electrode in required):
                valid.append(deriv)

        return valid

    def _init_electrode_files(self):
        """Inicializa las rutas de los archivos CSV de electrodos para lectura"""
        for position in self.available_electrodes:
            slave_id = self.position_to_slave[position]
            filepath = self.session_folder / f"{position}_{slave_id}_data.csv"

            self.electrode_files[position] = filepath

            print(f"[ECG_RT] Archivo electrodo {position}: {filepath}")

    def _init_derivation_csvs(self):
        """Inicializa archivos CSV solo para derivaciones válidas"""
        print(f"\n[ECG_RT] Creando CSVs de derivaciones...")

        for deriv in self.valid_derivations:
            csv_path = self.derivations_csv_folder / f"{deriv}_derivations.csv"
            file_handle = open(csv_path, 'w', newline='', encoding='utf-8')
            writer = csv.writer(file_handle)
            writer.writerow(['sample_num', 'derivation_value'])
            file_handle.flush()

            self.derivation_stats[deriv]['csv_file'] = file_handle
            self.derivation_stats[deriv]['csv_writer'] = writer

            print(f"[ECG_RT] ✓ CSV creado: {deriv} -> {csv_path}")

    # ==================== LECTURA DE ELECTRODOS ====================

    def _electrode_reader_loop(self, position):
        """Lee de forma incremental el CSV de un electrodo (solo bytes nuevos)."""
        print(f"[ECG_RT][{position}] Hilo de lectura iniciado")

        filepath = self.electrode_files[position]
        file_pos = 0
        pending = b''            # resto de una línea incompleta
        idx_sample = idx_voltage = None
        last_report = time.time()
        last_cleanup = time.time()
        samples_read = 0

        while not self.stop_event.is_set():
            try:
                if not filepath.exists():
                    self.stop_event.wait(timeout=0.1)
                    continue

                with open(filepath, 'rb') as f:
                    f.seek(file_pos)
                    chunk = f.read()
                    file_pos = f.tell()

                if not chunk:
                    self.stop_event.wait(timeout=0.02)
                    continue

                lines = (pending + chunk).split(b'\n')
                pending = lines.pop()            # la última línea puede estar a medias

                batch = {}
                for raw in lines:
                    line = raw.decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue

                    if idx_sample is None:       # primera línea = encabezado
                        header = [h.strip() for h in line.split(',')]
                        try:
                            idx_sample = header.index('sample_num')
                            idx_voltage = header.index('voltage')
                        except ValueError:
                            idx_sample = idx_voltage = None
                        continue

                    parts = line.split(',')
                    try:
                        batch[int(parts[idx_sample])] = float(parts[idx_voltage])
                    except (ValueError, IndexError):
                        continue

                if batch:
                    with self.electrode_locks[position]:     # un solo lock por lote
                        self.electrode_data[position].update(batch)
                    top = max(batch)
                    if top > self.electrode_max[position]:
                        self.electrode_max[position] = top   # se publica DESPUÉS de guardar los datos
                    samples_read += len(batch)

                if time.time() - last_cleanup >= 1.0:
                    self._cleanup_electrode(position)
                    last_cleanup = time.time()

                # Reportar cada 5 segundos
                if time.time() - last_report >= 5.0:
                    if samples_read > 0:
                        print(f"[ECG_RT][{position}] ✓ {samples_read} muestras leídas en 5s")
                        samples_read = 0
                    last_report = time.time()

            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"[ECG_RT][{position}] ERROR: {e}")
                self.stop_event.wait(timeout=0.5)

        print(f"[ECG_RT][{position}] Hilo de lectura detenido")

    # ==================== CÁLCULO DE DERIVACIONES ====================

    @staticmethod
    def _compute_value(derivation_name, v):
        """Calcula una derivación a partir de los voltajes {posición: voltaje}.
        Las fórmulas se conservan tal cual (la etapa analógica invierte la señal)."""
        RA = v.get('RA', 0)
        LA = v.get('LA', 0)
        LL = v.get('LL', 0)

        if derivation_name == 'I':
            return LA - (-RA)
        elif derivation_name == 'II':
            return LL - (-RA)
        elif derivation_name == 'III':
            return LL - (-LA)
        elif derivation_name == 'aVR':
            return RA - ((-LA + -LL) / 2)
        elif derivation_name == 'aVL':
            return LA - ((-RA + -LL) / 2)
        elif derivation_name == 'aVF':
            return LL - ((-RA + -LA) / 2)
        elif derivation_name in ('V1', 'V2', 'V3', 'V4', 'V5', 'V6'):
            VW_NEG = (-RA + -LA + -LL) / 3
            return v[derivation_name] - VW_NEG
        return None

    def _process_derivation_once(self, derivation_name):
        """
        Procesa TODAS las muestras nuevas disponibles de una derivación.
        Trabaja solo sobre el rango (último_procesado, mínimo de los máximos de sus
        electrodos], por lo que el costo es proporcional a las muestras nuevas
        (antes se construían conjuntos con todo el historial en cada vuelta).
        Devuelve la cantidad de muestras procesadas.
        """
        required = self.DERIVATION_REQUIREMENTS[derivation_name]
        st = self.derivation_stats[derivation_name]
        last = st['last_processed_sample']

        limit = min(self.electrode_max[p] for p in required)
        if limit <= last:
            return 0

        first = last + 1
        rng = range(first, limit + 1)

        # Una sola toma de lock por electrodo para copiar el rango
        snaps = {}
        for pos in required:
            with self.electrode_locks[pos]:
                data = self.electrode_data[pos]
                snaps[pos] = [data.get(s) for s in rng]

        rows = []
        new_last = last
        for i, s in enumerate(rng):
            vals = {}
            missing = False
            for pos in required:
                x = snaps[pos][i]
                if x is None:
                    missing = True
                    break
                vals[pos] = x

            if missing:
                # Un hueco puede ser un paquete que aún no llega: se espera
                # GAP_TOLERANCE muestras; si ya hay datos mucho más nuevos, se da por perdida.
                if limit - s > self.GAP_TOLERANCE:
                    st['lost'] += 1
                    new_last = s
                    continue
                break

            value = self._compute_value(derivation_name, vals)
            if value is None:
                new_last = s
                continue
            rows.append((s, value))
            new_last = s

        if rows:
            self._save_derivation_rows(derivation_name, rows)
            self._queue_ws_rows(derivation_name, rows)
            st['total_calculated'] += len(rows)

        st['last_processed_sample'] = new_last
        return len(rows)

    def _save_derivation_rows(self, derivation_name, rows):
        """Guarda un grupo de muestras en el CSV (flush como máximo cada CSV_FLUSH_INTERVAL s)."""
        if self.stop_event.is_set():
            return

        st = self.derivation_stats[derivation_name]
        try:
            writer = st['csv_writer']
            file_handle = st['csv_file']

            if file_handle and not file_handle.closed:
                writer.writerows((s, round(v, 6)) for s, v in rows)
                now = time.time()
                if now - st['csv_last_flush'] >= self.CSV_FLUSH_INTERVAL:
                    file_handle.flush()
                    st['csv_last_flush'] = now

        except (ValueError, OSError):
            pass  # archivo cerrado durante el apagado
        except Exception as e:
            if not self.stop_event.is_set():
                print(f"[ECG_RT][{derivation_name}] ERROR guardando CSV: {e}")

    # ==================== ENVÍO WEBSOCKET ====================

    def _queue_ws_rows(self, derivation_name, rows):
        """Encola muestras para WebSocket; envía por tamaño o por tiempo."""
        if not self.ws_callback:
            return
        buf = self.derivation_stats[derivation_name]['ws_batch_buffer']
        buf.extend({'sample_num': s, 'value': round(v, 6)} for s, v in rows)
        self._flush_ws_if_due(derivation_name)

    def _flush_ws_if_due(self, derivation_name, force=False):
        if not self.ws_callback:
            return
        st = self.derivation_stats[derivation_name]
        buf = st['ws_batch_buffer']
        if not buf:
            return
        now = time.time()
        if force or len(buf) >= self.ws_batch_size or (now - st['ws_last_flush']) >= self.ws_flush_interval:
            self._send_ws_batch(derivation_name)
            st['ws_last_flush'] = now

    def _send_ws_batch(self, derivation_name):
        """Envía el buffer de una derivación en mensajes de a lo más WS_MAX_MESSAGE muestras."""
        if not self.ws_callback:
            return

        st = self.derivation_stats[derivation_name]
        buf = st['ws_batch_buffer']
        if not buf:
            return

        # Se vacía antes de enviar: si el envío falla no se reenvía basura duplicada
        pending = list(buf)
        buf.clear()

        try:
            for i in range(0, len(pending), self.WS_MAX_MESSAGE):
                chunk = pending[i:i + self.WS_MAX_MESSAGE]   # ya viene ordenado por sample_num
                self.ws_callback('ecg_derivation_batch', {
                    'derivation': derivation_name,
                    'samples': chunk,
                    'batch_size': len(chunk),
                    'first_sample': chunk[0]['sample_num'],
                    'last_sample': chunk[-1]['sample_num']
                })
                st['ws_sent'] += len(chunk)
        except Exception as e:
            print(f"[ECG_RT][{derivation_name}][WS] ERROR: {e}")

    def _derivation_processor_loop(self, derivation_name):
        """Loop de procesamiento para una derivación específica"""
        print(f"[ECG_RT][{derivation_name}] Hilo de procesamiento iniciado")

        last_report = time.time()
        samples_processed = 0

        while not self.stop_event.is_set():
            try:
                n = self._process_derivation_once(derivation_name)
                samples_processed += n

                # Aunque no haya muestras nuevas, vaciar el lote por tiempo
                self._flush_ws_if_due(derivation_name)

                if time.time() - last_report >= 5.0:
                    st = self.derivation_stats[derivation_name]
                    if samples_processed > 0 or st['lost']:
                        print(f"[ECG_RT][{derivation_name}] ✓ {samples_processed} muestras en 5s "
                              f"(total: {st['total_calculated']}, perdidas: {st['lost']})")
                    samples_processed = 0
                    last_report = time.time()

                if n == 0:
                    self.stop_event.wait(timeout=0.01)

            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"[ECG_RT][{derivation_name}] ERROR: {e}")
                self.stop_event.wait(timeout=0.5)

        # Último envío y flush del CSV
        self._flush_ws_if_due(derivation_name, force=True)
        try:
            fh = self.derivation_stats[derivation_name]['csv_file']
            if fh and not fh.closed:
                fh.flush()
        except (ValueError, OSError):
            pass

        print(f"[ECG_RT][{derivation_name}] Hilo de procesamiento detenido")

    def _cleanup_electrode(self, position):
        """
        Libera memoria de un electrodo SOLO con muestras que ya procesaron todas las
        derivaciones que lo usan (antes, cada hilo borraba por su cuenta y podía
        eliminar muestras que otra derivación todavía no leía).
        """
        users = [d for d in self.valid_derivations
                 if position in self.DERIVATION_REQUIREMENTS[d]]
        if users:
            floor = min(self.derivation_stats[d]['last_processed_sample'] for d in users) - 100
        else:
            floor = self.electrode_max[position] - 2000

        with self.electrode_locks[position]:
            data = self.electrode_data[position]
            if len(data) > 3000:
                for s in [k for k in data if k < floor]:
                    del data[s]

    # ==================== CONTROL DEL SISTEMA ====================

    def start(self):
        """Inicia todos los hilos de lectura y procesamiento"""
        if self.running:
            print("[ECG_RT] Ya está ejecutándose")
            return False

        print("\n[ECG_RT] ========================================")
        print("[ECG_RT] INICIANDO PROCESAMIENTO MULTI-HILO")
        print("[ECG_RT] ========================================\n")

        self.running = True

        # Iniciar hilos de lectura de electrodos
        print(f"[ECG_RT] Iniciando {len(self.available_electrodes)} hilos de lectura de electrodos...")
        for position in self.available_electrodes:
            thread = Thread(
                target=self._electrode_reader_loop,
                args=(position,),
                name=f"Reader-{position}",
                daemon=False
            )
            thread.start()
            self.electrode_reader_threads[position] = thread
            print(f"[ECG_RT] ✓ Hilo lector {position} iniciado")

        time.sleep(0.5)

        # Iniciar hilos de procesamiento de derivaciones
        print(f"\n[ECG_RT] Iniciando {len(self.valid_derivations)} hilos de procesamiento de derivaciones...")
        for derivation in self.valid_derivations:
            thread = Thread(
                target=self._derivation_processor_loop,
                args=(derivation,),
                name=f"Processor-{derivation}",
                daemon=False
            )
            thread.start()
            self.derivation_processor_threads[derivation] = thread
            print(f"[ECG_RT] ✓ Hilo procesador {derivation} iniciado")

        print("\n[ECG_RT] ✓ Sistema completamente iniciado")
        print(f"[ECG_RT] Total hilos activos: {len(self.electrode_reader_threads) + len(self.derivation_processor_threads)}")

        return True

    def stop(self):
        """Detiene todos los hilos de forma ordenada"""
        if not self.running:
            return

        print("\n[ECG_RT] ========================================")
        print("[ECG_RT] DETENIENDO SISTEMA")
        print("[ECG_RT] ========================================\n")

        # Guardar estadísticas ANTES de comenzar el cierre
        final_stats = {}
        try:
            for deriv in self.valid_derivations:
                final_stats[deriv] = {
                    'total_calculated': self.derivation_stats[deriv]['total_calculated'],
                    'last_processed_sample': self.derivation_stats[deriv]['last_processed_sample']
                }
        except Exception as e:
            print(f"[ECG_RT] WARN: No se pudieron guardar estadísticas: {e}")

        # PASO 1: Señalar a todos los hilos que deben detenerse
        print("[ECG_RT] Señalando detención a todos los hilos...")
        self.running = False
        self.stop_event.set()

        # PASO 2: Esperar hilos de lectura
        print("[ECG_RT] Esperando hilos de lectura...")
        for position, thread in self.electrode_reader_threads.items():
            if thread.is_alive():
                thread.join(timeout=3)
                status = "✓" if not thread.is_alive() else "⚠️ timeout"
                print(f"[ECG_RT] {status} Hilo lector {position}")

        # PASO 3: Esperar hilos de procesamiento
        print("\n[ECG_RT] Esperando hilos de procesamiento...")
        for derivation, thread in self.derivation_processor_threads.items():
            if thread.is_alive():
                thread.join(timeout=3)
                status = "✓" if not thread.is_alive() else "⚠️ timeout"
                print(f"[ECG_RT] {status} Hilo procesador {derivation}")

        # PASO 4: Cerrar archivos CSV
        print("\n[ECG_RT] Cerrando archivos CSV...")
        for deriv in self.valid_derivations:
            try:
                file_handle = self.derivation_stats[deriv]['csv_file']
                if file_handle and not file_handle.closed:
                    file_handle.flush()
                    file_handle.close()
                    print(f"[ECG_RT] ✓ CSV cerrado: {deriv}")
            except Exception as e:
                print(f"[ECG_RT] ERROR cerrando {deriv}: {e}")

        # PASO 5: Mostrar estadísticas finales (usando stats guardadas)
        print("\n[ECG_RT] ========================================")
        print("[ECG_RT] ESTADÍSTICAS FINALES")
        print("[ECG_RT] ========================================")

        if final_stats:
            total_samples = sum(stats['total_calculated'] for stats in final_stats.values())
            print(f"[ECG_RT] Total muestras procesadas: {total_samples}")
            for deriv in sorted(final_stats.keys()):
                stats = final_stats[deriv]
                print(f"[ECG_RT] {deriv:>4}: {stats['total_calculated']:>6} muestras (último: {stats['last_processed_sample']})")
        else:
            print("[ECG_RT] ⚠️  Estadísticas no disponibles")

        print("\n[ECG_RT] ✓ Sistema detenido completamente")

    def get_stats(self):
        """Retorna estadísticas del sistema - versión robusta"""
        try:
            return {
                'running': self.running,
                'available_electrodes': sorted(self.available_electrodes),
                'valid_derivations': sorted(self.valid_derivations),
                'derivation_stats': {
                    deriv: {
                        'last_processed': stats.get('last_processed_sample', -1),
                        'total_calculated': stats.get('total_calculated', 0),
                        'ws_buffer_size': len(stats.get('ws_batch_buffer', [])),
                        'ws_sent': stats.get('ws_sent', 0),
                        'lost': stats.get('lost', 0)
                    }
                    for deriv, stats in self.derivation_stats.items()
                },
                'active_threads': {
                    'readers': len([t for t in self.electrode_reader_threads.values() if t.is_alive()]),
                    'processors': len([t for t in self.derivation_processor_threads.values() if t.is_alive()])
                },
                'derivations_folder': str(self.derivations_csv_folder)
            }
        except Exception as e:
            # Si falla, retornar info básica
            return {
                'running': self.running,
                'error': str(e),
                'available_electrodes': sorted(self.available_electrodes) if hasattr(self, 'available_electrodes') else [],
                'valid_derivations': sorted(self.valid_derivations) if hasattr(self, 'valid_derivations') else []
            }


# ==================== EJEMPLO DE USO ====================

if __name__ == "__main__":
    import os

    # Configuración de ejemplo
    session_folder = "./test_session"
    os.makedirs(session_folder, exist_ok=True)

    # Solo 3 electrodos disponibles (RA, LA, LL)
    electrodes_mapping = {
        1: 'RA',
        2: 'LA',
        3: 'LL'
    }

    print("\n[TEST] ========================================")
    print("[TEST] PRUEBA DE CALCULADOR MULTI-HILO")
    print("[TEST] ========================================")
    print(f"[TEST] Electrodos configurados: {electrodes_mapping}")
    print("[TEST] Se calcularán solo: I, II, III, aVR, aVL, aVF")
    print("[TEST] NO se calcularán: V1-V6 (faltan electrodos)\n")

    # Simular CSVs de electrodos
    for slave_id, position in electrodes_mapping.items():
        csv_path = Path(session_folder) / f"{position}_{slave_id}_data.csv"
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp_us', 'sample_num', 'adc_value', 'voltage'])

            for i in range(50):
                timestamp = i * 1000
                adc = 2048 + i * 10
                voltage = adc * (3.3 / 4095)
                writer.writerow([timestamp, i, adc, voltage])

    # Crear calculador
    calculator = ECGRealtimeDerivationsCalculator(session_folder, electrodes_mapping)

    # Iniciar procesamiento
    calculator.start()

    # Simular escritura continua
    print("\n[TEST] Simulando adquisición continua...\n")

    for cycle in range(3):
        time.sleep(3)

        # Agregar más datos
        for slave_id, position in electrodes_mapping.items():
            csv_path = Path(session_folder) / f"{position}_{slave_id}_data.csv"
            with open(csv_path, 'a', newline='') as f:
                writer = csv.writer(f)
                for i in range(50 + cycle * 50, 100 + cycle * 50):
                    timestamp = i * 1000
                    adc = 2048 + i * 10
                    voltage = adc * (3.3 / 4095)
                    writer.writerow([timestamp, i, adc, voltage])

        print(f"\n[TEST] Ciclo {cycle + 1}: Datos agregados")

    # Detener
    time.sleep(3)
    calculator.stop()

    print("\n[TEST] ✓ Prueba completada")
