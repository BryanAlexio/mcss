"""
MultiCameraManager MP2 - Arquitectura Multi-PROCESO para Aislamiento Real
==========================================================================
deteccion de cuadro 

(ver docstring original para el porqué de la arquitectura por-proceso;
se mantiene sin cambios.)

PORTE face_detection / lane_detection - FIDELIDAD COMPLETA
---------------------------------------------------------------------
Antes: FacialFeatureManager y LaneDetectionManager vivían en el
proceso PRINCIPAL y sus process_frame(...) mutaban directamente el
frame + estado de la UI de Tkinter. Eso no es picklable ni tiene
sentido compartirlo entre procesos.

Ahora: cada worker inicializa (de forma perezosa, solo si se activa)
su PROPIA instancia de detección, y esas instancias son AHORA UN
PORTE FIEL de las clases originales:

  - _FaceProcessor  == FacialFeatureManager original, sin los
    enganches a self.parent/self.window/self.gui_queue (no aplican
    dentro de un proceso worker sin Tk). Se conservan:
      * el diccionario FACIAL_FEATURES completo (contorno, cejas,
        ojos [con párpados], nariz, boca, puntos de control) y sus
        colores originales,
      * calibración automática de EAR/MAR (auto_calibrate) y
        calibración de head-pose por muestreo (calibrate_head_pose),
      * head-pose real por solvePnP (no la aproximación 2D simplificada
        que había en el porte anterior),
      * cálculo de MAR con área de boca + detección de habla vs.
        bostezo (calculate_mouth_area / detect_speech),
      * dibujo de landmarks/alertas pulsantes/tabla de métricas
        (draw_status) igual que el original.

  - _LaneProcessor  ==  detector de cambio de carril ROI-rectangular
    con línea central de referencia (LaneChangeDetector), en vez del
    LaneDetectionManager con presets por escenario que traía antes.
    Ver nota completa en la definición de la clase, más abajo.

Estos procesadores corren enteramente DENTRO del proceso worker,
dibujan el overlay sobre el frame (que después se manda a display Y
a grabación, igual que en la versión con hilos) y publican métricas
livianas (dict de floats/ints/bools/strings) por stats_queue cada
~100ms mediante `get_current_metrics()` (lane) / un dict equivalente
armado a partir de `self.states` + contadores (face).

El proceso principal YA NO necesita mediapipe ni tocar el frame: solo
lee `handle.stats['face']` / `handle.stats['lane']` (ver
CameraProcessHandle.poll_stats, sin cambios - ya hacía dict.update
así que conviven con el resto de las stats) para volcar a CSV
(save_facial_metrics / save_lane_metrics en SISTEMA_MULTISENSOR_FINAL
deben leer de ahí en vez de self.facial_feature_manager /
self.lane_detection_manager - ver guía de parche aparte).

Control ON/OFF y sub-opciones se mandan como comandos CMD_SET_CONFIG
normales por cmd_queue - mismo canal que flip_x/flip_y:
  Cara:   'face_detection', 'landmarks', 'hands', 'metrics', 'alerts',
          'calibration' (recalibra EAR/MAR + pose), 'debug'
  Carril: 'lane_detection', 'lane_metrics', 'lane_predictions',
          'lane_scenario' (conservado por compatibilidad; el detector
          actual no tiene presets por escenario, ver nota en _LaneProcessor)

Nota: el head-pose replica fielmente los umbrales/algoritmos originales
(EAR/MAR, calibración adaptativa, solvePnP). El detector de carril fue
reemplazado por el algoritmo de ROI rectangular + línea central de
referencia (detector_cambio_carril.py); ver la clase _LaneProcessor
para el detalle de la migración.
"""

import os
import platform
import time
import threading
import math
from collections import deque, Counter
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from queue import Empty, Full
from typing import Any, Dict, Optional

import cv2
import numpy as np
import multiprocessing as mp
from multiprocessing import Process, Queue as MPQueue

import tkinter as tk
from PIL import Image, ImageTk

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("⚠️ psutil no disponible - no se podrá fijar afinidad/prioridad "
          "por proceso (pip install psutil).")

try:
    import mediapipe as mp_solutions
    MEDIAPIPE_AVAILABLE = True
except ImportError:
    MEDIAPIPE_AVAILABLE = False
    print("⚠️ mediapipe no disponible en este worker - detección facial "
          "deshabilitada (pip install mediapipe).")

try:
    from scipy.spatial import distance as _scipy_distance

    def _dist(a, b):
        return _scipy_distance.euclidean(a, b)
except ImportError:
    def _dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])


class CameraState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class CameraConfig:
    camera_id: str
    device_index: int
    width: int = 1920
    height: int = 1080
    fps: int = 30
    flip_x: bool = False
    flip_y: bool = False
    enable_face_detection: bool = False
    enable_lane_detection: bool = False
    face_calibration: bool = True
    lane_scenario: str = 'highway_day'


CMD_START_RECORDING = "start_recording"
CMD_STOP_RECORDING = "stop_recording"
CMD_SET_CONFIG = "set_config"
CMD_STOP = "stop"


# ============================================================
# Reparto de cores de CPU entre procesos de cámara
# ============================================================
_CPU_COUNT = os.cpu_count() or 4


def _cores_for_index(idx: int, cores_per_camera: int = 2):
    if _CPU_COUNT < 2:
        return None
    start = (idx * cores_per_camera) % _CPU_COUNT
    return [(start + i) % _CPU_COUNT for i in range(cores_per_camera)]


def _apply_process_tuning(core_ids, role: str):
    if not PSUTIL_AVAILABLE:
        return
    try:
        p = psutil.Process(os.getpid())
        if core_ids:
            p.cpu_affinity(core_ids)
        if platform.system() == "Windows":
            p.nice(psutil.HIGH_PRIORITY_CLASS if role == "capture"
                   else psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(-5 if role == "capture" else -2)
    except Exception as e:
        print(f"[tuning pid={os.getpid()}] No se pudo fijar afinidad/"
              f"prioridad: {e}")


def _euclid(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ============================================================
# FACE PROCESSOR - corre DENTRO del proceso worker
# Porte 1:1 de FacialFeatureManager (sin parent/window/gui_queue)
# ============================================================
class _FaceProcessor:
    """Detección facial completa (EAR/MAR/blink/yawn/pose/calibración)
    para correr dentro del proceso worker. Es un porte fiel de
    FacialFeatureManager: mismos umbrales, mismo diccionario de
    landmarks/colores, misma calibración automática y el mismo
    head-pose por solvePnP (no una aproximación simplificada)."""

    def __init__(self):
        self.mp_face_mesh = mp_solutions.solutions.face_mesh
        self.mp_hands = mp_solutions.solutions.hands
        self.mp_drawing = mp_solutions.solutions.drawing_utils
        self.mp_drawing_styles = mp_solutions.solutions.drawing_styles

        self.face_mesh = self.mp_face_mesh.FaceMesh(
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.6,
            min_tracking_confidence=0.6
        )

        self.hands = self.mp_hands.Hands(
            model_complexity=0,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
            max_num_hands=2
        )

        # Variables de control
        self.detect_faces = True  # el worker ya decide si llama a process()
        self.show_landmarks = False
        self.show_hands = False
        self.show_metrics = False
        self.show_alerts = False

        # Puntos y conexiones faciales detalladas (idéntico al original)
        self.FACIAL_FEATURES = {
            'contorno': {
                'points': [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379,
                          378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127,
                          162, 21, 54, 103, 67, 109],
                'color': (0, 255, 0)  # Verde
            },
            'cejas': {
                'izquierda': [70, 63, 105, 66, 107, 55, 65, 52, 53, 46],
                'derecha': [336, 296, 334, 293, 300, 276, 283, 282, 295, 285],
                'color': (120, 255, 0)  # Verde claro
            },
            'ojos': {
                'izquierdo_exterior': [33, 7, 163, 144, 145, 153, 154, 155, 133],
                'izquierdo_interior': [133, 173, 157, 158, 159, 160, 161, 246],
                'derecho_exterior': [362, 382, 381, 380, 374, 373, 390, 249],
                'derecho_interior': [249, 263, 466, 388, 387, 386, 385, 384],
                'parpado_superior_izq': [159, 160, 161, 246, 33],
                'parpado_inferior_izq': [145, 144, 163, 7],
                'parpado_superior_der': [386, 385, 384, 398, 362],
                'parpado_inferior_der': [374, 373, 390, 249],
                'color': (255, 128, 0)  # Naranja
            },
            'nariz': {
                'puente': [6, 197, 195, 5],
                'centro': [1, 2, 3, 4],
                'aletas': [98, 219, 218, 237, 44, 1, 274, 457, 438, 439, 278],
                'base': [79, 166, 75, 167],
                'color': (0, 255, 255)  # Amarillo
            },
            'boca': {
                'labio_superior': [61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291],
                'labio_inferior': [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291],
                'comisuras': [61, 291],
                'interior': [78, 95, 88, 178, 87, 14, 317, 402, 318, 324],
                'color': (255, 0, 0)  # Rojo
            },
            'puntos_control': {
                'pose': [1, 33, 263, 61, 291, 234, 454],
                'parpadeo': [159, 145, 386, 374],
                'bostezo': [61, 291, 17, 0],
                'color': (0, 0, 255)  # Rojo brillante
            }
        }

        self.LEFT_EYE = [362, 385, 387, 263, 373, 380]
        self.RIGHT_EYE = [33, 160, 158, 133, 153, 144]
        self.MOUTH_POINTS = [61, 291, 0, 17, 14, 178]
        self.NOSE_POINTS = [1, 2, 3, 4]

        # Umbrales y contadores (idénticos al original)
        self.EYE_AR_THRESH = 0.2
        self.EYE_AR_CONSEC_FRAMES = 2
        self.MOUTH_AR_THRESH = 0.45
        self.MOUTH_AR_CONSEC_FRAMES = 3
        self.counter_eyes = 0
        self.counter_mouth = 0
        self.blink_counter = 0
        self.yawn_counter = 0
        self.last_blink = time.time()
        self.last_yawn = time.time()

        self.MOUTH_AREA_THRESH = 0.005

        self.lip_positions_history = deque(maxlen=10)
        self.YAWN_DURATION_THRESH = 1.5
        self.mouth_open_start_time = time.time()
        self.SPEECH_MOVEMENT_FACTOR = 1.5
        self.last_eye_closure_start = time.time()

        # Calibración EAR/MAR
        self.enable_calibration = True
        self.is_calibrated = False
        self.calibration_frames = 0
        self.calibration_data = {'ear_values': [], 'mar_values': []}

        self.ear_history = deque(maxlen=30)
        self.mar_history = deque(maxlen=30)

        self.states = {
            'drowsy': False,
            'yawning': False,
            'blink_rate': 0,
            'attention': 'Normal'
        }

        self.POSE_POINTS = {
            'nose': 1, 'left_eye': 33, 'right_eye': 263,
            'left_mouth': 61, 'right_mouth': 291,
            'left_ear': 234, 'right_ear': 454
        }

        self.YAW_THRESHOLD = 10.0
        self.PITCH_THRESHOLD = 8.0

        self.head_pose = {'yaw': 0, 'pitch': 0, 'roll': 0, 'direction': 'Centro'}

        self.blink_history = []
        self.yawn_history = []
        self.attention_history = []

        self.debug_mode = False

        self.head_pose_calibration = {
            'is_calibrated': False,
            'reference_yaw': 0.0,
            'reference_pitch': 0.0,
            'reference_roll': 0.0,
            'calibration_samples': [],
            'sample_count': 0,
            'required_samples': 30
        }

        self.pose_thresholds = {
            'yaw': {'default': 10.0, 'calibrated': 0.0},
            'pitch': {'default': 8.0, 'calibrated': 0.0}
        }

        self._last_mouth_area = 0.0
        self._last_is_speaking = False

    # -------------------- control de opciones --------------------
    def set_option(self, key: str, value: Any):
        if key == 'landmarks':
            self.toggle_landmarks(bool(value))
        elif key == 'hands':
            self.toggle_hands(bool(value))
        elif key == 'metrics':
            self.toggle_metrics(bool(value))
        elif key == 'alerts':
            self.toggle_alerts(bool(value))
        elif key == 'calibration':
            self.toggle_calibration(bool(value))
        elif key == 'debug':
            self.toggle_debug(bool(value))

    # -------------------- cálculo de métricas --------------------
    def calculate_ear(self, landmarks, eye_points):
        """Calcula el Eye Aspect Ratio (idéntico al original)"""
        try:
            points = []
            for point in eye_points:
                points.append([landmarks.landmark[point].x, landmarks.landmark[point].y])

            vertical_dist1 = _dist(points[1], points[5])
            vertical_dist2 = _dist(points[2], points[4])
            horizontal_dist = _dist(points[0], points[3])

            ear = (vertical_dist1 + vertical_dist2) / (2.0 * horizontal_dist) if horizontal_dist > 0 else 0.0

            if self.debug_mode:
                print(f"EAR: {ear:.6f}, Umbral: {self.EYE_AR_THRESH:.6f}")

            return ear
        except Exception as e:
            if self.debug_mode:
                print(f"Error en cálculo de EAR: {e}")
            return 0.0

    def calculate_mar(self, landmarks):
        """Calcula el Mouth Aspect Ratio mejorado (idéntico al original)"""
        try:
            v1 = _dist([landmarks.landmark[37].x, landmarks.landmark[37].y],
                       [landmarks.landmark[84].x, landmarks.landmark[84].y])
            v2 = _dist([landmarks.landmark[0].x, landmarks.landmark[0].y],
                       [landmarks.landmark[17].x, landmarks.landmark[17].y])
            v3 = _dist([landmarks.landmark[267].x, landmarks.landmark[267].y],
                       [landmarks.landmark[314].x, landmarks.landmark[314].y])
            v4 = _dist([landmarks.landmark[13].x, landmarks.landmark[13].y],
                       [landmarks.landmark[14].x, landmarks.landmark[14].y])
            h = _dist([landmarks.landmark[61].x, landmarks.landmark[61].y],
                      [landmarks.landmark[291].x, landmarks.landmark[291].y])

            mar = (v1 + v2 + v3 + v4) / (4 * h) if h > 0 else 0.0
            mouth_area = self.calculate_mouth_area(landmarks)

            if self.debug_mode:
                print(f"MAR: {mar:.6f}, Área: {mouth_area:.6f}, Umbral: {self.MOUTH_AR_THRESH:.6f}")

            return mar, mouth_area
        except Exception as e:
            if self.debug_mode:
                print(f"Error en cálculo de MAR: {e}")
            return 0.0, 0.0

    def calculate_mouth_area(self, landmarks):
        """Calcula el área aproximada de la boca (idéntico al original)"""
        try:
            mouth_points = []
            for point_idx in (self.FACIAL_FEATURES['boca']['labio_superior']
                               + self.FACIAL_FEATURES['boca']['labio_inferior']):
                point = landmarks.landmark[point_idx]
                mouth_points.append((point.x, point.y))

            mouth_points = list(set(mouth_points))
            if len(mouth_points) < 3:
                return 0.0

            area = 0.0
            n = len(mouth_points)
            for i in range(n):
                j = (i + 1) % n
                area += mouth_points[i][0] * mouth_points[j][1]
                area -= mouth_points[j][0] * mouth_points[i][1]

            return abs(area) / 2.0
        except Exception as e:
            if self.debug_mode:
                print(f"Error en cálculo de área de boca: {e}")
            return 0.0

    def detect_speech(self, landmarks):
        """Detecta habla vs bostezo por patrón de movimiento de labios
        (idéntico al original)"""
        try:
            lip_points = []
            for point_idx in self.FACIAL_FEATURES['boca']['interior'][:6]:
                point = landmarks.landmark[point_idx]
                lip_points.append((point.x, point.y))

            if len(self.lip_positions_history) < 5:
                self.lip_positions_history.append(lip_points)
                return False

            movement = 0
            for i, current_pos in enumerate(lip_points):
                prev_pos = self.lip_positions_history[-1][i]
                movement += _dist(current_pos, prev_pos)

            avg_movement = movement / len(lip_points)
            self.lip_positions_history.append(lip_points)

            current_time = time.time()
            mouth_duration = current_time - self.mouth_open_start_time

            is_speaking = 0.001 < avg_movement < 0.005 and mouth_duration < self.YAWN_DURATION_THRESH
            return is_speaking
        except Exception as e:
            if self.debug_mode:
                print(f"Error en detect_speech: {e}")
            return False

    def auto_calibrate(self, ear, mar):
        """Calibra automáticamente EAR/MAR (idéntico al original)"""
        if not self.is_calibrated and self.calibration_frames < 90:
            self.calibration_data['ear_values'].append(ear)
            self.calibration_data['mar_values'].append(mar)
            self.calibration_frames += 1
            calibration_progress = self.calibration_frames / 90 * 100
            return f"Calibrando: {calibration_progress:.0f}%"

        elif not self.is_calibrated and self.calibration_frames >= 90:
            ear_values = sorted(self.calibration_data['ear_values'])[10:-10]
            mar_values = sorted(self.calibration_data['mar_values'])[10:-10]

            if len(ear_values) > 0 and len(mar_values) > 0:
                avg_ear = np.mean(ear_values)
                avg_mar = np.mean(mar_values)
                mar_95_percentile = np.percentile(mar_values, 95)

                self.EYE_AR_THRESH = avg_ear * 0.75
                self.MOUTH_AR_THRESH = mar_95_percentile * 1.2
                self.MOUTH_AREA_THRESH = 0.005
                self.YAWN_MAR_THRESH = self.MOUTH_AR_THRESH * 1.2

                if self.debug_mode:
                    print("Calibracion completada:")
                    print(f"- EAR promedio: {avg_ear:.6f} → Umbral: {self.EYE_AR_THRESH:.6f}")
                    print(f"- MAR promedio: {avg_mar:.6f} → Umbral: {self.MOUTH_AR_THRESH:.6f}")

                self.is_calibrated = True
                return "Calibracion completada!"

        return None

    def calibrate_head_pose(self, yaw, pitch, roll):
        """Calibra la posición de referencia de la cabeza (idéntico al
        original)"""
        if not self.head_pose_calibration['is_calibrated']:
            self.head_pose_calibration['calibration_samples'].append(
                {'yaw': yaw, 'pitch': pitch, 'roll': roll})
            self.head_pose_calibration['sample_count'] += 1

            if self.head_pose_calibration['sample_count'] >= self.head_pose_calibration['required_samples']:
                samples = self.head_pose_calibration['calibration_samples']
                yaw_values = sorted([s['yaw'] for s in samples])[3:-3]
                pitch_values = sorted([s['pitch'] for s in samples])[3:-3]
                roll_values = sorted([s['roll'] for s in samples])[3:-3]

                self.head_pose_calibration['reference_yaw'] = sum(yaw_values) / len(yaw_values)
                self.head_pose_calibration['reference_pitch'] = sum(pitch_values) / len(pitch_values)
                self.head_pose_calibration['reference_roll'] = sum(roll_values) / len(roll_values)

                yaw_std = np.std(yaw_values)
                pitch_std = np.std(pitch_values)

                self.pose_thresholds['yaw']['calibrated'] = max(5.0, yaw_std * 2.5)
                self.pose_thresholds['pitch']['calibrated'] = max(4.0, pitch_std * 2.5)

                self.head_pose_calibration['is_calibrated'] = True

            return (f"Calibrando pose: "
                    f"{(self.head_pose_calibration['sample_count'] / self.head_pose_calibration['required_samples'] * 100):.0f}%")

        return None

    def detect_head_pose(self, landmarks, frame_shape):
        """Head-pose real por solvePnP (idéntico al original, no la
        aproximación 2D simplificada)"""
        h, w = frame_shape[:2]

        model_points = np.array([
            (0.0, 0.0, 0.0),
            (0.0, -330.0, -65.0),
            (-225.0, 170.0, -135.0),
            (225.0, 170.0, -135.0),
            (-150.0, -150.0, -125.0),
            (150.0, -150.0, -125.0)
        ])

        image_points = np.array([
            (landmarks.landmark[1].x * w, landmarks.landmark[1].y * h),
            (landmarks.landmark[152].x * w, landmarks.landmark[152].y * h),
            (landmarks.landmark[33].x * w, landmarks.landmark[33].y * h),
            (landmarks.landmark[263].x * w, landmarks.landmark[263].y * h),
            (landmarks.landmark[61].x * w, landmarks.landmark[61].y * h),
            (landmarks.landmark[291].x * w, landmarks.landmark[291].y * h)
        ], dtype="double")

        focal_length = w
        center = (w / 2, h / 2)
        camera_matrix = np.array([
            [focal_length, 0, center[0]],
            [0, focal_length, center[1]],
            [0, 0, 1]
        ], dtype="double")

        dist_coeffs = np.zeros((4, 1))

        try:
            success, rotation_vector, translation_vector = cv2.solvePnP(
                model_points, image_points, camera_matrix, dist_coeffs,
                flags=cv2.SOLVEPNP_ITERATIVE
            )

            if not success:
                return {'yaw': 0, 'pitch': 0, 'roll': 0, 'direction': 'Centro', 'raw_direction': 'Centro'}

            rotation_matrix, _ = cv2.Rodrigues(rotation_vector)
            proj_matrix = np.hstack((rotation_matrix, translation_vector))
            _, _, _, _, _, _, euler_angles = cv2.decomposeProjectionMatrix(proj_matrix)

            pitch = euler_angles[0, 0]
            yaw = euler_angles[1, 0]
            roll = euler_angles[2, 0]

            calibration_message = None
            if self.enable_calibration and not self.head_pose_calibration['is_calibrated']:
                calibration_message = self.calibrate_head_pose(yaw, pitch, roll)

            yaw_threshold = (self.pose_thresholds['yaw']['calibrated']
                              if self.head_pose_calibration['is_calibrated'] else self.YAW_THRESHOLD)
            pitch_threshold = (self.pose_thresholds['pitch']['calibrated']
                                if self.head_pose_calibration['is_calibrated'] else self.PITCH_THRESHOLD)

            if self.head_pose_calibration['is_calibrated']:
                adjusted_yaw = yaw - self.head_pose_calibration['reference_yaw']
                adjusted_pitch = pitch - self.head_pose_calibration['reference_pitch']
            else:
                adjusted_yaw = yaw
                adjusted_pitch = pitch

            direction = 'Centro'
            raw_direction = 'Centro'

            horizontal_dir = 'Centro'
            if adjusted_yaw > yaw_threshold:
                horizontal_dir = 'Izquierda'
            elif adjusted_yaw < -yaw_threshold:
                horizontal_dir = 'Derecha'

            vertical_dir = 'Centro'
            if adjusted_pitch > pitch_threshold:
                vertical_dir = 'Arriba'
            elif adjusted_pitch < -pitch_threshold:
                vertical_dir = 'Abajo'

            if horizontal_dir == 'Centro' and vertical_dir == 'Centro':
                raw_direction = 'Centro'
            elif horizontal_dir == 'Centro':
                raw_direction = vertical_dir
            elif vertical_dir == 'Centro':
                raw_direction = horizontal_dir
            else:
                raw_direction = f"{vertical_dir}-{horizontal_dir}"

            valid_directions = ['Centro', 'Arriba', 'Abajo', 'Izquierda', 'Derecha',
                                 'Arriba-Izquierda', 'Arriba-Derecha', 'Abajo-Izquierda', 'Abajo-Derecha']
            if raw_direction not in valid_directions:
                raw_direction = 'Centro'

            if hasattr(self, 'direction_history'):
                self.direction_history.append(raw_direction)
                if len(self.direction_history) > 5:
                    self.direction_history.pop(0)

                direction_counts = {}
                for d in self.direction_history:
                    direction_counts[d] = direction_counts.get(d, 0) + 1

                max_count = 0
                for d, count in direction_counts.items():
                    if count > max_count:
                        max_count = count
                        direction = d

                if max_count < 3:
                    direction = self.head_pose.get('direction', 'Centro')
            else:
                self.direction_history = [raw_direction]
                direction = raw_direction

            return {
                'yaw': yaw, 'pitch': pitch, 'roll': roll,
                'adjusted_yaw': adjusted_yaw if self.head_pose_calibration['is_calibrated'] else yaw,
                'adjusted_pitch': adjusted_pitch if self.head_pose_calibration['is_calibrated'] else pitch,
                'direction': direction,
                'raw_direction': raw_direction,
                'calibration_message': calibration_message
            }

        except Exception as e:
            if self.debug_mode:
                print(f"Error en detect_head_pose: {e}")
            return {'yaw': 0, 'pitch': 0, 'roll': 0, 'direction': 'Centro', 'raw_direction': 'Centro'}

    # -------------------- dibujo --------------------
    def draw_alert(self, image, text, color):
        """Alerta con efecto pulsante (idéntico al original)"""
        if not self.show_alerts:
            return image

        h, w = image.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        text_size = cv2.getTextSize(text, font, 1.2, 2)[0]

        pulse_factor = 0.5 + 0.5 * np.sin(time.time() * 5)
        alpha = 0.4 + (0.4 * pulse_factor)

        overlay = image.copy()
        cv2.rectangle(overlay, (w // 2 - text_size[0] // 2 - 20, 10),
                       (w // 2 + text_size[0] // 2 + 20, 60), (0, 0, 0), -1)
        cv2.addWeighted(overlay, alpha, image, 1 - alpha, 0, image)

        cv2.rectangle(image, (w // 2 - text_size[0] // 2 - 20, 10),
                       (w // 2 + text_size[0] // 2 + 20, 60), color, 2)

        cv2.putText(image, text, (w // 2 - text_size[0] // 2 + 2, 42),
                    font, 1.2, (0, 0, 0), 2)
        cv2.putText(image, text, (w // 2 - text_size[0] // 2, 40),
                    font, 1.2, color, 2)

        return image

    def draw_status(self, image, states):
        """Tabla de estado (idéntico al original)"""
        if not self.show_metrics:
            return image

        h, w = image.shape[:2]
        table_width = max(250, int(w * 0.25))
        row_height = max(25, int(h * 0.04))
        padding = max(10, int(w * 0.01))
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = max(0.5, w / 1920)
        line_thickness = max(1, int(w / 640))

        current_time = time.time()
        five_minutes_ago = current_time - 300
        recent_yawns = [t for t in self.yawn_history if t > five_minutes_ago]
        yawn_rate = len(recent_yawns) / 5.0 if recent_yawns else 0

        fatigue_level = "Normal"
        if yawn_rate > 3:
            fatigue_level = "Alta"
        elif yawn_rate > 1:
            fatigue_level = "Moderada"
        elif len(recent_yawns) > 2:
            fatigue_level = "Leve"

        metrics = [
            ("Estado", "Valor"),
            ("Parpadeos", str(self.blink_counter)),
            ("Bostezos", str(self.yawn_counter)),
            ("Bostezos/5min", f"{len(recent_yawns)}"),
            ("Fatiga", fatigue_level),
            ("Parpadeos/min", f"{states['blink_rate']:.1f}"),
            ("Direccion", self.head_pose['direction']),
            ("Atencion", states['attention'])
        ]

        if states['drowsy']:
            metrics.append(("ALERTA", "SOMNOLENCIA!"))
        if states['yawning']:
            metrics.append(("ALERTA", "BOSTEZO!"))

        if self.yawn_counter > 0:
            time_since_last = current_time - self.last_yawn
            if time_since_last < 300:
                metrics.append(("Último bostezo", f"{time_since_last:.0f}s atrás"))

        table_height = row_height * len(metrics)

        overlay = image.copy()
        cv2.rectangle(overlay, (padding, padding),
                       (table_width + padding, table_height + padding),
                       (40, 40, 40), -1)
        cv2.addWeighted(overlay, 0.7, image, 0.3, 0, image)

        cv2.rectangle(image, (padding, padding),
                       (table_width + padding, table_height + padding),
                       (200, 200, 200), line_thickness)

        for i, (label, value) in enumerate(metrics):
            y = padding + (i * row_height)

            if i > 0:
                cv2.line(image, (padding, y), (table_width + padding, y),
                          (200, 200, 200), line_thickness)

            if i == 0:
                text_color = (0, 255, 255)
            elif label == "ALERTA":
                text_color = (0, 0, 255)
            elif label == "Atención" and value != "Normal":
                text_color = (0, 0, 255)
            elif (label in ("Dirección",) and value != "Centro"):
                text_color = (0, 255, 255)
            elif label == "Parpadeos/min" and float(value) > 25:
                text_color = (0, 165, 255)
            else:
                text_color = (0, 255, 0)

            mid_x = padding + table_width // 2
            cv2.line(image, (mid_x, y), (mid_x, y + row_height), (200, 200, 200), line_thickness)

            text_y = y + int(row_height * 0.65)

            label_size = cv2.getTextSize(label, font, font_scale, line_thickness)[0]
            value_size = cv2.getTextSize(value, font, font_scale, line_thickness)[0]

            label_scale = font_scale
            value_scale = font_scale

            if label_size[0] > (mid_x - padding - 10):
                label_scale = font_scale * ((mid_x - padding - 10) / max(1, label_size[0]))
            if value_size[0] > (table_width - mid_x - 10):
                value_scale = font_scale * ((table_width - mid_x - 10) / max(1, value_size[0]))

            cv2.putText(image, label, (padding + 5, text_y), font, label_scale, text_color, line_thickness)
            cv2.putText(image, value, (mid_x + 5, text_y), font, value_scale, text_color, line_thickness)

        if self.show_metrics and self.head_pose_calibration['is_calibrated']:
            metrics.append(("Yaw Ajustado", f"{self.head_pose.get('adjusted_yaw', 0):.1f}°"))
            metrics.append(("Pitch Ajustado", f"{self.head_pose.get('adjusted_pitch', 0):.1f}°"))

        return image

    def _draw_feature(self, frame, landmarks, w, h, points, color, draw_points=True, thickness=1):
        if not points:
            return
        for i in range(len(points) - 1):
            start_point = landmarks.landmark[points[i]]
            end_point = landmarks.landmark[points[i + 1]]
            start_px = (int(start_point.x * w), int(start_point.y * h))
            end_px = (int(end_point.x * w), int(end_point.y * h))
            cv2.line(frame, start_px, end_px, color, thickness=thickness)

        if len(points) > 2:
            start_point = landmarks.landmark[points[-1]]
            end_point = landmarks.landmark[points[0]]
            start_px = (int(start_point.x * w), int(start_point.y * h))
            end_px = (int(end_point.x * w), int(end_point.y * h))
            cv2.line(frame, start_px, end_px, color, thickness=thickness)

        if draw_points:
            for point_idx in points:
                point = landmarks.landmark[point_idx]
                point_px = (int(point.x * w), int(point.y * h))
                cv2.circle(frame, point_px, 1, color, -1)

    # -------------------- proceso principal --------------------
    def process(self, frame: np.ndarray) -> Dict[str, Any]:
        """Equivalente a process_frame() del original: dibuja overlay
        IN-PLACE sobre `frame` y devuelve un dict de métricas livianas
        para publicar por stats_queue."""
        if not self.detect_faces:
            return {'face_detected': False}

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]

        face_results = self.face_mesh.process(frame_rgb)
        hand_results = self.hands.process(frame_rgb) if self.show_hands else None

        # --- calibración EAR/MAR ---
        if self.enable_calibration and face_results.multi_face_landmarks:
            landmarks = face_results.multi_face_landmarks[0]
            left_ear = self.calculate_ear(landmarks, self.LEFT_EYE)
            right_ear = self.calculate_ear(landmarks, self.RIGHT_EYE)
            ear = (left_ear + right_ear) / 2.0
            mar, _ = self.calculate_mar(landmarks)

            calibration_message = self.auto_calibrate(ear, mar)

            if calibration_message:
                cv2.putText(frame, calibration_message,
                            (w // 2 - 100, h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
                if not self.is_calibrated:
                    return {
                        'face_detected': True,
                        'calibrating': True,
                        'blink_count': self.blink_counter,
                        'yawn_count': self.yawn_counter,
                    }

        ear_value = 0.0
        mar_value = 0.0
        face_detected = False

        if face_results.multi_face_landmarks:
            face_detected = True
            landmarks = face_results.multi_face_landmarks[0]

            left_ear = self.calculate_ear(landmarks, self.LEFT_EYE)
            right_ear = self.calculate_ear(landmarks, self.RIGHT_EYE)
            ear_value = (left_ear + right_ear) / 2.0
            self.ear_history.append(ear_value)

            mar_value, mouth_area = self.calculate_mar(landmarks)
            self.mar_history.append(mar_value)
            self._last_mouth_area = mouth_area
            self._last_is_speaking = self.detect_speech(landmarks)

            # --- bostezo ---
            is_potential_yawn = mar_value > self.MOUTH_AR_THRESH and mouth_area > self.MOUTH_AREA_THRESH
            current_time = time.time()

            if is_potential_yawn:
                self.counter_mouth += 1
                if self.counter_mouth == 1:
                    self.mouth_open_start_time = current_time

                if self.counter_mouth >= self.MOUTH_AR_CONSEC_FRAMES:
                    if not self.states['yawning']:
                        if current_time - self.last_yawn > 2.0:
                            self.yawn_counter += 1
                            self.yawn_history.append(current_time)
                            self.last_yawn = current_time

                    self.states['yawning'] = True
                    if self.show_alerts:
                        self.draw_alert(frame, "ALERTA: BOSTEZO DETECTADO!", (0, 165, 255))
            else:
                self.counter_mouth = max(0, self.counter_mouth - 2)
                if self.counter_mouth == 0:
                    self.states['yawning'] = False

            # --- parpadeo / somnolencia ---
            if ear_value < self.EYE_AR_THRESH:
                self.counter_eyes += 1
                if self.counter_eyes >= self.EYE_AR_CONSEC_FRAMES:
                    closure_duration = time.time() - self.last_eye_closure_start
                    if closure_duration > 0.5 and self.counter_eyes >= self.EYE_AR_CONSEC_FRAMES * 2:
                        self.states['drowsy'] = True
                        if self.show_alerts:
                            self.draw_alert(frame, " ALERTA: SOMNOLENCIA DETECTADA!", (0, 0, 255))
                    else:
                        self.states['drowsy'] = False
            else:
                if self.counter_eyes >= self.EYE_AR_CONSEC_FRAMES:
                    current_time = time.time()
                    if current_time - self.last_blink > 0.3:
                        closure_duration = current_time - self.last_eye_closure_start
                        if closure_duration < 0.5:
                            self.blink_counter += 1
                            self.blink_history.append(current_time)
                            self.last_blink = current_time

                self.counter_eyes = 0
                self.states['drowsy'] = False
                self.last_eye_closure_start = time.time()

            # --- pose de cabeza (solvePnP real) ---
            head_pose_result = self.detect_head_pose(landmarks, frame.shape)
            self.head_pose = head_pose_result

            if head_pose_result.get('calibration_message'):
                cv2.putText(frame, head_pose_result['calibration_message'],
                            (w // 2 - 150, h // 2 + 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)

            if head_pose_result['raw_direction'] != 'Centro':
                if self.states['attention'] != 'Muy Baja':
                    if '-' in head_pose_result['raw_direction']:
                        self.states['attention'] = 'Baja'
                    else:
                        self.states['attention'] = 'Reducida'

                if self.show_alerts:
                    direction_text = head_pose_result['raw_direction'].upper()
                    self.draw_alert(frame, f"MIRANDO {direction_text}", (0, 165, 255))

            if self.states['drowsy'] and self.states['yawning']:
                self.states['attention'] = 'Muy Baja'
            elif self.states['drowsy']:
                self.states['attention'] = 'Baja'
            elif self.states['yawning'] and head_pose_result['raw_direction'] != 'Centro':
                self.states['attention'] = 'Baja'
            elif self.states['yawning']:
                self.states['attention'] = 'Reducida'
            else:
                current_time = time.time()
                minute_ago = current_time - 60
                self.blink_history = [t for t in self.blink_history if t > minute_ago]
                self.states['blink_rate'] = len(self.blink_history)

                if self.states['blink_rate'] > 30 and self.states['attention'] == 'Normal':
                    self.states['attention'] = 'Reducida'
                elif self.states['attention'] not in ('Baja', 'Muy Baja', 'Reducida'):
                    self.states['attention'] = 'Normal'

            # --- overlay de landmarks ---
            if self.show_landmarks:
                if 'contorno' in self.FACIAL_FEATURES:
                    self._draw_feature(frame, landmarks, w, h,
                                        self.FACIAL_FEATURES['contorno']['points'],
                                        self.FACIAL_FEATURES['contorno']['color'], False, 1)

                eye_color = self.FACIAL_FEATURES['ojos']['color']
                eye_thickness = 1
                if self.states['drowsy']:
                    eye_color = (0, 0, 255)
                    eye_thickness = 2

                for eye_part in ['izquierdo_exterior', 'izquierdo_interior',
                                  'derecho_exterior', 'derecho_interior',
                                  'parpado_superior_izq', 'parpado_inferior_izq',
                                  'parpado_superior_der', 'parpado_inferior_der']:
                    if eye_part in self.FACIAL_FEATURES['ojos']:
                        self._draw_feature(frame, landmarks, w, h,
                                            self.FACIAL_FEATURES['ojos'][eye_part],
                                            eye_color, False, eye_thickness)

                mouth_color = self.FACIAL_FEATURES['boca']['color']
                mouth_thickness = 1
                if self.states['yawning']:
                    mouth_color = (0, 165, 255)
                    mouth_thickness = 2

                for mouth_part in ['labio_superior', 'labio_inferior', 'comisuras', 'interior']:
                    if mouth_part in self.FACIAL_FEATURES['boca']:
                        self._draw_feature(frame, landmarks, w, h,
                                            self.FACIAL_FEATURES['boca'][mouth_part],
                                            mouth_color, False, mouth_thickness)

                if 'cejas' in self.FACIAL_FEATURES:
                    for brow in ['izquierda', 'derecha']:
                        if brow in self.FACIAL_FEATURES['cejas']:
                            self._draw_feature(frame, landmarks, w, h,
                                                self.FACIAL_FEATURES['cejas'][brow],
                                                self.FACIAL_FEATURES['cejas']['color'], False, 1)

                if 'nariz' in self.FACIAL_FEATURES:
                    for nose_part in ['puente', 'centro', 'aletas', 'base']:
                        if nose_part in self.FACIAL_FEATURES['nariz']:
                            self._draw_feature(frame, landmarks, w, h,
                                                self.FACIAL_FEATURES['nariz'][nose_part],
                                                self.FACIAL_FEATURES['nariz']['color'], False, 1)

        if self.show_hands and hand_results and hand_results.multi_hand_landmarks:
            for hand_landmarks in hand_results.multi_hand_landmarks:
                self.mp_drawing.draw_landmarks(
                    frame, hand_landmarks, self.mp_hands.HAND_CONNECTIONS,
                    self.mp_drawing_styles.get_default_hand_landmarks_style(),
                    self.mp_drawing_styles.get_default_hand_connections_style())

        frame[:] = self.draw_status(frame, self.states)

        return {
            'face_detected': face_detected,
            'blink_count': self.blink_counter,
            'yawn_count': self.yawn_counter,
            'blink_rate': self.states['blink_rate'],
            'attention': self.states['attention'],
            'head_direction': self.head_pose.get('direction', 'Centro'),
            'head_yaw': float(self.head_pose.get('yaw', 0.0)),
            'head_pitch': float(self.head_pose.get('pitch', 0.0)),
            'head_roll': float(self.head_pose.get('roll', 0.0)),
            'drowsy': self.states['drowsy'],
            'yawning': self.states['yawning'],
            'ear': ear_value,
            'mar': mar_value,
            'mouth_area': self._last_mouth_area,
            'is_speaking': self._last_is_speaking,
            'is_calibrated': self.is_calibrated,
            'pose_calibrated': self.head_pose_calibration['is_calibrated'],
        }

    # -------------------- toggles (idénticos al original) --------------------
    def toggle_face_detection(self, enable):
        self.detect_faces = enable
        if not enable:
            self.show_landmarks = False
            self.show_hands = False
            self.show_metrics = False
            self.show_alerts = False
            self.reset_counters()
        elif self.enable_calibration:
            self.is_calibrated = False
            self.calibration_frames = 0
            self.calibration_data = {'ear_values': [], 'mar_values': []}

    def toggle_landmarks(self, show):
        self.show_landmarks = show

    def toggle_hands(self, show):
        self.show_hands = show

    def toggle_metrics(self, show):
        self.show_metrics = show

    def toggle_alerts(self, show):
        self.show_alerts = show

    def toggle_calibration(self, enable):
        self.enable_calibration = enable
        if enable:
            self.is_calibrated = False
            self.calibration_frames = 0
            self.calibration_data = {'ear_values': [], 'mar_values': []}
            self.head_pose_calibration = {
                'is_calibrated': False,
                'reference_yaw': 0.0, 'reference_pitch': 0.0, 'reference_roll': 0.0,
                'calibration_samples': [], 'sample_count': 0, 'required_samples': 30
            }

    def toggle_debug(self, enable):
        self.debug_mode = enable

    def reset_counters(self):
        self.counter_eyes = 0
        self.counter_mouth = 0
        self.blink_counter = 0
        self.yawn_counter = 0

        current_time = time.time()
        self.last_blink = current_time
        self.last_yawn = current_time
        self.last_eye_closure_start = current_time
        self.mouth_open_start_time = current_time

        self.states = {'drowsy': False, 'yawning': False, 'blink_rate': 0, 'attention': 'Normal'}

        self.blink_history = []
        self.yawn_history = []
        self.attention_history = []

        self.ear_history.clear()
        self.mar_history.clear()
        self.lip_positions_history.clear()

        self.head_pose = {'yaw': 0, 'pitch': 0, 'roll': 0, 'direction': 'Centro'}

        self.head_pose_calibration['is_calibrated'] = False
        self.head_pose_calibration['calibration_samples'] = []
        self.head_pose_calibration['sample_count'] = 0

        if hasattr(self, 'direction_history'):
            self.direction_history = []

    def close(self):
        try:
            self.face_mesh.close()
            self.hands.close()
        except Exception:
            pass


# ============================================================
# LANE PROCESSOR - corre DENTRO del proceso worker
# Porte del detector de cambio de carril "ROI rectangular + línea
# central de referencia" (detector_cambio_carril.py / LaneChangeDetector),
# en reemplazo del LaneDetectionManager con presets por escenario.
# ============================================================
class LaneState(Enum):
    CENTERED = "CENTRADO"
    WARNING_LEFT = "ALERTA IZQUIERDA"
    WARNING_RIGHT = "ALERTA DERECHA"
    CHANGE_LEFT = "CAMBIO IZQUIERDA"
    CHANGE_RIGHT = "CAMBIO DERECHA"
    NO_DETECTION = "SIN DETECCION"


@dataclass
class _LaneConfig:
    """Configuración del detector ROI-rectangular (porte fiel del
    Config original de detector_cambio_carril.py, valores calibrados
    manualmente por el usuario)."""
    white_threshold: int = 131
    yellow_h_low: int = 10
    yellow_h_high: int = 40
    yellow_s_low: int = 40
    yellow_v_low: int = 80

    blur_kernel: int = 3
    canny_low: int = 44
    canny_high: int = 68

    roi_top: float = 0.52
    roi_bottom: float = 0.72
    roi_left: float = 0.02
    roi_right: float = 0.90

    hough_threshold: int = 97
    hough_min_line: int = 30
    hough_max_gap: int = 207
    min_slope: float = 0.3

    center_zone_width: float = 0.20
    warning_zone_width: float = 0.30
    smoothing_frames: int = 5

    use_clahe: bool = True
    clahe_clip: float = 1.8
    clahe_grid: int = 8


class _LaneProcessor:
    """Detección de cambio de carril - porte fiel de LaneChangeDetector
    (ROI rectangular fijo + línea central de referencia; un cruce de
    línea detectada sobre la línea central = cambio de carril).

    Reemplaza al LaneDetectionManager anterior (que promediaba líneas
    izquierda/derecha por pendiente y traía 5 presets por escenario).

    NOTA sobre 'lane_scenario': este algoritmo NO tiene presets por
    escenario (highway_day/city_day/night/rain/curves) - usa una única
    _LaneConfig calibrada a mano. El parámetro se conserva únicamente
    por compatibilidad con CameraConfig / la GUI /
    MultiCameraManagerMP.set_lane_scenario(): se guarda como etiqueta
    informativa en self.current_scenario (y viaja en las métricas) pero
    no cambia ningún umbral. Si en el futuro se necesitan distintos
    perfiles día/noche/lluvia para este algoritmo, hay que crear
    _LaneConfig alternativas y mapearlas aquí explícitamente.
    """

    def __init__(self, scenario='highway_day'):
        self.detect_lanes = True  # el worker ya decide si llama a process()
        self.show_metrics = True
        self.show_predictions = True  # sin efecto: este detector no predice líneas faltantes

        self.config = _LaneConfig()
        self.current_scenario = scenario  # solo informativo, ver nota de clase

        self.state = LaneState.NO_DETECTION
        self.state_history = deque(maxlen=self.config.smoothing_frames)

        # Historial de intersecciones con línea central (porte fiel del
        # original; se mantienen declaradas aunque no se consumen más
        # allá de esto, igual que en detector_cambio_carril.py)
        self.left_intersections = deque(maxlen=10)
        self.right_intersections = deque(maxlen=10)

        self._last_results = None

    # -------------------- control de opciones --------------------
    def set_option(self, key: str, value: Any):
        if key == 'lane_metrics':
            self.show_metrics = bool(value)
        elif key == 'lane_predictions':
            self.show_predictions = bool(value)
        elif key == 'lane_scenario':
            self.current_scenario = str(value)
            print(f"[LANE] '{value}' guardado solo como etiqueta informativa; "
                  f"este detector (ROI + línea central) no tiene presets por escenario.")

    def reset_state(self):
        self.state = LaneState.NO_DETECTION
        self.state_history.clear()
        self.left_intersections.clear()
        self.right_intersections.clear()
        self._last_results = None

    # -------------------- geometría / color / líneas --------------------
    def get_roi_coords(self, height: int, width: int):
        """Retorna coordenadas del ROI rectangular: (x1, y1, x2, y2)"""
        x1 = int(width * self.config.roi_left)
        x2 = int(width * self.config.roi_right)
        y1 = int(height * self.config.roi_top)
        y2 = int(height * self.config.roi_bottom)
        return x1, y1, x2, y2

    def create_color_mask(self, frame: np.ndarray) -> np.ndarray:
        """Crea máscara para líneas blancas y amarillas (idéntico al original)"""
        if self.config.use_clahe:
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(
                clipLimit=self.config.clahe_clip,
                tileGridSize=(self.config.clahe_grid, self.config.clahe_grid)
            )
            l = clahe.apply(l)
            lab = cv2.merge([l, a, b])
            frame_enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        else:
            frame_enhanced = frame

        gray = cv2.cvtColor(frame_enhanced, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame_enhanced, cv2.COLOR_BGR2HSV)

        # === MÁSCARA BLANCA (umbral fijo + adaptativo) ===
        mask_white_fixed = cv2.inRange(gray, self.config.white_threshold, 255)
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 15, -5
        )
        mask_white = cv2.bitwise_or(mask_white_fixed, adaptive)

        # === MÁSCARA AMARILLA ===
        lower_y = np.array([self.config.yellow_h_low, self.config.yellow_s_low,
                             self.config.yellow_v_low])
        upper_y = np.array([self.config.yellow_h_high, 255, 255])
        mask_yellow = cv2.inRange(hsv, lower_y, upper_y)

        # === MÁSCARA POR GRADIENTE (detecta bordes de líneas) ===
        sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        gradient_mag = np.sqrt(sobelx**2 + sobely**2)
        max_val = gradient_mag.max()
        if max_val > 0:
            gradient_mag = np.uint8(255 * gradient_mag / max_val)
        else:
            gradient_mag = np.zeros_like(gray, dtype=np.uint8)
        _, mask_gradient = cv2.threshold(gradient_mag, 30, 255, cv2.THRESH_BINARY)

        combined = cv2.bitwise_or(mask_white, mask_yellow)
        combined = cv2.bitwise_or(combined, mask_gradient)

        kernel = np.ones((3, 3), np.uint8)
        combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel)
        combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, kernel)

        return combined

    def detect_lines_in_roi(self, frame: np.ndarray):
        """Detecta líneas dentro del ROI rectangular (idéntico al original,
        con protección extra por si el ROI queda vacío por config inválida)"""
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = self.get_roi_coords(height, width)

        x1c, x2c = max(0, min(x1, width)), max(0, min(x2, width))
        y1c, y2c = max(0, min(y1, height)), max(0, min(y2, height))
        if x2c <= x1c or y2c <= y1c:
            return None, [], None

        roi = frame[y1c:y2c, x1c:x2c]
        color_mask = self.create_color_mask(roi)

        blur_k = self.config.blur_kernel
        if blur_k % 2 == 0:
            blur_k += 1
        blurred = cv2.GaussianBlur(color_mask, (blur_k, blur_k), 0)
        edges = cv2.Canny(blurred, self.config.canny_low, self.config.canny_high)

        lines = cv2.HoughLinesP(
            edges,
            rho=2,
            theta=np.pi / 180,
            threshold=self.config.hough_threshold,
            minLineLength=self.config.hough_min_line,
            maxLineGap=self.config.hough_max_gap
        )

        filtered_lines = []
        if lines is not None:
            for line in lines:
                lx1, ly1, lx2, ly2 = line[0]
                if lx2 != lx1:
                    slope = abs((ly2 - ly1) / (lx2 - lx1))
                    if slope >= self.config.min_slope:
                        filtered_lines.append((
                            lx1 + x1c, ly1 + y1c,
                            lx2 + x1c, ly2 + y1c
                        ))

        return edges, filtered_lines, color_mask

    def check_line_crosses_center(self, line, center_x: int, y_top: int, y_bottom: int):
        """Verifica si una línea cruza la línea central y retorna punto de
        intersección (idéntico al original)"""
        x1, y1, x2, y2 = line

        if abs(x2 - x1) < 1:
            if min(x1, x2) <= center_x <= max(x1, x2):
                y_intersect = (y1 + y2) // 2
                if y_top <= y_intersect <= y_bottom:
                    return (center_x, y_intersect)
            return None

        slope = (y2 - y1) / (x2 - x1)
        intercept = y1 - slope * x1
        y_at_center = slope * center_x + intercept

        x_min, x_max = min(x1, x2), max(x1, x2)
        if x_min <= center_x <= x_max and y_top <= y_at_center <= y_bottom:
            return (center_x, int(y_at_center))
        return None

    def analyze_lane_position(self, lines, height: int, width: int) -> dict:
        """Analiza la posición de las líneas respecto a la línea central
        (idéntico al original)"""
        x1_roi, y1_roi, x2_roi, y2_roi = self.get_roi_coords(height, width)
        roi_width = max(1, x2_roi - x1_roi)
        center_x = (x1_roi + x2_roi) // 2

        center_zone_half = int(roi_width * self.config.center_zone_width / 2)
        warning_zone_half = int(roi_width * self.config.warning_zone_width / 2)

        left_lines, right_lines, center_crossings = [], [], []

        for line in lines:
            lx1, ly1, lx2, ly2 = line
            mid_x = (lx1 + lx2) / 2

            if mid_x < center_x:
                left_lines.append(line)
            else:
                right_lines.append(line)

            crossing = self.check_line_crosses_center(line, center_x, y1_roi, y2_roi)
            if crossing:
                center_crossings.append(crossing)

        min_left_dist = roi_width
        min_right_dist = roi_width

        for line in left_lines:
            lx1, ly1, lx2, ly2 = line
            min_left_dist = min(min_left_dist, center_x - max(lx1, lx2))

        for line in right_lines:
            lx1, ly1, lx2, ly2 = line
            min_right_dist = min(min_right_dist, min(lx1, lx2) - center_x)

        return {
            'left_lines': left_lines,
            'right_lines': right_lines,
            'center_crossings': center_crossings,
            'min_left_dist': min_left_dist,
            'min_right_dist': min_right_dist,
            'center_x': center_x,
            'center_zone_half': center_zone_half,
            'warning_zone_half': warning_zone_half
        }

    def determine_state(self, analysis: dict) -> LaneState:
        """Determina el estado basado en el análisis (idéntico al original)"""
        crossings = analysis['center_crossings']
        min_left = analysis['min_left_dist']
        min_right = analysis['min_right_dist']
        center_zone = analysis['center_zone_half']
        warning_zone = analysis['warning_zone_half']

        if len(crossings) > 0:
            if len(analysis['left_lines']) > len(analysis['right_lines']):
                return LaneState.CHANGE_LEFT
            return LaneState.CHANGE_RIGHT

        if not analysis['left_lines'] and not analysis['right_lines']:
            return LaneState.NO_DETECTION

        if min_left < center_zone:
            return LaneState.CHANGE_LEFT
        if min_right < center_zone:
            return LaneState.CHANGE_RIGHT

        if min_left < warning_zone:
            return LaneState.WARNING_LEFT
        if min_right < warning_zone:
            return LaneState.WARNING_RIGHT

        return LaneState.CENTERED

    # -------------------- proceso principal --------------------
    def process(self, frame: np.ndarray) -> Dict[str, Any]:
        """Equivalente a process_frame() del LaneChangeDetector original:
        dibuja overlay IN-PLACE sobre `frame` y devuelve el dict de
        get_current_metrics() para publicar por stats_queue."""
        if not self.detect_lanes:
            return self.get_current_metrics()

        try:
            height, width = frame.shape[:2]

            edges, lines, color_mask = self.detect_lines_in_roi(frame)
            analysis = self.analyze_lane_position(lines, height, width)
            current_state = self.determine_state(analysis)
            self.state_history.append(current_state)

            if self.state_history:
                state_counts = Counter(self.state_history)
                self.state = state_counts.most_common(1)[0][0]

            self._last_results = {
                'analysis': analysis,
                'lines': lines,
                'state': self.state,
                'raw_state': current_state,
            }

            if self.show_metrics:
                self._draw_results(frame, self._last_results)

        except Exception as e:
            print(f"Error en process_frame (lane): {e}")

        return self.get_current_metrics()

    # -------------------- dibujo (idéntico al original, pero IN-PLACE) --------------------
    def _draw_results(self, frame: np.ndarray, results: dict):
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = self.get_roi_coords(height, width)
        analysis = results['analysis']
        center_x = analysis['center_x']

        state_colors = {
            LaneState.CENTERED: (0, 255, 0),
            LaneState.WARNING_LEFT: (0, 255, 255),
            LaneState.WARNING_RIGHT: (0, 255, 255),
            LaneState.CHANGE_LEFT: (0, 0, 255),
            LaneState.CHANGE_RIGHT: (0, 0, 255),
            LaneState.NO_DETECTION: (128, 128, 128)
        }
        state_color = state_colors.get(results['state'], (255, 255, 255))

        overlay = frame.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), state_color, -1)
        cv2.addWeighted(overlay, 0.15, frame, 0.85, 0, frame)
        cv2.rectangle(frame, (x1, y1), (x2, y2), state_color, 2)

        center_zone = analysis['center_zone_half']
        warning_zone = analysis['warning_zone_half']

        cv2.rectangle(frame,
                       (center_x - center_zone, y1),
                       (center_x + center_zone, y2),
                       (0, 100, 0), 1)
        cv2.rectangle(frame,
                       (center_x - warning_zone, y1),
                       (center_x - center_zone, y2),
                       (0, 100, 100), 1)
        cv2.rectangle(frame,
                       (center_x + center_zone, y1),
                       (center_x + warning_zone, y2),
                       (0, 100, 100), 1)

        cv2.line(frame, (center_x, y1), (center_x, y2), (255, 255, 0), 3)

        for line in results['lines']:
            lx1, ly1, lx2, ly2 = line
            line_color = (255, 100, 0) if (lx1 + lx2) / 2 < center_x else (0, 100, 255)
            cv2.line(frame, (lx1, ly1), (lx2, ly2), line_color, 2)

        for crossing in analysis['center_crossings']:
            cv2.circle(frame, crossing, 12, (0, 0, 255), -1)
            cv2.circle(frame, crossing, 15, (255, 255, 255), 2)

        self._draw_info_panel(frame, results)

    def _draw_info_panel(self, frame: np.ndarray, results: dict):
        """Panel de información (idéntico al original)"""
        state = results['state']
        analysis = results['analysis']

        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (400, 160), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

        state_colors = {
            LaneState.CENTERED: (0, 255, 0),
            LaneState.WARNING_LEFT: (0, 255, 255),
            LaneState.WARNING_RIGHT: (0, 255, 255),
            LaneState.CHANGE_LEFT: (0, 0, 255),
            LaneState.CHANGE_RIGHT: (0, 0, 255),
            LaneState.NO_DETECTION: (128, 128, 128)
        }
        color = state_colors.get(state, (255, 255, 255))

        cv2.putText(frame, f"Estado: {state.value}", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        n_left = len(analysis['left_lines'])
        n_right = len(analysis['right_lines'])
        n_cross = len(analysis['center_crossings'])

        cv2.putText(frame, f"Lineas: Izq={n_left} Der={n_right}", (20, 75),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        cv2.putText(frame, f"Cruces con centro: {n_cross}", (20, 100),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        cv2.putText(frame, f"Dist. Izq: {analysis['min_left_dist']:.0f}px", (20, 125),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
        cv2.putText(frame, f"Dist. Der: {analysis['min_right_dist']:.0f}px", (200, 125),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

        if n_cross > 0:
            cv2.putText(frame, "!! CRUCE DETECTADO !!", (20, 150),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    # -------------------- métricas para stats_queue --------------------
    def get_current_metrics(self) -> Dict[str, Any]:
        if self._last_results is None:
            return {
                'state': LaneState.NO_DETECTION.value,
                'left_lines': 0,
                'right_lines': 0,
                'center_crossings': 0,
                'min_left_dist': None,
                'min_right_dist': None,
                'lane_change_active': False,
                'lane_change_direction': None,
                'departure_warning': False,
                'scenario': self.current_scenario,
            }

        analysis = self._last_results['analysis']
        state = self._last_results['state']

        is_change = state in (LaneState.CHANGE_LEFT, LaneState.CHANGE_RIGHT)
        is_warning = state in (LaneState.WARNING_LEFT, LaneState.WARNING_RIGHT,
                                LaneState.CHANGE_LEFT, LaneState.CHANGE_RIGHT)

        direction = None
        if state in (LaneState.CHANGE_LEFT, LaneState.WARNING_LEFT):
            direction = 'left'
        elif state in (LaneState.CHANGE_RIGHT, LaneState.WARNING_RIGHT):
            direction = 'right'

        return {
            'state': state.value,
            'left_lines': len(analysis['left_lines']),
            'right_lines': len(analysis['right_lines']),
            'center_crossings': len(analysis['center_crossings']),
            'min_left_dist': analysis['min_left_dist'],
            'min_right_dist': analysis['min_right_dist'],
            'lane_change_active': is_change,
            'lane_change_direction': direction,
            'departure_warning': is_warning,
            'scenario': self.current_scenario,
        }


# ============================================================
# WORKER: corre en su propio proceso del SO
# ============================================================
def _camera_worker_main(config: CameraConfig, cmd_queue: MPQueue,
                         display_queue: MPQueue, stats_queue: MPQueue,
                         result_queue: MPQueue, stop_event,
                         core_ids, fps_cap: Optional[float]):
    _apply_process_tuning(core_ids, role="capture")

    cap = None
    OPEN_RETRIES = 4
    for attempt in range(OPEN_RETRIES):
        candidate = cv2.VideoCapture(config.device_index)
        if candidate.isOpened():
            cap = candidate
            break
        candidate.release()
        if attempt < OPEN_RETRIES - 1:
            time.sleep(1.0)

    if cap is None:
        result_queue.put({'type': 'open_result', 'ok': False,
                           'error': f'No se pudo abrir la cámara tras '
                                    f'{OPEN_RETRIES} intentos (dispositivo '
                                    f'posiblemente ocupado por otro proceso)'})
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.height)
    cap.set(cv2.CAP_PROP_FPS, config.fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

    for _ in range(3):
        cap.read()
        time.sleep(0.03)

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or config.width
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or config.height
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    if not actual_fps or actual_fps <= 0 or actual_fps > 240:
        actual_fps = float(config.fps)

    RESOLUTION_FPS_CAP = [
        (1920 * 1080, 24.0),
        (1280 * 720, 30.0),
        (640 * 480, 30.0),
    ]
    pixels = actual_w * actual_h
    auto_cap = 30.0
    for max_px, cap_fps in RESOLUTION_FPS_CAP:
        if pixels >= max_px:
            auto_cap = cap_fps
            break
    caps = [actual_fps, auto_cap]
    if fps_cap is not None and fps_cap > 0:
        caps.append(fps_cap)
    writer_fps = min(caps)
    frame_interval = 1.0 / max(writer_fps, 1.0)

    result_queue.put({
        'type': 'open_result', 'ok': True, 'pid': os.getpid(),
        'actual_width': actual_w, 'actual_height': actual_h,
        'actual_fps': actual_fps, 'writer_fps': writer_fps,
    })

    flip_x, flip_y = config.flip_x, config.flip_y

    # --- detección facial / de líneas (perezoso, solo si se activa) ---
    face_enabled = bool(config.enable_face_detection)
    lane_enabled = bool(config.enable_lane_detection)
    face_processor: Optional[_FaceProcessor] = None
    lane_processor: Optional[_LaneProcessor] = None
    last_face_stats_send = 0.0
    last_lane_stats_send = 0.0
    FACE_LANE_STATS_INTERVAL = 0.05  # 20Hz, igual cadencia que save_*_metrics

    def _ensure_face_processor():
        nonlocal face_processor
        if face_processor is None:
            if not MEDIAPIPE_AVAILABLE:
                print(f"[{config.camera_id}] ⚠️ mediapipe no disponible, "
                      f"no se puede activar detección facial.")
                return None
            face_processor = _FaceProcessor()
            face_processor.enable_calibration = bool(config.face_calibration)
            print(f"[{config.camera_id}] Detección facial inicializada "
                  f"en worker (pid={os.getpid()})")
        return face_processor

    def _ensure_lane_processor():
        nonlocal lane_processor
        if lane_processor is None:
            lane_processor = _LaneProcessor(scenario=config.lane_scenario)
            print(f"[{config.camera_id}] Detección de líneas (ROI + línea "
                  f"central) inicializada en worker (pid={os.getpid()})")
        return lane_processor

    if face_enabled:
        _ensure_face_processor()
    if lane_enabled:
        _ensure_lane_processor()

    # --- estado de grabación (vive solo en este proceso) ---
    is_recording = False
    writer = None
    recording_path = None
    recording_start_time = None
    recording_frames = 0
    next_write_time = 0.0
    last_record_frame = None
    CATCHUP_MAX_SECONDS = 2.0
    max_catchup_frames = max(1, int(CATCHUP_MAX_SECONDS * writer_fps))

    io_stop = threading.Event()
    _write_times = deque(maxlen=60)

    def _io_writer_loop(q, wctrl):
        while not (io_stop.is_set() and q.empty()):
            try:
                frame = q.get(timeout=0.2)
            except Empty:
                continue
            w = wctrl.get('writer')
            if w is not None:
                _t0 = time.time()
                try:
                    w.write(frame)
                except Exception as e:
                    print(f"[{config.camera_id}] Error escribiendo: {e}")
                _write_times.append(time.time() - _t0)

    writer_ctrl = {'writer': None}
    from queue import Queue as ThreadQueue
    local_io_queue = ThreadQueue(maxsize=8)
    dropped_io = 0
    io_thread = None

    last_display_send = 0.0
    display_interval = 1.0 / 15.0
    last_stats_send = 0.0

    frame_number = 0
    running = True

    while running and not stop_event.is_set():
        try:
            while True:
                cmd = cmd_queue.get_nowait()
                ctype = cmd.get('type')
                if ctype == CMD_STOP:
                    running = False
                elif ctype == CMD_SET_CONFIG:
                    key, value = cmd['key'], cmd['value']
                    if key == 'flip_x':
                        flip_x = value
                    elif key == 'flip_y':
                        flip_y = value
                    elif key == 'face_detection':
                        face_enabled = bool(value)
                        if face_enabled:
                            fp = _ensure_face_processor()
                            if fp is not None:
                                fp.toggle_face_detection(True)
                        elif face_processor is not None:
                            face_processor.toggle_face_detection(False)
                    elif key == 'lane_detection':
                        lane_enabled = bool(value)
                        if lane_enabled:
                            _ensure_lane_processor()
                        elif lane_processor is not None:
                            lane_processor.reset_state()
                    elif key in ('landmarks', 'hands', 'metrics', 'alerts', 'calibration', 'debug'):
                        if face_processor is not None:
                            face_processor.set_option(key, value)
                    elif key in ('lane_metrics', 'lane_predictions'):
                        if lane_processor is not None:
                            lane_processor.set_option(key, bool(value))
                    elif key == 'lane_scenario':
                        if lane_processor is not None:
                            lane_processor.set_option('lane_scenario', value)
                elif ctype == CMD_START_RECORDING and not is_recording:
                    folder = cmd['folder']
                    os.makedirs(folder, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    candidates = [
                        ('mp4v', '.mp4'),
                        ('XVID', '.avi'), ('MJPG', '.avi'),
                    ]
                    writer = None
                    file_prefix = cmd.get('file_prefix', f"video_{config.camera_id}")
                    for codec_name, ext in candidates:
                        path = os.path.join(
                            folder, f"{file_prefix}_{ts}{ext}")
                        fourcc = cv2.VideoWriter_fourcc(*codec_name)
                        w_ = cv2.VideoWriter(path, fourcc, writer_fps,
                                              (actual_w, actual_h))
                        if w_.isOpened():
                            writer = w_
                            recording_path = path
                            break
                        w_.release()
                    if writer is None:
                        result_queue.put({
                            'type': 'record_error', 'camera_id': config.camera_id,
                            'error': 'No se pudo crear VideoWriter'})
                    else:
                        writer_ctrl['writer'] = writer
                        is_recording = True
                        recording_start_time = time.time()
                        recording_frames = 0
                        dropped_io = 0
                        next_write_time = time.time()
                        io_stop.clear()
                        io_thread = threading.Thread(
                            target=_io_writer_loop,
                            args=(local_io_queue, writer_ctrl),
                            name=f"RecordIO-{config.camera_id}",
                            daemon=True)
                        io_thread.start()
                elif ctype == CMD_STOP_RECORDING and is_recording:
                    is_recording = False
                    io_stop.set()
                    if io_thread:
                        io_thread.join(timeout=5.0)
                    if writer_ctrl['writer'] is not None:
                        writer_ctrl['writer'].release()
                        writer_ctrl['writer'] = None
                    duration = (time.time() - recording_start_time
                                if recording_start_time else 0)
                    size_mb = (os.path.getsize(recording_path) / (1024 * 1024)
                               if recording_path and os.path.exists(recording_path)
                               else 0)
                    result_queue.put({
                        'type': 'stop_result', 'camera_id': config.camera_id,
                        'path': recording_path, 'duration': duration,
                        'frames': recording_frames, 'size_mb': size_mb,
                        'dropped_io_frames': dropped_io,
                    })
                    recording_path = None
                    recording_start_time = None
        except Empty:
            pass

        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.001)
            continue
        frame_number += 1

        if flip_x:
            frame = cv2.flip(frame, 1)
        if flip_y:
            frame = cv2.flip(frame, 0)

        # --- detección facial / de líneas (dibuja overlay IN-PLACE) ---
        now = time.time()
        if face_enabled and face_processor is not None:
            try:
                face_metrics = face_processor.process(frame)
                if now - last_face_stats_send >= FACE_LANE_STATS_INTERVAL:
                    try:
                        stats_queue.put_nowait({'face': face_metrics})
                    except Full:
                        pass
                    last_face_stats_send = now
            except Exception as e:
                print(f"[{config.camera_id}] Error en detección facial: {e}")

        if lane_enabled and lane_processor is not None:
            try:
                lane_metrics = lane_processor.process(frame)
                if now - last_lane_stats_send >= FACE_LANE_STATS_INTERVAL:
                    try:
                        stats_queue.put_nowait({'lane': lane_metrics})
                    except Full:
                        pass
                    last_lane_stats_send = now
            except Exception as e:
                print(f"[{config.camera_id}] Error en detección de líneas: {e}")

        if is_recording:
            last_record_frame = frame
            if now >= next_write_time:
                deficit = int((now - next_write_time) / frame_interval) + 1
                if deficit > max_catchup_frames:
                    print(f"[{config.camera_id}] ⚠️ Atraso de "
                          f"{deficit * frame_interval:.1f}s en grabación, "
                          f"resincronizando sin rellenar todo el hueco.")
                    deficit = 1
                    next_write_time = now + frame_interval
                else:
                    next_write_time += deficit * frame_interval
                for _ in range(deficit):
                    try:
                        local_io_queue.put_nowait(last_record_frame)
                        recording_frames += 1
                    except Full:
                        dropped_io += 1

        if now - last_display_send >= display_interval:
            small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
            ok, buf = cv2.imencode('.jpg', small,
                                    [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                try:
                    display_queue.put_nowait(buf.tobytes())
                except Full:
                    try:
                        display_queue.get_nowait()
                        display_queue.put_nowait(buf.tobytes())
                    except (Empty, Full):
                        pass
            last_display_send = now

        if now - last_stats_send >= 1.0:
            avg_ms = ((sum(_write_times) / len(_write_times)) * 1000.0
                      if _write_times else 0.0)
            try:
                stats_queue.put_nowait({
                    'camera_id': config.camera_id,
                    'recording_frames': recording_frames,
                    'is_recording': is_recording,
                    'avg_write_ms': avg_ms,
                    'dropped_io_frames': dropped_io,
                    'pid': os.getpid(),
                })
            except Full:
                pass
            last_stats_send = now

    # --- apagado limpio ---
    if is_recording:
        io_stop.set()
        if io_thread:
            io_thread.join(timeout=5.0)
        if writer_ctrl['writer'] is not None:
            writer_ctrl['writer'].release()
    if face_processor is not None:
        face_processor.close()
    cap.release()
    print(f"[{config.camera_id}] Proceso worker terminado (pid={os.getpid()})")


# ============================================================
# Handle en el proceso PRINCIPAL para cada cámara
# ============================================================
class CameraProcessHandle:
    def __init__(self, config: CameraConfig, fps_cap: Optional[float]):
        self.config = config
        self.state = CameraState.STOPPED
        self.cmd_queue: MPQueue = MPQueue(maxsize=16)
        self.display_queue: MPQueue = MPQueue(maxsize=2)
        self.stats_queue: MPQueue = MPQueue(maxsize=32)
        self.result_queue: MPQueue = MPQueue(maxsize=8)
        self.stop_event = mp.Event()
        self.process: Optional[Process] = None
        self.canvas = None

        self.actual_width = config.width
        self.actual_height = config.height
        self.actual_fps = float(config.fps)
        self.is_recording = False
        self.recording_path: Optional[str] = None

        self.stats: Dict[str, Any] = {
            'recording_frames': 0, 'avg_write_ms': 0.0,
            'dropped_io_frames': 0, 'pid': None,
            'face': {}, 'lane': {},
        }
        self._last_stop_result: Dict[str, Any] = {}

    def start(self, core_ids, fps_cap: Optional[float]) -> bool:
        self.state = CameraState.STARTING
        self.process = Process(
            target=_camera_worker_main,
            args=(self.config, self.cmd_queue, self.display_queue,
                  self.stats_queue, self.result_queue, self.stop_event,
                  core_ids, fps_cap),
            name=f"CamProc-{self.config.camera_id}",
            daemon=True,
        )
        self.process.start()

        deadline = time.time() + 60.0
        while time.time() < deadline:
            try:
                msg = self.result_queue.get(timeout=0.5)
            except Empty:
                if not self.process.is_alive():
                    self.state = CameraState.ERROR
                    return False
                continue
            if msg.get('type') == 'open_result':
                if not msg.get('ok'):
                    print(f"[{self.config.camera_id}] {msg.get('error')}")
                    self.state = CameraState.ERROR
                    return False
                self.actual_width = msg['actual_width']
                self.actual_height = msg['actual_height']
                self.actual_fps = msg['actual_fps']
                self.state = CameraState.RUNNING
                return True
        self.state = CameraState.ERROR
        return False

    def stop(self):
        if self.state == CameraState.STOPPED:
            return
        if self.is_recording:
            self.stop_recording()
        self.stop_event.set()
        try:
            self.cmd_queue.put_nowait({'type': CMD_STOP})
        except Full:
            pass
        if self.process:
            self.process.join(timeout=5.0)
            if self.process.is_alive():
                self.process.terminate()
        self.state = CameraState.STOPPED

    def start_recording(self, folder: str, file_prefix: str = None) -> bool:
        if self.state != CameraState.RUNNING:
            return False
        cmd = {'type': CMD_START_RECORDING, 'folder': folder}
        if file_prefix:
            cmd['file_prefix'] = file_prefix
        try:
            self.cmd_queue.put_nowait(cmd)
        except Full:
            return False
        self.is_recording = True
        return True

    def stop_recording(self) -> Dict[str, Any]:
        if not self.is_recording:
            return {}
        self.is_recording = False
        try:
            self.cmd_queue.put_nowait({'type': CMD_STOP_RECORDING})
        except Full:
            pass
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                msg = self.result_queue.get(timeout=0.5)
            except Empty:
                continue
            if msg.get('type') == 'stop_result':
                self._last_stop_result = msg
                return msg
        return {}

    def get_display_frame(self) -> Optional[np.ndarray]:
        try:
            jpeg_bytes = self.display_queue.get_nowait()
        except Empty:
            return None
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return frame

    def poll_stats(self):
        """Drena stats_queue. Los mensajes son dicts parciales (a veces
        solo {'face': {...}} o {'lane': {...}}, a veces el bloque general
        cada 1s) - dict.update() los va fusionando todos en self.stats."""
        try:
            while True:
                s = self.stats_queue.get_nowait()
                self.stats.update(s)
        except Empty:
            pass

    def set_config(self, key: str, value: Any):
        try:
            self.cmd_queue.put_nowait({'type': CMD_SET_CONFIG, 'key': key,
                                        'value': value})
        except Full:
            pass


# ============================================================
# MANAGER: API compatible con MultiCameraManagerOptimized
# ============================================================
class MultiCameraManagerMP:
    CAMERA_SPECS = {
        'main': {'width': 640, 'height': 480, 'fps': 30,
                 'label': 'Principal (frontal)'},
        'secondary1': {'width': 1280, 'height': 720, 'fps': 30,
                        'label': 'Secundaria 1 (frontal)'},
        'secondary2': {'width': 1280, 'height': 720, 'fps': 30,
                        'label': 'Secundaria 2 (trasera)'},
    }

    # Conservado por compatibilidad con la GUI / CameraConfig.lane_scenario.
    # El detector actual (ROI + línea central) no usa estos presets para
    # cambiar umbrales; ver nota en _LaneProcessor.
    LANE_SCENARIOS = ('highway_day', 'city_day', 'night', 'rain', 'curves')

    def __init__(self, parent=None):
        self.parent = parent
        self.window = parent.window if parent else None

        self.handles: Dict[str, CameraProcessHandle] = {}
        self.pipelines = self.handles
        _CAMERA_FLIP_DEFAULTS = {
            'main':       {'flip_x': True,  'flip_y': True},  # frontal conductor → espejo H
            'secondary1': {'flip_x': False, 'flip_y': False},  # frontal carretera → sin espejo
            'secondary2': {'flip_x': False, 'flip_y': False},  # trasera           → sin espejo
        }

        self.cameras = {
            cam_id: {
                'active': False, 'index': None, 'canvas': None,
                'recording': False, 'path': None,
                'flip_x': _CAMERA_FLIP_DEFAULTS[cam_id]['flip_x'],
                'flip_y': _CAMERA_FLIP_DEFAULTS[cam_id]['flip_y'],
                'actual_width': 0, 'actual_height': 0, 'actual_fps': 0,
                'face_detection': False, 'lane_detection': False,
                'face_calibration': True, 'lane_scenario': 'highway_day',
            }
            for cam_id in self.CAMERA_SPECS
        }


        self._gui_update_interval = 33
        self._gui_update_id = None
        self._running = False
        self._next_core_idx = 0

        self.default_record_fps_cap: Optional[float] = 30.0
        self.detect_faces = False
        self.show_landmarks = False

        print("✅ MultiCameraManagerMP inicializado "
              f"({_CPU_COUNT} cores detectados, "
              f"psutil={'sí' if PSUTIL_AVAILABLE else 'no'}, "
              f"mediapipe={'sí' if MEDIAPIPE_AVAILABLE else 'no'})")

    def list_cameras(self) -> Dict[int, str]:
        """Búsqueda ULTRA-RÁPIDA de cámaras (parallelizada)"""
        import concurrent.futures
        
        # Rango reducido: la mayoría de PCs no tienen >5 cámaras
        CAMERA_RANGE = range(6)  # ← cambiar a range(10) si necesitas más
        TIMEOUT_MS = 500  # muy agresivo
        
        available = {}
        
        def _try_camera(idx):
            """Intenta abrir cámara con timeout mínimo"""
            try:
                cap = cv2.VideoCapture(idx)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # buffer mínimo
                
                # Timeout: si no responde en TIMEOUT_MS, considerar no disponible
                import time
                t0 = time.time()
                ret = False
                
                # Máximo 2 intentos de lectura con timeout
                for attempt in range(2):
                    if (time.time() - t0) > (TIMEOUT_MS / 1000.0):
                        break
                    ret, frame = cap.read()
                    if ret:
                        h, w = frame.shape[:2]
                        cap.release()
                        return idx, f"Cámara {idx} ({w}x{h})"
                    time.sleep(0.05)
                
                cap.release()
                return None, None
                
            except Exception:
                return None, None
        
        # ═══════════════════════════════════════════════════════════
        # PARALELIZAR: buscar múltiples cámaras SIMULTÁNEAMENTE
        # ═══════════════════════════════════════════════════════════
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_try_camera, i): i 
                for i in CAMERA_RANGE
            }
            
            for future in concurrent.futures.as_completed(futures, timeout=3.0):
                try:
                    idx, name = future.result()
                    if idx is not None and name is not None:
                        available[idx] = name
                except Exception:
                    pass
        
        print(f"🎥 {len(available)} cámara(s) encontrada(s): {list(available.values())}")
        return available
    
    def start_camera(self, camera_id: str, camera_idx: int,
                      width: Optional[int] = None,
                      height: Optional[int] = None,
                      fps: Optional[int] = None) -> bool:
        if camera_id not in self.CAMERA_SPECS:
            print(f"Camera ID '{camera_id}' no válido")
            return False

        if camera_id in self.handles:
            self.stop_camera(camera_id)

        spec = self.CAMERA_SPECS[camera_id]
        config = CameraConfig(
            camera_id=camera_id,
            device_index=camera_idx,
            width=width if width is not None else spec['width'],
            height=height if height is not None else spec['height'],
            fps=fps if fps is not None else spec['fps'],
            flip_x=self.cameras[camera_id]['flip_x'],
            flip_y=self.cameras[camera_id]['flip_y'],
            enable_face_detection=self.cameras[camera_id]['face_detection'],
            enable_lane_detection=self.cameras[camera_id]['lane_detection'],
            face_calibration=self.cameras[camera_id]['face_calibration'],
            lane_scenario=self.cameras[camera_id]['lane_scenario'],
        )

        core_ids = _cores_for_index(self._next_core_idx)
        self._next_core_idx += 1

        handle = CameraProcessHandle(config, self.default_record_fps_cap)
        handle.canvas = self.cameras[camera_id].get('canvas')

        if handle.start(core_ids, self.default_record_fps_cap):
            self.handles[camera_id] = handle
            self.cameras[camera_id]['active'] = True
            self.cameras[camera_id]['index'] = camera_idx
            self.cameras[camera_id]['actual_width'] = handle.actual_width
            self.cameras[camera_id]['actual_height'] = handle.actual_height
            self.cameras[camera_id]['actual_fps'] = handle.actual_fps

            if not self._running:
                self._running = True
                self._start_gui_updates()
            return True
        return False

    def stop_camera(self, camera_id: str):
        if camera_id in self.handles:
            self.handles[camera_id].stop()
            del self.handles[camera_id]

        self.cameras[camera_id]['active'] = False

        canvas = self.cameras[camera_id].get('canvas')
        if canvas:
            try:
                canvas.delete("all")
                canvas.create_text(
                    canvas.winfo_width() // 2, canvas.winfo_height() // 2,
                    text="Cámara desactivada", fill="white",
                    font=("Arial", 16))
            except Exception:
                pass

        if not any(h.state == CameraState.RUNNING for h in self.handles.values()):
            self._running = False

    def _start_gui_updates(self):
        if self.window and self._running:
            self._update_all_displays()

    def _update_all_displays(self):
        if not self._running:
            return
        for camera_id, handle in self.handles.items():
            if handle.state != CameraState.RUNNING:
                continue
            handle.poll_stats()
            frame = handle.get_display_frame()
            if frame is None:
                continue
            canvas = self.cameras[camera_id].get('canvas')
            if not canvas:
                continue
            try:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                cw, ch = canvas.winfo_width(), canvas.winfo_height()
                if cw > 1 and ch > 1:
                    fh, fw = frame_rgb.shape[:2]
                    scale = min(cw / fw, ch / fh)
                    nw, nh = int(fw * scale), int(fh * scale)
                    resized = cv2.resize(frame_rgb, (nw, nh),
                                          interpolation=cv2.INTER_LINEAR)
                    img = Image.fromarray(resized)
                    imgtk = ImageTk.PhotoImage(image=img)
                    canvas.delete("all")
                    x, y = (cw - nw) // 2, (ch - nh) // 2
                    canvas.create_image(x, y, image=imgtk, anchor=tk.NW)
                    canvas.image = imgtk
            except Exception:
                pass

        if self.window and self._running:
            self._gui_update_id = self.window.after(
                self._gui_update_interval, self._update_all_displays)

    def start_recording(self, camera_id: str, folder: str,
                         fps_cap: Optional[float] = None,
                         file_prefix: str = None) -> bool:
        if camera_id not in self.handles:
            return False
        result = self.handles[camera_id].start_recording(folder, file_prefix)
        if result:
            self.cameras[camera_id]['recording'] = True
        return result

    def stop_recording(self, camera_id: str) -> Dict[str, Any]:
        if camera_id not in self.handles:
            return {}
        result = self.handles[camera_id].stop_recording()
        self.cameras[camera_id]['recording'] = False
        self.cameras[camera_id]['path'] = None
        return result

    def toggle_flip_x(self, camera_id: str) -> bool:
        self.cameras[camera_id]['flip_x'] = not self.cameras[camera_id]['flip_x']
        if camera_id in self.handles:
            self.handles[camera_id].set_config('flip_x', self.cameras[camera_id]['flip_x'])
        return self.cameras[camera_id]['flip_x']

    def toggle_flip_y(self, camera_id: str) -> bool:
        self.cameras[camera_id]['flip_y'] = not self.cameras[camera_id]['flip_y']
        if camera_id in self.handles:
            self.handles[camera_id].set_config('flip_y', self.cameras[camera_id]['flip_y'])
        return self.cameras[camera_id]['flip_y']

    # ------------------------------------------------------------
    # Detección facial / de líneas - _FaceProcessor y _LaneProcessor
    # corriendo dentro del worker.
    # ------------------------------------------------------------
    def set_face_detection(self, camera_id: str, enable: bool):
        """Activa/desactiva detección facial en el worker de camera_id
        (normalmente 'main'). Si la cámara aún no está corriendo, el
        flag se guarda y se aplica en el próximo start_camera()."""
        self.cameras[camera_id]['face_detection'] = enable
        if camera_id in self.handles:
            self.handles[camera_id].set_config('face_detection', enable)
        if not MEDIAPIPE_AVAILABLE and enable:
            print("⚠️ mediapipe no está instalado en este entorno - la "
                  "detección facial no podrá activarse en el worker.")

    def set_lane_detection(self, camera_id: str, enable: bool):
        """Activa/desactiva detección de líneas en el worker de
        camera_id (normalmente 'secondary1')."""
        self.cameras[camera_id]['lane_detection'] = enable
        if camera_id in self.handles:
            self.handles[camera_id].set_config('lane_detection', enable)

    def set_face_option(self, camera_id: str, key: str, value: Any):
        """key in {'landmarks','hands','metrics','alerts','calibration','debug'}"""
        if key == 'calibration':
            self.cameras[camera_id]['face_calibration'] = bool(value)
        if camera_id in self.handles:
            self.handles[camera_id].set_config(key, value)

    def set_lane_option(self, camera_id: str, key: str, value: Any):
        """key in {'lane_metrics','lane_predictions','lane_scenario'}"""
        if key == 'lane_scenario':
            if value not in self.LANE_SCENARIOS:
                print(f"⚠️ Escenario '{value}' no válido. Opciones: {self.LANE_SCENARIOS}")
                return
            self.cameras[camera_id]['lane_scenario'] = value
        if camera_id in self.handles:
            self.handles[camera_id].set_config(key, value)

    def set_lane_scenario(self, camera_id: str, scenario: str):
        """Atajo para etiquetar el escenario actual. Con el detector
        ROI + línea central esto es solo informativo (ver nota en
        _LaneProcessor); no cambia umbrales de detección."""
        self.set_lane_option(camera_id, 'lane_scenario', scenario)

    # Alias retrocompatibles (algunas versiones de la GUI llamaban así)
    def toggle_face_detection(self, enable: bool):
        self.detect_faces = enable
        self.set_face_detection('main', enable)

    def toggle_landmarks(self, show: bool):
        self.show_landmarks = show
        self.set_face_option('main', 'landmarks', show)

    def get_stats(self, camera_id: str) -> Dict[str, Any]:
        if camera_id in self.handles:
            self.handles[camera_id].poll_stats()
            return self.handles[camera_id].stats.copy()
        return {}

    def cleanup(self):
        print("🧹 Limpiando MultiCameraManagerMP...")
        self._running = False
        if self._gui_update_id and self.window:
            try:
                self.window.after_cancel(self._gui_update_id)
            except Exception:
                pass
        for camera_id in list(self.handles.keys()):
            self.stop_camera(camera_id)
        print("✅ MultiCameraManagerMP limpiado")


def create_optimized_camera_manager(parent=None):
    return MultiCameraManagerMP(parent)


# ============================================================
# LÍMITES / NOTAS DE ESTA ARQUITECTURA
# ============================================================
# 1) Disco compartido sigue siendo un límite físico (ver docstring
#    original) - sin cambios.
# 2) face_detection: PORTADO CON FIDELIDAD COMPLETA (_FaceProcessor ==
#    FacialFeatureManager: mismos umbrales, colores, calibración,
#    head-pose por solvePnP).
# 3) lane_detection: REEMPLAZADO. _LaneProcessor ya no es un porte de
#    LaneDetectionManager (promedio de líneas por pendiente + 5 presets
#    por escenario); ahora es un porte fiel de LaneChangeDetector
#    (ROI rectangular fijo + línea central de referencia + zonas de
#    alerta/cambio por proximidad o cruce). 'lane_scenario' se conserva
#    en la API solo como etiqueta informativa - no cambia umbrales.
# 4) mediapipe se importa dentro de CADA worker (proceso 'main' con
#    face_detection=True). Esto agrega ~1-2s de import + init la
#    primera vez que se activa, pero no afecta a las otras cámaras
#    porque cada una tiene su propio intérprete.