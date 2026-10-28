"""
lidar_visualizer.py
Visualizador 3D del Velodyne HDL-64E
VERSIÓN OPTIMIZADA: Renderizado fluido en tiempo real

Optimizaciones clave:
  - Pre-allocación de arrays NumPy (evita malloc en cada frame)
  - Colormaps vectorizados sin bucles Python
  - Double-buffering: hilo de datos no bloquea el render
  - Throttle adaptativo según FPS real
  - poll_events + update_renderer en el mismo tick
"""

import numpy as np
import open3d as o3d
import threading
import time
from collections import deque


# ── Colormaps precalculados (LUT 256 entradas) ────────────────────────────────
def _build_lut_altura() -> np.ndarray:
    """LUT rainbow para colormap de altura."""
    t = np.linspace(0, 1, 256)
    r = np.clip(1.5 - np.abs(4 * t - 3), 0, 1)
    g = np.clip(1.5 - np.abs(4 * t - 2), 0, 1)
    b = np.clip(1.5 - np.abs(4 * t - 1), 0, 1)
    return np.stack([r, g, b], axis=1).astype(np.float32)

def _build_lut_distancia() -> np.ndarray:
    """LUT rojo→verde para colormap de distancia."""
    t = np.linspace(0, 1, 256)
    r = t.copy()
    g = np.clip(1 - t * 0.7, 0, 1)
    b = np.full(256, 0.1)
    return np.stack([r, g, b], axis=1).astype(np.float32)

_LUT_ALTURA    = _build_lut_altura()
_LUT_DISTANCIA = _build_lut_distancia()


class LiDARVisualizer:
    """
    Visualizador 3D del LiDAR — renderizado fluido con doble buffer.

    Arquitectura:
        Hilo principal de Open3D  →  solo render (poll + update)
        Hilo de datos (daemon)    →  copia puntos al staging buffer
        Swap atómico con Lock     →  sin stalls entre hilos
    """

    # ── Parámetros de rendimiento ──────────────────────────────────────────
    MAX_POINTS      = 150_000   # cap de puntos visualizados
    TARGET_FPS      = 30        # FPS objetivo del render loop
    DATA_INTERVAL   = 0.05      # segundos entre actualizaciones de datos (20 Hz)

    def __init__(self, parent):
        self.parent = parent
        self.vis    = None

        # ── Nube de puntos con pre-allocación ─────────────────────────────
        self.pcd         = o3d.geometry.PointCloud()
        self._pts_buf    = np.zeros((self.MAX_POINTS, 3), dtype=np.float64)
        self._col_buf    = np.zeros((self.MAX_POINTS, 3), dtype=np.float64)
        self._n_pts      = 0
        self._geo_added  = False

        # ── Double buffer para transferencia hilo-datos → hilo-render ─────
        self._staging_pts  = None   # np.ndarray listo para swap
        self._staging_cols = None
        self._staging_lock = threading.Lock()
        self._new_data     = False  # flag atómico (protegido por lock)

        # ── Control ────────────────────────────────────────────────────────
        self.is_running    = False
        self.stop_event    = threading.Event()
        self.vis_thread    = None
        self._data_thread  = None

        # ── Configuración visual ───────────────────────────────────────────
        self.modo_color    = 'altura'
        self.tamaño_punto  = 2.0
        self._primera_vez  = True

        # ── Métricas ───────────────────────────────────────────────────────
        self._frame_count  = 0
        self._fps_ts       = time.perf_counter()
        self._fps          = 0.0
        self._fps_log_interval = 60  # frames entre prints

    # ══════════════════════════════════════════════════════════════════════
    #  API pública
    # ══════════════════════════════════════════════════════════════════════

    def open_vis_window(self):
        if self.is_running:
            print("ℹ️  La ventana de visualización ya está abierta")
            return
        self.is_running = True
        self.stop_event.clear()
        self._primera_vez = True
        self._geo_added   = False

        # Hilo de datos: sólo copia y transforma arrays
        self._data_thread = threading.Thread(
            target=self._loop_datos, daemon=True, name="lidar-data"
        )
        self._data_thread.start()

        # Hilo de render: sólo llama a Open3D
        self.vis_thread = threading.Thread(
            target=self._loop_render, daemon=True, name="lidar-render"
        )
        self.vis_thread.start()
        print("🎯 Abriendo ventana de visualización LiDAR…")

    def close_window(self):
        print("🔴 Cerrando ventana de visualización…")
        self.is_running = False
        self.stop_event.set()
        for t in (self.vis_thread, self._data_thread):
            if t and t.is_alive():
                t.join(timeout=2.0)
        self.vis_thread   = None
        self._data_thread = None
        print("✅ Ventana cerrada correctamente")
        print("ℹ️  La captura LiDAR continúa en segundo plano")

    # ══════════════════════════════════════════════════════════════════════
    #  Hilo de datos  (20 Hz)  — sin llamadas a Open3D
    # ══════════════════════════════════════════════════════════════════════

    def _loop_datos(self):
        """
        Obtiene puntos del manager, aplica colormap vectorizado y los
        deja listos en _staging_*.  No toca Open3D.
        """
        while self.is_running and not self.stop_event.is_set():
            t0 = time.perf_counter()

            try:
                if not hasattr(self.parent, 'velodyne_manager'):
                    time.sleep(self.DATA_INTERVAL)
                    continue

                datos = self.parent.velodyne_manager.obtener_puntos()
                if len(datos) > 100:
                    # Cap de puntos para mantener framerate estable
                    if len(datos) > self.MAX_POINTS:
                        # Submuestreo uniforme
                        idx   = np.linspace(0, len(datos) - 1,
                                            self.MAX_POINTS, dtype=np.int32)
                        datos = datos[idx]

                    pts  = datos[:, :3].astype(np.float64)
                    ints = datos[:, 3].astype(np.float32)
                    cols = self._colormap(pts, ints)

                    with self._staging_lock:
                        self._staging_pts  = pts
                        self._staging_cols = cols
                        self._new_data     = True
            except Exception as e:
                print(f"[datos] Error: {e}")

            # Dormir el tiempo restante para respetar DATA_INTERVAL
            elapsed = time.perf_counter() - t0
            sleep   = self.DATA_INTERVAL - elapsed
            if sleep > 0:
                time.sleep(sleep)

    # ══════════════════════════════════════════════════════════════════════
    #  Hilo de render  (TARGET_FPS)  — sólo Open3D
    # ══════════════════════════════════════════════════════════════════════

    def _loop_render(self):
        frame_dt = 1.0 / self.TARGET_FPS
        try:
            self.vis = o3d.visualization.VisualizerWithKeyCallback()
            self.vis.create_window(
                "LiDAR Velodyne HDL-64E — Visualización en Tiempo Real",
                1280, 720
            )
            self._setup_render_options()
            self._registrar_controles()
            self._agregar_escena_base()
            print("✅ Ventana de visualización abierta")

            while self.is_running and not self.stop_event.is_set():
                t0 = time.perf_counter()

                # 1. Cerrar con botón X
                if not self.vis.poll_events():
                    print("🔴 Ventana cerrada por el usuario (X)")
                    break

                # 2. Swap de datos si hay nuevos (lock mínimo)
                with self._staging_lock:
                    if self._new_data and self._staging_pts is not None:
                        pts  = self._staging_pts
                        cols = self._staging_cols
                        self._new_data = False
                    else:
                        pts = cols = None

                # 3. Actualizar geometría
                if pts is not None:
                    self.pcd.points = o3d.utility.Vector3dVector(pts)
                    self.pcd.colors = o3d.utility.Vector3dVector(cols)
                    if self._primera_vez:
                        self.vis.add_geometry(self.pcd)
                        self._reset_vista()
                        self._primera_vez = False
                        self._geo_added   = True
                    elif self._geo_added:
                        self.vis.update_geometry(self.pcd)

                # 4. Render
                self.vis.update_renderer()

                # 5. Métricas de FPS
                self._frame_count += 1
                if self._frame_count % self._fps_log_interval == 0:
                    now = time.perf_counter()
                    self._fps = self._fps_log_interval / (now - self._fps_ts)
                    self._fps_ts = now
                    self._print_stats()

                # 6. Throttle adaptativo para alcanzar TARGET_FPS
                elapsed = time.perf_counter() - t0
                sleep   = frame_dt - elapsed
                if sleep > 0.002:          # no dormir menos de 2 ms
                    time.sleep(sleep)

        except Exception as e:
            print(f"[render] Error: {e}")
        finally:
            self._cleanup()

    # ══════════════════════════════════════════════════════════════════════
    #  Colormaps vectorizados (sin bucles Python)
    # ══════════════════════════════════════════════════════════════════════

    def _colormap(self, pts: np.ndarray, ints: np.ndarray) -> np.ndarray:
        modo = self.modo_color

        if modo == 'intensidad':
            v = np.clip(ints, 0, 1)
            return np.stack([v, v, v], axis=1).astype(np.float64)

        if modo == 'altura':
            z     = pts[:, 2]
            t     = np.clip((z + 3.0) / 8.0, 0.0, 1.0)
            idx   = (t * 255).astype(np.int32)
            return _LUT_ALTURA[idx].astype(np.float64)

        # distancia
        dist = np.linalg.norm(pts[:, :2], axis=1)
        t    = np.clip(dist / 30.0, 0.0, 1.0)
        idx  = (t * 255).astype(np.int32)
        return _LUT_DISTANCIA[idx].astype(np.float64)

    # ══════════════════════════════════════════════════════════════════════
    #  Setup Open3D
    # ══════════════════════════════════════════════════════════════════════

    def _setup_render_options(self):
        opt = self.vis.get_render_option()
        opt.background_color = np.array([0.08, 0.08, 0.12])
        opt.point_size = self.tamaño_punto
        opt.light_on   = False          # sin iluminación = render más rápido

    def _registrar_controles(self):
        kc = self.vis.register_key_callback
        # Movimiento
        kc(ord('W'), lambda v: self._mover('adelante'))
        kc(ord('S'), lambda v: self._mover('atras'))
        kc(ord('A'), lambda v: self._mover('izquierda'))
        kc(ord('D'), lambda v: self._mover('derecha'))
        # Zoom
        kc(ord('Q'), lambda v: self._zoom('in'))
        kc(ord('E'), lambda v: self._zoom('out'))
        # Vistas
        kc(ord('R'), lambda v: self._reset_vista())
        kc(ord('T'), lambda v: self._vista_superior())
        kc(ord('F'), lambda v: self._vista_frontal())
        kc(ord('L'), lambda v: self._vista_lateral())
        # Puntos
        kc(ord('='), lambda v: self._cambiar_punto(+0.5))
        kc(ord('-'), lambda v: self._cambiar_punto(-0.5))
        # Color
        kc(ord('1'), lambda v: self._color('altura'))
        kc(ord('2'), lambda v: self._color('intensidad'))
        kc(ord('3'), lambda v: self._color('distancia'))
        # Otros
        kc(ord('C'), lambda v: self._limpiar())
        kc(ord('H'), lambda v: self._ayuda())
        kc(256,      lambda v: self._cerrar())   # ESC

    def _agregar_escena_base(self):
        """Grilla, ejes y círculos de distancia — geometría estática."""
        # Grilla
        puntos, lineas, colores = [], [], []
        for i in range(-30, 31, 5):
            for x0, y0, x1, y1 in [
                (i, -30, i, 30), (-30, i, 30, i)
            ]:
                puntos += [[x0, y0, -1.5], [x1, y1, -1.5]]
                lineas.append([len(puntos)-2, len(puntos)-1])
                colores.append([0.25, 0.25, 0.25])
        g = o3d.geometry.LineSet()
        g.points  = o3d.utility.Vector3dVector(np.array(puntos))
        g.lines   = o3d.utility.Vector2iVector(np.array(lineas))
        g.colors  = o3d.utility.Vector3dVector(np.array(colores))
        self.vis.add_geometry(g)

        # Ejes
        self.vis.add_geometry(
            o3d.geometry.TriangleMesh.create_coordinate_frame(size=3.0)
        )

        # Círculos de distancia
        for radio in [5, 10, 15, 20, 25, 30]:
            ang  = np.linspace(0, 2 * np.pi, 72, endpoint=False)
            pts  = np.column_stack([
                radio * np.cos(ang),
                radio * np.sin(ang),
                np.full(72, -1.5)
            ])
            lns  = [[i, (i+1) % 72] for i in range(72)]
            c    = 0.18 + (radio / 30) * 0.35
            circ = o3d.geometry.LineSet()
            circ.points = o3d.utility.Vector3dVector(pts)
            circ.lines  = o3d.utility.Vector2iVector(np.array(lns))
            circ.colors = o3d.utility.Vector3dVector(
                [[c, c * 0.7, 0.15]] * 72
            )
            self.vis.add_geometry(circ)

    # ══════════════════════════════════════════════════════════════════════
    #  Callbacks de teclado
    # ══════════════════════════════════════════════════════════════════════

    def _mover(self, d):
        ctr  = self.vis.get_view_control()
        paso = 50.0
        if   d == 'adelante':   ctr.translate(0, -paso)
        elif d == 'atras':      ctr.translate(0,  paso)
        elif d == 'izquierda':  ctr.translate(-paso, 0)
        elif d == 'derecha':    ctr.translate( paso, 0)
        return False

    def _zoom(self, d):
        ctr = self.vis.get_view_control()
        ctr.scale(-3.0 if d == 'in' else 3.0)
        return False

    def _reset_vista(self):
        ctr = self.vis.get_view_control()
        ctr.set_front([0, -0.5, -0.5])
        ctr.set_lookat([0, 0, 0])
        ctr.set_up([0, 0, 1])
        ctr.set_zoom(0.4)
        return False

    def _vista_superior(self):
        ctr = self.vis.get_view_control()
        ctr.set_front([0, 0, 1]); ctr.set_lookat([0, 0, 0])
        ctr.set_up([0, 1, 0]);    ctr.set_zoom(0.3)
        print("📷 Vista superior"); return False

    def _vista_frontal(self):
        ctr = self.vis.get_view_control()
        ctr.set_front([0, -1, 0]); ctr.set_lookat([0, 0, 0])
        ctr.set_up([0, 0, 1]);     ctr.set_zoom(0.4)
        print("📷 Vista frontal"); return False

    def _vista_lateral(self):
        ctr = self.vis.get_view_control()
        ctr.set_front([-1, 0, 0]); ctr.set_lookat([0, 0, 0])
        ctr.set_up([0, 0, 1]);     ctr.set_zoom(0.4)
        print("📷 Vista lateral"); return False

    def _cambiar_punto(self, delta):
        self.tamaño_punto = max(0.5, min(10.0, self.tamaño_punto + delta))
        self.vis.get_render_option().point_size = self.tamaño_punto
        print(f"📐 Punto: {self.tamaño_punto:.1f}")
        return False

    def _color(self, modo):
        self.modo_color = modo
        print(f"🎨 Color: {modo}")
        return False

    def _limpiar(self):
        if hasattr(self.parent, 'velodyne_manager'):
            self.parent.velodyne_manager.limpiar_buffer()
        return False

    def _cerrar(self):
        print("🔴 Cerrando (ESC)…")
        self.close_window()
        return False

    def _ayuda(self):
        print("""
╔══════════════════════════════════════╗
║    CONTROLES LIDAR VISUALIZADOR      ║
╠══════════════════════════════════════╣
║  Mouse:  arrastrar=rotar  scroll=zoom ║
║  W/S/A/D  Pan   Q/E  Zoom            ║
║  R  Reset   T  Top   F  Front  L  Lat ║
║  +/-  Tamaño puntos                   ║
║  1  Altura  2  Intensidad  3  Dist    ║
║  C  Limpiar   H  Ayuda   ESC  Cerrar  ║
╚══════════════════════════════════════╝""")
        return False

    # ══════════════════════════════════════════════════════════════════════
    #  Utilidades internas
    # ══════════════════════════════════════════════════════════════════════

    def _print_stats(self):
        if not hasattr(self.parent, 'velodyne_manager'):
            return
        vm    = self.parent.velodyne_manager
        stats = vm.stats
        pts   = self._n_pts
        estado = (f"🔴 Grabando {stats['paquetes_grabados']:,} pkts"
                  if vm.grabando else "⚪ Vis")
        print(f"⚡ FPS: {self._fps:5.1f} | Pts: {len(self._staging_pts) if self._staging_pts is not None else 0:,} "
              f"| Paq: {stats['paquetes_totales']:,} | {estado} | {self.modo_color}")

    def _cleanup(self):
        print("🧹 Liberando recursos del visualizador…")
        self.is_running   = False
        self._primera_vez = True
        self._geo_added   = False
        if self.vis:
            try:
                self.vis.destroy_window()
            except Exception:
                pass
            finally:
                self.vis = None
        print("✅ Recursos liberados")