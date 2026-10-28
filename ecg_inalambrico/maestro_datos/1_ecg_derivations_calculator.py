"""
ECG Derivations Calculator - VERSIÓN MULTI-HILO POR DERIVACIÓN
Cada derivación válida se procesa en su propio hilo independiente
Solo se procesan derivaciones con electrodos disponibles
"""

import time
import csv
from pathlib import Path
from threading import Thread, Lock
from collections import defaultdict
from threading import Thread, Lock, Event  # Event ya está importado

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
        self.stop_event = Event()  # **NUEVO**: Event para señalización
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
        self.electrode_last_line = {}
        
        # Buffer de datos por posición de electrodo
        self.electrode_data = defaultdict(dict)
        self.electrode_locks = {pos: Lock() for pos in self.available_electrodes}
        
        # Archivos CSV de derivaciones
        parent_folder = self.session_folder.parent
        self.derivations_csv_folder = parent_folder / "derivaciones"
        self.derivations_csv_folder.mkdir(exist_ok=True)
        
        # WebSocket batching
        self.ws_callback = ws_callback
        self.ws_batch_size = 200
        
        # Control de estado
        self.running = False
        self.electrode_reader_threads = {}
        self.derivation_processor_threads = {}
        
        # Estado por derivación
        self.derivation_stats = {deriv: {
            'last_processed_sample': -1,
            'total_calculated': 0,
            'ws_batch_buffer': [],
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
        """Inicializa los archivos CSV de electrodos para lectura"""
        for position in self.available_electrodes:
            slave_id = self.position_to_slave[position]
            filepath = self.session_folder / f"{position}_{slave_id}_data.csv"
            
            self.electrode_files[position] = filepath
            self.electrode_last_line[position] = 0
            
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
        """Loop de lectura continua para un electrodo específico"""
        print(f"[ECG_RT][{position}] Hilo de lectura iniciado")
        
        filepath = self.electrode_files[position]
        last_report = time.time()
        samples_read = 0
        
        while not self.stop_event.is_set():  # **CAMBIADO**: usar Event en vez de self.running
            try:
                if not filepath.exists():
                    time.sleep(0.1)
                    continue
                
                new_samples = 0
                
                with open(filepath, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    
                    # Saltar líneas ya leídas
                    for _ in range(self.electrode_last_line[position]):
                        try:
                            next(reader)
                        except StopIteration:
                            break
                    
                    # Leer nuevas líneas
                    for row in reader:
                        if self.stop_event.is_set():  # **NUEVO**: Check en loop interno
                            break
                        
                        try:
                            sample_num = int(row['sample_num'])
                            voltage = float(row['voltage'])
                            
                            with self.electrode_locks[position]:
                                self.electrode_data[position][sample_num] = voltage
                            
                            new_samples += 1
                            self.electrode_last_line[position] += 1
                            samples_read += 1
                        
                        except (ValueError, KeyError):
                            continue
                
                # Reportar cada 5 segundos
                if time.time() - last_report >= 5.0:
                    if samples_read > 0:
                        print(f"[ECG_RT][{position}] ✓ {samples_read} muestras leídas en 5s")
                        samples_read = 0
                    last_report = time.time()
                
                # Pequeña pausa si no hay datos nuevos
                if new_samples == 0:
                    self.stop_event.wait(timeout=0.01)  # **CAMBIADO**: usar Event.wait()
            
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"[ECG_RT][{position}] ERROR: {e}")
                self.stop_event.wait(timeout=0.5)
        
        print(f"[ECG_RT][{position}] Hilo de lectura detenido")
    
    # ==================== CÁLCULO DE DERIVACIONES ====================
    
    def _calculate_derivation(self, derivation_name, sample_num):
        """Calcula una derivación específica para un sample_num"""
        required = self.DERIVATION_REQUIREMENTS[derivation_name]
        
        # Obtener voltajes necesarios
        voltages = {}
        for pos in required:
            with self.electrode_locks[pos]:
                if sample_num not in self.electrode_data[pos]:
                    return None  # Datos incompletos
                voltages[pos] = self.electrode_data[pos][sample_num]
        
        # Calcular según el tipo de derivación
        RA = voltages.get('RA', 0)
        LA = voltages.get('LA', 0)
        LL = voltages.get('LL', 0)
        
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
        
        elif derivation_name in ['V1', 'V2', 'V3', 'V4', 'V5', 'V6']:
            VW_NEG = (-RA + -LA + -LL) / 3
            return voltages[derivation_name] - VW_NEG
        
        return None
    
    def _get_next_sample_to_process(self, derivation_name):
        """Obtiene el siguiente sample_num disponible para una derivación"""
        required = self.DERIVATION_REQUIREMENTS[derivation_name]
        last_processed = self.derivation_stats[derivation_name]['last_processed_sample']
        
        # Obtener samples disponibles en TODOS los electrodos requeridos
        sample_sets = []
        for pos in required:
            with self.electrode_locks[pos]:
                samples = set(self.electrode_data[pos].keys())
                sample_sets.append(samples)
        
        if not sample_sets:
            return None
        
        # Intersección de todos los sets
        common_samples = set.intersection(*sample_sets)
        
        # Filtrar samples ya procesados
        new_samples = sorted([s for s in common_samples if s > last_processed])
        
        return new_samples if new_samples else None
    
    def _save_derivation_to_csv(self, derivation_name, sample_num, value):
        """Guarda una muestra en el CSV de la derivación"""
        if self.stop_event.is_set():  # **CAMBIADO**: verificar Event
            return
        
        try:
            writer = self.derivation_stats[derivation_name]['csv_writer']
            file_handle = self.derivation_stats[derivation_name]['csv_file']
            
            # Verificar que el archivo esté abierto
            if file_handle and not file_handle.closed:
                writer.writerow([sample_num, round(value, 6)])
                file_handle.flush()
        
        except (ValueError, OSError) as e:
            # Archivo cerrado o no disponible - ignorar silenciosamente
            pass
        except Exception as e:
            if not self.stop_event.is_set():
                print(f"[ECG_RT][{derivation_name}] ERROR guardando CSV: {e}")
    
    def _add_to_ws_batch(self, derivation_name, sample_num, value):
        """Agrega muestra al batch de WebSocket"""
        if not self.ws_callback:
            return
        
        batch_buffer = self.derivation_stats[derivation_name]['ws_batch_buffer']
        batch_buffer.append({
            'sample_num': sample_num,
            'value': round(value, 6)
        })
        
        # Enviar si alcanzamos el tamaño del batch
        if len(batch_buffer) >= self.ws_batch_size:
            self._send_ws_batch(derivation_name)
    
    def _send_ws_batch(self, derivation_name):
        """Envía un batch de WebSocket para una derivación"""
        if not self.ws_callback:
            return
        
        batch_buffer = self.derivation_stats[derivation_name]['ws_batch_buffer']
        
        if not batch_buffer:
            return
        
        try:
            batch_sorted = sorted(batch_buffer, key=lambda x: x['sample_num'])
            
            self.ws_callback('ecg_derivation_batch', {
                'derivation': derivation_name,
                'samples': batch_sorted,
                'batch_size': len(batch_sorted),
                'first_sample': batch_sorted[0]['sample_num'],
                'last_sample': batch_sorted[-1]['sample_num']
            })
            
            print(f"[ECG_RT][{derivation_name}][WS] ✓ Batch enviado: {len(batch_sorted)} muestras ({batch_sorted[0]['sample_num']}-{batch_sorted[-1]['sample_num']})")
            
            batch_buffer.clear()
        
        except Exception as e:
            print(f"[ECG_RT][{derivation_name}][WS] ERROR: {e}")
    
    def _derivation_processor_loop(self, derivation_name):
        """Loop de procesamiento para una derivación específica"""
        print(f"[ECG_RT][{derivation_name}] Hilo de procesamiento iniciado")
        
        last_report = time.time()
        samples_processed = 0
        
        while not self.stop_event.is_set():  # **CAMBIADO**: usar Event
            try:
                # Obtener siguiente sample disponible
                available_samples = self._get_next_sample_to_process(derivation_name)
                
                if available_samples:
                    for sample_num in available_samples:
                        if self.stop_event.is_set():  # **NUEVO**: Check en loop interno
                            break
                        
                        # Calcular derivación
                        value = self._calculate_derivation(derivation_name, sample_num)
                        
                        if value is not None:
                            # Guardar en CSV
                            self._save_derivation_to_csv(derivation_name, sample_num, value)
                            
                            # Agregar a batch WebSocket
                            self._add_to_ws_batch(derivation_name, sample_num, value)
                            
                            # Actualizar estadísticas
                            self.derivation_stats[derivation_name]['last_processed_sample'] = sample_num
                            self.derivation_stats[derivation_name]['total_calculated'] += 1
                            samples_processed += 1
                            
                            # Limpiar buffer antiguo
                            self._cleanup_old_samples(derivation_name, sample_num)
                
                # Reportar cada 5 segundos
                if time.time() - last_report >= 5.0:
                    if samples_processed > 0:
                        total = self.derivation_stats[derivation_name]['total_calculated']
                        print(f"[ECG_RT][{derivation_name}] ✓ {samples_processed} muestras en 5s (total: {total})")
                        samples_processed = 0
                    last_report = time.time()
                
                # Pausa si no hay datos
                if not available_samples:
                    self.stop_event.wait(timeout=0.01)  # **CAMBIADO**: usar Event.wait()
            
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"[ECG_RT][{derivation_name}] ERROR: {e}")
                self.stop_event.wait(timeout=0.5)
        
        # Enviar batch final
        self._send_ws_batch(derivation_name)
        
        print(f"[ECG_RT][{derivation_name}] Hilo de procesamiento detenido")
    
    def _cleanup_old_samples(self, derivation_name, current_sample):
        """Limpia samples antiguos del buffer para liberar memoria"""
        required = self.DERIVATION_REQUIREMENTS[derivation_name]
        threshold = current_sample - 500
        
        for pos in required:
            with self.electrode_locks[pos]:
                if len(self.electrode_data[pos]) > 600:
                    old_samples = [s for s in self.electrode_data[pos] if s < threshold]
                    for old_s in old_samples:
                        del self.electrode_data[pos][old_s]
    
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
        
        # **NUEVO**: Guardar estadísticas ANTES de comenzar el cierre
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
                        'last_processed': stats.get('last_processed_sample', -1),  # **CAMBIADO**: usar .get()
                        'total_calculated': stats.get('total_calculated', 0),      # **CAMBIADO**: usar .get()
                        'ws_buffer_size': len(stats.get('ws_batch_buffer', []))    # **CAMBIADO**: usar .get()
                    }
                    for deriv, stats in self.derivation_stats.items()
                },
                'active_threads': {
                    'readers': len([t for t in self.electrode_reader_threads.values() if t.is_alive()]),
                    'processors': len([t for t in self.derivation_processor_threads.values() if t.is_alive()])
                },
                'derivations_folder': str(self.derivations_csv_folder)  # **NUEVO**: agregar carpeta
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