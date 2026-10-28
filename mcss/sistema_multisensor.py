#principal
import websocket  
import json
import csv
import os   
import tkinter as tk 
from tkinter import messagebox, ttk
import threading
from datetime import datetime
import time
import pandas as pd
import numpy as np
from queue import Queue, Empty , Full
import serial
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PIL import Image, ImageTk
from enum import Enum
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET
import serial.tools.list_ports
import cv2
from scipy.spatial import distance
from collections import deque
import sys
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import seaborn as sns
from matplotlib.colors import LinearSegmentedColormap
import queue
import shutil
from tkinter import ttk
import random
import traceback
from velodyne_manager_karen  import VelodyneHDL64E
from lidar_visualizer2 import LiDARVisualizer
import bioread
import hashlib
import signal
#from multi_camera_optimized import MultiCameraManagerOptimized
from multi_camera_manager_mp3 import MultiCameraManagerMP
import pickle
import tempfile
import atexit
import struct
import mmap
import re

# Constants
# Credenciales de la API Cortex: se leen de config.py (raíz del repositorio, ignorado por git).
# Copie config.example.py a config.py y complete sus propios valores.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from config import CORTEX_URL, CLIENT_ID, CLIENT_SECRET
except ImportError:
    CORTEX_URL = "wss://localhost:6868"
    CLIENT_ID = ""
    CLIENT_SECRET = ""
    print("[AVISO] No se encontró config.py: copie config.example.py a config.py "
          "y complete CLIENT_ID / CLIENT_SECRET para usar el EEG (Emotiv Cortex).")
 

BAUD_RATE = 115200
ECG_PATH = r"C:\ECG Synchronous V1.4.1\Archive"

# ── Constantes y utilidades PCAP (adaptado de pcap_cutter.py, sin GUI) ───────
PCAP_MAGIC_LE_US   = 0xA1B2C3D4
PCAP_MAGIC_LE_NS   = 0xA1B23C4D
PCAP_MAGIC_BE_US   = 0xD4C3B2A1
PCAP_MAGIC_BE_NS   = 0x4D3CB2A1

PCAP_GLOBAL_HDR_SIZE   = 24
PCAP_PACKET_HDR_SIZE   = 16
PCAP_WRITE_BUFFER_SIZE = 8 * 1024 * 1024


def _pcap_detect_format(magic: int):
    if magic == PCAP_MAGIC_LE_US:
        return '<', 1_000_000
    if magic == PCAP_MAGIC_LE_NS:
        return '<', 1_000_000_000
    if magic == PCAP_MAGIC_BE_US:
        return '>', 1_000_000
    if magic == PCAP_MAGIC_BE_NS:
        return '>', 1_000_000_000
    raise ValueError(f"Magic number PCAP desconocido: 0x{magic:08X}")


def _pcap_build_global_header(byte_order: str, ts_scale: int) -> bytes:
    magic = {('<', 1_000_000): PCAP_MAGIC_LE_US,
             ('<', 1_000_000_000): PCAP_MAGIC_LE_NS,
             ('>', 1_000_000): PCAP_MAGIC_BE_US,
             ('>', 1_000_000_000): PCAP_MAGIC_BE_NS}[(byte_order, ts_scale)]
    fmt = byte_order + 'IHHiIII'
    return struct.pack(fmt, magic, 2, 4, 0, 0, 65535, 1)


def _pcap_cut(pcap_path: str, output_path: str, start_time: float, end_time: float) -> dict:
    """
    Corta un PCAP dejando solo los paquetes en [start_time, end_time] (epoch, s).
    Versión sin GUI de cut_pcap() de pcap_cutter.py, pensada para llamarse
    automáticamente al finalizar la sesión. Lanza excepción si algo falla.
    """
    file_size = os.path.getsize(pcap_path)

    with open(pcap_path, 'rb') as f_in:
        mm = mmap.mmap(f_in.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if mm.size() < PCAP_GLOBAL_HDR_SIZE:
                raise ValueError("Archivo PCAP demasiado pequeño / inválido")

            magic = struct.unpack_from('<I', mm, 0)[0]
            byte_order, ts_scale = _pcap_detect_format(magic)
            hdr_fmt = byte_order + 'IIII'

            with open(output_path, 'wb', buffering=PCAP_WRITE_BUFFER_SIZE) as f_out:
                f_out.write(_pcap_build_global_header(byte_order, ts_scale))

                offset         = PCAP_GLOBAL_HDR_SIZE
                total_packets  = 0
                copied_packets = 0
                mm_size        = mm.size()
                first_ts = last_ts = None

                while offset + PCAP_PACKET_HDR_SIZE <= mm_size:
                    ts_sec, ts_frac, incl_len, orig_len = struct.unpack_from(hdr_fmt, mm, offset)
                    pkt_end = offset + PCAP_PACKET_HDR_SIZE + incl_len
                    if pkt_end > mm_size:
                        break  # paquete truncado al final

                    total_packets += 1
                    timestamp = ts_sec + ts_frac / ts_scale

                    if start_time <= timestamp <= end_time:
                        f_out.write(mm[offset:pkt_end])
                        copied_packets += 1
                        if first_ts is None:
                            first_ts = timestamp
                        last_ts = timestamp

                    offset = pkt_end
        finally:
            mm.close()

    return {
        'total_packets':  total_packets,
        'copied_packets': copied_packets,
        'first_ts':       first_ts,
        'last_ts':        last_ts,
        'size_in_mb':     file_size / 1_048_576,
        'size_out_mb':    os.path.getsize(output_path) / 1_048_576,
    }

class EEGPowerVisualizer:
    def __init__(self, parent):
        """
        Inicializa el visualizador de potencia EEG con soporte para EPOC (14 canales) y FLEX (32 canales)
        """
        self.parent = parent
        self.pow_window = None
        self.is_running = False
        self.pow_data_queue = queue.Queue()
        self.pow_data_thread = None
        self.stop_event = threading.Event()
        self.is_capturing = False
        self.update_interval = 50

        self.debug_mode = False  # Modo de depuración desactivado por defecto
        # NUEVO: Flag para saber si la visualización está habilitada
        self.visualization_enabled = False
        
        # Determinar tipo de dispositivo y configurar canales
        self.device_type = None  # Se determinará al recibir los primeros datos
        
        # Configuración para EPOC (14 canales)
        self.channels_epoc = ["AF3", "F7", "F3", "FC5", "T7", "P7", "O1", 
                              "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"]
        
        # Configuración para FLEX2 (32 canales)
        self.channels_flex = [
            "LH", "RH", "LG", "RG", "BF", "LK", "RK", "RC", "LC",
            "LB", "LA", "RA", "RB", "RP", "LP", "RN", "LN", "RL",
            "LL", "RJ", "LJ", "RM", "LM", "RQ", "LQ", "RO", "LO",
            "RG", "LG", "RL", "LL", "CMS"
        ]
        
        # Canales activos (se configurarán según el dispositivo detectado)
        self.channels = []
        
        # Bandas de frecuencia (igual para ambos dispositivos)
        self.bands = ["theta", "alpha", "betaL", "betaH", "gamma"]
        
        # Colores para las bandas
        self.band_colors = {
            "theta": "#3498db",
            "alpha": "#2ecc71",
            "betaL": "#f1c40f",
            "betaH": "#e67e22",
            "gamma": "#e74c3c"
        }
        
        # Datos más recientes por canal y banda
        self.latest_data = {}
        self.data_history = {}
        
        # Referencias a las gráficas de barras
        self.bar_plots = {}
        
        # Rangos adaptativos por canal
        self.min_values = {}
        self.max_values = {}
        
        self.history_window = 50
        self.range_percentile = 98
        self.range_update_interval = 0.1
        self.last_range_update = time.time()

        # Monitoreo de contacto de electrodos
        self.contact_status = {}
        self.contact_indicators = {}
        
        self.contact_thresholds = {
            "good": 35.0,
            "fair": 15.0
        }
        
        self.contact_colors = {
            "good": "#2ecc71",
            "fair": "#f1c40f",
            "poor": "#e74c3c",
            "unknown": "#95a5a6"
        }
        
        self.contact_history = {}
        
        for channel in self.channels:
            self.latest_data[channel] = {}
            self.data_history[channel] = {}
            for band in self.bands:
                self.latest_data[channel][band] = 0.0
                self.data_history[channel][band] = []
        
        # Referencias a las gráficas de barras
        self.bar_plots = {}
        
        # Configuración para adaptación de rangos individuales por canal
        self.min_values = {}
        self.max_values = {}
        
        # Inicializar rangos para cada canal
        for channel in self.channels:
            self.min_values[channel] = -0.5  # Todos inician en -0.5 como solicitado
            self.max_values[channel] = 0.5   # Valor inicial
            
        self.history_window = 50  # Reducido drásticamente para respuesta inmediata
        self.range_percentile = 98  # Percentil para el cálculo del rango (evita outliers)
        
        # Tiempo entre actualizaciones de rango (inmediato)
        self.range_update_interval = 0.1  # segundos
        self.last_range_update = time.time()

        # Añadir monitoreo de contacto de electrodos
        self.contact_status = {}
        self.contact_indicators = {}
        for channel in self.channels:
            self.contact_status[channel] = "unknown"  # Valores: "good", "fair", "poor", "unknown"
        
        # Umbrales para calidad de contacto (basados en valores típicos de potencia)
        self.contact_thresholds = {
            "good": 35.0,   # Por encima de este valor, el contacto es bueno
            "fair": 15.0    # Por encima de este valor, el contacto es aceptable
        }
        
        # Colores para los estados de contacto
        self.contact_colors = {
            "good": "#2ecc71",    # Verde
            "fair": "#f1c40f",    # Amarillo
            "poor": "#e74c3c",    # Rojo
            "unknown": "#95a5a6"  # Gris
        }
        
        # Historial para la detección de contacto con menos falsos positivos
        self.contact_history = {}
        for channel in self.channels:
            self.contact_history[channel] = []  # Lista en lugar de deque para compatibilidad

    def detect_device_type(self, pow_data_length):
        """
        Detecta el tipo de dispositivo basado en la longitud de los datos POW
        
        Args:
            pow_data_length: Longitud del array de datos POW
            
        Returns:
            str: 'EPOC' o 'FLEX2'
        """
        if pow_data_length == 70:  # 14 canales × 5 bandas
            return 'EPOC'
        elif pow_data_length == 160:  # 32 canales × 5 bandas
            return 'FLEX2'
        else:
            print(f"⚠️ Longitud de datos desconocida: {pow_data_length}")
            return None

    def initialize_device_configuration(self, device_type):
        """
        Inicializa la configuración según el tipo de dispositivo
        
        Args:
            device_type: 'EPOC' o 'FLEX2'
        """
        if device_type == 'EPOC':
            self.channels = self.channels_epoc.copy()
            print("✅ Dispositivo detectado: EPOC (14 canales)")
        elif device_type == 'FLEX2':
            self.channels = self.channels_flex.copy()
            print("✅ Dispositivo detectado: FLEX2 (32 canales)")
        else:
            print("❌ Tipo de dispositivo no reconocido")
            return False
        
        # Inicializar estructuras de datos
        for channel in self.channels:
            self.latest_data[channel] = {}
            self.data_history[channel] = {}
            self.contact_history[channel] = []
            self.contact_status[channel] = "unknown"
            
            for band in self.bands:
                self.latest_data[channel][band] = 0.0
                self.data_history[channel][band] = []
            
            self.min_values[channel] = -0.5
            self.max_values[channel] = 0.5
        
        return True
            
    def open_pow_window(self):
        """Abre la ventana de visualización adaptada al tipo de dispositivo"""
        if self.pow_window is not None:
            return
        
        # Asegurar que se ejecuta en el hilo principal
        if not hasattr(self.parent, 'window') or not self.parent.window.winfo_exists():
            print("❌ Error: Ventana principal no disponible")
            return
        
        # Crear ventana
        self.pow_window = tk.Toplevel(self.parent.window)
        
        # Ajustar título según dispositivo
        device_name = "EEG" if not self.device_type else self.device_type
        self.pow_window.title(f"Visualización de Potencia {device_name}")
        
        # Ajustar tamaño según número de canales
        if self.device_type == 'FLEX2':
            self.pow_window.geometry("1400x900")
        else:
            self.pow_window.geometry("1200x700")
        
        self.pow_window.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.pow_window.resizable(True, True)
        
        # Configurar estilo
        style = ttk.Style(self.pow_window)
        style.configure('Header.TLabel', font=('Segoe UI', 14, 'bold'))
        style.configure('Section.TLabelframe', font=('Segoe UI', 11, 'bold'))
        
        # Frame principal
        main_frame = ttk.Frame(self.pow_window, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=0)
        main_frame.rowconfigure(1, weight=1)
        
        # Header con información del dispositivo
        header_frame = ttk.Frame(main_frame)
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header_frame.columnconfigure(0, weight=1)
        header_frame.columnconfigure(1, weight=0)
        
        title_text = f"Visualización de Potencia {device_name}"
        if self.device_type:
            title_text += f" - {len(self.channels)} canales"
        
        self.title_label = ttk.Label(header_frame, text=title_text, style='Header.TLabel')
        self.title_label.grid(row=0, column=0, sticky="w")
        
        # Leyenda de bandas
        legend_frame = ttk.Frame(header_frame)
        legend_frame.grid(row=0, column=1, sticky="e")
        
        ttk.Label(legend_frame, text="Bandas: ").pack(side=tk.LEFT)
        
        for band, color in self.band_colors.items():
            band_frame = ttk.Frame(legend_frame, width=15, height=15)
            band_frame.pack(side=tk.LEFT, padx=(10, 5))
            
            band_canvas = tk.Canvas(band_frame, width=15, height=15, bg=color, highlightthickness=0)
            band_canvas.pack(side=tk.LEFT)
            
            ttk.Label(legend_frame, text=band).pack(side=tk.LEFT, padx=(0, 10))
        
        # Panel central
        content_frame = ttk.Frame(main_frame)
        content_frame.grid(row=1, column=0, sticky="nsew", pady=5)
        content_frame.columnconfigure(0, weight=4)
        content_frame.columnconfigure(1, weight=1)
        content_frame.rowconfigure(0, weight=1)
        
        # Frame para gráficas
        self.graph_frame = ttk.LabelFrame(content_frame, text="Potencia por Canal y Banda", padding=10)
        self.graph_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        
        # Frame para monitor de contacto
        contact_side_frame = ttk.Frame(content_frame)
        contact_side_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        
        self.contact_monitor_frame = ttk.LabelFrame(
            contact_side_frame, 
            text="Monitor de Contacto de Electrodos", 
            padding=10,
            style='Section.TLabelframe'
        )
        self.contact_monitor_frame.pack(fill=tk.BOTH, expand=True)
        
        # Configurar elementos
        self.setup_contact_indicators(self.contact_monitor_frame)
        
        # CRÍTICO: Configurar gráficas DESPUÉS de crear todos los frames
        self.parent.window.after(100, self.setup_adaptive_bar_graphs)
        
        # Vincular redimensionamiento
        self.pow_window.bind("<Configure>", self.on_window_resize)
        
        # Iniciar captura DESPUÉS de configurar todo
        self.parent.window.after(200, self.start_data_capture)
        
        print(f"✅ Ventana de visualización POW abierta para {device_name}")

    def on_window_resize(self, event):
        """Maneja el redimensionamiento de la ventana actualizando los elementos gráficos"""
        try:
            # Verificar que la ventana sigue existiendo
            if not hasattr(self, 'pow_window') or self.pow_window is None:
                return
                
            if not self.pow_window.winfo_exists():
                return
            
            # Solo procesar si la ventana es la principal de visualización
            if event.widget == self.pow_window and hasattr(self, 'canvas'):
                # Cancelar timer anterior si existe
                if hasattr(self, 'resize_timer'):
                    try:
                        self.pow_window.after_cancel(self.resize_timer)
                    except:
                        pass
                
                # Programar la actualización después de un breve retraso
                self.resize_timer = self.pow_window.after(150, self.update_canvas_sizes)
                
        except Exception as e:
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"Error en on_window_resize: {e}")

    def update_canvas_sizes(self):
        """Actualiza los tamaños de los elementos gráficos cuando cambia el tamaño de la ventana"""
        try:
            # Verificar que la ventana aún existe
            if not hasattr(self, 'pow_window') or self.pow_window is None:
                return
                
            if not self.pow_window.winfo_exists():
                return
            
            # Verificar que el canvas existe y es válido
            if not hasattr(self, 'canvas') or not self.canvas:
                return
                
            canvas_widget = self.canvas.get_tk_widget()
            
            # Verificar que el widget existe
            if not canvas_widget.winfo_exists():
                return
                
            # Verificar que la figura existe
            if not hasattr(self, 'fig') or not self.fig:
                return
            
            # Actualizar tamaños
            canvas_widget.update()
            width = canvas_widget.winfo_width() / 100
            height = canvas_widget.winfo_height() / 100
            
            # Mantener proporciones razonables
            self.fig.set_size_inches(max(8, width), max(6, height))
            self.canvas.draw_idle()
            
        except Exception as e:
            # Error silencioso si estamos en proceso de cierre
            if hasattr(self, 'is_capturing') and not self.is_capturing:
                return  # Ignorar errores si ya no estamos capturando
            
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"Error en update_canvas_sizes: {e}")

    
    def update_contact_indicators(self):
        """
        Actualiza los indicadores visuales de calidad de contacto de los electrodos
        """
        try:
            if not hasattr(self, 'contact_indicators') or not self.contact_indicators:
                return
                
            for channel in self.channels:
                if channel not in self.contact_indicators:
                    continue
                    
                # Calcular potencia promedio del canal para determinar calidad de contacto
                channel_power = self.calculate_channel_power(channel)
                
                # Actualizar historial de contacto
                if channel not in self.contact_history:
                    self.contact_history[channel] = []
                
                self.contact_history[channel].append(channel_power)
                
                # Mantener solo las últimas 10 mediciones
                if len(self.contact_history[channel]) > 10:
                    self.contact_history[channel].pop(0)
                
                # Calcular promedio para suavizar detección
                if len(self.contact_history[channel]) >= 3:
                    avg_power = sum(self.contact_history[channel]) / len(self.contact_history[channel])
                    
                    # Determinar estado de contacto basado en umbrales
                    if avg_power > self.contact_thresholds["good"]:
                        new_status = "good"
                    elif avg_power > self.contact_thresholds["fair"]:
                        new_status = "fair"
                    else:
                        new_status = "poor"
                    
                    # Solo actualizar si el estado cambió
                    if new_status != self.contact_status.get(channel, "unknown"):
                        self.contact_status[channel] = new_status
                        
                        # Actualizar indicador visual
                        indicator = self.contact_indicators[channel]
                        color = self.contact_colors[new_status]
                        
                        indicator.delete("all")
                        indicator.create_oval(3, 3, 22, 22, fill=color, outline="")
                        
        except Exception as e:
            if self.debug_mode:
                print(f"Error en update_contact_indicators: {e}")
                
    def setup_contact_indicators(self, parent_frame):
        """Configura los indicadores de contacto de electrodos con diseño adaptativo"""
        # Frame principal para la sección de contacto
        contact_main_frame = ttk.Frame(parent_frame)
        contact_main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Configurar pesos para adaptabilidad
        contact_main_frame.columnconfigure(0, weight=1)
        contact_main_frame.rowconfigure(0, weight=0)  # Imagen
        contact_main_frame.rowconfigure(1, weight=0)  # Separador
        contact_main_frame.rowconfigure(2, weight=0)  # Leyenda
        contact_main_frame.rowconfigure(3, weight=1)  # Grid de indicadores
        
        # Frame superior para la imagen
        contact_image_frame = ttk.Frame(contact_main_frame)
        contact_image_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        # Frame inferior para los indicadores
        contact_grid_frame = ttk.Frame(contact_main_frame)
        contact_grid_frame.grid(row=3, column=0, sticky="nsew")
        
        # Cargar y mostrar la imagen de contacto
        try:
            # Intentar cargar la imagen desde la ruta especificada
            contact_img = Image.open("contacto.png")
            
            # Guardar la imagen original para redimensionar posteriormente
            self.original_contact_img = contact_img.copy()
            
            # Tamaño inicial para la imagen
            fixed_width, fixed_height = 230, 150
            
            # Redimensionar manteniendo proporciones
            img_width, img_height = contact_img.size
            ratio = min(fixed_width/img_width, fixed_height/img_height)
            new_width = int(img_width * ratio)
            new_height = int(img_height * ratio)
            
            contact_img = contact_img.resize((new_width, new_height), Image.LANCZOS)
            
            # Convertir a formato compatible con tkinter
            tk_img = ImageTk.PhotoImage(contact_img)
            
            # Guardar referencia para evitar que sea recolectada por el garbage collector
            self.contact_img_ref = tk_img
            
            # Frame contenedor para centrar la imagen
            self.img_container = ttk.Frame(contact_image_frame, width=fixed_width, height=fixed_height)
            self.img_container.pack(pady=5)
            self.img_container.pack_propagate(False)  # Mantener el tamaño fijo
            
            # Mostrar la imagen en un label centrado
            self.img_label = ttk.Label(self.img_container, image=tk_img)
            self.img_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
            
            # Añadir título a la imagen
            ttk.Label(
                contact_image_frame, 
                text="Mapa de Contacto", 
                font=('Segoe UI', 10, 'bold')
            ).pack(pady=(0, 5))
            
        except Exception as e:
            # Si hay error al cargar la imagen, mostrar mensaje de error
            error_label = ttk.Label(
                contact_image_frame, 
                text=f"Error al cargar imagen:\n{str(e)}",
                foreground="red"
            )
            error_label.pack(pady=20)
            print(f"Error al cargar imagen de contacto: {e}")
        
        # Separador visual
        ttk.Separator(contact_main_frame, orient='horizontal').grid(row=1, column=0, sticky="ew", pady=10)
        
        # Añadir leyenda para colores de contacto
        legend_frame = ttk.Frame(contact_main_frame)
        legend_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        
        ttk.Label(legend_frame, text="Calidad de contacto: ").pack(side=tk.LEFT)
        
        for status, color in [("Bueno", self.contact_colors["good"]), 
                            ("Regular", self.contact_colors["fair"]), 
                            ("Pobre", self.contact_colors["poor"])]:
            # Frame para cada entrada de leyenda
            status_frame = ttk.Frame(legend_frame)
            status_frame.pack(side=tk.LEFT, padx=5)
            
            # Indicador de color
            color_indicator = tk.Canvas(status_frame, width=15, height=15, 
                                    bg=color, highlightthickness=0)
            color_indicator.pack(side=tk.LEFT, padx=(0, 5))
            
            # Etiqueta de estado
            ttk.Label(status_frame, text=status).pack(side=tk.LEFT)
        
        # Grid para los indicadores en el frame de contacto
        contact_grid = ttk.Frame(contact_grid_frame)
        contact_grid.pack(fill=tk.BOTH, expand=True, pady=5)
        
        # Obtener el ancho disponible para el grid
        available_width = parent_frame.winfo_width()
        
        # Determinar el número de columnas basado en el ancho disponible
        cols_per_row = 2 if available_width < 200 else 3
        
        # Crear indicadores de contacto para cada canal
        self.contact_indicators = {}
        
        for i, channel in enumerate(self.channels):
            row = i // cols_per_row
            col = i % cols_per_row
            
            # Frame para cada canal con borde y padding
            channel_frame = ttk.Frame(contact_grid, padding=5)
            channel_frame.grid(row=row, column=col, padx=5, pady=3, sticky="w")
            
            # Indicador circular de contacto
            indicator = tk.Canvas(channel_frame, width=30, height=30, highlightthickness=0)
            indicator.create_oval(5, 5, 25, 25, fill=self.contact_colors["unknown"], outline="")
            indicator.pack(side=tk.LEFT)
            
            # Etiqueta con nombre del canal
            ttk.Label(channel_frame, text=channel, width=6).pack(side=tk.LEFT, padx=(5, 0))
            
            # Guardar referencia al indicador
            self.contact_indicators[channel] = indicator

    def setup_adaptive_bar_graphs(self):
        """Configura las gráficas de barras adaptadas al número de canales"""
        if not self.channels:
            print("⚠️ No se han configurado canales aún")
            return
        
        num_channels = len(self.channels)
        
        # Determinar layout de subplots según número de canales
        if num_channels <= 14:
            rows, cols = 4, 4  # EPOC: 4x4
            figsize = (10, 8)
        else:
            rows, cols = 6, 6  # FLEX2: 6x6
            figsize = (14, 10)
        
        # CRÍTICO: Usar pyplot en lugar de subplots directamente
        plt.ioff()  # Desactivar modo interactivo
        
        # Crear figura
        self.fig = plt.figure(figsize=figsize, dpi=100)
        
        # Crear subplots manualmente
        self.axes = []
        for i in range(rows * cols):
            ax = self.fig.add_subplot(rows, cols, i + 1)
            self.axes.append(ax)
        
        self.fig.subplots_adjust(hspace=0.5, wspace=0.3)
        
        # Configurar cada subplot
        bar_width = 0.15
        x = np.arange(len(self.bands))
        
        for i, channel in enumerate(self.channels):
            if i >= len(self.axes):
                break
                
            ax = self.axes[i]
            ax.set_title(channel, fontsize=9, fontweight='bold')
            ax.set_ylim([self.min_values[channel], self.max_values[channel]])
            ax.set_xticks(x)
            ax.set_xticklabels(self.bands, rotation=45, fontsize=7)
            ax.tick_params(axis='both', which='major', labelsize=7)
            ax.grid(True, linestyle='--', alpha=0.7)
            
            # Crear barras
            bars = []
            for j, band in enumerate(self.bands):
                bar = ax.bar(x[j], 0, width=bar_width, color=self.band_colors[band], 
                            label=band, edgecolor='black', linewidth=0.5)
                bars.append(bar[0])
            
            self.bar_plots[channel] = dict(zip(self.bands, bars))
        
        # Ocultar subplots no usados
        for i in range(num_channels, len(self.axes)):
            self.axes[i].set_visible(False)
        
        self.fig.tight_layout()
        
        # Crear canvas SIN eventos interactivos
        self.canvas = FigureCanvasTkAgg(self.fig, self.graph_frame)
        canvas_widget = self.canvas.get_tk_widget()
        canvas_widget.pack(fill=tk.BOTH, expand=True)
        
        # CRÍTICO: Desconectar TODOS los event handlers para evitar threading issues
        try:
            # Limpiar callbacks
            self.canvas.callbacks.callbacks.clear()
        except:
            pass
        
        # Dibujar canvas inicial
        self.canvas.draw()
        
        print(f"✅ Gráficas configuradas para {num_channels} canales")

    def setup_contact_indicators(self, parent_frame):
        """Configura indicadores de contacto adaptados al número de canales"""
        if not self.channels:
            return
        
        # Frame principal
        contact_main_frame = ttk.Frame(parent_frame)
        contact_main_frame.pack(fill=tk.BOTH, expand=True)
        contact_main_frame.columnconfigure(0, weight=1)
        contact_main_frame.rowconfigure(0, weight=0)
        contact_main_frame.rowconfigure(1, weight=0)
        contact_main_frame.rowconfigure(2, weight=0)
        contact_main_frame.rowconfigure(3, weight=1)
        
        # Imagen de referencia (si existe)
        contact_image_frame = ttk.Frame(contact_main_frame)
        contact_image_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        try:
            # Cargar imagen según dispositivo
            if self.device_type == 'FLEX2':
                img_path = "contacto_flex.png"  # Necesitarás crear esta imagen
            else:
                img_path = "contacto.png"
            
            if os.path.exists(img_path):
                contact_img = Image.open(img_path)
                self.original_contact_img = contact_img.copy()
                
                fixed_width, fixed_height = 230, 150
                img_width, img_height = contact_img.size
                ratio = min(fixed_width/img_width, fixed_height/img_height)
                new_width = int(img_width * ratio)
                new_height = int(img_height * ratio)
                
                contact_img = contact_img.resize((new_width, new_height), Image.LANCZOS)
                tk_img = ImageTk.PhotoImage(contact_img)
                
                self.contact_img_ref = tk_img
                self.img_container = ttk.Frame(contact_image_frame, width=fixed_width, height=fixed_height)
                self.img_container.pack(pady=5)
                self.img_container.pack_propagate(False)
                
                self.img_label = ttk.Label(self.img_container, image=tk_img)
                self.img_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
                
                ttk.Label(contact_image_frame, text="Mapa de Contacto", 
                         font=('Segoe UI', 10, 'bold')).pack(pady=(0, 5))
        except Exception as e:
            print(f"No se pudo cargar imagen de contacto: {e}")
        
        # Separador
        ttk.Separator(contact_main_frame, orient='horizontal').grid(row=1, column=0, sticky="ew", pady=10)
        
        # Leyenda
        legend_frame = ttk.Frame(contact_main_frame)
        legend_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        
        ttk.Label(legend_frame, text="Calidad: ").pack(side=tk.LEFT)
        
        for status, color in [("Bueno", self.contact_colors["good"]), 
                            ("Regular", self.contact_colors["fair"]), 
                            ("Pobre", self.contact_colors["poor"])]:
            status_frame = ttk.Frame(legend_frame)
            status_frame.pack(side=tk.LEFT, padx=5)
            
            color_indicator = tk.Canvas(status_frame, width=15, height=15, 
                                      bg=color, highlightthickness=0)
            color_indicator.pack(side=tk.LEFT, padx=(0, 5))
            
            ttk.Label(status_frame, text=status).pack(side=tk.LEFT)
        
        # Grid de indicadores con scroll si hay muchos canales
        if len(self.channels) > 20:
            # Crear canvas con scrollbar para muchos canales
            canvas_frame = ttk.Frame(contact_main_frame)
            canvas_frame.grid(row=3, column=0, sticky="nsew")
            
            canvas = tk.Canvas(canvas_frame)
            scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
            scrollable_frame = ttk.Frame(canvas)
            
            scrollable_frame.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
            )
            
            canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            
            contact_grid = scrollable_frame
        else:
            # Grid simple para pocos canales
            contact_grid = ttk.Frame(contact_main_frame)
            contact_grid.grid(row=3, column=0, sticky="nsew", pady=5)
        
        # Determinar columnas según número de canales
        cols_per_row = 2 if len(self.channels) <= 14 else 3
        
        # Crear indicadores
        self.contact_indicators = {}
        
        for i, channel in enumerate(self.channels):
            row = i // cols_per_row
            col = i % cols_per_row
            
            channel_frame = ttk.Frame(contact_grid, padding=3)
            channel_frame.grid(row=row, column=col, padx=3, pady=2, sticky="w")
            
            # Indicador circular
            indicator = tk.Canvas(channel_frame, width=25, height=25, highlightthickness=0)
            indicator.create_oval(3, 3, 22, 22, fill=self.contact_colors["unknown"], outline="")
            indicator.pack(side=tk.LEFT)
            
            # Etiqueta
            ttk.Label(channel_frame, text=channel, width=5).pack(side=tk.LEFT, padx=(3, 0))
            
            self.contact_indicators[channel] = indicator

    def start_data_capture(self):
        """Inicia la captura y actualización de datos usando el hilo principal de tkinter"""
        if self.is_capturing:
            return
            
        self.is_capturing = True
        
        # Función para realizar actualizaciones periódicas EN EL HILO PRINCIPAL
        def update_loop():
            if not self.is_capturing or not hasattr(self, 'pow_window') or self.pow_window is None:
                self.is_capturing = False
                return
                
            try:
                # NO llamar a update_data() ya que los datos vienen de add_pow_data()
                self.update_adaptive_range()  # Actualizar rangos individuales
                self.update_bar_plots()        # Actualizar gráficas
                self.update_contact_indicators()  # Actualizar indicadores de contacto
                
                # Programar la siguiente actualización en el hilo principal
                if self.pow_window and self.pow_window.winfo_exists():
                    self.pow_window.after(self.update_interval, update_loop)
            except Exception as e:
                print(f"Error en el ciclo de actualización: {e}")
                import traceback
                traceback.print_exc()
                self.is_capturing = False
        
        # Iniciar el ciclo de actualización en el hilo principal
        if self.pow_window and self.pow_window.winfo_exists():
            self.pow_window.after(self.update_interval, update_loop)


    def update_adaptive_range(self):
        """
        Actualiza el rango de visualización para cada canal individualmente
        con respuesta ultra-rápida a cambios en la señal
        """
        try:
            # CRÍTICO: Verificar que las gráficas estén configuradas
            if not hasattr(self, 'axes') or not self.axes:
                return
                
            # Verificar que el canvas existe
            if not hasattr(self, 'canvas') or not self.canvas:
                return
                   
            # Actualizar rangos para cada canal individualmente
            ranges_updated = False
            
            for channel in self.channels:
                # Recopilar todos los valores del histórico para este canal
                channel_values = []
                for band in self.bands:
                    if self.data_history[channel][band]:
                        channel_values.extend(self.data_history[channel][band])
                
                # Si no hay suficientes datos para este canal, continuar con el siguiente
                if len(channel_values) < 3:  # Mínimo reducido para adaptación instantánea
                    continue
                    
                # Calcular percentiles para determinar el rango de este canal específico
                min_percentile = 0.5  # Muy agresivo para capturar valores negativos
                p_low = np.percentile(channel_values, min_percentile)
                p_high = np.percentile(channel_values, self.range_percentile)
                
                # Asegurar que el mínimo nunca sea mayor que -0.5 como solicitado
                new_min = min(p_low, 0)
                
                # Margen muy reducido para respuesta más directa
                margin = (p_high - new_min) * 0.05  # Solo 5% de margen
                new_max = p_high + margin
                
                # Asegurar un rango mínimo para evitar problemas con señales muy débiles
                if new_max - new_min < 1:
                    new_max = new_min + 1
                
                # Actualizar sin umbral - siempre adaptar inmediatamente
                if new_min != self.min_values[channel] or new_max != self.max_values[channel]:
                    self.min_values[channel] = new_min
                    self.max_values[channel] = new_max
                    
                    # Actualizar rango de este canal específicamente
                    channel_idx = self.channels.index(channel)
                    if channel_idx < len(self.axes) and self.axes[channel_idx]:
                        try:
                            self.axes[channel_idx].set_ylim([new_min, new_max])
                            ranges_updated = True
                        except Exception as e:
                            print(f"Error actualizando eje {channel}: {e}")
            
            # Si al menos un canal tuvo su rango actualizado, actualizar canvas
            if ranges_updated and hasattr(self, 'canvas') and self.canvas:
                try:
                    self.canvas.draw_idle()  # Usar draw_idle en lugar de draw
                except Exception as e:
                    print(f"Error dibujando canvas: {e}")
                    
        except Exception as e:
            print(f"Error en update_adaptive_range: {e}")
            import traceback
            traceback.print_exc()

    def update_bar_plots(self):
        """Actualiza las gráficas de barras con los datos más recientes"""
        try:
            # Verificar que todo está inicializado
            if not hasattr(self, 'bar_plots') or not self.bar_plots:
                return
                
            if not hasattr(self, 'canvas') or not self.canvas:
                return
            
            # Actualizar cada gráfica de barras
            plots_updated = False
            for channel in self.channels:
                if channel not in self.bar_plots:
                    continue
                    
                for band in self.bands:
                    # Obtener la barra correspondiente
                    bar = self.bar_plots.get(channel, {}).get(band)
                    if bar is None:
                        continue
                        
                    # Obtener valor actual
                    value = self.latest_data.get(channel, {}).get(band, 0)
                    
                    # Actualizar altura de la barra
                    try:
                        bar.set_height(value)
                        plots_updated = True
                    except Exception as e:
                        print(f"Error actualizando barra {channel}/{band}: {e}")
            
            # Actualizar canvas solo si hubo cambios
            if plots_updated and hasattr(self, 'canvas') and self.canvas:
                try:
                    self.canvas.draw_idle()
                except Exception as e:
                    print(f"Error dibujando canvas: {e}")
                    
        except Exception as e:
            print(f"Error en update_bar_plots: {e}")
            import traceback
            traceback.print_exc()

    def calculate_channel_power(self, channel):
        """Calcula la potencia promedio de un canal a través de todas las bandas"""
        if channel not in self.latest_data:
            return 0.0
            
        # Calcular promedio ponderado de potencia
        # Damos más peso a alpha y beta que son buenos indicadores de actividad
        weights = {
            "theta": 0.8,
            "alpha": 1.2,
            "betaL": 1.2,
            "betaH": 1.0,
            "gamma": 0.7
        }
        
        total_power = 0.0
        total_weight = 0.0
        
        for band in self.bands:
            if band in self.latest_data[channel]:
                power = self.latest_data[channel][band]
                weight = weights.get(band, 1.0)
                
                total_power += power * weight
                total_weight += weight
                
        if total_weight == 0:
            return 0.0
            
        return total_power / total_weight
    
    # NUEVO: Método para habilitar/deshabilitar visualización
    def toggle_visualization(self, enabled):
        """
        Activa o desactiva la visualización de potencia
        
        Args:
            enabled (bool): True para activar, False para desactivar
        """
        self.visualization_enabled = enabled
        
        if enabled:
            # Si se activa y hay datos disponibles, abrir ventana
            if self.device_type and not self.pow_window:
                self.open_pow_window()
        else:
            # Si se desactiva, cerrar ventana
            if self.pow_window:
                self.close_window()

    def add_pow_data(self, pow_data):
        """
        Añade nuevos datos POW con detección automática de dispositivo
        Solo procesa datos si la visualización está habilitada
        """
        try:
            # Detectar tipo de dispositivo si aún no se ha hecho
            if self.device_type is None:
                detected_type = self.detect_device_type(len(pow_data))
                
                if detected_type:
                    self.device_type = detected_type
                    success = self.initialize_device_configuration(detected_type)
                    
                    if not success:
                        print("❌ Error al inicializar configuración del dispositivo")
                        return
                    
                    # NO abrir ventana automáticamente
                    # Solo inicializar configuración
                    print(f"✅ Dispositivo {detected_type} configurado. Visualización lista para activarse.")
                else:
                    print(f"❌ No se pudo detectar el tipo de dispositivo (datos: {len(pow_data)})")
                    return
            
            # MODIFICADO: Solo procesar datos si la visualización está habilitada
            if not self.visualization_enabled:
                return
            
            # Verificar formato de datos
            expected_length = len(self.channels) * len(self.bands)
            if len(pow_data) != expected_length:
                print(f"Error: formato de datos incorrecto. Se esperaban {expected_length} valores, se recibieron {len(pow_data)}")
                return
            
            # Procesar datos
            for c_idx, channel in enumerate(self.channels):
                for b_idx, band in enumerate(self.bands):
                    data_idx = c_idx * 5 + b_idx
                    
                    if data_idx < len(pow_data):
                        value = pow_data[data_idx]
                        
                        self.latest_data[channel][band] = value
                        self.data_history[channel][band].append(value)
                        
                        max_history = self.history_window
                        if len(self.data_history[channel][band]) > max_history:
                            self.data_history[channel][band] = self.data_history[channel][band][-max_history:]
                            
        except Exception as e:
            print(f"Error en add_pow_data: {e}")
            import traceback
            traceback.print_exc()

    def get_contact_quality_summary(self):
        """
        Obtiene un resumen de la calidad de contacto de los electrodos
        
        Returns:
            tuple: (porcentaje_buenos, porcentaje_malos, canales_malos)
        """
        if not self.contact_status:
            return 0, 0, []
            
        # Contar canales por estado
        good_count = sum(1 for status in self.contact_status.values() if status == "good")
        fair_count = sum(1 for status in self.contact_status.values() if status == "fair")
        poor_count = sum(1 for status in self.contact_status.values() if status == "poor")
        total = len(self.contact_status)
        
        # Calcular porcentajes
        good_percent = (good_count / total) * 100 if total > 0 else 0
        poor_percent = (poor_count / total) * 100 if total > 0 else 0
        
        # Obtener lista de canales con mal contacto
        poor_channels = [channel for channel, status in self.contact_status.items() 
                       if status == "poor"]
        
        return good_percent, poor_percent, poor_channels

    def on_closing(self):
        """Maneja el cierre de la ventana liberando recursos correctamente"""
        self.close_window()
        
    def close_window(self):
        """Cierra la ventana de visualización POW completamente"""
        try:
            
            # ✅ NUEVO: Notificar al sistema principal que se cerró la ventana
            self.visualization_enabled = False
            
            # Actualizar checkbox y label en el sistema principal
            if hasattr(self.parent, 'pow_visualization_var'):
                self.parent.window.after(0, lambda: self.parent.pow_visualization_var.set(False))
            
            if hasattr(self.parent, 'pow_status_label'):
                self.parent.window.after(0, lambda: self.parent.pow_status_label.config(
                    text="⚪ Inactivo", 
                    foreground="gray"
                ))

            # Primero detener la visualización
            self.is_capturing = False
            self.is_running = False
            
            # Cancelar cualquier callback pendiente
            if hasattr(self, 'resize_timer') and hasattr(self, 'pow_window') and self.pow_window:
                try:
                    self.pow_window.after_cancel(self.resize_timer)
                except:
                    pass
            
            if hasattr(self, 'stop_event'):
                self.stop_event.set()
            
            # Esperar a que el hilo termine si existe
            if hasattr(self, 'pow_data_thread') and self.pow_data_thread and self.pow_data_thread.is_alive():
                print("Esperando a que termine el hilo de visualización...")
                self.pow_data_thread.join(timeout=1.0)
            
            # Limpiar canvas y desconectar eventos
            if hasattr(self, 'canvas') and self.canvas:
                try:
                    # Desconectar todos los eventos
                    if hasattr(self.canvas, 'callbacks'):
                        self.canvas.callbacks.callbacks.clear()
                    
                    # Eliminar el widget
                    widget = self.canvas.get_tk_widget()
                    if widget:
                        try:
                            if widget.winfo_exists():
                                widget.destroy()
                        except:
                            pass
                except Exception as e:
                    print(f"Error limpiando canvas: {e}")
            
            # Cerrar figura de matplotlib
            if hasattr(self, 'fig') and self.fig is not None:
                try:
                    # Limpiar callbacks de la figura también
                    if hasattr(self.fig, 'canvas') and hasattr(self.fig.canvas, 'callbacks'):
                        try:
                            self.fig.canvas.callbacks.callbacks.clear()
                        except:
                            pass
                    
                    plt.close(self.fig)
                    self.fig = None
                except Exception as e:
                    # Ignorar errores de 'button_press_event' al cerrar
                    if 'button_press_event' not in str(e):
                        print(f"Error cerrando figura: {e}")
            
            # Limpiar referencias
            if hasattr(self, 'canvas'):
                self.canvas = None
            if hasattr(self, 'axes'):
                self.axes = None
            
            # Limpiar cola de datos
            try:
                if hasattr(self, 'pow_data_queue'):
                    while not self.pow_data_queue.empty():
                        try:
                            self.pow_data_queue.get_nowait()
                            self.pow_data_queue.task_done()
                        except:
                            break
            except Exception as e:
                pass  # Ignorar errores al limpiar la cola
            
            # Cerrar la ventana
            if hasattr(self, 'pow_window') and self.pow_window is not None:
                try:
                    if self.pow_window.winfo_exists():
                        self.pow_window.destroy()
                except Exception as e:
                    pass  # Ignorar errores al destruir la ventana
                finally:
                    self.pow_window = None
                    
            print("✅ Ventana de visualización POW cerrada correctamente")
            
        except Exception as e:
            # Solo mostrar errores críticos
            if 'button_press_event' not in str(e):
                print(f"Error en close_window: {e}")

class ScreenshotHandler(FileSystemEventHandler):
    """
    Mueve a <sesión>/Screenshots las capturas creadas durante la grabación.
    - Detecta la carpeta REAL de capturas de Windows (Known Folder), no solo por nombre.
    - Vigila varias carpetas candidatas a la vez.
    - Espera a que el archivo termine de escribirse antes de moverlo.
    - Maneja created / modified / moved y evita duplicados.
    """

    IMG_EXT = ('.png', '.jpg', '.jpeg', '.bmp')

    FOLDERID_SCREENSHOTS = '{b7bede81-df94-4682-a7d8-57a52620b86f}'
    FOLDERID_PICTURES    = '{33E28130-4E1E-4676-835A-98395C3BC3BB}'

    def __init__(self, parent):
        self.parent             = parent
        self.observer           = None
        self.is_monitoring      = False
        self.session_start_time = None
        self._lock              = threading.Lock()
        self._processed         = set()
        self.screenshot_folders = self._detect_screenshot_folders()

        if self.screenshot_folders:
            for f in self.screenshot_folders:
                print(f"📸 Carpeta de capturas detectada: {f}")
        else:
            print("⚠️ No se encontró ninguna carpeta de capturas de pantalla")

    # ── Detección de carpetas ────────────────────────────────────────────
    @staticmethod
    def _known_folder(guid_str):
        """Ruta real de una Known Folder de Windows (None si falla)."""
        if sys.platform != 'win32':
            return None
        try:
            import ctypes, uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD),  ("Data4", ctypes.c_ubyte * 8)]

            u    = uuid.UUID(guid_str)
            guid = GUID(u.fields[0], u.fields[1], u.fields[2],
                        (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            ptr  = ctypes.c_void_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(ptr)) != 0:
                return None
            path = ctypes.wstring_at(ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return path
        except Exception as e:
            print(f"   (Known Folder no disponible: {e})")
            return None

    def _detect_screenshot_folders(self):
        candidatos = []

        # 1. Carpeta oficial de capturas (Win+ImpPt, Recortes)
        ks = self._known_folder(self.FOLDERID_SCREENSHOTS)
        if ks:
            candidatos.append(ks)

        # 2. Subcarpetas típicas dentro de la carpeta Imágenes real
        pics = self._known_folder(self.FOLDERID_PICTURES)
        if pics:
            candidatos += [os.path.join(pics, "Screenshots"),
                           os.path.join(pics, "Capturas de pantalla")]

        # 3. Rutas comunes (OneDrive / local, español / inglés)
        home = os.path.expanduser("~")
        for base in ("OneDrive\\Imágenes", "OneDrive\\Pictures",
                     "Imágenes", "Pictures"):
            for sub in ("Screenshots", "Capturas de pantalla"):
                candidatos.append(os.path.join(home, base, sub))

        # Existentes y sin duplicados
        vistos, finales = set(), []
        for c in candidatos:
            key = os.path.normcase(os.path.abspath(c))
            if key not in vistos and os.path.isdir(c):
                vistos.add(key)
                finales.append(c)
        return finales

    # ── Control ──────────────────────────────────────────────────────────
    def start_monitoring(self):
        if self.is_monitoring:
            return True
        if not self.screenshot_folders:
            self.screenshot_folders = self._detect_screenshot_folders()
        if not self.screenshot_folders:
            print("⚠️ Monitoreo de capturas NO iniciado: sin carpetas válidas")
            return False

        self.session_start_time = time.time()
        with self._lock:
            self._processed.clear()

        self.observer = Observer()
        for folder in self.screenshot_folders:
            try:
                self.observer.schedule(self, folder, recursive=False)
                print(f"📸 Monitoreando capturas en: {folder}")
            except Exception as e:
                print(f"⚠️ No se pudo monitorear {folder}: {e}")
        self.observer.start()
        self.is_monitoring = True
        return True

    def stop_monitoring(self):
        if not self.is_monitoring:
            return
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=2.0)
            except Exception:
                pass
            self.observer = None
        self.is_monitoring      = False
        self.session_start_time = None
        print("Monitoreo de capturas de pantalla detenido")

    # ── Eventos watchdog ─────────────────────────────────────────────────
    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event):
        # Recortes/OneDrive escriben un temporal y lo renombran al final
        if not event.is_directory:
            self._enqueue(event.dest_path)

    def _enqueue(self, path):
        if not path.lower().endswith(self.IMG_EXT):
            return
        if not getattr(self.parent, 'is_recording', False):
            return
        key = os.path.normcase(os.path.abspath(path))
        with self._lock:
            if key in self._processed:
                return
            self._processed.add(key)
        # Procesar en hilo aparte: no bloquear el hilo del observer
        threading.Thread(target=self._process_screenshot, args=(path, key),
                         daemon=True, name="screenshot-mover").start()

    # ── Procesamiento ────────────────────────────────────────────────────
    @staticmethod
    def _wait_stable(path, quiet=0.4, timeout=6.0):
        t0, last, last_chg = time.time(), None, time.time()
        while time.time() - t0 < timeout:
            try:
                sz = os.path.getsize(path)
            except OSError:
                return False
            if sz != last:
                last, last_chg = sz, time.time()
            elif sz > 0 and time.time() - last_chg >= quiet:
                return True
            time.sleep(0.1)
        return os.path.exists(path)

    def _process_screenshot(self, file_path, key):
        try:
            if not self._wait_stable(file_path):
                with self._lock:
                    self._processed.discard(key)   # permitir reintento por otro evento
                return

            # Solo capturas hechas durante la sesión
            try:
                st = os.stat(file_path)
                t_file = max(st.st_ctime, st.st_mtime)
            except OSError:
                return
            if self.session_start_time and t_file < self.session_start_time - 1:
                return

            session_folder = getattr(self.parent, 'session_folder', None)
            if not session_folder:
                print("⚠️ Captura ignorada: no hay carpeta de sesión")
                return

            dest_folder = os.path.join(session_folder, "Screenshots")
            os.makedirs(dest_folder, exist_ok=True)

            participant = self.parent.validated_name.get().strip() or "Participante"
            modo        = self.parent.test_mode.get()
            ext         = os.path.splitext(file_path)[1].lower()
            stamp       = datetime.fromtimestamp(t_file).strftime("%Y%m%d_%H%M%S_%f")[:-3]

            dest = os.path.join(dest_folder, f"{participant}_{modo}_{stamp}{ext}")
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(dest_folder, f"{participant}_{modo}_{stamp}_{n}{ext}")
                n += 1

            # Copiar (con reintentos por bloqueo) y luego borrar original
            for intento in range(10):
                try:
                    shutil.copy2(file_path, dest)
                    break
                except PermissionError:
                    time.sleep(0.3)
            else:
                print(f"❌ No se pudo copiar la captura (bloqueada): {file_path}")
                with self._lock:
                    self._processed.discard(key)
                return

            for intento in range(10):
                try:
                    os.remove(file_path)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    time.sleep(0.3)
            else:
                print(f"⚠️ Copia hecha, pero el original sigue en: {file_path}")

            print(f"📸 Captura guardada en sesión: {os.path.basename(dest)}")
            if hasattr(self.parent, 'gui_queue'):
                self.parent.gui_queue.put({
                    'type': 'system_status',
                    'data': f'Captura guardada: {os.path.basename(dest)}'
                })

        except Exception as e:
            print(f"❌ Error al procesar captura {file_path}: {e}")
            
class CircularMetricsBuffer:
    """
    Buffer circular thread-safe con respaldo automático.
    Garantiza cero pérdida de datos incluso si la aplicación crashea.
    """
    
    def __init__(self, 
                 batch_size=300, 
                 flush_interval=30.0,
                 backup_interval=10.0,
                 max_memory_rows=10000):
        """
        Args:
            batch_size: Registros antes de escribir al CSV principal
            flush_interval: Segundos máximos entre escrituras al CSV
            backup_interval: Segundos entre respaldos de seguridad
            max_memory_rows: Máximo de filas en memoria antes de forzar flush
        """
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.backup_interval = backup_interval
        self.max_memory_rows = max_memory_rows
        
        # ========================================
        # Buffers circulares por tipo de métrica
        # ========================================
        self.buffers = {}
        # Estructura: {
        #   'metric_type': {
        #       'pending': deque(),      # Datos pendientes de escribir al CSV
        #       'file': file_handle,     # Archivo CSV abierto
        #       'writer': csv_writer,
        #       'path': str,
        #       'headers': list,
        #       'total_written': int,    # Total de filas escritas al CSV
        #       'backup_path': str,      # Archivo de respaldo temporal
        #   }
        # }
        
        # ========================================
        # Control de hilos
        # ========================================
        self.write_queue = Queue()
        self.stop_event = threading.Event()
        self.write_thread = None
        self.backup_thread = None
        
        # ========================================
        # Timestamps y estadísticas
        # ========================================
        self.last_flush_time = {}
        self.last_backup_time = {}
        self.stats = {
            'total_rows_buffered': 0,
            'total_rows_written': 0,
            'total_flushes': 0,
            'total_backups': 0,
            'recovered_rows': 0,
        }
        
        # Lock para acceso thread-safe
        self.lock = threading.Lock()
        
        # Estado
        self.is_active = False
        
        # Directorio para archivos de respaldo
        self.backup_dir = os.path.join(tempfile.gettempdir(), 'metrics_backup')
        os.makedirs(self.backup_dir, exist_ok=True)
        
        # Registrar limpieza al cerrar
        atexit.register(self._emergency_save)
        
    def start(self):
        """Inicia los hilos de escritura y respaldo"""
        if self.is_active:
            return
            
        self.stop_event.clear()
        
        # Hilo de escritura principal
        self.write_thread = threading.Thread(
            target=self._write_loop,
            name="MetricsWriter",
            daemon=True
        )
        self.write_thread.start()
        
        # Hilo de respaldo automático
        self.backup_thread = threading.Thread(
            target=self._backup_loop,
            name="MetricsBackup",
            daemon=True
        )
        self.backup_thread.start()
        
        self.is_active = True
        print("✅ CircularMetricsBuffer iniciado (con respaldo automático)")
        
    def stop(self):
        """
        Detiene el gestor y escribe TODOS los datos pendientes.
        GARANTIZA cero pérdida de datos.
        
        MEJORADO v2:
        - Verifica conteo final de datos pendientes
        - Flush en bloques si hay muchos datos
        - fsync para garantizar escritura física
        - Backup de emergencia si falla el flush
        """
        if not self.is_active:
            return
            
        print("🔄 Deteniendo CircularMetricsBuffer...")
        
        # ══════════════════════════════════════════════════════════════
        # PASO 1: Señalar detención
        # ══════════════════════════════════════════════════════════════
        self.stop_event.set()
        
        # ══════════════════════════════════════════════════════════════
        # PASO 2: Esperar hilos
        # ══════════════════════════════════════════════════════════════
        if self.write_thread and self.write_thread.is_alive():
            print("   ⏳ Esperando hilo de escritura...")
            self.write_thread.join(timeout=5.0)
            if self.write_thread.is_alive():
                print("   ⚠️ Hilo de escritura no terminó a tiempo")
        
        if self.backup_thread and self.backup_thread.is_alive():
            print("   ⏳ Esperando hilo de backup...")
            self.backup_thread.join(timeout=2.0)
        
        # ══════════════════════════════════════════════════════════════
        # PASO 3: FLUSH FINAL DE TODOS LOS DATOS ← CRÍTICO
        # ══════════════════════════════════════════════════════════════
        print("   💾 Flush final de todos los buffers...")
        
        with self.lock:
            total_flushed = 0
            
            for metric_type in list(self.buffers.keys()):
                try:
                    buffer_info = self.buffers[metric_type]
                    pending_count = len(buffer_info['pending'])
                    
                    if pending_count > 0:
                        print(f"      📊 '{metric_type}': {pending_count} filas pendientes")
                        self._flush_all(metric_type)
                        total_flushed += pending_count
                    else:
                        print(f"      ✅ '{metric_type}': Sin datos pendientes")
                    
                    # Cerrar archivo
                    self._close_file(metric_type)
                    
                    # Limpiar backup
                    self._cleanup_backup(metric_type)
                    
                except Exception as e:
                    print(f"   ❌ Error en flush '{metric_type}': {e}")
                    
                    # PLAN B: Guardar backup de emergencia
                    try:
                        self._save_backup(metric_type)
                        print(f"   ⚠️ Backup de emergencia guardado")
                    except:
                        pass
        
        self.is_active = False
        
        # ══════════════════════════════════════════════════════════════
        # PASO 4: Mostrar estadísticas
        # ══════════════════════════════════════════════════════════════
        print(f"✅ CircularMetricsBuffer detenido")
        print(f"   💾 Filas guardadas en flush final: {total_flushed}")
        print(f"   📊 Filas totales escritas: {self.stats['total_rows_written']}")
        print(f"   🔄 Flushes realizados: {self.stats['total_flushes']}")
        
        # Verificar si quedó algo (no debería)
        total_remaining = sum(len(b['pending']) for b in self.buffers.values())
        if total_remaining > 0:
            print(f"   ⚠️ ADVERTENCIA: {total_remaining} filas sin guardar")
        else:
            print(f"   ✅ Todos los datos guardados correctamente")

    def register_metric(self, metric_type, file_path, headers):
        """
        Registra un nuevo tipo de métrica
        
        Args:
            metric_type: Identificador único (ej: 'facial', 'lane')
            file_path: Ruta del archivo CSV
            headers: Lista de encabezados
        """
        with self.lock:
            try:
                # Verificar si hay datos de respaldo para recuperar
                backup_path = os.path.join(self.backup_dir, f"{metric_type}_backup.pkl")
                recovered_data = self._recover_backup(backup_path)
                
                # Crear directorio si no existe
                os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
                
                # Abrir archivo CSV
                file_handle = open(file_path, 'w', newline='', buffering=8192)
                writer = csv.writer(file_handle)
                writer.writerow(headers)
                file_handle.flush()
                
                # Crear buffer circular
                self.buffers[metric_type] = {
                    'pending': deque(maxlen=self.max_memory_rows),
                    'file': file_handle,
                    'writer': writer,
                    'path': file_path,
                    'headers': headers,
                    'total_written': 0,
                    'backup_path': backup_path,
                }
                
                # Si hay datos recuperados, añadirlos al buffer
                if recovered_data:
                    for row in recovered_data:
                        self.buffers[metric_type]['pending'].append(row)
                    self.stats['recovered_rows'] += len(recovered_data)
                    print(f"   ♻️ Recuperados {len(recovered_data)} registros de '{metric_type}'")
                
                self.last_flush_time[metric_type] = time.time()
                self.last_backup_time[metric_type] = time.time()
                
                print(f"✅ Métrica '{metric_type}' registrada: {os.path.basename(file_path)}")
                return True
                
            except Exception as e:
                print(f"❌ Error registrando métrica '{metric_type}': {e}")
                import traceback
                traceback.print_exc()
                return False
    
    def add_row(self, metric_type, row_data):
        """
        Añade una fila al buffer circular (NO BLOQUEA)
        
        Args:
            metric_type: Tipo de métrica
            row_data: Lista con los datos de la fila
        """
        if metric_type not in self.buffers:
            return False
        
        with self.lock:
            # Añadir al buffer circular
            self.buffers[metric_type]['pending'].append(row_data)
            self.stats['total_rows_buffered'] += 1
            
            # Verificar si necesita flush
            pending_count = len(self.buffers[metric_type]['pending'])
            time_since_flush = time.time() - self.last_flush_time.get(metric_type, 0)
            
            needs_flush = (
                pending_count >= self.batch_size or 
                time_since_flush >= self.flush_interval or
                pending_count >= self.max_memory_rows * 0.9  # 90% de capacidad
            )
            
            if needs_flush:
                # Encolar para escritura (no bloqueante)
                try:
                    self.write_queue.put_nowait(metric_type)
                except:
                    pass  # Cola llena, se procesará en el siguiente ciclo
                    
        return True
    
    def _write_loop(self):
        """Hilo de escritura - procesa flush de buffers"""
        while not self.stop_event.is_set():
            try:
                # Esperar trabajo
                try:
                    metric_type = self.write_queue.get(timeout=1.0)
                except Empty:
                    # Verificar flush por tiempo
                    self._check_time_flush()
                    continue
                
                # Realizar flush
                with self.lock:
                    self._flush_batch(metric_type)
                    
            except Exception as e:
                print(f"❌ Error en hilo de escritura: {e}")
                
        print("🔄 Hilo de escritura terminado")
    
    def _backup_loop(self):
        """Hilo de respaldo - guarda estado periódicamente"""
        while not self.stop_event.is_set():
            try:
                time.sleep(self.backup_interval)
                
                if self.stop_event.is_set():
                    break
                
                # Realizar backup de todos los buffers
                with self.lock:
                    for metric_type in self.buffers:
                        self._save_backup(metric_type)
                        
            except Exception as e:
                print(f"❌ Error en hilo de backup: {e}")
                
        print("🔄 Hilo de backup terminado")
    
    def _check_time_flush(self):
        """Verifica si algún buffer necesita flush por tiempo"""
        current_time = time.time()
        
        with self.lock:
            for metric_type, last_time in list(self.last_flush_time.items()):
                if metric_type not in self.buffers:
                    continue
                    
                pending = self.buffers[metric_type]['pending']
                if pending and (current_time - last_time) >= self.flush_interval:
                    self._flush_batch(metric_type)
    
    def _flush_batch(self, metric_type):
        """Escribe un lote del buffer al CSV (llamar con lock)"""
        if metric_type not in self.buffers:
            return
            
        buffer_info = self.buffers[metric_type]
        pending = buffer_info['pending']
        
        if not pending:
            return
            
        try:
            writer = buffer_info['writer']
            file_handle = buffer_info['file']
            
            # Extraer hasta batch_size elementos
            rows_to_write = []
            count = min(len(pending), self.batch_size)
            
            for _ in range(count):
                if pending:
                    rows_to_write.append(pending.popleft())
            
            # Escribir al archivo
            if rows_to_write:
                writer.writerows(rows_to_write)
                file_handle.flush()
                
                buffer_info['total_written'] += len(rows_to_write)
                self.stats['total_rows_written'] += len(rows_to_write)
                self.stats['total_flushes'] += 1
                
            self.last_flush_time[metric_type] = time.time()
            
        except Exception as e:
            print(f"❌ Error en flush '{metric_type}': {e}")
    
    def _flush_all(self, metric_type):
        """
        Escribe TODO el buffer al CSV.
        MEJORADO v2: Flush en bloques + fsync
        """
        if metric_type not in self.buffers:
            return
            
        buffer_info = self.buffers[metric_type]
        pending = buffer_info['pending']
        
        if not pending:
            return
            
        try:
            writer = buffer_info['writer']
            file_handle = buffer_info['file']
            
            # Copiar datos completos
            rows_to_write = list(pending)
            rows_count = len(rows_to_write)
            
            if rows_to_write:
                # Escribir en bloques si hay muchos datos
                BLOCK_SIZE = 1000
                written = 0
                
                for i in range(0, rows_count, BLOCK_SIZE):
                    block = rows_to_write[i:i+BLOCK_SIZE]
                    writer.writerows(block)
                    written += len(block)
                
                # CRÍTICO: Flush al disco físico
                file_handle.flush()
                os.fsync(file_handle.fileno())
                
                # Actualizar contadores
                buffer_info['total_written'] += written
                self.stats['total_rows_written'] += written
                self.stats['total_flushes'] += 1
                
                # Limpiar buffer DESPUÉS de escritura exitosa
                pending.clear()
                
                print(f"      ✅ Flush completo: {written} filas")
                
        except Exception as e:
            print(f"   ❌ Error crítico en flush: {e}")
            raise  # NO limpiar buffer si falló
    
    def _save_backup(self, metric_type):
        """Guarda el estado actual del buffer a un archivo de respaldo"""
        if metric_type not in self.buffers:
            return
            
        buffer_info = self.buffers[metric_type]
        pending = buffer_info['pending']
        
        if not pending:
            # Limpiar backup si no hay datos pendientes
            self._cleanup_backup(metric_type)
            return
            
        try:
            backup_path = buffer_info['backup_path']
            
            # Guardar como pickle (más rápido que JSON para listas)
            backup_data = {
                'metric_type': metric_type,
                'timestamp': time.time(),
                'headers': buffer_info['headers'],
                'pending': list(pending),  # Convertir deque a lista
            }
            
            # Escribir a archivo temporal primero, luego renombrar (atómico)
            temp_path = backup_path + '.tmp'
            with open(temp_path, 'wb') as f:
                pickle.dump(backup_data, f)
            
            # Renombrar (operación atómica en la mayoría de sistemas)
            os.replace(temp_path, backup_path)
            
            self.stats['total_backups'] += 1
            self.last_backup_time[metric_type] = time.time()
            
        except Exception as e:
            # Error silencioso - el backup es secundario
            pass
    
    def _recover_backup(self, backup_path):
        """Intenta recuperar datos de un archivo de respaldo"""
        if not os.path.exists(backup_path):
            return None
            
        try:
            with open(backup_path, 'rb') as f:
                backup_data = pickle.load(f)
            
            # Verificar que no sea muy antiguo (máximo 1 hora)
            if time.time() - backup_data.get('timestamp', 0) > 3600:
                os.remove(backup_path)
                return None
            
            pending = backup_data.get('pending', [])
            if pending:
                print(f"   🔄 Encontrado backup con {len(pending)} registros")
                return pending
                
        except Exception as e:
            print(f"   ⚠️ No se pudo recuperar backup: {e}")
            
        return None
    
    def _cleanup_backup(self, metric_type):
        """Elimina el archivo de respaldo de una métrica"""
        if metric_type not in self.buffers:
            return
            
        try:
            backup_path = self.buffers[metric_type].get('backup_path')
            if backup_path and os.path.exists(backup_path):
                os.remove(backup_path)
        except:
            pass
    
    def _close_file(self, metric_type):
        """
        Cierra archivo CSV con flush final.
        MEJORADO v2: fsync antes de cerrar
        """
        if metric_type not in self.buffers:
            return
            
        try:
            buffer_info = self.buffers[metric_type]
            file_handle = buffer_info.get('file')
            
            if file_handle:
                # Último flush + fsync
                try:
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                except:
                    pass
                
                file_handle.close()
                print(f"   ✅ Archivo cerrado: '{metric_type}'")
                
        except Exception as e:
            print(f"   ❌ Error cerrando: {e}")
    
    def _emergency_save(self):
        """Guardado de emergencia al cerrar la aplicación"""
        if not self.is_active:
            return
            
        print("⚠️ Guardado de emergencia activado...")
        
        with self.lock:
            for metric_type in self.buffers:
                try:
                    # Intentar flush final
                    self._flush_all(metric_type)
                except:
                    # Si falla, al menos guardar backup
                    self._save_backup(metric_type)
    
    def get_stats(self):
        """Retorna estadísticas de los buffers"""
        stats = dict(self.stats)
        stats['buffers'] = {}
        
        with self.lock:
            for metric_type, buffer_info in self.buffers.items():
                stats['buffers'][metric_type] = {
                    'pending': len(buffer_info['pending']),
                    'written': buffer_info['total_written'],
                    'path': buffer_info.get('path', 'N/A')
                }
        return stats
    
class ECGState(Enum):
    WAITING = ("Esperando", "gray")
    PREMUESTREO = ("Premuestreo", "yellow")
    CONNECTED = ("Conectado", "green")
    FINISHED = ("Fin de Grabado", "blue")
    DISCONNECTED = ("Desconectado", "red")

    def __init__(self, display_text: str, color: str):
        self.display_text = display_text
        self.color = color

class ECGEventHandler(FileSystemEventHandler):
    def __init__(self, parent):
        self.parent = parent
        self.current_state = ECGState.WAITING
        self.event_sequence: List[str] = []
        self.current_folder_path = None 

        # Flags de estado
        self.flags = {
            'yellow_sequence': False,
            'green_sequence': False,
            'blue_sequence': False,
            'yellow_maintenance': False,
            'green_maintenance': False,
            'blue_maintenance': False,
            'beginning_blue': False
        }
        
        # Archivos requeridos
        self.required_files = {
            'pms12.c8k': False,
            'ecg.c8k': False,
            'tmpecg.c8k': False,
            'tmpqrs.c8k': False
        }
        
        # Secuencias definidas
        self.sequences = {
            'yellow': ['pms12.c8k', 'ecg.c8k', 'tmpecg.c8k', 'tmpqrs.c8k'],
            'green': [
                'ecg.c8k_del', 'pms12.c8k_del',
                'pms12.c8k', 'ecg.c8k'
            ],
            'blue': ['pms12.c8k_del', 'tmpqrs.c8k_del', 'tmpecg.c8k_del']
        }
        
        # Contadores y tiempo
        self.modification_counts = {'yellow': 0, 'green': 0}
        self.last_modification_time: Optional[float] = None
        self.inactivity_timeout = 5

    def start_inactivity_monitoring(self):
        """Inicia el monitoreo periódico de inactividad"""
        def check_periodically():
            # Verificar que la ventana principal aún existe
            if (not self.parent.stop_event.is_set() and 
                hasattr(self.parent, 'window') and 
                self.parent.window and 
                self.parent.window.winfo_exists()):
                self.update_state()
                try:
                    self.parent.window.after(1000, check_periodically)
                except:
                    pass  # Ignorar si la ventana ya no existe
        
        if hasattr(self.parent, 'window') and self.parent.window:
            self.parent.window.after(1000, check_periodically)

    def _check_sequence(self, sequence_type: str) -> bool:
        """Verifica si la secuencia reciente coincide con la secuencia esperada"""
        expected_sequence = self.sequences[sequence_type]
        if len(self.event_sequence) < len(expected_sequence):
            return False
        recent_sequence = self.event_sequence[-len(expected_sequence):]
        return recent_sequence == expected_sequence

    def _handle_inactivity(self) -> bool:
        """Verifica y maneja la inactividad del sistema"""
        if not self.last_modification_time:
            return False
            
        if (self.current_state in [ECGState.FINISHED, ECGState.WAITING] or 
            self.flags['blue_sequence']):
            return False
        
        if time.time() - self.last_modification_time > self.inactivity_timeout:
            # Resetear el sistema para volver a esperar secuencia amarilla
            self._reset_flags()
            self.event_sequence.clear()  # Limpiar la secuencia de eventos
            for file in self.required_files:
                self.required_files[file] = False
            self.modification_counts = {'yellow': 0, 'green': 0}
            self.last_modification_time = None
            
            # Mostrar mensaje de desconexión
            self.parent.window.after(0, lambda: messagebox.showerror(
                "Desconexión",
                "Se ha detectado inactividad en el sistema ECG.\nEl sistema volverá al estado de espera."
            ))
            return True
            
        return False

    def _reset_flags(self):
        """Resetea todos los flags del sistema"""
        for key in self.flags:
            self.flags[key] = False

    def update_state(self):
        """Actualiza el estado del ECG basado en el ciclo de estados"""
        new_state = self.current_state

        # 1. Verificar inactividad
        if self._handle_inactivity():
            # Primero notificar la desconexión
            self._notify_state_change(ECGState.DISCONNECTED)
            # Después de un breve delay, volver a estado de espera
            self.parent.window.after(2000, lambda: self._notify_state_change(ECGState.WAITING))
            return

        # 2. Verificar reset desde azul
        if self._check_blue_reset():
            self._reset_flags()
            new_state = ECGState.WAITING
            self._notify_state_change(new_state)
            return

        # 3. Mantener estado azul si ya estamos en él
        if self.flags['blue_sequence'] or self.flags['blue_maintenance']:
            new_state = ECGState.FINISHED
            self._notify_state_change(new_state)
            return

        # 4. Transición a azul desde verde
        if self._check_green_to_blue_transition():
            new_state = ECGState.FINISHED
            self._notify_state_change(new_state)
            return

        # 5. Verificar y actualizar otros estados
        new_state = self._check_and_update_states()
        self._notify_state_change(new_state)

    def _check_blue_reset(self) -> bool:
        """Verifica si se debe resetear desde el estado azul"""
        if ((self.current_state == ECGState.FINISHED or self.flags['blue_sequence']) and 
            self.event_sequence and self.event_sequence[-1] == "tmpqrs.c8k_del"):
            return True
        return False

    def _check_green_to_blue_transition(self) -> bool:
        """Verifica la transición de verde a azul"""
        if ((self.flags['green_sequence'] or self.flags['green_maintenance']) and 
            self._check_sequence('blue')):
            self.flags['blue_sequence'] = True
            self.flags['blue_maintenance'] = True
            self.flags['green_maintenance'] = False
            self.flags['green_sequence'] = False
            return True
        return False

    def _check_and_update_states(self) -> ECGState:
        """Verifica y actualiza los estados del sistema"""
        if self.flags['green_sequence'] or self.flags['green_maintenance']:
            return ECGState.CONNECTED
        elif self._check_sequence('yellow'):
            if not self.flags['yellow_sequence']:
                print("------FIN SECUENCIA DE IDENTIFICACION PARA AMARILLO")
                self.flags['yellow_sequence'] = True
                self.flags['yellow_maintenance'] = True
            return ECGState.PREMUESTREO
        elif self._check_sequence('green'):
            if not self.flags['green_sequence']:
                print("------FIN SECUENCIA DE IDENTIFICACION PARA VERDE")
                self.flags['green_sequence'] = True
                self.flags['yellow_maintenance'] = False
                self.flags['green_maintenance'] = True
            return ECGState.CONNECTED
        return self.current_state

    def _notify_state_change(self, new_state: ECGState):
        """Notifica cambios de estado a la GUI"""
        if new_state != self.current_state:
            self.current_state = new_state
            print(f"ECG Estado: {new_state.name} - {new_state.display_text} ({new_state.color})")
            self.parent.gui_queue.put({
                'type': 'ecg_status',
                'data': (new_state.display_text, new_state.color)
            })

    def _handle_file_event(self, event_type: str, filename: str):
        """Maneja eventos de archivo y actualiza el estado"""
        if filename in self.required_files:
            # Si es el primer archivo detectado, imprimir la carpeta
            if not any(self.required_files.values()):
                # Buscar el archivo en las subcarpetas de Archive
                for root, dirs, files in os.walk(ECG_PATH):
                    if filename in files:
                        # Obtener el nombre de la subcarpeta (un nivel debajo de Archive)
                        self.current_folder_path = os.path.relpath(root, ECG_PATH)
                        if self.current_folder_path != '.':  # Evitar mostrar 'Archive'
                            print(f"\nCarpeta ECG activa: {self.current_folder_path}")
                        break
            
            if event_type == 'created':
                self.required_files[filename] = True
                self.event_sequence.append(filename)
            elif event_type == 'deleted':
                self.required_files[filename] = False
                self.event_sequence.append(f"{filename}_del")
            elif event_type == 'modified':
                self.event_sequence.append(f"{filename}_mod")
                if self.flags['yellow_maintenance'] or self.flags['green_maintenance']:
                    self.last_modification_time = time.time()
                    maintenance_type = 'yellow' if self.flags['yellow_maintenance'] else 'green'
                    self.modification_counts[maintenance_type] += 1

            self.update_state()

    def on_created(self, event):
        filename = os.path.basename(event.src_path)
        self._handle_file_event('created', filename)

    def on_deleted(self, event):
        filename = os.path.basename(event.src_path)
        self._handle_file_event('deleted', filename)

    def on_modified(self, event):
        filename = os.path.basename(event.src_path)
        self._handle_file_event('modified', filename)
            
def show_calibration_window(parent):
    calibration = tk.Toplevel(parent)
    calibration.title("Calibración del Sistema Multiple")
    calibration.geometry("500x400")
    calibration.resizable(False, False)
    
    # Hacer la ventana modal y centrarla
    calibration.transient(parent)
    calibration.grab_set()
    
    # Centrar la ventana
    screen_width = parent.winfo_screenwidth()
    screen_height = parent.winfo_screenheight()
    x = (screen_width - 500) // 2
    y = (screen_height - 400) // 2
    calibration.geometry(f"500x400+{x}+{y}")
    
    # Configurar el estilo
    style = ttk.Style()
    style.configure("Custom.TFrame", background="#f0f0f0")
    style.configure("Custom.TLabel", background="#f0f0f0", font=("Arial", 12))
    
    # Frame principal
    main_frame = ttk.Frame(calibration, style="Custom.TFrame", padding="20")
    main_frame.pack(fill=tk.BOTH, expand=True)
    
    # Logo IPN
    try:
        # Primero intenta cargar desde la ruta directa
        image_path = "Instituto_Politécnico_Nacional.png"
        if not os.path.exists(image_path):
            # Si no encuentra, busca en el directorio de la aplicación
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            image_path = os.path.join(base_path, "Instituto_Politécnico_Nacional.png")
        
        logo_img = Image.open(image_path)
        logo_img = logo_img.resize((150, 150), Image.Resampling.LANCZOS)
        logo_photo = ImageTk.PhotoImage(logo_img)
        logo_label = ttk.Label(main_frame, image=logo_photo, style="Custom.TLabel")
        logo_label.image = logo_photo  # Mantener referencia
        logo_label.pack(pady=(0, 20))
    except Exception as e:
        print(f"Error al cargar la imagen: {e}")
        # Crear un label de texto en lugar de la imagen si falla
        logo_label = ttk.Label(main_frame, text="IPN", font=("Arial", 30, "bold"), style="Custom.TLabel")
        logo_label.pack(pady=(0, 20))
    
    # Frame para información
    info_frame = ttk.Frame(main_frame, style="Custom.TFrame")
    info_frame.pack(fill=tk.BOTH, expand=True)
    
    # Título
    title_label = ttk.Label(info_frame,
                           text="Sistema de captura multiple",
                           font=("Arial", 14, "bold"),
                           style="Custom.TLabel")
    title_label.pack(pady=(0, 10))
    
    # Mensaje de estado
    status_label = ttk.Label(info_frame,
                            text="Calibrando y conectando los dispositivos...",
                            font=("Arial", 12),
                            style="Custom.TLabel")
    status_label.pack(pady=(0, 20))
    
    # Frame para la barra de progreso
    progress_frame = ttk.Frame(info_frame, style="Custom.TFrame")
    progress_frame.pack(fill=tk.X, pady=(0, 20))
    
    # Barra de progreso
    progress = ttk.Progressbar(progress_frame, 
                              mode='indeterminate',
                              length=300)
    progress.pack(fill=tk.X, padx=20)
    progress.start(10)
    
    # Información adicional
    info_text = "Por favor, mantenga el dispositivo EEG correctamente colocado\n" 
                
    info_label = ttk.Label(info_frame,
                          text=info_text,
                          justify=tk.CENTER,
                          wraplength=400,
                          style="Custom.TLabel")
    info_label.pack(pady=(0, 20))
    
    def update_status():
        status_texts = [
            "Inicializando dispositivo...",
            "Verificando conexión...",
            "Calibrando señales...",
            "Finalizando configuración..."
        ]
        current_text = status_label.cget("text")
        next_index = (status_texts.index(current_text) + 1) if current_text in status_texts else 0
        if next_index < len(status_texts):
            status_label.config(text=status_texts[next_index])
            calibration.after(500, update_status)
        else:
            close_calibration(calibration, progress)
    
    # Iniciar actualización de estado
    status_label.config(text="Inicializando dispositivo...")
    calibration.after(500, update_status)

def close_calibration(window, progress):
    progress.stop()
    window.destroy()


BIOPAC_PATH = r"C:\Users\Public\Documents\BSL Lessons\Data Files"

DEVICES_CONFIG = {
    "BIOPAC": {
        "watch_path": BIOPAC_PATH,
        "states": {
            "WAITING":      ("Esperando",        "gray"),
            "CONNECTED":    ("Conectado",         "yellow"),
            "NAMED":        ("Archivo Nombrado",  "orange"),
            "READY":        ("Grabando",          "green"),
            "FINISHED":     ("Fin de Grabado",    "blue"),
            "DISCONNECTED": ("Desconectado",      "red"),
        },
        "trigger_file":       "_.tmp",
        "record_extension":   ".acq",
        "inactivity_timeout": None,
        "sequences":          {},
        "export_on_ready":    False,   # export manual en stop_acquisition
        "finish_timeout":     1.0,     # ← NUEVO: s sin modificaciones → Fin de Grabado
        "min_mods_to_ready":  2,       # ← NUEVO: modificaciones seguidas para pasar a Grabando
    },
}

class DeviceStateMachine:
    """
    Máquina de estados BIOPAC (.acq / _.tmp).

        _.tmp creado                         → CONNECTED
        .acq  creado                         → NAMED
        .acq  modificado N veces (< timeout) → READY   ("Grabando")
        cada modificación en READY           → reinicia inactividad
        sin modificaciones durante timeout   → FINISHED ("Fin de Grabado")
        .acq  modificado estando FINISHED    → READY   (reanudó)
        .acq  eliminado                      → CONNECTED (BSL borra/recrea)
        _.tmp eliminado en READY/FINISHED    → DISCONNECTED → (3 s) → WAITING
    """

    def __init__(self, device_name: str, config: dict,
                 on_state_change=None, on_export=None):
        self.device_name     = device_name
        self.config          = config
        self.on_state_change = on_state_change
        self.on_export       = on_export

        self.current_state: str = "WAITING"
        self.active_file:   str = ""
        self.prev_sizes:    dict = {}

        self.finish_timeout    = float(config.get("finish_timeout", 1.0))
        self.min_mods_to_ready = int(config.get("min_mods_to_ready", 2))

        self._lock           = threading.RLock()
        self._last_mod_time  = 0.0
        self._mod_count      = 0          # modificaciones consecutivas en NAMED
        self._monitor_gen    = 0          # invalida monitores viejos
        self._waiting_timer: threading.Timer = None

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _same(a: str, b: str) -> bool:
        if not a or not b:
            return False
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def _display(self, state_key: str):
        return self.config["states"].get(state_key, (state_key, "gray"))

    def _set_state(self, new_state: str, path: str = ""):
        if new_state == self.current_state:
            return
        self.current_state = new_state
        self.active_file   = path or self.active_file
        display, color     = self._display(new_state)

        print(f"\n{'═'*70}\n[{self.device_name}] ▶ {new_state} — {display} ({color})"
              + (f"\n  archivo: {path}" if path else "")
              + f"\n{'═'*70}\n", flush=True)

        if self.on_state_change:
            self.on_state_change(new_state, display, color)

        if new_state == "READY" and self.config.get("export_on_ready") and path:
            (self.on_export or exportar_acq)(path)

    def _reset(self):
        self.active_file    = ""
        self.prev_sizes     = {}
        self._mod_count     = 0
        self._last_mod_time = 0.0

    def _cancel_timer(self, timer_attr: str):
        timer = getattr(self, timer_attr, None)
        if timer is not None:
            timer.cancel()
            setattr(self, timer_attr, None)

    def _stop_monitor(self):
        self._monitor_gen += 1        # cualquier monitor vivo termina solo

    # ── Monitor de inactividad (READY → FINISHED) ────────────────────────────
    def _start_finish_monitor(self):
        self._monitor_gen += 1
        gen = self._monitor_gen

        def _loop():
            while True:
                time.sleep(0.1)
                with self._lock:
                    if gen != self._monitor_gen or self.current_state != "READY":
                        return
                    idle = time.time() - self._last_mod_time
                    if idle >= self.finish_timeout:
                        print(f"[{self.device_name}] ⏹ Sin modificaciones durante "
                              f"{idle:.2f}s → Fin de Grabado", flush=True)
                        self._set_state("FINISHED")
                        return

        threading.Thread(target=_loop, daemon=True,
                         name=f"{self.device_name}-finish-monitor").start()

    # ── Desconexión / reset ──────────────────────────────────────────────────
    def handle_disconnect(self):
        with self._lock:
            self._stop_monitor()
            self._cancel_timer('_waiting_timer')
            self._set_state("DISCONNECTED")
            print(f"[{self.device_name}] ⏱ WAITING programado en 3s", flush=True)

            def _go_waiting():
                with self._lock:
                    if self.current_state == "DISCONNECTED":
                        self._reset()
                        self._set_state("WAITING")
                    self._waiting_timer = None

            self._waiting_timer = threading.Timer(3.0, _go_waiting)
            self._waiting_timer.daemon = True
            self._waiting_timer.start()

    def reset_to_waiting(self):
        """Reset manual (botón Reset BIOPAC)."""
        with self._lock:
            self._stop_monitor()
            self._cancel_timer('_waiting_timer')
            self._reset()
            self._set_state("WAITING")

    # ── Eventos de archivo ───────────────────────────────────────────────────
    def handle_biopac_event(self, event_type: str, path: str):
        filename   = os.path.basename(path)
        ext        = os.path.splitext(path)[1].lower()
        trigger    = self.config.get("trigger_file")
        record_ext = self.config.get("record_extension", "")

        with self._lock:
            # ── CREADO ───────────────────────────────────────────────────────
            if event_type == "created":
                if trigger and filename == trigger:
                    self._set_state("CONNECTED", path)

                elif ext == record_ext and self.current_state in (
                        "CONNECTED", "WAITING", "NAMED"):
                    self._mod_count     = 0
                    self._last_mod_time = 0.0
                    self.active_file    = path
                    self._set_state("NAMED", path)

            # ── MODIFICADO ───────────────────────────────────────────────────
            elif event_type == "modified":
                if ext != record_ext or not self._same(path, self.active_file):
                    return
                try:
                    if os.path.getsize(path) <= 0:
                        return
                except OSError:
                    return

                now = time.time()

                if self.current_state == "READY":
                    self._last_mod_time = now          # sigue grabando
                    return

                if self.current_state == "NAMED":
                    # Contar modificaciones seguidas (separadas < timeout)
                    if now - self._last_mod_time <= self.finish_timeout:
                        self._mod_count += 1
                    else:
                        self._mod_count = 1
                    self._last_mod_time = now

                    if self._mod_count >= self.min_mods_to_ready:
                        self._set_state("READY", path)
                        self._start_finish_monitor()
                    return

                if self.current_state == "FINISHED":
                    # BSL reanudó la grabación del mismo archivo
                    self._last_mod_time = now
                    self._set_state("READY", path)
                    self._start_finish_monitor()
                    return

            # ── ELIMINADO ────────────────────────────────────────────────────
            elif event_type == "deleted":
                if trigger and filename == trigger:
                    if self.current_state in ("READY", "FINISHED"):
                        print(f"[{self.device_name}] 🔴 _.tmp eliminado en "
                              f"{self.current_state} — BSL cerró.", flush=True)
                        self.handle_disconnect()
                    else:
                        print(f"[{self.device_name}] ℹ️ _.tmp eliminado durante "
                              f"{self.current_state} — normal en BSL, ignorando.",
                              flush=True)

                elif ext == record_ext and self._same(path, self.active_file):
                    print(f"[{self.device_name}] ⚠️ .acq eliminado temporalmente, "
                          f"esperando recreación...", flush=True)
                    self._stop_monitor()
                    self._mod_count = 0
                    if self.current_state in ("NAMED", "READY", "FINISHED"):
                        self.current_state = "CONNECTED"
                        print(f"[{self.device_name}] ↩ Estado interno → CONNECTED",
                              flush=True)
                        
class UnifiedDeviceHandler(FileSystemEventHandler):
    """
    Handler watchdog genérico que delega eventos a DeviceStateMachine.
    """

    def __init__(self, device_name: str, state_machine: DeviceStateMachine):
        super().__init__()
        self.device_name   = device_name
        self.state_machine = state_machine
        self._dispatch     = state_machine.handle_biopac_event

    # ── Utilidades estáticas ──────────────────────────────────────────────────

    @staticmethod
    def get_file_info(path: str) -> str:
        try:
            stat     = os.stat(path)
            size     = stat.st_size
            mtime    = datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S.%f")[:-3]
            md5      = ""
            if 0 < size < 10 * 1024 * 1024:
                try:
                    with open(path, "rb") as f:
                        md5 = hashlib.md5(f.read()).hexdigest()[:8]
                except:
                    md5 = "??"
            size_str = (
                f"{size} B"            if size < 1024    else
                f"{size/1024:.1f} KB"  if size < 1024**2 else
                f"{size/1024**2:.2f} MB"
            )
            return f"tamaño={size_str} | modificado={mtime} | md5={md5}"
        except FileNotFoundError:
            return "⚠️ archivo ya no existe"
        except Exception as e:
            return f"⚠️ error: {e}"

    @staticmethod
    def get_dir_info(path: str) -> str:
        try:
            entries    = os.listdir(path)
            files      = [e for e in entries if os.path.isfile(os.path.join(path, e))]
            dirs       = [e for e in entries if os.path.isdir(os.path.join(path, e))]
            total      = sum(os.path.getsize(os.path.join(path, f)) for f in files)
            size_str   = (
                f"{total} B"           if total < 1024    else
                f"{total/1024:.1f} KB" if total < 1024**2 else
                f"{total/1024**2:.2f} MB"
            )
            return f"archivos={len(files)} | subcarpetas={len(dirs)} | total={size_str} | contenido={entries}"
        except Exception as e:
            return f"⚠️ error: {e}"

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, event_type: str, path: str, is_dir: bool):
        icon = "📁" if is_dir else "📄"
        info = (
            self.get_dir_info(path)
            if is_dir
            else self.get_file_info(path)
        )
        print(
            f"[{self.device_name}] [{event_type}] {icon} {os.path.basename(path)}\n"
            f"    └─ {info}\n",
            flush=True
        )

    # ── Eventos watchdog ──────────────────────────────────────────────────────

    def on_created(self, event):
        self._log("CREADO", event.src_path, event.is_directory)
        if not event.is_directory:
            self._dispatch("created", event.src_path)

    def on_deleted(self, event):
        self._log("ELIMINADO", event.src_path, event.is_directory)
        if not event.is_directory:
            self._dispatch("deleted", event.src_path)

    def on_modified(self, event):
        # Sin _log: durante la grabación BSL modifica el .acq muchas veces por
        # segundo y calcular MD5 en cada evento satura consola y disco.
        if not event.is_directory:
            self._dispatch("modified", event.src_path)

    def on_moved(self, event):
        tipo = "📁" if event.is_directory else "📄"
        print(
            f"[{self.device_name}] [MOVIDO] {tipo} "
            f"{os.path.basename(event.src_path)} → {os.path.basename(event.dest_path)}\n",
            flush=True
        )

def exportar_acq(acq_path: str):
    """Lee un .acq de BIOPAC y lo exporta como CSV en la misma carpeta."""
    print(f"[BIOPAC] 📦 Exportando: {acq_path}", flush=True)
    try:
        data        = bioread.read_file(acq_path)
        inicio_unix = data.earliest_marker_created_at.timestamp()
        fs          = data.channels[0].samples_per_second
        n_muestras  = len(data.channels[0].data)
        unix_ts     = inicio_unix + np.arange(n_muestras) / fs
        timestamps  = [
            datetime.fromtimestamp(u).strftime('%Y-%m-%d %H:%M:%S.%f')[:-2]
            for u in unix_ts
        ]
        df = pd.DataFrame({
            "timestamp":      timestamps,
            "unix_timestamp": np.round(unix_ts, 4),
        })
        for ch in data.channels:
            df[f"{ch.name} ({ch.units})"] = ch.data

        csv_path = os.path.splitext(acq_path)[0] + ".csv"
        df.to_csv(csv_path, index=False)
        print(
            f"[BIOPAC] ✅ CSV guardado: {csv_path} | "
            f"{len(df)} muestras | canales: {[ch.name for ch in data.channels]}",
            flush=True
        )
    except Exception as e:
        print(f"[BIOPAC] ❌ Error exportando: {e}", flush=True)


class CombinedAcquisitionSystem:
    def __init__(self):
        self.window = tk.Tk()
        self.window.title("Sistema de Adquisición EEG-ECG con Multi-Cámara")
        self.window.configure(bg='#f0f8ff')
        
        # Configurar estilos
        self.setup_styles()
        
        # Queue para comunicación entre hilos
        self.gui_queue = Queue()
        
        # Variables de control
        self.is_recording = False
        self.start_time = None
        self.temp_name  = tk.StringVar()
        self.validated_name = tk.StringVar()  # Para el nombre validado
        self.selected_port = tk.StringVar()
        self.selected_camera = tk.StringVar()  # Para la cámara seleccionada
        self.test_mode = tk.StringVar(value="pista")  # "pista" o "ciudad"
        self.TEMP_STALE_S = 8.0   # ≈ 3-4 × intervalo del ESP32 (usa 30.0 si envías cada 10 s)
        
        # Variables para detección facial
        self.face_detection_var = tk.BooleanVar(value=False)
        self.landmarks_var = tk.BooleanVar(value=False)
        self.hands_var = tk.BooleanVar(value=False)
        self.metrics_var = tk.BooleanVar(value=False)
        self.alerts_var = tk.BooleanVar(value=False)

        # NUEVO: Variables para detección de líneas
        self.lane_detection_var = tk.BooleanVar(value=False)
        self.lane_metrics_var = tk.BooleanVar(value=True)
        self.lane_predictions_var = tk.BooleanVar(value=True)


        # Variables de control para LiDAR
        self.lidar_enabled = tk.BooleanVar(value=False)

        # NUEVO: Variable para control de visualización POW
        self.pow_visualization_var = tk.BooleanVar(value=False)

        # Inicializar el diccionario de indicadores de estado
        self.status_indicators = {}
        # Variables de estado
        self.auth_token = None
        self.headset_id = None
        self.session_id = None
        self.eeg_csv_files = {}
        self.events_file = None
       
        # Control de hilos
        self.stop_event = threading.Event()
        self.ws = None
        self.serial_port = None
        self.serial_stop_event = threading.Event()   # detiene solo el monitor del ESP32 control
        self.serial_thread     = None
                # ── Sensor de temperatura (segundo ESP32) ──
        self.selected_temp_port = tk.StringVar()
        self.temp_port_mapping  = {}
        self.temp_serial        = None
        self.temp_thread        = None
        self.temp_stop_event    = threading.Event()
        self._temp_lock         = threading.Lock()
        self.last_temp_value    = None
        self.last_temp_time     = 0.0
        self.temp_csv_file      = None

        # Configuración del observer ECG
        self.ecg_handler = ECGEventHandler(self)
        self.ecg_observer = Observer()
        self.ecg_observer.schedule(self.ecg_handler, ECG_PATH, recursive=True)
        self.ecg_observer.start()
        
        # ── BIOPAC observer (nuevo) ───────────────────────────────────────────────────
        biopac_path = DEVICES_CONFIG["BIOPAC"]["watch_path"]
        if os.path.exists(biopac_path):
            self.biopac_sm = DeviceStateMachine(
                device_name="BIOPAC",
                config=DEVICES_CONFIG["BIOPAC"],
                on_state_change=self._on_biopac_state_change,
                on_export=self._on_biopac_export,
            )
            self.biopac_handler  = UnifiedDeviceHandler("BIOPAC", self.biopac_sm)
            self.biopac_observer = Observer()
            self.biopac_observer.schedule(         # ← orden correcto de argumentos
                self.biopac_handler,               # 1. handler
                path=biopac_path,                  # 2. path
                recursive=True                     # 3. recursive
            )
            self.biopac_observer.start()
            print(f"✅ BIOPAC observer activo: {biopac_path}")
        else:
            print(f"⚠️  BIOPAC: ruta no encontrada ({biopac_path}), observer omitido.")
            self.biopac_sm       = None
            self.biopac_observer = None

        # Inicializar el gestor de múltiples cámaras
        self.multi_camera_manager = MultiCameraManagerMP(self)
        self.add_screenshot_handler()


        # Inicializar el gestor del Velodyne HDL-64E
        self.velodyne_manager = VelodyneHDL64E(puerto=2368, parent=self)
        self.lidar_visualizer = LiDARVisualizer(self)

        # Variables para el control de conexión EEG
        self.reconnect_attempts = 0
        self.connection_in_progress = False
        self.headset_detected = False  # Variable para rastrear si se detectó físicamente un dispositivo
        self.last_device_check = 0     # Tiempo del último chequeo de dispositivo
        # Contador de escrituras por tipo de señal EEG
        self._eeg_write_count: dict = {}
                # Control de escritura EEG (thread-safe, sesión persistente)
        self._eeg_lock          = threading.Lock()
        self._eeg_file_handles  = {}
        self._eeg_csv_writers   = {}
        self._eeg_last_flush    = 0.0
        self._eeg_last_fsync    = 0.0
        self._closing_session   = False
        self._stopping          = False

        # NUEVO: Variables para métricas de video
        self.video_metrics_files = {}

        MetricsBufferManager = CircularMetricsBuffer
        # Gestor de buffers circular con respaldo
        self.metrics_buffer = CircularMetricsBuffer(
            batch_size=600,        # ~30s a 20Hz (2 métricas en paralelo)
            flush_interval=30.0,
            backup_interval=10.0,
            max_memory_rows=10000
        )

        # Inicializar GUI
        self.setup_gui()
        
        # Centrar ventana
        self.center_window()
        
        # Iniciar hilos de manera controlada
        self.window.after(200, self.connect_eeg)
        
        # Procesar eventos de la queue
        self.process_gui_queue()
        self.window.after(5000, self.check_eeg_connection)
       
        # Inicializar el visualizador de potencia EEG
        self.pow_visualizer = EEGPowerVisualizer(self)


        self.ecg_handler.start_inactivity_monitoring()
        self.window.after(1000, self._check_temp_stale)

    # ── Callbacks BIOPAC ──────────────────────────────────────────────────────────
    def wait_for_acq(self) -> str | None:
        """
        Busca recursivamente el .acq más reciente en la carpeta BIOPAC.
        Espera que el archivo esté completamente escrito antes de retornarlo.
        """
        biopac_path = DEVICES_CONFIG["BIOPAC"]["watch_path"]

        while True:
            acq_files = []
            if os.path.exists(biopac_path):
                for root, dirs, files in os.walk(biopac_path):
                    for f in files:
                        if f.lower().endswith(".acq"):
                            full_path = os.path.join(root, f)
                            try:
                                size = os.path.getsize(full_path)
                                if size > 0:
                                    acq_files.append(full_path)
                            except OSError:
                                pass
                acq_files.sort(key=os.path.getmtime, reverse=True)

            if acq_files:
                most_recent = acq_files[0]

                # Esperar escritura completa (tamaño estable 3 veces seguidas)
                print(f"[BIOPAC] Esperando escritura completa de: {most_recent}", flush=True)
                prev_size    = -1
                stable_count = 0
                while stable_count < 3:
                    try:
                        curr_size = os.path.getsize(most_recent)
                    except OSError:
                        curr_size = 0
                    if curr_size == prev_size and curr_size > 0:
                        stable_count += 1
                    else:
                        stable_count = 0
                    prev_size = curr_size
                    time.sleep(0.5)

                rel_path = os.path.relpath(most_recent, biopac_path)
                size_kb   = os.path.getsize(most_recent) / 1024

                confirm = messagebox.askyesno(
                    "Archivo BIOPAC encontrado",
                    f"Se encontró el siguiente archivo .acq:\n\n"
                    f"📄 {rel_path}\n"
                    f"📦 Tamaño: {size_kb:.1f} KB\n\n"
                    f"¿Es este el archivo de la sesión actual?"
                )
                if confirm:
                    return most_recent

                if len(acq_files) > 1:
                    file_list = "\n".join(
                        [f"• {os.path.relpath(f, biopac_path)} "
                         f"({os.path.getsize(f)/1024:.1f} KB)"
                         for f in acq_files[:5]]
                    )
                    messagebox.showinfo(
                        "Otros archivos disponibles",
                        f"Archivos .acq encontrados:\n\n{file_list}\n\n"
                        f"Asegúrese de confirmar el archivo correcto."
                    )

            retry = messagebox.askretrycancel(
                "Archivo .acq no confirmado",
                f"No se encontró o no se confirmó ningún archivo .acq en:\n"
                f"{biopac_path}\n\n"
                f"Por favor:\n"
                f"1. Guarde la sesión en el software BSL\n"
                f"2. Presione 'Reintentar' para buscar nuevamente\n"
                f"   o 'Cancelar' para omitir el procesamiento BIOPAC."
            )
            if not retry:
                return None

            time.sleep(2)
    # ═══════════════════════════════════════════════════════════════
    #  RECORTE AUTOMÁTICO DE SESIÓN (adaptado de recorte_csv.py)
    # ═══════════════════════════════════════════════════════════════
    def _read_csv_flexible(self, file_path):
        """Lee un CSV tolerando columnas desalineadas (misma lógica que recorte_csv.py)"""
        import io
        with open(file_path, 'rb') as f:
            content = f.read()
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            text = content.decode('latin-1')

        text = text.replace('\r\n', '\n').replace('\r', '')
        lines = text.strip().split('\n')
        if len(lines) < 2:
            return None

        headers = lines[0].split(',')
        first_data = lines[1].split(',')

        if len(first_data) != len(headers):
            df = pd.read_csv(io.StringIO(text), header=None, skiprows=1)
            new_headers = ['Timestamp', 'unix_timestamp']
            for i in range(2, len(df.columns)):
                new_headers.append(headers[i] if i < len(headers) else f'COL_{i}')
            df.columns = new_headers
        else:
            df = pd.read_csv(io.StringIO(text))
            df.columns = df.columns.str.strip()
            if 'EpochTime' in df.columns:
                df = df.rename(columns={'EpochTime': 'unix_timestamp'})
        return df

    def _trim_and_save_csv(self, df, output_path, start_time, end_time, filename):
        """
        Filtra por rango de tiempo con prioridad:
        INICIO -> exacto, si no el más cercano POR ARRIBA
        FIN    -> exacto, si no el más cercano POR ABAJO
        (idéntico criterio a recorte_csv.py)
        """
        df = df.copy()
        df['unix_timestamp'] = pd.to_numeric(df['unix_timestamp'], errors='coerce')
        df = df.dropna(subset=['unix_timestamp'])

        start_time = float(start_time)
        end_time   = float(end_time)

        exact_start = df[df['unix_timestamp'] == start_time]
        if not exact_start.empty:
            actual_start = start_time
        else:
            after_start = df[df['unix_timestamp'] >= start_time]
            actual_start = float(after_start['unix_timestamp'].min()) if not after_start.empty else start_time

        exact_end = df[df['unix_timestamp'] == end_time]
        if not exact_end.empty:
            actual_end = end_time
        else:
            before_end = df[df['unix_timestamp'] <= end_time]
            actual_end = float(before_end['unix_timestamp'].max()) if not before_end.empty else end_time

        mask = (df['unix_timestamp'] >= actual_start) & (df['unix_timestamp'] <= actual_end)
        filtered_df = df[mask]

        if not filtered_df.empty:
            filtered_df.to_csv(str(output_path), index=False)
            print(f"      ✅ {filename} [FILTRADO] {len(df)}→{len(filtered_df)} filas "
                  f"({actual_start:.4f} → {actual_end:.4f})")
            return True

        print(f"      ⚠️ {filename}: sin datos en rango [{start_time:.4f} → {end_time:.4f}]")
        return False



    def send_serial_command(self, cmd: str) -> bool:
        """Envía un comando al MASTER_ESP32. False si no hay conexión."""
        try:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.write((cmd.strip() + "\n").encode())
                self.serial_port.flush()
                return True
        except Exception as e:
            print(f"Error enviando '{cmd}' al ESP32: {e}")
        return False

    def request_start(self):
        """Botón Iniciar: valida y pide al ESP32 el INICIO (TTL pin 10 -> LOW)."""
        if self.is_recording or self._stopping:
            return
        if not self.validated_name.get().strip():
            messagebox.showerror("Error", "Por favor ingrese el nombre del participante")
            return
        if not (self.serial_port and self.serial_port.is_open):
            messagebox.showerror("ESP32 Control",
                                 "Conecte el MASTER_ESP32: es quien genera el TTL para el VBOX.")
            return
        if not self.send_serial_command("INICIO"):
            return

        self.start_button.config(state='disabled')
        # Si el ESP32 no contesta, reactivar el botón
        def _check():
            if not self.is_recording and not self._stopping:
                self.start_button.config(state='normal')
                print("⚠️ El ESP32 no confirmó INICIO")
        self.window.after(3000, _check)

    def request_stop(self):
        """Botón Detener: pide al ESP32 el FIN (TTL pin 10 -> HIGH)."""
        if not self.is_recording:
            return
        self.stop_button.config(state='disabled')
        if not self.send_serial_command("FIN"):
            self.stop_acquisition()          # sin ESP32: cerrar igual
            return
        # Respaldo: si no llega la respuesta FIN en 2 s, cerrar igualmente
        self.window.after(2000, lambda: self.stop_acquisition() if self.is_recording else None)




    def apply_session_trim(self, session_folder=None, start_time=None, end_time=None,
                           exclude_folders=None):
        """
        Recorta TODOS los CSV de la sesión al rango INICIO→FIN.
        1. COPIA el original a '<subcarpeta>/originales/' (siempre se conserva).
        2. Escribe la versión recortada en un .tmp y reemplaza el archivo.
        Si el recorte falla, el archivo queda intacto y el original ya está respaldado.
        """
        from pathlib import Path

        session_folder = Path(session_folder or self.session_folder)
        start_time = float(start_time if start_time is not None else self.start_epoch_time)
        end_time   = float(end_time   if end_time   is not None else self.end_epoch_time)
        exclude_folders = exclude_folders or {'originales', 'Screenshots'}

        print("\n" + "=" * 60)
        print("✂️  Aplicando recorte automático de sesión (INICIO → FIN)")
        print("=" * 60)
        print(f"   INICIO: {start_time:.6f} | FIN: {end_time:.6f} | Δ={end_time - start_time:.2f}s")

        total_ok, total_fail = 0, 0

        for subfolder in sorted(session_folder.iterdir()):
            if not subfolder.is_dir() or subfolder.name in exclude_folders:
                continue

            csv_files = list(subfolder.glob('*.csv'))
            if not csv_files:
                continue

            originales_folder = subfolder / 'originales'
            originales_folder.mkdir(parents=True, exist_ok=True)
            print(f"\n📂 {subfolder.name}/  ({len(csv_files)} archivo(s))")

            for csv_file in csv_files:
                filename      = csv_file.name
                original_dest = originales_folder / filename
                tmp_path      = subfolder / (filename + '.tmp')

                # ── 1. Respaldar original (con reintentos por si está bloqueado) ──
                copiado = False
                for intento in range(5):
                    try:
                        shutil.copy2(csv_file, original_dest)   # sobrescribe backup previo
                        copiado = True
                        break
                    except PermissionError:
                        time.sleep(0.5)
                    except Exception as e:
                        print(f"      ❌ {filename}: no se pudo respaldar - {e}")
                        break
                if not copiado:
                    print(f"      ❌ {filename}: archivo bloqueado, se omite el recorte")
                    total_fail += 1
                    continue

                # ── 2. Leer y recortar ──
                try:
                    df = self._read_csv_flexible(csv_file)
                    if df is None:
                        print(f"      ⚠️ {filename}: solo header / vacío — original respaldado, sin recorte")
                        total_fail += 1
                        continue
                    if 'unix_timestamp' not in df.columns:
                        print(f"      ⚠️ {filename}: sin columna unix_timestamp — original respaldado, sin recorte")
                        total_fail += 1
                        continue

                    if self._trim_and_save_csv(df, tmp_path, start_time, end_time, filename):
                        os.replace(tmp_path, csv_file)       # reemplazo atómico
                        total_ok += 1
                    else:
                        total_fail += 1                      # archivo intacto + respaldo

                except Exception as e:
                    print(f"      ❌ {filename}: error al recortar - {e}")
                    total_fail += 1
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass

        print(f"\n✅ Recorte finalizado: {total_ok} archivo(s) recortados, {total_fail} con problemas.")
        return total_ok, total_fail
    
    def apply_pcap_trim(self, session_folder=None, start_time=None, end_time=None):
        """
        Recorta automáticamente el .pcap del Velodyne al rango INICIO→FIN.

        Nota: PcapWriter ya filtra en vivo por t_ini/t_fin (ver
        VelodyneHDL64E.iniciar_grabacion / detener_grabacion), así que este
        paso es una segunda pasada de seguridad/consistencia -- garantiza un
        recorte estricto por epoch real y deja el mismo rastro 'originales/'
        que apply_session_trim() usa para los CSV.
        """
        from pathlib import Path

        session_folder = Path(session_folder or self.session_folder)
        start_time = float(start_time if start_time is not None else self.start_epoch_time)
        end_time   = float(end_time   if end_time   is not None else self.end_epoch_time)

        print("\n" + "=" * 60)
        print("✂️  Aplicando recorte automático de PCAP LiDAR (INICIO → FIN)")
        print("=" * 60)
        print(f"   INICIO: {start_time:.6f} | FIN: {end_time:.6f} | Δ={end_time - start_time:.2f}s")

        # ── Ruta real expuesta por VelodyneHDL64E ──
        pcap_files = []
        if hasattr(self, 'velodyne_manager'):
            pcap_path = getattr(self.velodyne_manager, '_pcap_ruta', None)
            if pcap_path and os.path.exists(pcap_path):
                pcap_files.append(Path(pcap_path))

        # ── Fallback: buscar en session_folder/LIDAR si no se encontró ──
        if not pcap_files:
            lidar_folder = session_folder / "LIDAR"
            if lidar_folder.exists():
                pcap_files = [p for p in lidar_folder.glob('*.pcap')]
            else:
                pcap_files = [p for p in session_folder.rglob('*.pcap')
                              if 'originales' not in p.parts]

        if not pcap_files:
            print("   ⚠️ No se encontró ningún archivo .pcap para recortar. Se omite.")
            return 0, 0

        total_ok, total_fail = 0, 0

        for pcap_file in pcap_files:
            filename = pcap_file.name
            originales_folder = pcap_file.parent / 'originales'
            originales_folder.mkdir(parents=True, exist_ok=True)
            original_dest = originales_folder / filename

            try:
                size_kb = pcap_file.stat().st_size / 1024
                if size_kb < 1:
                    print(f"   ⚠️ {filename}: archivo vacío o casi vacío ({size_kb:.1f} KB) — se omite")
                    total_fail += 1
                    continue

                if original_dest.exists():
                    original_dest.unlink()          # reemplaza backup previo
                pcap_file.rename(original_dest)      # mover original

                stats = _pcap_cut(str(original_dest), str(pcap_file), start_time, end_time)

                if stats['copied_packets'] > 0:
                    print(f"   ✅ {filename}: {stats['total_packets']:,} → "
                          f"{stats['copied_packets']:,} paquetes "
                          f"({stats['size_in_mb']:.1f} MB → {stats['size_out_mb']:.1f} MB)")
                    total_ok += 1
                else:
                    print(f"   ⚠️ {filename}: sin paquetes en rango — se restaura el original")
                    if pcap_file.exists():
                        pcap_file.unlink()
                    original_dest.rename(pcap_file)
                    total_fail += 1

            except Exception as e:
                print(f"   ❌ {filename}: error al recortar - {e}")
                if not pcap_file.exists() and original_dest.exists():
                    original_dest.rename(pcap_file)
                total_fail += 1

        print(f"\n✅ Recorte PCAP finalizado: {total_ok} archivo(s) recortados, {total_fail} con problemas.")
        return total_ok, total_fail
    
    def process_biopac_data(self, acq_path: str) -> bool:
        try:
            if not hasattr(self, 'ecg_folder') or not os.path.exists(self.ecg_folder):
                raise ValueError("Subcarpeta ECG de sesión no encontrada.")

            # Esperar que el archivo esté completamente escrito
            print(f"[BIOPAC] Verificando integridad de: {acq_path}", flush=True)
            prev_size    = -1
            stable_count = 0
            while stable_count < 3:
                try:
                    curr_size = os.path.getsize(acq_path)
                except OSError:
                    curr_size = 0
                if curr_size == prev_size and curr_size > 0:
                    stable_count += 1
                else:
                    stable_count = 0
                prev_size = curr_size
                time.sleep(0.5)

            print(f"[BIOPAC] Archivo estable ({prev_size/1024:.1f} KB), procediendo...", flush=True)

            import shutil
            modo = self.test_mode.get()
            name = self.validated_name.get().strip()
            acq_ext = os.path.splitext(acq_path)[1]  # ".acq"
            dest_acq = os.path.join(self.ecg_folder, f"{name}_{modo}_biopac{acq_ext}")
            shutil.copy2(acq_path, dest_acq)
            print(f"[BIOPAC] .acq copiado a sesión: {dest_acq}", flush=True)

            exportar_acq(dest_acq)

            csv_path = os.path.splitext(dest_acq)[0] + ".csv"
            if os.path.exists(csv_path):
                print(f"[BIOPAC] CSV exportado: {csv_path} "
                      f"(se recortará automáticamente al finalizar la sesión)", flush=True)
            else:
                print(f"[BIOPAC] ⚠️ CSV no encontrado tras exportar: {csv_path}", flush=True)

            self.biopac_acq_file = dest_acq
            return True

        except Exception as e:
            print(f"[BIOPAC] Error procesando .acq: {e}", flush=True)
            messagebox.showerror("Error BIOPAC", f"Error al procesar el archivo .acq:\n{e}")
            return False
     
    def _on_biopac_state_change(self, state_key: str, display: str, color: str):
        """Recibe cambios de estado del BIOPAC y los reenvía a la GUI."""
        self.gui_queue.put({
            'type': 'biopac_status',
            'data': (display, color)
        })

    def _on_biopac_export(self, acq_path: str):
        """
        Callback al llegar a READY: copia el .acq a la carpeta de sesión
        y lo exporta como CSV. Si no hay sesión activa, exporta en origen.
        """
        if self.is_recording and hasattr(self, 'ecg_folder') and os.path.exists(self.ecg_folder):
            try:
                import shutil
                modo = self.test_mode.get()
                name = self.validated_name.get().strip()
                acq_ext = os.path.splitext(acq_path)[1]
                dest = os.path.join(self.ecg_folder, f"{name}_{modo}_biopac{acq_ext}")
                shutil.copy2(acq_path, dest)
                print(f"[BIOPAC] .acq copiado a sesión: {dest}", flush=True)
                exportar_acq(dest)
            except Exception as e:
                print(f"[BIOPAC] Error copiando/exportando: {e}", flush=True)
                exportar_acq(acq_path)      # fallback: exportar en origen
        else:
            exportar_acq(acq_path)

    def force_biopac_disconnect(self):
        """Fuerza el reset del estado BIOPAC a WAITING."""
        try:
            if messagebox.askyesno(
                "Confirmar Reset BIOPAC",
                "¿Desea resetear el estado del BIOPAC?\n"
                "El sistema volverá al estado de espera."
            ):
                if getattr(self, 'biopac_sm', None):
                    self.biopac_sm.reset_to_waiting()
                    self.gui_queue.put({
                        'type': 'system_status',
                        'data': 'BIOPAC reseteado manualmente'
                    })
        except Exception as e:
            messagebox.showerror("Error", f"Error al resetear BIOPAC: {e}")


    def add_screenshot_handler(self):
        """
        Inicializa y agrega el gestor de capturas de pantalla al sistema principal
        """
        try:
            self.screenshot_handler = ScreenshotHandler(self)
            print("Gestor de capturas de pantalla inicializado")
        except Exception as e:
            print(f"Error al inicializar gestor de capturas de pantalla: {e}")
            
            
    def get_synchronized_timestamp(self, cortex_time=None):
        """
        Genera timestamps sincronizados para eventos y datos
        
        Args:
            cortex_time (float, optional): Tiempo proporcionado por el sistema Cortex
            
        Returns:
            tuple: (formatted_timestamp, epoch_timestamp)
                formatted_timestamp: str - Timestamp en formato ISO con 4 decimales
                epoch_timestamp: str - Timestamp epoch con 4 decimales
        """
        if cortex_time:
            # Si tenemos tiempo de Cortex, lo usamos como base
            epoch_time = float(cortex_time)
            dt = datetime.fromtimestamp(epoch_time)
        else:
            # Si no, usamos el tiempo actual
            current = time.time()
            epoch_time = float(current)
            dt = datetime.fromtimestamp(epoch_time)
        
        # Formatear con exactamente 4 decimales
        formatted_timestamp = dt.strftime('%Y-%m-%d %H:%M:%S.%f')# Cortamos a 4 decimales
        epoch_timestamp = f"{epoch_time:.6f}"
        
        return formatted_timestamp, epoch_timestamp

    def mark_event(self, event_type):
        """Marca un evento en ambos archivos de eventos usando el timestamp sincronizado"""
        if self.is_recording:
            formatted_time, epoch_time = self.get_synchronized_timestamp()
            event_line = f"{formatted_time},{epoch_time},{event_type}\n"
            
            # Escribir en el archivo de la sesión
            if hasattr(self, 'session_events_file'):
                try:
                    with open(self.session_events_file, 'a') as f:
                        f.write(event_line)
                except Exception as e:
                    print(f"Error al escribir en archivo de eventos de sesión: {str(e)}")
            
            # Escribir en el archivo del ECG
            if hasattr(self, 'ecg_events_file'):
                try:
                    with open(self.ecg_events_file, 'a') as f:
                        f.write(event_line)
                except Exception as e:
                    print(f"Error al escribir en archivo de eventos de ECG: {str(e)}")

    def center_window(self):
        """Centra la ventana con tamaño maximizado"""
        # Obtener dimensiones de la pantalla
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        
        # Opción 1: Maximizar completamente (recomendado)
        self.window.state('zoomed')  # En Windows maximiza la ventana
        
        # Opción 2: Usar dimensiones de pantalla completa manualmente
        # self.window.geometry(f"{screen_width}x{screen_height}+0+0")
        
        # Permitir redimensionar
        self.window.resizable(True, True)

    def setup_styles(self):
        """Configura los estilos de la interfaz con colores vino tornasol elegantes"""
        self.style = ttk.Style()
        
        # Paleta vino tornasol moderna y elegante
        color_fondo_principal = "#F5F0FF"  # Lavanda muy suave
        color_fondo_cards = "#FFFFFF"  # Blanco puro para tarjetas
        color_primario = "#6B1B6B"  # Vino medio
        color_secundario = "#8B3A8B"  # Tornasol
        color_acento = "#FF69B4"  # Rosa brillante
        color_texto_oscuro = "#2D0A3D"  # Vino muy oscuro para texto
        color_texto_claro = "#5A1A5A"  # Vino para subtextos
        color_exito = "#4CAF50"  # Verde para acciones positivas
        color_peligro = "#E91E63"  # Rosa-rojo para acciones de alerta
        
        # Configuración general
        self.style.configure('Main.TFrame', background=color_fondo_principal)
        
        # Etiquetas
        self.style.configure('TLabel', 
                            font=('Segoe UI', 9),
                            background=color_fondo_principal,
                            foreground=color_texto_oscuro)
        self.style.configure('Header.TLabel', 
                            font=('Segoe UI', 11, 'bold'),
                            foreground=color_primario,
                            background=color_fondo_principal)
        
        # Botones
        self.style.configure('Green.TButton',
                            font=('Segoe UI', 9, 'bold'),
                            padding=5)
        self.style.map('Green.TButton',
                    background=[('active', '#66BB6A'), ('pressed', '#43A047')],
                    foreground=[('active', 'purple'), ('pressed', 'purple')])
                    
        self.style.configure('Red.TButton',
                            font=('Segoe UI', 9, 'bold'),
                            padding=5)
        self.style.map('Red.TButton',
                    background=[('active', '#F06292'), ('pressed', '#D81B60')],
                    foreground=[('active', 'purple'), ('pressed', 'purple')])
        
        # Frames y LabelFrames
        self.style.configure('Card.TFrame',
                            background=color_fondo_cards,
                            relief='solid',
                            borderwidth=1)
        self.style.configure('Card.TLabelframe',
                            background=color_fondo_cards,
                            relief='groove',
                            borderwidth=2)
        self.style.configure('Card.TLabelframe.Label',
                            font=('Segoe UI', 10, 'bold'),
                            foreground=color_primario,
                            background=color_fondo_cards)
        
        # Entradas y combos
        self.style.configure('TEntry',
                            fieldbackground='white',
                            foreground=color_texto_oscuro,
                            bordercolor=color_secundario,
                            insertcolor=color_primario)
        self.style.configure('TCombobox',
                            fieldbackground='white',
                            foreground=color_texto_oscuro,
                            bordercolor=color_secundario,
                            arrowcolor=color_primario)
        self.style.map('TCombobox',
                    fieldbackground=[('readonly', 'white')],
                    foreground=[('readonly', color_texto_oscuro)])
        
    def setup_gui(self):
        """Configura la interfaz gráfica principal con distribución optimizada"""
        # Configurar dimensiones fijas de la ventana
        self.window.geometry("1280x720")
        self.window.resizable(True, True)  # Permitir redimensionamiento
        
        # Frame principal con padding
        main_frame = ttk.Frame(self.window, style='Main.TFrame', padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Crear dos frames principales: izquierdo (control) y derecho (visualización)
        left_frame = ttk.Frame(main_frame, style='Main.TFrame')
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        
        right_frame = ttk.Frame(main_frame, style='Main.TFrame')
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Configurar tamaño fijo para el panel izquierdo
        left_frame.configure(width=320)
        left_frame.pack_propagate(False)

        self.setup_control_section(left_frame)        
        # Configurar secciones del frame izquierdo usando pack()
        self.setup_com_port_section(left_frame)
        self.setup_participant_section(left_frame)
        self.setup_test_mode_section(left_frame)
        self.setup_status_section(left_frame)

        
        # Crear frame para las secciones de información y cámara
        right_top_frame = ttk.Frame(right_frame, style='Main.TFrame')
        right_top_frame.pack(fill=tk.X, pady=(0, 5))
        
        # Configurar sección de información temporal (reducida en altura)
        self.setup_time_section(right_top_frame)
        
        # Configurar sección de la cámara (ocupando el resto del espacio)
        self.setup_camera_section(right_frame)

    def setup_test_mode_section(self, parent):
        """Configura la sección de selección de modo de prueba (Pista / Ciudad)"""
        mode_section = ttk.LabelFrame(parent,
                                    text="Modo de Prueba",
                                    style='Card.TLabelframe',
                                    padding="10")
        mode_section.pack(fill=tk.X, pady=(0, 10))

        mode_frame = ttk.Frame(mode_section)
        mode_frame.pack(fill=tk.X, pady=5)

        self.radio_pista = ttk.Radiobutton(
            mode_frame,
            text="🏁 Pista",
            variable=self.test_mode,
            value="pista"
        )
        self.radio_pista.pack(side=tk.LEFT, padx=(5, 20))

        self.radio_ciudad = ttk.Radiobutton(
            mode_frame,
            text="🏙️ Ciudad",
            variable=self.test_mode,
            value="ciudad"
        )
        self.radio_ciudad.pack(side=tk.LEFT, padx=(0, 5))

        # Label que muestra la selección actual
        self.mode_display_label = ttk.Label(
            mode_section,
            text="Seleccionado: PISTA",
            style='Header.TLabel'
        )
        self.mode_display_label.pack(pady=(5, 0))

        # Actualizar label al cambiar selección
        def on_mode_change(*args):
            modo = self.test_mode.get().upper()
            self.mode_display_label.config(text=f"Seleccionado: {modo}")
        
        self.test_mode.trace_add('write', on_mode_change)

    def get_available_ports(self):
        """Obtiene la lista de puertos COM disponibles con sus descripciones"""
        ports = []
        for port in serial.tools.list_ports.comports():
            # Crear una descripción más informativa sin repetir COM
            if port.description and port.description != port.device:
                # Mostrar solo la descripción (que ya incluye el COM al final)
                port_info = port.description
            else:
                port_info = port.device
            
            # Guardar tanto la info para mostrar como el device real
            ports.append({
                'display': port_info,
                'device': port.device
            })
        
        return sorted(ports, key=lambda x: x['device'])
    
    def setup_combobox_styles(self):
        """Configura estilos personalizados para el Combobox"""
        style = ttk.Style()
        
        # Estilo para el Combobox
        style.configure('Bold.TCombobox',
                    font=('Segoe UI', 10, 'bold'),
                    padding=5)
        
    def setup_com_port_section(self, parent):
        """Configura la sección de selección de puerto COM"""
        com_section = ttk.LabelFrame(parent,
                                    text="Configuración Serial - ESP32 CONTROL",
                                    style='Card.TLabelframe',
                                    padding="10")
        com_section.pack(fill=tk.X, pady=(0, 10))
        
        # Frame principal usando pack para consistencia
        com_frame = ttk.Frame(com_section)
        com_frame.pack(fill=tk.X, pady=5)
        
        # Frame para la etiqueta del puerto (en su propia línea)
        label_frame = ttk.Frame(com_frame)
        label_frame.pack(fill=tk.X, pady=(0, 5))
        
        # Label del puerto
        ttk.Label(label_frame,
                text="Puerto COM:",
                style='Header.TLabel').pack(side=tk.LEFT, padx=5)
        
        # Frame para la selección de puerto y botón refrescar (en otra línea)
        port_frame = ttk.Frame(com_frame)
        port_frame.pack(fill=tk.X, pady=(0, 5))
        
        # Dropdown para selección de puerto con estilo en negrita
        self.port_dropdown = ttk.Combobox(port_frame,
                                        textvariable=self.selected_port,
                                        state='readonly',
                                        font=('Segoe UI', 8, 'bold'),  # Fuente en negrita
                                        width=35)
        self.port_dropdown.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Configurar el estilo de la lista desplegable
        self.port_dropdown.option_add('*TCombobox*Listbox.font', ('Segoe UI', 8, 'bold'))
        
        # Botón de actualizar puertos
        refresh_button = ttk.Button(port_frame,
                                text="🔄",
                                command=self.refresh_ports,
                                width=3)
        refresh_button.pack(side=tk.LEFT, padx=2)
        
        # Frame para el botón conectar
        connect_frame = ttk.Frame(com_frame)
        connect_frame.pack(fill=tk.X, pady=(5, 0))
        
        # Botón de conectar
        self.connect_button = ttk.Button(connect_frame,
                                        text="Conectar",
                                        command=self.toggle_serial_connection,
                                        style='Green.TButton')
        self.connect_button.pack(pady=5, fill=tk.X)
        
        # Inicializar lista de puertos
        self.refresh_ports()
        
        # Bind para el evento de selección
        self.port_dropdown.bind('<<ComboboxSelected>>', self.on_port_selected)
        
        # Frame para mensajes de estado
        status_frame = ttk.Frame(com_section)
        status_frame.pack(fill=tk.X, pady=(5,0))
        
        # Label para mensajes de estado
        self.com_status_label = ttk.Label(status_frame,
                                        text="",
                                        style='TLabel')
        self.com_status_label.pack(side=tk.LEFT)

    def refresh_ports(self):
        """Actualiza la lista de puertos disponibles"""
        available_ports = self.get_available_ports()
        
        if available_ports:
            # Crear lista de strings para mostrar
            display_values = [p['display'] for p in available_ports]
            self.port_dropdown['values'] = display_values
            
            # Guardar el mapeo para luego recuperar el device real
            self.port_mapping = {p['display']: p['device'] for p in available_ports}
            
            # Si el puerto actual no está disponible, seleccionar el primero
            current_port = self.selected_port.get()
            if current_port not in display_values:
                self.selected_port.set(display_values[0])
        else:
            self.selected_port.set('')
            self.port_dropdown['values'] = ['No hay puertos disponibles']
            self.port_mapping = {}


    def on_port_selected(self, event):
        """Maneja el evento de selección de puerto"""
        if self.serial_port and self.serial_port.is_open:
            return                                  # conectado: el botón es "Desconectar"
        selected = self.selected_port.get()
        if selected and selected != 'No hay puertos disponibles':
            self.connect_button.config(state='normal')
        else:
            self.connect_button.config(state='disabled')

    def connect_serial(self):
        """Conecta al puerto serial y captura el SYNC inicial durante la verificación."""
        if self.serial_port:
            try:
                self.serial_port.close()
            except:
                pass
            self.serial_port = None

        # Resetear referencias de sincronización
        self._esp32_offset_epoch  = None
        self._esp32_offset_micros = None

        try:
            selected_display = self.selected_port.get()
            if not selected_display or selected_display == 'No hay puertos disponibles':
                self.gui_queue.put({'type': 'serial_status', 'data': 'Puerto no seleccionado'})
                return

            selected_port = self.port_mapping.get(selected_display, selected_display)

            self.serial_port = serial.Serial(
                port=selected_port,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=2
            )

            # Reset hardware
            self.serial_port.setDTR(False)
            time.sleep(0.1)
            self.serial_port.setDTR(True)
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()

            print("Esperando identificación del dispositivo...")
            self.gui_queue.put({'type': 'serial_status', 'data': 'Verificando...'})

            device_identified = False
            max_attempts      = 2

            for attempt in range(max_attempts):
                print(f"Intento de verificación {attempt + 1}/{max_attempts}")
                self.serial_port.write(b'RESET\n')

                start_time = time.time()
                buffer     = ""

                while (time.time() - start_time) < 10:
                    if self.serial_port.in_waiting > 0:
                        char    = self.serial_port.read().decode('utf-8', errors='ignore')
                        buffer += char

                        if '\n' in buffer:
                            # Procesar TODAS las líneas acumuladas
                            while '\n' in buffer:
                                line, buffer = buffer.split('\n', 1)
                                line = line.strip()
                                if not line:
                                    continue

                                python_now = time.time()
                                if not line.upper().startswith("SYNC"):
                                    print(f"Mensaje recibido: {line}")

                                # ── Identificación ──────────────────────────
                                if "MASTER_ESP32" in line:
                                    device_identified = True
                                    # No break aún: seguir leyendo por si viene SYNC

                                # ── Capturar SYNC inicial ────────────────────
                                parts = line.split(',')
                                if (parts[0].upper() == "SYNC"
                                        and len(parts) == 2
                                        and self._esp32_offset_epoch is None):
                                    try:
                                        esp_micros = int(parts[1])
                                        self._esp32_offset_epoch  = python_now
                                        self._esp32_offset_micros = esp_micros
                                        print(f"[Serial] SYNC capturado en verificación | "
                                            f"epoch={python_now:.6f}, micros={esp_micros}")
                                    except ValueError:
                                        pass

                            if device_identified:
                                break  # salir del while de tiempo

                    time.sleep(0.01)

                if device_identified:
                    break

                if attempt < max_attempts - 1:
                    print("Reintentando con reset hardware...")
                    self.serial_port.setDTR(False)
                    time.sleep(0.1)
                    self.serial_port.setDTR(True)
                    time.sleep(0.5)
                    self.serial_port.reset_input_buffer()

            if not device_identified:
                raise Exception("No se identificó como MASTER_ESP32 después de múltiples intentos")

            # Si no llegó SYNC durante la verificación, esperar 1.2s (un ciclo completo)
            if self._esp32_offset_epoch is None:
                print("[Serial] Sin SYNC aún, esperando primer heartbeat...")
                wait_start = time.time()
                buffer     = ""
                while (time.time() - wait_start) < 1.5:
                    if self.serial_port.in_waiting > 0:
                        raw     = self.serial_port.read(
                                    self.serial_port.in_waiting
                                ).decode('utf-8', errors='ignore')
                        buffer += raw
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line = line.strip()
                            if not line:
                                continue
                            python_now = time.time()
                            parts = line.split(',')
                            if (parts[0].upper() == "SYNC"
                                    and len(parts) == 2
                                    and self._esp32_offset_epoch is None):
                                try:
                                    esp_micros = int(parts[1])
                                    self._esp32_offset_epoch  = python_now
                                    self._esp32_offset_micros = esp_micros
                                    print(f"[Serial] SYNC capturado en espera | "
                                        f"epoch={python_now:.6f}, micros={esp_micros}")
                                    break
                                except ValueError:
                                    pass
                        if self._esp32_offset_epoch is not None:
                            break
                    time.sleep(0.005)

            if self._esp32_offset_epoch is None:
                print("[Serial] ⚠️  Sin SYNC — timestamps usarán tiempo Python como fallback")
            else:
                print(f"[Serial] ✅ Referencia SYNC lista antes de iniciar monitoreo")

            self.serial_port.timeout = 0.1

            print(f"Puerto serial inicializado: {self.serial_port}")
            self.gui_queue.put({'type': 'serial_status', 'data': 'Conectado'})

            self.start_serial_monitoring()

            self.port_dropdown.config(state='disabled')
            self.connect_button.config(text="Desconectar", state='normal',
                                       style='Red.TButton')

            messagebox.showinfo(
                "Conexión Exitosa",
                f"Conectado exitosamente al dispositivo MASTER_ESP32 en puerto {selected_port}"
            )

        except Exception as e:
            error_msg = f"Error en puerto {selected_port}: {str(e)}"
            print(error_msg)
            self.gui_queue.put({'type': 'serial_status', 'data': f'Error: {str(e)}'})

            if "No se identificó como MASTER_ESP32" in str(e):
                messagebox.showerror(
                    "Error",
                    f"El dispositivo en el puerto {selected_port} no es el MASTER_ESP32.\n"
                    "Por favor, seleccione otro puerto COM o verifique la conexión."
                )
            else:
                messagebox.showerror("Error", error_msg)

            if self.serial_port:
                try:
                    self.serial_port.close()
                except:
                    pass
                self.serial_port = None

            self.port_dropdown.config(state='readonly')
            self.connect_button.config(text="Conectar", state='normal',
                                       style='Green.TButton')

    def toggle_serial_connection(self):
        """Botón único Conectar ↔ Desconectar del MASTER_ESP32."""
        if self.serial_port and self.serial_port.is_open:
            if self.is_recording or self._stopping:
                messagebox.showwarning(
                    "ESP32 Control",
                    "No se puede desconectar durante una grabación.\n"
                    "Detenga la adquisición primero."
                )
                return
            self.disconnect_serial()
        else:
            self.connect_serial()

    def disconnect_serial(self):
        """Detiene el monitor, cierra el puerto del MASTER_ESP32 y restaura la UI."""
        # 1) Detener el hilo lector ANTES de cerrar el puerto
        self.serial_stop_event.set()
        if self.serial_thread and self.serial_thread.is_alive() \
                and threading.current_thread() is not self.serial_thread:
            self.serial_thread.join(timeout=1.0)
        self.serial_thread = None

        # 2) Cerrar el puerto
        if self.serial_port:
            try:
                self.serial_port.close()
            except Exception as e:
                print(f"Error al cerrar puerto serial: {e}")
        self.serial_port = None

        # 3) Resetear la referencia de sincronización
        self._esp32_offset_epoch  = None
        self._esp32_offset_micros = None

        self.gui_queue.put({'type': 'serial_status', 'data': 'Desconectado'})

        # 4) Restaurar controles (siempre en el hilo de Tkinter)
        def _ui():
            try:
                self.port_dropdown.config(state='readonly')
                self.connect_button.config(text="Conectar", state='normal',
                                           style='Green.TButton')
            except Exception:
                pass
        if threading.current_thread() is threading.main_thread():
            _ui()
        else:
            self.window.after(0, _ui)

        print("🔌 MASTER_ESP32 desconectado")

    # ═══════════════════════════════════════════════════════════════
    #  SENSOR DE TEMPERATURA (segundo ESP32)
    # ═══════════════════════════════════════════════════════════════
    def refresh_temp_ports(self):
        ports = self.get_available_ports()
        values = [p['display'] for p in ports]
        self.temp_port_mapping = {p['display']: p['device'] for p in ports}
        self.temp_port_combo['values'] = values or ['No hay puertos disponibles']
        if values and self.selected_temp_port.get() not in values:
            self.selected_temp_port.set(values[-1])   # suele ser el último conectado
        elif not values:
            self.selected_temp_port.set('')

    def toggle_temp_connection(self):
        if self.temp_serial and self.temp_serial.is_open:
            self.disconnect_temp_serial()
        else:
            self.connect_temp_serial()

    def connect_temp_serial(self):
        display = self.selected_temp_port.get()
        port = self.temp_port_mapping.get(display)
        if not port:
            messagebox.showerror("Temperatura", "Seleccione un puerto válido.")
            return

        # Evitar usar el mismo COM que el ESP32 de control
        if self.serial_port and getattr(self.serial_port, 'port', None) == port:
            messagebox.showerror("Temperatura",
                                 f"{port} ya está en uso por el ESP32 de control.")
            return
        try:
            self.temp_serial = serial.Serial(port=port, baudrate=BAUD_RATE, timeout=0.1)
        except Exception as e:
            self.temp_serial = None
            messagebox.showerror("Temperatura", f"No se pudo abrir {port}:\n{e}")
            return

        # ── Identificación: esperar "ESP32_TEMP" (arranque o respuesta a ID) ──
        identificado = False
        buf, t0 = "", time.time()
        try:
            time.sleep(0.3)
            self.temp_serial.write(b"ID\n")
            while time.time() - t0 < 4.0 and not identificado:
                n = self.temp_serial.in_waiting
                if n:
                    buf += self.temp_serial.read(n).decode('utf-8', errors='ignore')
                    if "ESP32_TEMP" in buf:
                        identificado = True
                    elif "MASTER_ESP32" in buf:
                        break                      # es el ESP32 de control
                else:
                    time.sleep(0.05)
                    if time.time() - t0 > 2.5 and not buf:
                        self.temp_serial.write(b"ID\n")   # reintento tras el reinicio
        except Exception:
            pass

        if not identificado:
            if not messagebox.askyesno(
                    "Temperatura",
                    f"El dispositivo en {port} no respondió como ESP32_TEMP.\n\n"
                    f"¿Conectar de todas formas?"):
                try:
                    self.temp_serial.close()
                except Exception:
                    pass
                self.temp_serial = None
                return

        self.temp_serial.reset_input_buffer()

        self.temp_stop_event.clear()
        self.temp_thread = threading.Thread(target=self._temp_reader,
                                            daemon=True, name="temp-reader")
        self.temp_thread.start()

        self.temp_connect_btn.config(text="Desconectar")
        self.temp_port_combo.config(state='disabled')
        self.temp_label.config(text="esperando…", foreground='gray')
        print(f"🌡️ Sensor de temperatura conectado en {port}")

    def disconnect_temp_serial(self):
        self.temp_stop_event.set()
        if self.temp_thread and self.temp_thread.is_alive():
            self.temp_thread.join(timeout=1.0)
        if self.temp_serial:
            try:
                self.temp_serial.close()
            except Exception:
                pass
        self.temp_serial = None
        with self._temp_lock:
            self.last_temp_value = None

        def _ui():
            try:
                self.temp_connect_btn.config(text="Conectar")
                self.temp_port_combo.config(state='readonly')
                self.temp_label.config(text="-- °C", foreground='gray')
            except Exception:
                pass
        self.window.after(0, _ui)
        print("🌡️ Sensor de temperatura desconectado")

    @staticmethod
    def _parse_temp(line: str):
        """Acepta 'TEMP,23.4', '23.4', 'T:23.4'… Devuelve float o None."""
        if not line:
            return None
        for part in reversed(re.split(r'[,:;=\s]+', line.strip())):
            try:
                v = float(part)
                return v if -50.0 <= v <= 150.0 else None
            except ValueError:
                continue
        return None

    def _temp_reader(self):
        buf = ""
        while not self.temp_stop_event.is_set():
            try:
                n = self.temp_serial.in_waiting
                if not n:
                    time.sleep(0.02)
                    continue
                buf += self.temp_serial.read(n).decode('utf-8', errors='ignore')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue

                    # Error del sensor ("TEMP_ERR" o "Error al leer el DHT11")
                    if 'ERR' in line.upper():
                        self.gui_queue.put({'type': 'temperature', 'data': 'error'})
                        continue

                    # Identificador de arranque (tras un reinicio del ESP32)
                    if 'ESP32_TEMP' in line:
                        continue

                    val = self._parse_temp(line)
                    if val is None:
                        continue

                    now = time.time()
                    with self._temp_lock:
                        self.last_temp_value = val
                        self.last_temp_time  = now
                    self.gui_queue.put({'type': 'temperature', 'data': val})
                    self._log_temperature(now, val)

            except (serial.SerialException, OSError) as e:
                print(f"🌡️ Error en puerto de temperatura: {e}")
                self.window.after(0, self.disconnect_temp_serial)
                break
            except Exception as e:
                print(f"🌡️ Error leyendo temperatura: {e}")
                buf = ""
                time.sleep(0.1)
                
    def _log_temperature(self, epoch, value):
        """Escribe la lectura en el CSV de la sesión (solo mientras se graba)."""
        path = self.temp_csv_file
        if not (self.is_recording and path):
            return
        try:
            fmt, ep = self.get_synchronized_timestamp(epoch)
            with open(path, 'a', newline='') as f:
                csv.writer(f).writerow([fmt, ep, f"{value:.2f}"])
        except Exception as e:
            print(f"🌡️ Error guardando temperatura: {e}")

    def _check_temp_stale(self):
        """Si no llegan datos en 5 s, marcar la lectura como caducada."""
        try:
            if self.temp_serial and self.temp_serial.is_open:
                with self._temp_lock:
                    age = time.time() - self.last_temp_time
                if self.last_temp_time and age > self.TEMP_STALE_S:
                    self.temp_label.config(text="sin datos", foreground='#ff6b6b')
        except Exception:
            pass
        if not self.stop_event.is_set():
            self.window.after(1000, self._check_temp_stale)
    

    def start_serial_monitoring(self):
        if not hasattr(self, '_esp32_offset_epoch'):
            self._esp32_offset_epoch  = None
            self._esp32_offset_micros = None
        self.serial_stop_event.clear()
        SYNC_LOG_INTERVAL = 60.0   # s entre resúmenes de SYNC en consola
        sync_stats = {'n': 0, 'last_log': 0.0, 'max_drift_ms': 0.0}

        def calcular_timestamp_real(esp32_micros: int) -> float:
            if self._esp32_offset_epoch is None:
                return time.time()
            delta_s = (esp32_micros - self._esp32_offset_micros) / 1_000_000.0
            if delta_s < -30:
                delta_s += 4_294_967_296 / 1_000_000.0
            return self._esp32_offset_epoch + delta_s

        def monitor_serial():
            buffer = ""
            port   = self.serial_port          # referencia local: no depende de self.serial_port
            while not (self.stop_event.is_set() or self.serial_stop_event.is_set()):
                try:
                    if port is None or not port.is_open:
                        break
                    if port.in_waiting > 0:
                        raw     = port.read(port.in_waiting).decode('utf-8', errors='ignore')
                        buffer += raw

                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line = line.strip()
                            if not line:
                                continue

                            python_now   = time.time()
                            parts        = line.split(',')
                            command      = parts[0].upper()
                            esp32_micros = None

                            if len(parts) == 2:
                                try:
                                    esp32_micros = int(parts[1])
                                except ValueError:
                                    pass

                            # ── SYNC (silencioso, resumen periódico) ──
                            if command == "SYNC" and esp32_micros is not None:
                                if self._esp32_offset_epoch is not None:
                                    drift_ms = (python_now -
                                                calcular_timestamp_real(esp32_micros)) * 1000
                                    if abs(drift_ms) > abs(sync_stats['max_drift_ms']):
                                        sync_stats['max_drift_ms'] = drift_ms

                                self._esp32_offset_epoch  = python_now
                                self._esp32_offset_micros = esp32_micros
                                sync_stats['n'] += 1

                                if (sync_stats['n'] == 1 or
                                        python_now - sync_stats['last_log'] >= SYNC_LOG_INTERVAL):
                                    print(f"[Serial] SYNC OK | {sync_stats['n']} recibidos | "
                                          f"deriva máx.: {sync_stats['max_drift_ms']:+.2f} ms")
                                    sync_stats['last_log']     = python_now
                                    sync_stats['max_drift_ms'] = 0.0
                                continue

                            # ── Epoch preciso del evento ──────────────
                            if esp32_micros is not None:
                                event_epoch = calcular_timestamp_real(esp32_micros)
                            else:
                                event_epoch = python_now

                            # ── INICIO ────────────────────────────────
                            if command == "INICIO" and not self.is_recording:
                                print(f"[Serial] INICIO | epoch={event_epoch:.6f} | "
                                    f"ref={'✅' if self._esp32_offset_epoch else '⚠️ sin SYNC'}")
                                # Guardar epoch del click para usarlo en start_acquisition
                                self._pending_inicio_epoch = event_epoch
                                self.window.after(0, self.start_acquisition)

                            # ── FIN ───────────────────────────────────
                            elif command == "FIN" and self.is_recording:
                                print(f"[Serial] FIN | epoch={event_epoch:.6f}")
                                self.window.after(
                                    0,
                                    lambda e=event_epoch: self._mark_event_with_epoch("FIN", e)
                                )
                                self.window.after(60, self.stop_acquisition)

                    time.sleep(0.001)

                except (serial.SerialException, OSError) as e:
                    if self.serial_stop_event.is_set():
                        break                      # cierre voluntario, no es error
                    print(f"🔌 MASTER_ESP32 perdido: {e}")
                    self.window.after(0, self.disconnect_serial)   # cable desconectado
                    break
                except Exception as e:
                    print(f"Error en monitor_serial: {e}")
                    buffer = ""
                    time.sleep(0.1)

            print("Monitor serial detenido")

        self.serial_thread = threading.Thread(
            target=monitor_serial, daemon=True, name="serial-monitor"
        )
        self.serial_thread.start()


    def _mark_event_with_epoch(self, event_type: str, epoch: float):
        if not self.is_recording and event_type != "INICIO":
            return

        dt             = datetime.fromtimestamp(epoch)
        formatted_time = dt.strftime('%Y-%m-%d %H:%M:%S.%f')
        epoch_str      = f"{epoch:.6f}"

        # Momento en que Python procesa el evento (llega al callback)
        proc_epoch  = time.time()
        proc_dt     = datetime.fromtimestamp(proc_epoch)
        proc_fmt    = proc_dt.strftime('%Y-%m-%d %H:%M:%S.%f')
        proc_str    = f"{proc_epoch:.6f}"
        latencia_ms = (proc_epoch - epoch) * 1000

        event_line = (
            f"{formatted_time},{epoch_str},CLICK_FISICO_{event_type},Timestamp ISR ESP32\n"
            f"{proc_fmt},{proc_str},{event_type}_PROCESADO,Procesamiento en Python\n"
            f"{proc_fmt},{proc_str},LATENCIA_{event_type},{latencia_ms:.6f} ms click->procesado\n"
            f"{formatted_time},{epoch_str},{event_type},\n"
        )

        for attr in ('session_events_file', 'ecg_events_file'):
            if hasattr(self, attr):
                try:
                    with open(getattr(self, attr), 'a') as f:
                        f.write(event_line)
                except Exception as e:
                    print(f"Error escribiendo evento en {attr}: {e}")

        if event_type == "FIN":
            self._fin_event_written   = True
            self._fin_click_epoch     = epoch            # ← click físico real (ESP32)
            self._fin_click_formatted = formatted_time
            print(f"[Serial] CLICK_FISICO_FIN: {formatted_time} ({epoch_str}) | "
                f"Latencia: {latencia_ms:.6f} ms")

    def setup_participant_section(self, parent):
        """Configura la sección de información del participante"""
        name_section = ttk.LabelFrame(parent,
                                    text="Información del Participante - Conductor",
                                    style='Card.TLabelframe',
                                    padding="10")
        name_section.pack(fill=tk.X, pady=(0, 10))
        
        # Frame principal
        name_frame = ttk.Frame(name_section)
        name_frame.pack(fill=tk.X, pady=5)
        
        # Frame para la etiqueta (en su propia línea)
        label_frame = ttk.Frame(name_frame)
        label_frame.pack(fill=tk.X, pady=(0, 5))
        
        # Label del participante
        ttk.Label(label_frame,
                text="Nombre:",
                style='Header.TLabel').pack(side=tk.LEFT, padx=5)
        
        # Frame para el entry (en otra línea)
        input_frame = ttk.Frame(name_frame)
        input_frame.pack(fill=tk.X, pady=(0, 5))
        
        # Variable para el nombre temporal (entrada del usuario)
        self.temp_name = tk.StringVar()
        # Variable para el nombre validado (después de actualizar)
        self.validated_name = tk.StringVar()
        
        # Entry con validación, estilo en negrita y texto centrado
        vcmd = (self.window.register(self.validate_name_input), '%P')
        self.name_entry = ttk.Entry(input_frame,
                                textvariable=self.temp_name,
                                font=('Segoe UI', 10, 'bold'),  # Fuente en negrita
                                justify='center',  # Texto centrado
                                validate='key',
                                validatecommand=vcmd)
        self.name_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Frame para el botón
        button_frame = ttk.Frame(name_frame)
        button_frame.pack(fill=tk.X, pady=(5, 0))
        
        # Botón de actualizar
        self.update_button = ttk.Button(button_frame,
                                    text="Actualizar",
                                    command=self.update_name,
                                    style='Green.TButton',
                                    state='disabled')
        self.update_button.pack(pady=5, fill=tk.X)
        
        # Label para mensajes de validación
        self.name_validation_label = ttk.Label(name_section,
                                            text="",
                                            foreground='red',
                                            style='TLabel')
        self.name_validation_label.pack(pady=(5, 0))
        
        # Rastrear cambios en el nombre temporal
        self.temp_name.trace_add('write', self.validate_name)
        
        # Guardar con Enter
        self.name_entry.bind('<Return>', lambda e: self.update_name() 
                             if self.update_button['state'] != 'disabled' else None)

    def setup_status_section(self, parent):
        """Configura la sección de estado del sistema con botones de conexión manual y desconexión forzada"""
        status_frame = ttk.LabelFrame(parent,
                                    text="Estado del Sistema",
                                    style='Card.TLabelframe',
                                    padding="10")
        status_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Estado general
        self.status_label = ttk.Label(status_frame,
                                    text="Estado: Iniciando...",
                                    style='Header.TLabel')
        self.status_label.pack(pady=5)
        
        # Indicadores de estado
        indicators_frame = ttk.Frame(status_frame)
        indicators_frame.pack(fill=tk.X, pady=3)
        
        # Crear indicadores alineados verticalmente
        self.create_status_indicator(indicators_frame, "EEG")
        self.create_status_indicator(indicators_frame, "Serial")
        self.create_status_indicator(indicators_frame, "ECG")
        self.create_status_indicator(indicators_frame, "LiDAR")
        self.create_status_indicator(indicators_frame, "BIOPAC")   # ← NUEVO
        
        # Frame para botones de conexión y desconexión
        buttons_frame = ttk.Frame(status_frame)
        buttons_frame.pack(fill=tk.X, pady=3)
        
        # Dividir en dos columnas para los botones
        left_buttons = ttk.Frame(buttons_frame)
        left_buttons.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        
        right_buttons = ttk.Frame(buttons_frame)
        right_buttons.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))
        
        # ── fila extra para botones BIOPAC / ECG ─────────────────────────────
        extra_buttons = ttk.Frame(status_frame)
        extra_buttons.pack(fill=tk.X, pady=(2, 0))

        left_extra = ttk.Frame(extra_buttons)
        left_extra.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        right_extra = ttk.Frame(extra_buttons)
        right_extra.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        # Botón de conexión manual de EEG (inicialmente deshabilitado)
        self.manual_connect_button = ttk.Button(
            left_buttons,
            text="Conectar EEG",
            command=self.manual_connect_eeg,
            state='disabled',
            style='Green.TButton'  # Añadido estilo consistente
        )
        self.manual_connect_button.pack(pady=5, fill=tk.X)
        self.manual_connect_button.configure(width=15)
        
        # Botón de desconexión forzada de ECG
        self.ecg_disconnect_button = ttk.Button(
            right_buttons,
            text="Desconexión ECG",
            command=self.force_ecg_disconnect,
            style='Red.TButton'
        )
        self.ecg_disconnect_button.pack(pady=5, fill=tk.X)
        self.ecg_disconnect_button.configure(width=15)

        # Botón reset BIOPAC  ← NUEVO
        self.biopac_reset_button = ttk.Button(
            right_extra,
            text="Reset BIOPAC",
            command=self.force_biopac_disconnect,
            style='Red.TButton'
        )
        self.biopac_reset_button.pack(pady=5, fill=tk.X)
        self.biopac_reset_button.configure(width=15)

    def force_ecg_disconnect(self):
        """Fuerza la desconexión del ECG y reinicia el estado"""
        try:
            if messagebox.askyesno("Confirmar Desconexión", 
                                "¿Está seguro de que desea forzar la desconexión del ECG?\n\n" +
                                "Esto reiniciará el estado del sistema ECG y " +
                                "será necesario volver a conectarlo."):
                
                # Reiniciar el estado del ECG
                if hasattr(self, 'ecg_handler'):
                    # Primero notificar la desconexión
                    self.ecg_handler._notify_state_change(ECGState.DISCONNECTED)
                    
                    # Reiniciar todos los flags y variables de estado
                    self.ecg_handler._reset_flags()
                    self.ecg_handler.event_sequence.clear()
                    for file in self.ecg_handler.required_files:
                        self.ecg_handler.required_files[file] = False
                    self.ecg_handler.modification_counts = {'yellow': 0, 'green': 0}
                    self.ecg_handler.last_modification_time = None
                    
                    # Después de un breve delay, volver a estado de espera
                    self.window.after(2000, lambda: self.ecg_handler._notify_state_change(ECGState.WAITING))
                    
                    # Notificar al usuario
                    self.gui_queue.put({'type': 'system_status', 'data': 'ECG desconectado forzadamente'})
                    messagebox.showinfo("Desconexión Exitosa", 
                                    "El ECG ha sido desconectado forzadamente.\n" +
                                    "El sistema volverá al estado de espera.")
        except Exception as e:
            print(f"Error al forzar desconexión del ECG: {e}")
            messagebox.showerror("Error", f"Error al forzar desconexión del ECG: {e}")
            
    def setup_time_section(self, parent):
        """Configura la sección de información temporal"""
        time_frame = ttk.LabelFrame(parent,
                                text="Información Temporal",
                                style='Card.TLabelframe',
                                padding="10")
        time_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Frame para tiempo, detalles y logo
        info_frame = ttk.Frame(time_frame)
        info_frame.pack(fill=tk.X)
        info_frame.grid_columnconfigure(0, weight=1)
        info_frame.grid_columnconfigure(1, weight=1)
        info_frame.grid_columnconfigure(2, weight=0)  # Columna para el logo (no expandible)
        
        # Frame izquierdo - Tiempo transcurrido
        elapsed_frame = ttk.Frame(info_frame)
        elapsed_frame.grid(row=0, column=0, padx=10, sticky='ew')
        
        self.time_label = ttk.Label(elapsed_frame,
                                text="00:00:00.000000",
                                font=('Segoe UI', 20, 'bold'),
                                foreground='#6B1B6B')
        self.time_label.pack(pady=5)
        
        # Frame central - Detalles
        details_frame = ttk.Frame(info_frame)
        details_frame.grid(row=0, column=1, padx=10, sticky='ew')
        
        # Grid para etiquetas de tiempo
        details_frame.grid_columnconfigure(1, weight=1)
        
        ttk.Label(details_frame,
                text="Inicio:",
                style='Header.TLabel').grid(row=0, column=0, padx=(0,10), sticky='e')
        
        self.start_time_label = ttk.Label(details_frame,
                                        text="--:--:------",
                                        style='TLabel')
        self.start_time_label.grid(row=0, column=1, sticky='w')
        
        ttk.Label(details_frame,
                text="Fecha:",
                style='Header.TLabel').grid(row=1, column=0, padx=(0,10), sticky='e')
        
        self.date_label = ttk.Label(details_frame,
                                text="--/--/----",
                                style='TLabel')
        self.date_label.grid(row=1, column=1, sticky='w')

                # ── Temperatura ──
        ttk.Label(details_frame,
                text="Temp:",
                style='Header.TLabel').grid(row=2, column=0, padx=(0,10), sticky='e')

        self.temp_label = ttk.Label(details_frame,
                                    text="-- °C",
                                    font=('Segoe UI', 11, 'bold'),
                                    foreground='gray')
        self.temp_label.grid(row=2, column=1, sticky='w')

        # Selector de puerto del sensor
        temp_ctrl = ttk.Frame(details_frame)
        temp_ctrl.grid(row=3, column=0, columnspan=2, sticky='w', pady=(4, 0))

        self.temp_port_combo = ttk.Combobox(temp_ctrl,
                                            textvariable=self.selected_temp_port,
                                            state='readonly', width=24)
        self.temp_port_combo.pack(side=tk.LEFT)

        ttk.Button(temp_ctrl, text="🔄", width=3,
                command=self.refresh_temp_ports).pack(side=tk.LEFT, padx=2)

        self.temp_connect_btn = ttk.Button(temp_ctrl, text="Conectar", width=10,
                                        command=self.toggle_temp_connection)
        self.temp_connect_btn.pack(side=tk.LEFT)

        self.refresh_temp_ports()
        
        # Separador vertical
        separator = ttk.Separator(info_frame, orient='vertical')
        separator.grid(row=0, column=2, padx=10, sticky='ns')
        
        # Frame derecho - Logo
        logo_frame = ttk.Frame(info_frame)
        logo_frame.grid(row=0, column=3, padx=10, sticky='ew')
        
        try:
            # Cargar y mostrar el logo
            logo_image = Image.open("LANCEI_vA.png")
            # Redimensionar el logo si es necesario (ajusta el tamaño según tu preferencia)
            logo_image = logo_image.resize((380, 90), Image.Resampling.LANCZOS)
            self.logo_photo = ImageTk.PhotoImage(logo_image)
            
            logo_label = ttk.Label(logo_frame, image=self.logo_photo)
            logo_label.pack(pady=5)
            
        except Exception as e:
            # Si no se encuentra el logo, mostrar un mensaje
            ttk.Label(logo_frame,
                    text="Logo\nNo disponible",
                    style='TLabel',
                    justify='center').pack(pady=5)
            print(f"Error al cargar logo: {e}")
        

    def setup_camera_section(self, parent):
        """Configura la sección de visualización de 2 cámaras con diseño horizontal optimizado"""
        camera_frame = ttk.LabelFrame(parent,
                                    text="Sistema Multi-Cámara (2 Cámaras Facial - Carretera)",
                                    style='Card.TLabelframe',
                                    padding="15")
        camera_frame.pack(fill=tk.BOTH, expand=True)

        # Frame para controles principales en una fila superior
        controls_main_frame = ttk.Frame(camera_frame)
        controls_main_frame.pack(fill=tk.X, pady=(0, 15))
        
        # Variables para controles
        self.face_detection_var = tk.BooleanVar(value=False)
        self.landmarks_var = tk.BooleanVar(value=False)
        self.hands_var = tk.BooleanVar(value=False)
        self.metrics_var = tk.BooleanVar(value=False)
        self.alerts_var = tk.BooleanVar(value=False)
        
        # === PANEL 1: CONTROLES GENERALES ===
        general_frame = ttk.Frame(controls_main_frame)
        general_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 2))
        
        ttk.Label(general_frame, 
                text="Controles generales", 
                style='Header.TLabel').pack(anchor='w', pady=(0, 8))
        
        buttons_row = ttk.Frame(general_frame)
        buttons_row.pack(fill=tk.X)
        
        # Botones en fila
        refresh_button = ttk.Button(buttons_row,
                                text="🔄 Actualizar",
                                command=self.update_camera_list,
                                style='Green.TButton',
                                width=12)
        refresh_button.pack(side=tk.LEFT, padx=(0, 5))
        
        self.start_all_cameras_btn = ttk.Button(buttons_row,
                                            text="▶ Todas",
                                            command=self.start_all_cameras,
                                            style='Green.TButton',
                                            width=10)
        self.start_all_cameras_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        self.stop_all_cameras_btn = ttk.Button(buttons_row,
                                            text="⏹ Todas",
                                            command=self.stop_all_cameras,
                                            style='Red.TButton',
                                            state='disabled',
                                            width=10)
        self.stop_all_cameras_btn.pack(side=tk.LEFT)
        
        # FRANJA DIVISORIA 1
        separator1 = ttk.Separator(controls_main_frame, orient='vertical')
        separator1.pack(side=tk.LEFT, fill=tk.Y, padx=10)
        
        recognition_frame = ttk.Frame(controls_main_frame)
        recognition_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 2))

        ttk.Label(recognition_frame, 
                text="Detección Facial (principal)", 
                style='Header.TLabel').pack(anchor='w', pady=(0, 8))

        # Frame contenedor para las 2 filas
        facial_controls_container = ttk.Frame(recognition_frame)
        facial_controls_container.pack(fill=tk.X)

        # === FILA 1: Activar + Landmarks + Manos ===
        facial_controls_row1 = ttk.Frame(facial_controls_container)
        facial_controls_row1.pack(fill=tk.X, pady=(0, 5))

        self.face_detection_check = ttk.Checkbutton(
            facial_controls_row1,
            text="👤 Activar",
            variable=self.face_detection_var,
            command=self.toggle_face_detection,
            state='disabled'
        )
        self.face_detection_check.pack(side=tk.LEFT, padx=(0, 8))
        
        # === FILA 2: Métricas + Alertas ===
        facial_controls_row2 = ttk.Frame(facial_controls_container)
        facial_controls_row2.pack(fill=tk.X)

        self.landmarks_check = ttk.Checkbutton(
            facial_controls_row2,
            text="📍 Landmarks",
            variable=self.landmarks_var,
            command=self.toggle_landmarks,
            state='disabled'
        )
        self.landmarks_check.pack(side=tk.LEFT, padx=(0, 8))

        self.hands_check = ttk.Checkbutton(
            facial_controls_row2,
            text="✋ Manos",
            variable=self.hands_var,
            command=self.toggle_hands,
            state='disabled'
        )
        self.hands_check.pack(side=tk.LEFT)

        

        self.metrics_check = ttk.Checkbutton(
            facial_controls_row2,
            text="📊 Métricas",
            variable=self.metrics_var,
            command=self.toggle_metrics,
            state='disabled'
        )
        self.metrics_check.pack(side=tk.LEFT, padx=(0, 8))

        self.alerts_check = ttk.Checkbutton(
            facial_controls_row2,
            text="🔔 Alertas",
            variable=self.alerts_var,
            command=self.toggle_alerts,
            state='disabled'
        )
        self.alerts_check.pack(side=tk.LEFT)

        # FRANJA DIVISORIA 2
        separator2 = ttk.Separator(controls_main_frame, orient='vertical')
        separator2.pack(side=tk.LEFT, fill=tk.Y, padx=10)

        # === PANEL 3: DETECCIÓN DE LÍNEAS ===
        lane_frame = ttk.Frame(controls_main_frame)
        lane_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 2))
        
        ttk.Label(lane_frame, 
                text="Detección Líneas (secundaria)", 
                style='Header.TLabel').pack(anchor='w', pady=(0, 8))
        
        lane_controls = ttk.Frame(lane_frame)
        lane_controls.pack(fill=tk.X)
        
        self.lane_detection_check = ttk.Checkbutton(
            lane_controls,
            text="🛣️ Activar",
            variable=self.lane_detection_var,
            command=self.toggle_lane_detection,
            state='disabled'
        )
        self.lane_detection_check.pack(side=tk.LEFT, padx=(0, 8))
        
        self.lane_metrics_check = ttk.Checkbutton(
            lane_controls,
            text="📊 Métricas",
            variable=self.lane_metrics_var,
            command=self.toggle_lane_metrics,
            state='disabled'
        )
        self.lane_metrics_check.pack(side=tk.LEFT, padx=(0, 8))
        
        self.lane_predictions_check = ttk.Checkbutton(
            lane_controls,
            text="🔮 Predicciones",
            variable=self.lane_predictions_var,
            command=self.toggle_lane_predictions,
            state='disabled'
        )
        self.lane_predictions_check.pack(side=tk.LEFT)

        # FRANJA DIVISORIA 3
        separator3 = ttk.Separator(controls_main_frame, orient='vertical')
        separator3.pack(side=tk.LEFT, fill=tk.Y, padx=10)

        # === PANEL 4: LIDAR ===
        lidar_frame = ttk.Frame(controls_main_frame)
        lidar_frame.pack(side=tk.LEFT, fill=tk.Y)

        ttk.Label(lidar_frame, 
                text="LiDAR Velodyne HDL-64E", 
                style='Header.TLabel').pack(anchor='w', pady=(0, 8))

        lidar_controls = ttk.Frame(lidar_frame)
        lidar_controls.pack(fill=tk.X)

        self.lidar_enabled_check = ttk.Checkbutton(
            lidar_controls,
            text="📡 Habilitar",
            variable=self.lidar_enabled,
            command=self.toggle_lidar
        )
        self.lidar_enabled_check.pack(side=tk.LEFT, padx=(0, 8))

        self.lidar_status_label = ttk.Label(
            lidar_controls,
            text="⚪ Inactivo",
            foreground="gray"
        )
        self.lidar_status_label.pack(side=tk.LEFT, padx=(0, 8))

        vis_button = ttk.Button(
            lidar_controls,
            text="🎯 Visualizar",
            command=self.open_lidar_visualizer,
            style='Green.TButton',
            width=12
        )
        vis_button.pack(side=tk.LEFT)

        # FRANJA DIVISORIA 4
        separator4 = ttk.Separator(controls_main_frame, orient='vertical')
        separator4.pack(side=tk.LEFT, fill=tk.Y, padx=10)

        # === PANEL 5: VISUALIZACIÓN POW (NUEVO) ===
        pow_frame = ttk.Frame(controls_main_frame)
        pow_frame.pack(side=tk.LEFT, fill=tk.Y)

        ttk.Label(pow_frame, 
                text="Potencia EEG (POW)", 
                style='Header.TLabel').pack(anchor='w', pady=(0, 8))

        pow_controls = ttk.Frame(pow_frame)
        pow_controls.pack(fill=tk.X)

        self.pow_visualization_check = ttk.Checkbutton(
            pow_controls,
            text="📊 Visualizar",
            variable=self.pow_visualization_var,
            command=self.toggle_pow_visualization,
            state='disabled'  # Inicialmente deshabilitado
        )
        self.pow_visualization_check.pack(side=tk.LEFT, padx=(0, 8))

        self.pow_status_label = ttk.Label(
            pow_controls,
            text="⚪ Inactivo",
            foreground="gray"
        )
        self.pow_status_label.pack(side=tk.LEFT)

        # Línea separadora horizontal principal
        main_separator = ttk.Separator(camera_frame, orient='horizontal')
        main_separator.pack(fill=tk.X, pady=10)
        
        # === ESTRUCTURA DE CÁMARAS HORIZONTALES ===
        cameras_container = ttk.Frame(camera_frame)
        cameras_container.pack(fill=tk.BOTH, expand=True)
        
        # Configurar grid para división equitativa 50-50
        cameras_container.columnconfigure(0, weight=1, uniform="cameras")
        cameras_container.columnconfigure(1, weight=1, uniform="cameras")
        cameras_container.columnconfigure(2, weight=1, uniform="cameras")  # NUEVO

        cameras_container.rowconfigure(0, weight=1)
        
        # Frame para cámara principal (izquierda)
        main_cam_frame = ttk.Frame(cameras_container)
        main_cam_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        
        # Frame para cámara secundaria (derecha)
        secondary_cam_frame = ttk.Frame(cameras_container)
        secondary_cam_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        # Agregar frame y panel para secondary2:
        secondary_cam2_frame = ttk.Frame(cameras_container)
        secondary_cam2_frame.grid(row=0, column=2, sticky="nsew", padx=(5, 0))  # NUEVO

        
        # Configurar paneles de cámara
        self.setup_horizontal_camera_panel(main_cam_frame, 'main', "Principal")
        self.setup_horizontal_camera_panel(secondary_cam_frame, 'secondary1', "Frontal")
        self.setup_horizontal_camera_panel(secondary_cam2_frame, 'secondary2', "Trasera")  # NUEVO

        
        # Guardar referencia al frame principal
        self.camera_frame = camera_frame

    def setup_horizontal_camera_panel(self, parent, camera_id, camera_title):
        """
        Configura un panel de cámara optimizado para disposición horizontal (50-50)
        
        Args:
            parent: Widget padre
            camera_id: Identificador de la cámara ('main', 'secondary1')
            camera_title: Título del panel
        """
        # Panel general para la cámara - SIN LabelFrame
        camera_panel = ttk.Frame(parent, padding="10")
        camera_panel.pack(fill=tk.BOTH, expand=True)
        
        # Título del panel
        ttk.Label(camera_panel, 
                text=f"📹 Cámara {camera_title}", 
                style='Header.TLabel').pack(anchor='w', pady=(0, 10))
        
        # Frame para controles compactos en la parte superior
        controls_frame = ttk.Frame(camera_panel)
        controls_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Primera fila: Selección de cámara
        select_row = ttk.Frame(controls_frame)
        select_row.pack(fill=tk.X, pady=(0, 5))
        
        ttk.Label(select_row, text="Seleccionar:").pack(side=tk.LEFT, padx=(0, 8))
        
        # Variable para el dropdown
        camera_var_name = f"{camera_id}_camera_var"
        setattr(self, camera_var_name, tk.StringVar())
        
        # ComboBox
        combo_name = f"{camera_id}_combo"
        combo = ttk.Combobox(select_row, 
                        textvariable=getattr(self, camera_var_name), 
                        state='readonly', 
                        width=25)
        setattr(self, combo_name, combo)
        combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # Segunda fila: Botones de control
        buttons_row = ttk.Frame(controls_frame)
        buttons_row.pack(fill=tk.X, pady=(0, 5))
        
        # Botón de inicio
        start_btn_name = f"start_{camera_id}_btn"
        start_btn = ttk.Button(buttons_row,
                            text="▶ Iniciar",
                            command=lambda c_id=camera_id: self.start_specific_camera(c_id),
                            style='Green.TButton',
                            width=12)
        setattr(self, start_btn_name, start_btn)
        start_btn.pack(side=tk.LEFT, padx=(0, 8))
        
        # Botón de detención
        stop_btn_name = f"stop_{camera_id}_btn"
        stop_btn = ttk.Button(buttons_row,
                            text="⏹ Detener",
                            command=lambda c_id=camera_id: self.stop_specific_camera(c_id),
                            style='Red.TButton',
                            state='disabled',
                            width=12)
        setattr(self, stop_btn_name, stop_btn)
        stop_btn.pack(side=tk.LEFT)
        
        # Tercera fila: Controles de espejo
        mirror_row = ttk.Frame(controls_frame)
        mirror_row.pack(fill=tk.X)
        
        # Variables para estado de botones espejo
        flip_x_var_name = f"{camera_id}_flip_x_var"
        flip_y_var_name = f"{camera_id}_flip_y_var"
        _defaults = self.multi_camera_manager.cameras.get(camera_id, {})
        setattr(self, flip_x_var_name, tk.BooleanVar(value=_defaults.get('flip_x', False)))
        setattr(self, flip_y_var_name, tk.BooleanVar(value=_defaults.get('flip_y', False)))
        # Checkbox Espejo X
        flip_x_btn_name = f"{camera_id}_flip_x_btn"
        flip_x_btn = ttk.Checkbutton(
            mirror_row,
            text="↔ Espejo Horizontal",
            variable=getattr(self, flip_x_var_name),
            command=lambda c_id=camera_id: self.toggle_camera_flip_x(c_id),
            state='disabled'
        )
        setattr(self, flip_x_btn_name, flip_x_btn)
        flip_x_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        # Vincular directamente a la variable
        def update_flip_x(*args):
            if hasattr(self, 'multi_camera_manager'):
                is_active = getattr(self, flip_x_var_name).get()
                self.multi_camera_manager.cameras[camera_id]['flip_x'] = is_active
        
        getattr(self, flip_x_var_name).trace_add('write', update_flip_x)
        
        # Checkbox Espejo Y
        flip_y_btn_name = f"{camera_id}_flip_y_btn"
        flip_y_btn = ttk.Checkbutton(
            mirror_row,
            text="↕ Espejo Vertical",
            variable=getattr(self, flip_y_var_name),
            command=lambda c_id=camera_id: self.toggle_camera_flip_y(c_id),
            state='disabled'
        )
        setattr(self, flip_y_btn_name, flip_y_btn)
        flip_y_btn.pack(side=tk.LEFT)
        
        # Vincular directamente a la variable
        def update_flip_y(*args):
            if hasattr(self, 'multi_camera_manager'):
                is_active = getattr(self, flip_y_var_name).get()
                self.multi_camera_manager.cameras[camera_id]['flip_y'] = is_active
        
        getattr(self, flip_y_var_name).trace_add('write', update_flip_y)
        
        # Línea separadora antes del video
        separator = ttk.Separator(camera_panel, orient='horizontal')
        separator.pack(fill=tk.X, pady=(0, 10))
        
        # Panel de visualización de cámara con borde sutil
        view_frame = ttk.Frame(camera_panel, relief='solid', borderwidth=1)
        view_frame.pack(fill=tk.BOTH, expand=True)
        
        # Canvas para visualización
        canvas = tk.Canvas(view_frame, bg="black", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        
        # Mostrar mensaje inicial
        canvas.create_text(
            canvas.winfo_width() // 2 or 300,
            canvas.winfo_height() // 2 or 200,
            text=f"Cámara {camera_title} desactivada",
            fill="white",
            font=("Arial", 14),
            tags=f"initial_message_{camera_id}"
        )
        
        # Guardar referencia al canvas
        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.cameras[camera_id]['canvas'] = canvas
        
        # Función para actualizar posición del mensaje cuando se redimensiona
        def on_canvas_configure(event, cam_id=camera_id):
            for item in canvas.find_withtag(f"initial_message_{cam_id}"):
                canvas.coords(item, event.width // 2, event.height // 2)
        
        # Vincular evento de redimensionamiento
        canvas.bind("<Configure>", on_canvas_configure)
    
    def toggle_camera_flip_x(self, camera_id):
        """
        Cambia la rotación en espejo horizontal para una cámara específica
        
        Args:
            camera_id (str): ID de la cámara ('main', 'secondary1')
        """
        if hasattr(self, 'multi_camera_manager'):
            # Obtener variable BooleanVar
            var_name = f"{camera_id}_flip_x_var"
            is_active = getattr(self, var_name).get()
            
            # Aplicar cambio en el MultiCameraManager
            self.multi_camera_manager.toggle_flip_x(camera_id)
            
            # Actualizar mensaje de estado
            self.gui_queue.put({
                'type': 'system_status',
                'data': f'Espejo X para Cámara {camera_id}: {"activado" if is_active else "desactivado"}'
            })
    
    def toggle_camera_flip_y(self, camera_id):
        """
        Cambia la rotación en espejo vertical para una cámara específica
        
        Args:
            camera_id (str): ID de la cámara ('main', 'secondary1', 'secondary2')
        """
        if hasattr(self, 'multi_camera_manager'):
            # Obtener variable BooleanVar
            var_name = f"{camera_id}_flip_y_var"
            is_active = getattr(self, var_name).get()
            
            # Aplicar cambio en el MultiCameraManager
            self.multi_camera_manager.toggle_flip_y(camera_id)
            
            # Actualizar mensaje de estado
            self.gui_queue.put({
                'type': 'system_status',
                'data': f'Espejo Y para Cámara {camera_id}: {"activado" if is_active else "desactivado"}'
            })

    def update_camera_list(self):
        """Actualiza la lista de cámaras disponibles (versión rápida con feedback)"""
        # Mostrar estado inmediatamente - NO bloquea
        for camera_id in ['main', 'secondary1', 'secondary2']:
            combo_name = f"{camera_id}_combo"
            var_name = f"{camera_id}_camera_var"
            if hasattr(self, combo_name):
                getattr(self, combo_name)['values'] = ['🔍 Buscando...']
                getattr(self, var_name).set('🔍 Buscando...')
            
            start_btn_name = f"start_{camera_id}_btn"
            if hasattr(self, start_btn_name):
                getattr(self, start_btn_name).config(state='disabled')
        
        # Lanzar búsqueda en hilo secundario (NO bloquea UI)
        search_thread = threading.Thread(
            target=self._search_cameras_background,
            daemon=True,
            name="CameraSearchWorker"
        )
        search_thread.start()


    def _search_cameras_background(self):
        """Busca cámaras en hilo secundario sin bloquear UI"""
        try:
            print("🔍 Buscando cámaras disponibles...")
            start_time = time.time()
            
            # Trabajo pesado aquí (OpenCV puede tardar)
            self.available_cameras = (
                self.multi_camera_manager.list_cameras() 
                if hasattr(self, 'multi_camera_manager') 
                else {}
            )
            
            cameras = list(self.available_cameras.values())
            elapsed = time.time() - start_time
            print(f"✅ Se encontraron {len(cameras)} cámara(s) en {elapsed:.2f}s")
            
            # Actualizar UI DESDE EL HILO PRINCIPAL
            self.window.after(0, lambda: self._update_camera_dropdowns(cameras))
            
        except Exception as e:
            print(f"❌ Error buscando cámaras: {e}")
            self.window.after(0, lambda: self._update_camera_dropdowns([]))


    def _update_camera_dropdowns(self, cameras):
        """Actualiza los dropdowns con las cámaras encontradas 
        (ejecutado en UI thread - seguro)"""
        try:
            for camera_id in ['main', 'secondary1', 'secondary2']:
                combo_name = f"{camera_id}_combo"
                var_name = f"{camera_id}_camera_var"
                start_btn_name = f"start_{camera_id}_btn"
                
                if hasattr(self, combo_name):
                    combo = getattr(self, combo_name)
                    
                    if cameras:
                        # ✅ Mostrar cámaras
                        combo['values'] = cameras
                        
                        # Seleccionar primera cámara si no hay selección
                        current_val = getattr(self, var_name).get()
                        if current_val not in cameras and current_val.startswith('🔍'):
                            getattr(self, var_name).set(cameras[0])
                        
                        # Habilitar botón
                        if hasattr(self, start_btn_name):
                            getattr(self, start_btn_name).config(state='normal')
                    else:
                        # ❌ Sin cámaras
                        combo['values'] = ['❌ No disponibles']
                        getattr(self, var_name).set('❌ No disponibles')
                        
                        if hasattr(self, start_btn_name):
                            getattr(self, start_btn_name).config(state='disabled')
            
            # Actualizar botón "Todas"
            if hasattr(self, 'start_all_cameras_btn'):
                if cameras:
                    self.start_all_cameras_btn.config(state='normal')
                else:
                    self.start_all_cameras_btn.config(state='disabled')
                    
        except Exception as e:
            print(f"❌ Error actualizando dropdowns: {e}")
                
    def start_specific_camera(self, camera_id):
        """
        Inicia una cámara específica - VERSIÓN CORREGIDA
        
        Args:
            camera_id (str): ID de la cámara a iniciar ('main', 'secondary1', 'secondary2')
        """
        try:
            # Verificar que existe el manager de cámaras
            if not hasattr(self, 'multi_camera_manager'):
                messagebox.showerror("Error", "Sistema de cámaras no inicializado")
                return
                
            # Obtener el índice de la cámara seleccionada
            var_name = f"{camera_id}_camera_var"
            selected_camera = getattr(self, var_name).get()
            
            camera_idx = None
            for idx, name in self.available_cameras.items():
                if name == selected_camera:
                    camera_idx = idx
                    break
                    
            if camera_idx is None:
                messagebox.showinfo("Información", "No hay cámara seleccionada válida")
                return
                
            # Iniciar la cámara
            success = self.multi_camera_manager.start_camera(camera_id, camera_idx)
            
            if success:
                # Actualizar estado de botones
                start_btn_name = f"start_{camera_id}_btn"
                stop_btn_name = f"stop_{camera_id}_btn"
                combo_name = f"{camera_id}_combo"
                flip_x_btn_name = f"{camera_id}_flip_x_btn"
                flip_y_btn_name = f"{camera_id}_flip_y_btn"
                
                if hasattr(self, start_btn_name):
                    getattr(self, start_btn_name).config(state='disabled')
                if hasattr(self, stop_btn_name):
                    getattr(self, stop_btn_name).config(state='normal')
                if hasattr(self, combo_name):
                    getattr(self, combo_name).config(state='disabled')
                
                # Habilitar los botones de espejo
                if hasattr(self, flip_x_btn_name):
                    getattr(self, flip_x_btn_name).config(state='normal')
                if hasattr(self, flip_y_btn_name):
                    getattr(self, flip_y_btn_name).config(state='normal')
                
                # ====== CORRECCIÓN PRINCIPAL ======
                # Habilitar controles de detección facial SOLO para cámara principal
                if camera_id == 'main':
                    if hasattr(self, 'face_detection_check'):
                        self.face_detection_check.config(state='normal')
                    print("✅ Controles de detección facial habilitados para cámara principal")

                elif camera_id == 'secondary1':
                    if hasattr(self, 'lane_detection_check'):
                        self.lane_detection_check.config(state='normal')
                    if hasattr(self, 'lane_metrics_check'):
                        self.lane_metrics_check.config(state='normal')
                    if hasattr(self, 'lane_predictions_check'):
                        self.lane_predictions_check.config(state='normal')
                    print("✅ Controles de detección de líneas habilitados para cámara secundaria 1")

                elif camera_id == 'secondary2':
                    print("✅ Cámara secundaria 2 iniciada (solo grabación)")
                    
                # Actualizar botón de detener todas
                active_cameras = sum(1 for c in self.multi_camera_manager.cameras.values() if c['active'])
                if active_cameras > 0 and hasattr(self, 'stop_all_cameras_btn'):
                    self.stop_all_cameras_btn.config(state='normal')
                    
                print(f"Cámara {camera_id} iniciada correctamente")
            else:
                messagebox.showerror("Error", f"No se pudo iniciar la cámara {camera_id}")
                
        except Exception as e:
            print(f"Error al iniciar la cámara {camera_id}: {e}")
            import traceback
            traceback.print_exc()
            messagebox.showerror("Error", f"Error al iniciar la cámara {camera_id}: {e}")



    def stop_specific_camera(self, camera_id):
        """
        Detiene una cámara específica - VERSIÓN CORREGIDA
        
        Args:
            camera_id (str): ID de la cámara a detener ('main', 'secondary1', 'secondary2')
        """
        try:
            # Verificar que existe el manager de cámaras
            if not hasattr(self, 'multi_camera_manager'):
                return
                
            # Detener la cámara
            self.multi_camera_manager.stop_camera(camera_id)
            
            # Actualizar estado de botones
            start_btn_name = f"start_{camera_id}_btn"
            stop_btn_name = f"stop_{camera_id}_btn"
            combo_name = f"{camera_id}_combo"
            flip_x_btn_name = f"{camera_id}_flip_x_btn"
            flip_y_btn_name = f"{camera_id}_flip_y_btn"
            
            if hasattr(self, start_btn_name):
                getattr(self, start_btn_name).config(state='normal')
            if hasattr(self, stop_btn_name):
                getattr(self, stop_btn_name).config(state='disabled')
            if hasattr(self, combo_name):
                getattr(self, combo_name).config(state='readonly')
                
            # Deshabilitar los botones de espejo
            if hasattr(self, flip_x_btn_name):
                getattr(self, flip_x_btn_name).config(state='disabled')
            if hasattr(self, flip_y_btn_name):
                getattr(self, flip_y_btn_name).config(state='disabled')
                
            # ====== CORRECCIÓN: Deshabilitar controles específicos por cámara ======
            
            # Si es la cámara principal, deshabilitar controles de detección facial
            if camera_id == 'main':
                if hasattr(self, 'face_detection_check'):
                    self.face_detection_check.config(state='disabled')
                    self.face_detection_var.set(False)
                if hasattr(self, 'landmarks_check'):
                    self.landmarks_check.config(state='disabled')
                    self.landmarks_var.set(False)
                if hasattr(self, 'hands_check'):
                    self.hands_check.config(state='disabled')
                    self.hands_var.set(False)
                if hasattr(self, 'metrics_check'):
                    self.metrics_check.config(state='disabled')
                    self.metrics_var.set(False)
                if hasattr(self, 'alerts_check'):
                    self.alerts_check.config(state='disabled')
                    self.alerts_var.set(False)
                    
                # Actualizar estado interno del gestor facial si existe
                if hasattr(self, 'facial_feature_manager'):
                    self.facial_feature_manager.toggle_face_detection(False)
                    self.facial_feature_manager.toggle_landmarks(False)
                    self.facial_feature_manager.toggle_hands(False)
                    self.facial_feature_manager.toggle_metrics(False)
                    self.facial_feature_manager.toggle_alerts(False)
                    
                print("✅ Controles de detección facial deshabilitados")
            
            # Si es la cámara secundaria 1, deshabilitar controles de detección de líneas
            elif camera_id == 'secondary1':
                if hasattr(self, 'lane_detection_check'):
                    self.lane_detection_check.config(state='disabled')
                    self.lane_detection_var.set(False)
                if hasattr(self, 'lane_metrics_check'):
                    self.lane_metrics_check.config(state='disabled')
                    self.lane_metrics_var.set(True)
                if hasattr(self, 'lane_predictions_check'):
                    self.lane_predictions_check.config(state='disabled')
                    self.lane_predictions_var.set(True)
                
                    
                print("✅ Controles de detección de líneas deshabilitados")
            
            # Cámara secundaria 2 - sin controles especiales
            elif camera_id == 'secondary2':
                print("✅ Cámara secundaria 2 detenida")
                        
            # Actualizar botón de detener todas
            active_cameras = sum(1 for c in self.multi_camera_manager.cameras.values() if c['active'])
            if active_cameras == 0 and hasattr(self, 'stop_all_cameras_btn'):
                self.stop_all_cameras_btn.config(state='disabled')
                
            print(f"Cámara {camera_id} detenida")
            
        except Exception as e:
            print(f"Error al detener la cámara {camera_id}: {e}")
            import traceback
            traceback.print_exc()



    def start_all_cameras(self):
        """Inicia todas las cámaras disponibles"""
        try:
            # Verificar que existe el manager de cámaras
            if not hasattr(self, 'multi_camera_manager'):
                messagebox.showerror("Error", "Sistema de cámaras no inicializado")
                return
                
            # Intentar iniciar cada cámara
            cameras_started = 0
            for camera_id in ['main', 'secondary1', 'secondary2']:
                # Obtener el índice de la cámara seleccionada
                var_name = f"{camera_id}_camera_var"
                selected_camera = getattr(self, var_name).get()
                
                # Buscar índice de cámara
                camera_idx = None
                for idx, name in self.available_cameras.items():
                    if name == selected_camera:
                        camera_idx = idx
                        break
                        
                # Si no hay una cámara disponible para este slot, continuar
                if camera_idx is None or selected_camera == 'No hay cámaras disponibles':
                    continue
                    
                # Iniciar la cámara
                if not self.multi_camera_manager.cameras[camera_id]['active']:
                    success = self.multi_camera_manager.start_camera(camera_id, camera_idx)
                    
                    if success:
                        cameras_started += 1
                        
                        # Actualizar estado de botones
                        start_btn_name = f"start_{camera_id}_btn"
                        stop_btn_name = f"stop_{camera_id}_btn"
                        combo_name = f"{camera_id}_combo"
                        
                        if hasattr(self, start_btn_name):
                            getattr(self, start_btn_name).config(state='disabled')
                        if hasattr(self, stop_btn_name):
                            getattr(self, stop_btn_name).config(state='normal')
                        if hasattr(self, combo_name):
                            getattr(self, combo_name).config(state='disabled')
                            
            # Habilitar detección facial solo si se inició la cámara principal
            if self.multi_camera_manager.cameras['main']['active'] and hasattr(self, 'face_detection_check'):
                self.face_detection_check.config(state='normal')
                
            # Actualizar botón de detener todas
            if cameras_started > 0:
                self.stop_all_cameras_btn.config(state='normal')
                self.start_all_cameras_btn.config(state='disabled')
                
            if cameras_started == 0:
                messagebox.showinfo("Información", "No se inició ninguna cámara")
            else:
                print(f"Se iniciaron {cameras_started} cámaras correctamente")
                
        except Exception as e:
            print(f"Error al iniciar todas las cámaras: {e}")
            messagebox.showerror("Error", f"Error al iniciar las cámaras: {e}")

    

    def stop_all_cameras(self):
        """Detiene todas las cámaras activas"""
        try:
            # Verificar que existe el manager de cámaras
            if not hasattr(self, 'multi_camera_manager'):
                return
                
            # Detener todas las cámaras
            for camera_id in ['main', 'secondary1', 'secondary2']:
                if self.multi_camera_manager.cameras[camera_id]['active']:
                    self.multi_camera_manager.stop_camera(camera_id)
                    
                    # Actualizar estado de botones
                    start_btn_name = f"start_{camera_id}_btn"
                    stop_btn_name = f"stop_{camera_id}_btn"
                    combo_name = f"{camera_id}_combo"
                    
                    if hasattr(self, start_btn_name):
                        getattr(self, start_btn_name).config(state='normal')
                    if hasattr(self, stop_btn_name):
                        getattr(self, stop_btn_name).config(state='disabled')
                    if hasattr(self, combo_name):
                        getattr(self, combo_name).config(state='readonly')
            
            # Deshabilitar controles de detección facial
            if hasattr(self, 'face_detection_check'):
                self.face_detection_check.config(state='disabled')
                self.face_detection_var.set(False)
            if hasattr(self, 'landmarks_check'):
                self.landmarks_check.config(state='disabled')
                self.landmarks_var.set(False)
            if hasattr(self, 'hands_check'):
                self.hands_check.config(state='disabled')
                self.hands_var.set(False)
            if hasattr(self, 'metrics_check'):
                self.metrics_check.config(state='disabled')
                self.metrics_var.set(False)
            if hasattr(self, 'alerts_check'):
                self.alerts_check.config(state='disabled')
                self.alerts_var.set(False)
                
            # Actualizar estado interno del gestor facial si existe
            if hasattr(self, 'facial_feature_manager'):
                self.facial_feature_manager.toggle_face_detection(False)
                self.facial_feature_manager.toggle_landmarks(False)
                self.facial_feature_manager.toggle_hands(False)
                self.facial_feature_manager.toggle_metrics(False)
                self.facial_feature_manager.toggle_alerts(False)
                
            # Actualizar botones generales
            self.stop_all_cameras_btn.config(state='disabled')
            self.start_all_cameras_btn.config(state='normal')
            
            print("Todas las cámaras detenidas")
            
        except Exception as e:
            print(f"Error al detener todas las cámaras: {e}")

    # ===== Nuevos métodos de control deteccion de lineas  =====
    def toggle_face_detection(self):
        """Activa o desactiva la detección facial (corre en el worker de 'main')"""
        enabled = self.face_detection_var.get()

        # Habilitar/deshabilitar checkboxes dependientes
        if enabled:
            self.landmarks_check.config(state='normal')
            self.hands_check.config(state='normal')
            self.metrics_check.config(state='normal')
            self.alerts_check.config(state='normal')
        else:
            self.landmarks_check.config(state='disabled')
            self.hands_check.config(state='disabled')
            self.metrics_check.config(state='disabled')
            self.alerts_check.config(state='disabled')
            self.landmarks_var.set(False)
            self.hands_var.set(False)
            self.metrics_var.set(False)
            self.alerts_var.set(False)

        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_face_detection('main', enabled)
            # sincronizar sub-opciones actuales (por si ya estaban tildadas)
            self.multi_camera_manager.set_face_option('main', 'landmarks', self.landmarks_var.get())
            self.multi_camera_manager.set_face_option('main', 'hands', self.hands_var.get())
            self.multi_camera_manager.set_face_option('main', 'metrics', self.metrics_var.get())
            self.multi_camera_manager.set_face_option('main', 'alerts', self.alerts_var.get())

        status = "activada" if enabled else "desactivada"
        print(f"✅ Detección facial {status}")


    def toggle_landmarks(self):
        """Activa o desactiva la visualización de landmarks faciales"""
        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_face_option('main', 'landmarks', self.landmarks_var.get())

    def toggle_hands(self):
        """Activa o desactiva la detección de manos"""
        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_face_option('main', 'hands', self.hands_var.get())

    def toggle_metrics(self):
        """Activa o desactiva la visualización de métricas"""
        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_face_option('main', 'metrics', self.metrics_var.get())

    def toggle_alerts(self):
        """Activa o desactiva las alertas visuales"""
        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_face_option('main', 'alerts', self.alerts_var.get())
            
    # ===== Nuevos métodos de control =====
    #  Agregar método para inicializar archivos de métricas de video
    def initialize_video_metrics_files(self, video_folder, timestamp):
        """
        Inicializa archivos CSV SOLO para las detecciones activas.
        """
        try:
            participant_name = self.validated_name.get().strip()
            modo = self.test_mode.get()

            # Verificar qué detecciones están activas
            face_active = (
                hasattr(self, 'face_detection_var') and
                self.face_detection_var.get()
            )
            lane_active = (
                hasattr(self, 'lane_detection_var') and
                self.lane_detection_var.get()
            )
            any_camera_active = (
                hasattr(self, 'multi_camera_manager') and
                any(c['active'] for c in self.multi_camera_manager.cameras.values())
            )

            # Solo iniciar el buffer si hay algo que guardar
            if not any_camera_active:
                print("⚠️ Sin cámaras activas, no se crean archivos de métricas de video.")
                return False

            self.metrics_buffer.start()

            # ── Métricas faciales — solo si detección facial activa ──────
            if face_active:
                facial_metrics_file = os.path.join(
                    video_folder,
                    f"{participant_name}_{modo}_facial_metrics_{timestamp}.csv"
                )
                facial_headers = [
                    'Timestamp', 'EpochTime', 'Camera_ID', 'Face_Detected',
                    'Blink_Count', 'Yawn_Count', 'Blink_Rate',
                    'Attention_Level', 'Head_Direction', 'Head_Yaw',
                    'Head_Pitch', 'Head_Roll', 'Drowsy', 'Yawning',
                    'EAR_Value', 'MAR_Value', 'Mouth_Area', 'Is_Speaking',
                    'Fatigue_Level', 'Blinks_Last_5min', 'Yawns_Last_5min',
                    'Is_Calibrated', 'Pose_Calibrated'
                ]
                self.metrics_buffer.register_metric(
                    'facial', facial_metrics_file, facial_headers
                )
                self.video_metrics_files['facial'] = facial_metrics_file
                print(f"✅ Métricas faciales inicializadas: {os.path.basename(facial_metrics_file)}")
            else:
                print("ℹ️ Detección facial inactiva — no se crea facial_metrics.csv")

            # ── Métricas de carril — solo si detección de líneas activa ──
            if lane_active:
                lane_metrics_file = os.path.join(
                    video_folder,
                    f"{participant_name}_{modo}_lane_metrics_{timestamp}.csv"
                )
                lane_headers = [
                    'Timestamp', 'EpochTime', 'Camera_ID',
                    'Lane_State', 'Left_Lines_Count', 'Right_Lines_Count',
                    'Center_Crossings', 'Min_Left_Dist_px', 'Min_Right_Dist_px',
                    'Lane_Change_Active', 'Lane_Change_Direction',
                    'Lane_Departure_Warning', 'Scenario_Label'
                ]
                self.metrics_buffer.register_metric(
                    'lane', lane_metrics_file, lane_headers
                )
                self.video_metrics_files['lane'] = lane_metrics_file
                print(f"✅ Métricas de carril inicializadas: {os.path.basename(lane_metrics_file)}")
            else:
                print("ℹ️ Detección de líneas inactiva — no se crea lane_metrics.csv")

            # ── Video info — solo si hay cámaras activas ─────────────────
            video_info_file = os.path.join(
                video_folder,
                f"{participant_name}_{modo}_video_info_{timestamp}.csv"
            )
            video_info_headers = [
                'Camera_ID', 'Camera_Name', 'Start_Time', 'End_Time',
                'Duration_Seconds', 'Resolution_Width', 'Resolution_Height',
                'Target_FPS', 'Recorded_Frames', 'Actual_FPS',
                'File_Path', 'File_Size_MB', 'Flip_X', 'Flip_Y',
                'Face_Detection_Active', 'Lane_Detection_Active'
            ]
            with open(video_info_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(video_info_headers)
            self.video_metrics_files['info'] = video_info_file

            # Inicializar timestamps de control
            self.last_metric_save = {
                'facial': time.time(),
                'lane':   time.time()
            }

            print(
                f"✅ Sistema de métricas inicializado | "
                f"facial={'sí' if face_active else 'no'} | "
                f"carril={'sí' if lane_active else 'no'}"
            )
            return True

        except Exception as e:
            print(f"❌ Error al inicializar archivos de métricas: {e}")
            import traceback
            traceback.print_exc()
            return False
        
    # Agregar método para guardar métricas faciales
    def save_facial_metrics(self):
        """
        Guarda las métricas faciales actuales usando el buffer.
        Las métricas ahora vienen del worker de la cámara 'main' via
        multi_camera_manager.get_stats() (ver stats_queue en el worker).
        """
        if not self.is_recording:
            return
        if not hasattr(self, 'metrics_buffer') or not self.metrics_buffer.is_active:
            return
        if not hasattr(self, 'multi_camera_manager'):
            return

        try:
            current_time = time.time()
            if current_time - self.last_metric_save.get('facial', 0) < 0.05:
                return
            self.last_metric_save['facial'] = current_time

            stats = self.multi_camera_manager.get_stats('main')
            face = stats.get('face') or {}
            if not face:
                return  # detección no activa / aún sin primer frame procesado

            formatted_time, epoch_time = self.get_synchronized_timestamp()

            face_detected = face.get('face_detected', False)
            blink_count = face.get('blink_count', 0)
            yawn_count = face.get('yawn_count', 0)
            blink_rate = face.get('blink_rate', 0.0)
            attention = face.get('attention', 'Normal')
            head_direction = face.get('head_direction', 'Centro')
            head_yaw = face.get('head_yaw', 0.0)
            head_pitch = face.get('head_pitch', 0.0)
            head_roll = face.get('head_roll', 0.0)
            drowsy = face.get('drowsy', False)
            yawning = face.get('yawning', False)
            ear_value = face.get('ear', 0.0)
            mar_value = face.get('mar', 0.0)
            mouth_area = face.get('mouth_area', 0.0)
            is_speaking = face.get('is_speaking', False)
            is_calibrated = face.get('is_calibrated', False)
            pose_calibrated = face.get('pose_calibrated', False)

            # Nivel de fatiga (misma heurística que antes, pero basada en blink_rate)
            fatigue_level = "Normal"
            if blink_rate > 30:
                fatigue_level = "Alta"
            elif blink_rate > 20:
                fatigue_level = "Moderada"
            elif yawn_count > 2:
                fatigue_level = "Leve"

            row_data = [
                formatted_time,
                epoch_time,
                'main',
                int(face_detected),
                blink_count,
                yawn_count,
                f"{blink_rate:.2f}",
                attention,
                head_direction,
                f"{head_yaw:.2f}",
                f"{head_pitch:.2f}",
                f"{head_roll:.2f}",
                int(drowsy),
                int(yawning),
                f"{ear_value:.6f}",
                f"{mar_value:.6f}",
                f"{mouth_area:.6f}",
                int(is_speaking),
                fatigue_level,
                blink_count,
                yawn_count,
                int(is_calibrated),
                int(pose_calibrated),
            ]
            self.metrics_buffer.add_row('facial', row_data)

        except Exception:
            pass


    # Agregar método para guardar métricas de líneas
    def save_lane_metrics(self):
        """
        Guarda las métricas de detección de carril (ROI rectangular +
        línea central de referencia) usando el buffer. Las métricas vienen
        del worker de 'secondary1' vía multi_camera_manager.get_stats()
        (stats_queue -> handle.stats['lane'], ver _LaneProcessor.get_current_metrics()).
        """
        if not self.is_recording:
            return
        if not hasattr(self, 'metrics_buffer') or not self.metrics_buffer.is_active:
            return
        if not hasattr(self, 'multi_camera_manager'):
            return
    
        try:
            current_time = time.time()
            if current_time - self.last_metric_save.get('lane', 0) < 0.05:
                return
            self.last_metric_save['lane'] = current_time
    
            stats = self.multi_camera_manager.get_stats('secondary1')
            lane = stats.get('lane') or {}
            if not lane:
                return  # detección no activa / aún sin primer frame procesado
    
            formatted_time, epoch_time = self.get_synchronized_timestamp()
    
            min_left = lane.get('min_left_dist')
            min_right = lane.get('min_right_dist')
    
            row_data = [
                formatted_time,
                epoch_time,
                'secondary1',
                lane.get('state', 'SIN DETECCION'),
                lane.get('left_lines', 0),
                lane.get('right_lines', 0),
                lane.get('center_crossings', 0),
                f"{min_left:.1f}" if min_left is not None else '',
                f"{min_right:.1f}" if min_right is not None else '',
                int(lane.get('lane_change_active', False)),
                lane.get('lane_change_direction') or '',
                int(lane.get('departure_warning', False)),
                lane.get('scenario', ''),
            ]
            self.metrics_buffer.add_row('lane', row_data)
    
        except Exception:
            pass

    # Agregar método para guardar información de videos
    def save_video_info(self):
        """
        Guarda información detallada de todos los videos grabados
        """
        if 'info' not in self.video_metrics_files:
            return
            
        try:
            if not hasattr(self, 'multi_camera_manager'):
                return
                
            with open(self.video_metrics_files['info'], 'a', newline='') as f:
                writer = csv.writer(f)
                
                for camera_id, camera_info in self.multi_camera_manager.cameras.items():
                    if camera_info.get('path') and os.path.exists(camera_info['path']):
                        # Calcular duración y FPS real
                        duration = 0
                        actual_fps = 0
                        
                        if 'recording_start_time' in camera_info:
                            duration = time.time() - camera_info['recording_start_time']
                            
                        if 'recorded_frames_count' in camera_info and duration > 0:
                            actual_fps = camera_info['recorded_frames_count'] / duration
                        
                        # Obtener tamaño del archivo
                        file_size_mb = os.path.getsize(camera_info['path']) / (1024 * 1024)
                        
                        # Obtener resolución
                        width = int(camera_info['cap'].get(cv2.CAP_PROP_FRAME_WIDTH)) if camera_info['cap'] else 0
                        height = int(camera_info['cap'].get(cv2.CAP_PROP_FRAME_HEIGHT)) if camera_info['cap'] else 0
                        
                        # Nombre de cámara
                        camera_name = "Principal" if camera_id == "main" else f"Secundaria"
                        
                        # Determinar qué detecciones están activas
                        face_detection = (camera_id == 'main' and 
                                        self.face_detection_var.get() if hasattr(self, 'face_detection_var') else False)
                        lane_detection = (camera_id == 'secondary1' and 
                                        self.lane_detection_var.get() if hasattr(self, 'lane_detection_var') else False)
                        
                        writer.writerow([
                            camera_id,
                            camera_name,
                            datetime.fromtimestamp(camera_info.get('recording_start_time', 0)).strftime('%Y-%m-%d %H:%M:%S.%f')[:-2],
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-2],
                            f"{duration:.6f}",
                            width,
                            height,
                            camera_info.get('recording_fps', 30),
                            camera_info.get('recorded_frames_count', 0),
                            f"{actual_fps:.2f}",
                            camera_info['path'],
                            f"{file_size_mb:.2f}",
                            int(camera_info.get('flip_x', False)),
                            int(camera_info.get('flip_y', False)),
                            int(face_detection),
                            int(lane_detection)
                        ])
                        
        except Exception as e:
            print(f"Error al guardar información de videos: {e}")

    # Agregar método para guardar métricas periódicamente
    def save_metrics_periodically(self):
        """
        Guarda métricas de video periódicamente durante la grabación.
        - Solo guarda si la detección está activa Y registrada en el buffer
        - Loop robusto: no se rompe si hay excepción en save_*
        """
        if not self.is_recording:
            return

        try:
            # ── Métricas faciales ─────────────────────────────────────
            face_activa = (
                hasattr(self, 'face_detection_var') and
                self.face_detection_var.get() and
                hasattr(self, 'metrics_buffer') and
                self.metrics_buffer.is_active and
                'facial' in self.metrics_buffer.buffers
            )
            if face_activa:
                self.save_facial_metrics()

            # ── Métricas de carril ────────────────────────────────────
            lane_activa = (
                hasattr(self, 'lane_detection_var') and
                self.lane_detection_var.get() and
                hasattr(self, 'metrics_buffer') and
                self.metrics_buffer.is_active and
                'lane' in self.metrics_buffer.buffers
            )
            if lane_activa:
                self.save_lane_metrics()

        except Exception as e:
            print(f"Error en save_metrics_periodically: {e}")

        finally:
            # Siempre reprogramar si seguimos grabando,
            # incluso si hubo excepción arriba
            if self.is_recording:
                self.window.after(50, self.save_metrics_periodically)

    #Agregar método para generar resumen de métricas
    def generate_metrics_summary(self):
        """
        Genera un archivo de resumen con estadísticas de las métricas
        """
        if not hasattr(self, 'video_metrics_files') or not self.video_metrics_files:
            return
            
        try:
            participant_name = self.validated_name.get().strip()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            modo = self.test_mode.get()
            summary_file = os.path.join(
                self.video_folder,
                f"{participant_name}_{modo}_metrics_summary_{timestamp}.txt"
            )
            
            with open(summary_file, 'w', encoding='utf-8') as f:
                f.write("=" * 60 + "\n")
                f.write("RESUMEN DE MÉTRICAS DE VIDEO\n")
                f.write("=" * 60 + "\n\n")
                
                # Resumen de métricas faciales
                if 'facial' in self.video_metrics_files and os.path.exists(self.video_metrics_files['facial']):
                    f.write("\n--- MÉTRICAS FACIALES ---\n")
                    df_facial = pd.read_csv(self.video_metrics_files['facial'])
                    
                    if not df_facial.empty:
                        f.write(f"Total de registros: {len(df_facial)}\n")
                        f.write(f"Parpadeos totales: {df_facial['Blink_Count'].iloc[-1]}\n")
                        f.write(f"Bostezos totales: {df_facial['Yawn_Count'].iloc[-1]}\n")
                        f.write(f"Tasa promedio de parpadeo: {df_facial['Blink_Rate'].mean():.2f}/min\n")
                        f.write(f"EAR promedio: {df_facial['EAR_Value'].mean():.6f}\n")
                        f.write(f"MAR promedio: {df_facial['MAR_Value'].mean():.6f}\n")
                        
                        # Distribución de niveles de atención
                        attention_dist = df_facial['Attention_Level'].value_counts()
                        f.write("\nDistribución de niveles de atención:\n")
                        for level, count in attention_dist.items():
                            percentage = (count / len(df_facial)) * 100
                            f.write(f"  {level}: {count} ({percentage:.1f}%)\n")
                        
                        # Distribución de dirección de mirada
                        direction_dist = df_facial['Head_Direction'].value_counts()
                        f.write("\nDistribución de dirección de mirada:\n")
                        for direction, count in direction_dist.items():
                            percentage = (count / len(df_facial)) * 100
                            f.write(f"  {direction}: {count} ({percentage:.1f}%)\n")
                
                # Resumen de métricas de líneas
                if 'lane' in self.video_metrics_files and os.path.exists(self.video_metrics_files['lane']):
                    f.write("\n--- MÉTRICAS DE CARRIL (ROI + línea central) ---\n")
                    df_lane = pd.read_csv(self.video_metrics_files['lane'])
 
                    if not df_lane.empty:
                        f.write(f"Total de registros: {len(df_lane)}\n")
 
                        state_dist = df_lane['Lane_State'].value_counts()
                        f.write("\nDistribución de estados:\n")
                        for state, count in state_dist.items():
                            percentage = (count / len(df_lane)) * 100
                            f.write(f"  {state}: {count} ({percentage:.1f}%)\n")
 
                        changes = df_lane['Lane_Change_Active'].sum()
                        warnings = df_lane['Lane_Departure_Warning'].sum()
                        f.write(f"\nCambios de carril activos (frames): {changes}\n")
                        f.write(f"Alertas de salida de carril (frames): {warnings}\n")
 
                        if 'Left_Lines_Count' in df_lane.columns and 'Right_Lines_Count' in df_lane.columns:
                            f.write(f"Promedio de líneas detectadas: "
                                    f"Izq={df_lane['Left_Lines_Count'].mean():.2f}, "
                                    f"Der={df_lane['Right_Lines_Count'].mean():.2f}\n")
 
                        if 'Center_Crossings' in df_lane.columns:
                            f.write(f"Total de cruces con línea central: {int(df_lane['Center_Crossings'].sum())}\n")
 
                        if df_lane['Lane_Change_Direction'].notna().any():
                            direction_dist = df_lane['Lane_Change_Direction'].replace('', pd.NA).dropna().value_counts()
                            if not direction_dist.empty:
                                f.write("\nDistribución de dirección de cambio:\n")
                                for direction, count in direction_dist.items():
                                    f.write(f"  {direction}: {count}\n")
                # Información de videos
                if 'info' in self.video_metrics_files and os.path.exists(self.video_metrics_files['info']):
                    f.write("\n--- INFORMACIÓN DE VIDEOS ---\n")
                    df_info = pd.read_csv(self.video_metrics_files['info'])
                    
                    for _, row in df_info.iterrows():
                        f.write(f"\nCámara: {row['Camera_Name']} ({row['Camera_ID']})\n")
                        f.write(f"  Resolución: {row['Resolution_Width']}x{row['Resolution_Height']}\n")
                        f.write(f"  Duración: {row['Duration_Seconds']:.2f} segundos\n")
                        f.write(f"  FPS objetivo: {row['Target_FPS']}\n")
                        f.write(f"  FPS real: {row['Actual_FPS']}\n")
                        f.write(f"  Frames grabados: {row['Recorded_Frames']}\n")
                        f.write(f"  Tamaño: {row['File_Size_MB']} MB\n")
                        f.write(f"  Detección facial: {'Sí' if row['Face_Detection_Active'] else 'No'}\n")
                        f.write(f"  Detección de líneas: {'Sí' if row['Lane_Detection_Active'] else 'No'}\n")
                
                f.write("\n" + "=" * 60 + "\n")
                f.write(f"Reporte generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                
            print(f"Resumen de métricas guardado en: {summary_file}")
            
        except Exception as e:
            print(f"Error al generar resumen de métricas: {e}")

    def open_lidar_visualizer(self):
        """Abre la ventana de visualización LiDAR"""
        if hasattr(self, 'lidar_visualizer'):
            self.lidar_visualizer.open_vis_window()

    def toggle_lidar(self):
        # ── Guardia: no permitir cambios durante una sesión activa ──
        if hasattr(self, 'velodyne_manager') and not self.velodyne_manager.toggle_habilitado:
            # Revertir visualmente el checkbox a su estado anterior
            self.lidar_enabled.set(not self.lidar_enabled.get())
            self.show_temporary_warning("No se puede cambiar el LiDAR durante la grabación")
            return

        enabled = self.lidar_enabled.get()

        if enabled:
            self.update_status_indicator(
                self.status_indicators['lidar'],
                self.lidar_status,
                ('Iniciando...', 'yellow')
            )
            if self.velodyne_manager.iniciar_captura():
                self.lidar_status_label.config(text="🟡 Iniciando...", foreground="orange")
                self.gui_queue.put({'type': 'system_status', 'data': 'LiDAR iniciando captura'})
                self.gui_queue.put({'type': 'lidar_status', 'data': ('Iniciando...', 'yellow')})
                self.window.after(2000, self.verificar_datos_lidar)
            else:
                self.lidar_enabled.set(False)
                self.lidar_status_label.config(text="🔴 Error", foreground="red")
                self.gui_queue.put({'type': 'lidar_status', 'data': ('Error', 'red')})
        else:
            self.velodyne_manager.detener()
            self.lidar_status_label.config(text="⚪ Inactivo", foreground="gray")
            self.gui_queue.put({'type': 'lidar_status', 'data': ('Inactivo', 'gray')})
            self.gui_queue.put({'type': 'system_status', 'data': 'LiDAR desactivado'})
            
    def verificar_datos_lidar(self):
        if not self.lidar_enabled.get():
            return
        
        stats = self.velodyne_manager.obtener_estadisticas()
        
        # v5: clave renombrada a 'paquetes_grabados', sin 'puntos'
        paquetes = stats.get('paquetes_totales', 0)
        
        if self.velodyne_manager.esta_recibiendo_datos() and paquetes > 0:
            self.lidar_status_label.config(
                text=f"🟢 {paquetes:,} pkts",
                foreground="green"
            )
            self.gui_queue.put({
                'type': 'lidar_status',
                'data': ('Activo', 'green')
            })
        else:
            self.lidar_status_label.config(
                text="🟡 Sin datos...",
                foreground="orange"
            )
            self.window.after(1000, self.verificar_datos_lidar)

    def toggle_lane_detection(self):
        """Activa o desactiva la detección de líneas (corre en el worker de 'secondary1')"""
        enabled = self.lane_detection_var.get()

        if enabled:
            self.lane_metrics_check.config(state='normal')
            self.lane_predictions_check.config(state='normal')
        else:
            self.lane_metrics_check.config(state='disabled')
            self.lane_predictions_check.config(state='disabled')
            self.lane_metrics_var.set(True)
            self.lane_predictions_var.set(True)

        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_lane_detection('secondary1', enabled)
            self.multi_camera_manager.set_lane_option('secondary1', 'lane_metrics', self.lane_metrics_var.get())
            self.multi_camera_manager.set_lane_option('secondary1', 'lane_predictions', self.lane_predictions_var.get())

        status = "activada" if enabled else "desactivada"
        print(f"✅ Detección de líneas {status}")


    def toggle_lane_metrics(self):
        """Activa o desactiva la visualización de métricas de líneas"""
        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_lane_option('secondary1', 'lane_metrics', self.lane_metrics_var.get())

    def toggle_lane_predictions(self):
        """Activa o desactiva las predicciones de líneas"""
        if hasattr(self, 'multi_camera_manager'):
            self.multi_camera_manager.set_lane_option('secondary1', 'lane_predictions', self.lane_predictions_var.get())

        
    def create_status_indicator(self, parent, text):
        """Crea un indicador de estado individual"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=2)
        
        # Create colored circle indicator
        canvas = tk.Canvas(frame, width=12, height=12, 
                        background='#f0f8ff', highlightthickness=0)
        self.status_indicators[text.lower()] = canvas
        canvas.create_oval(2, 2, 10, 10, fill='gray', outline='gray')
        canvas.pack(side=tk.LEFT, padx=2)
        
        # Create label and store reference
        status_label = ttk.Label(frame, text=f"{text}: Esperando...", 
                                style='TLabel')
        status_label.pack(side=tk.LEFT)
        
        # Store references to status labels
        if text == "EEG":
            self.eeg_status = status_label
        elif text == "Serial":
            self.serial_status = status_label
        elif text == "ECG":
            self.ecg_status = status_label
        elif text == "LiDAR":  # 👈 AÑADIR ESTAS LÍNEAS
            self.lidar_status = status_label
        elif text == "BIOPAC":
            self.biopac_status = status_label    

        

    def validate_name_input(self, new_value):
        """Validación básica de caracteres permitidos"""
        if all(c.isalnum() or c in ' -_' for c in new_value):
            self.name_validation_label.config(text="")
            return True
        self.name_validation_label.config(text="Solo se permiten letras, números, espacios, guiones y guiones bajos")
        return False

    def validate_name(self, *args):
            """Validación completa del nombre y actualización del estado del botón"""
            name = self.temp_name.get().strip()
            
            if not name:
                self.update_button.config(state='disabled')
                self.name_validation_label.config(text="El nombre es requerido")
            elif len(name) < 3:
                self.update_button.config(state='disabled')
                self.name_validation_label.config(text="El nombre debe tener al menos 3 caracteres")
            elif name == self.validated_name.get():
                self.update_button.config(state='disabled')
                self.name_validation_label.config(text="")
            else:
                self.update_button.config(state='normal')
                self.name_validation_label.config(text="")

    def update_name(self):
            """Actualiza el nombre validado solo cuando se presiona el botón"""
            name = self.temp_name.get().strip()
            
            if not name:
                messagebox.showerror("Error", "El nombre no puede estar vacío")
                return
            if len(name) < 3:
                messagebox.showerror("Error", "El nombre debe tener al menos 3 caracteres")
                return
            if not all(c.isalnum() or c in ' -_' for c in name):
                messagebox.showerror("Error", "El nombre contiene caracteres no permitidos")
                return
            
            # Actualizar el nombre validado
            self.validated_name.set(name)
            self.update_button.config(state='disabled')
            self.gui_queue.put({'type': 'system_status', 'data': f'Nombre actualizado: {name}'})
            messagebox.showinfo("Éxito", f"Nombre actualizado correctamente a: {name}")

    def update_status_indicator(self, indicator, label, status_info):
        """
        Actualiza un indicador de estado con color y texto específicos
        
        Args:
            indicator: Widget Canvas para el indicador
            label: Widget Label para actualizar
            status_info: Puede ser un booleano para conectado/desconectado,
                        o una tupla de (texto_estado, color) para estado detallado
        """
        if isinstance(status_info, bool):
            # Estado simple conectado/desconectado
            color = '#1dd1a1' if status_info else '#ff6b6b'  # Verde/Rojo
            status = "Conectado" if status_info else "Desconectado"
        else:
            # Estado detallado con color personalizado
            status, color_name = status_info
            color_map = {
                'gray': '#cdcdcd',
                'yellow': '#feca57',  # Amarillo ámbar más visible}
                'orange': '#ff9f43',  # ← agregar
                'green': '#1dd1a1',
                'blue': '#4a86e8',
                'red': '#ff6b6b'
            }
            color = color_map.get(color_name, '#cdcdcd')
            
        indicator.delete("all")
        indicator.create_oval(2, 2, 13, 13, fill=color, outline=color)
        label.configure(text=f"{label.cget('text').split(':')[0]}: {status}")

    def setup_control_section(self, parent):
        """Configura la sección de controles (anclada abajo)"""
        control_frame = ttk.LabelFrame(parent,
                                    text="Controles",
                                    style='Card.TLabelframe',
                                    padding="10")
        control_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 0))

        btns = ttk.Frame(control_frame)
        btns.pack(fill=tk.X)
        btns.columnconfigure(0, weight=1, uniform="ctl")
        btns.columnconfigure(1, weight=1, uniform="ctl")

        self.start_button = ttk.Button(btns,
                                    text="▶ Iniciar",
                                    command=self.request_start,
                                    style='Green.TButton')
        self.start_button.grid(row=0, column=0, padx=(0, 3), sticky='ew')

        self.stop_button = ttk.Button(btns,
                                    text="⏹ Detener",
                                    command=self.request_stop,
                                    state='disabled',
                                    style='Red.TButton')
        self.stop_button.grid(row=0, column=1, padx=(3, 0), sticky='ew')
    
    def connect_eeg(self):
        """Inicia el proceso de conexión con el EEG con intentos automáticos"""
        # Resetear variables
        self.reconnect_attempts = 0
        self.headset_detected = False
        self.auth_token = None
        self.headset_id = None
        self.connection_in_progress = False
        
        # Deshabilitar botón mientras se hacen intentos automáticos
        if hasattr(self, 'manual_connect_button'):
            self.manual_connect_button.configure(state='disabled')
        
        # Mostrar estado inicial
        self.gui_queue.put({'type': 'eeg_status', 'data': 'Desconectado'})
        
        # Iniciar primer intento de conexión
        self.attempt_connection()
    
    def attempt_connection(self):
        """Realiza un intento de conexión al EEG"""
        # Si ya hicimos el máximo de intentos, habilitar el botón manual
        if self.reconnect_attempts >= 3:  # MAX_AUTO_RECONNECT_ATTEMPTS = 3
            self.enable_manual_button()
            return
        
        # Incrementar contador e iniciar intento
        self.reconnect_attempts += 1
        self.connection_in_progress = True
        
        # Actualizar estado
        self.gui_queue.put({'type': 'eeg_status', 
                        'data': f'Conectando... (Intento {self.reconnect_attempts}/3)'})
        
        # Cerrar conexión anterior si existe
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None
        
        # Reiniciar variables de estado
        self.auth_token = None
        self.headset_id = None
        self.headset_detected = False
        
        try:
            # Iniciar nueva conexión
            self.ws = websocket.WebSocketApp(
                CORTEX_URL,
                on_message=self.on_eeg_message,
                on_error=self.on_eeg_error,
                on_open=self.on_eeg_open
            )
            
            # Ejecutar websocket en un hilo separado
            ws_thread = threading.Thread(target=self.ws.run_forever)
            ws_thread.daemon = True
            ws_thread.start()
            
            # Programar verificación del resultado después de un tiempo
            self.window.after(6000, self.check_connection_result)
            
        except Exception as e:
            print(f"Error al iniciar conexión: {e}")
            self.connection_failed()

    def check_connection_result(self):
        """Verifica el resultado del intento de conexión actual"""
        # Si no se detectó ningún headset físico
        if not self.headset_detected:
            self.connection_failed()

    def connection_failed(self):
        """Maneja un intento de conexión fallido"""
        self.connection_in_progress = False
        
        # Si todavía no alcanzamos el máximo de intentos, intentar de nuevo
        if self.reconnect_attempts < 3:
            self.window.after(1000, self.attempt_connection)
        else:
            # Si ya hicimos todos los intentos, habilitar el botón manual
            self.enable_manual_button()

    def enable_manual_button(self):
        """Habilita el botón de conexión manual después de que fallan los intentos automáticos"""
        self.connection_in_progress = False
        self.gui_queue.put({'type': 'eeg_status', 'data': 'Desconectado - Conexión manual requerida'})
        self.gui_queue.put({'type': 'system_status', 'data': 'Esperando conexión manual'})
        if hasattr(self, 'manual_connect_button'):
            self.window.after(0, lambda: self.manual_connect_button.configure(state='normal'))

    def manual_connect_eeg(self):
        """Maneja el evento de clic en el botón de conexión manual"""
        # Deshabilitar el botón durante el intento
        self.manual_connect_button.configure(state='disabled')
        
        # Reiniciar contadores
        self.reconnect_attempts = 0
        
        # Mostrar mensaje
        messagebox.showinfo("Conexión EEG", "Intentando conectar al EEG manualmente...")
        
        # Iniciar intentos de conexión
        self.attempt_connection()
    
    def seleccionar_dispositivo_eeg(self, headsets):
        """Selecciona dispositivo con prioridad al FLEX"""
        conectados = [
            h for h in headsets 
            if h.get('status', '').lower() == 'connected'
            and h.get('dongle', '') != ''          # ← tiene dongle físico
            and h.get('firmware', '') != ''        # ← tiene firmware respondiendo
        ]
        descubiertos = [
            h for h in headsets 
            if h.get('status', '').lower() == 'discovered'
        ]
        
        # Prioridad 1: FLEX conectado
        for headset in conectados:
            if 'FLEX' in headset.get('id', '').upper():
                return headset, True
        
        # Prioridad 2: EPOC conectado
        for headset in conectados:
            if 'EPOC' in headset.get('id', '').upper():
                return headset, True
        
        # Prioridad 3: Cualquier conectado
        if conectados:
            return conectados[0], True
        
        # Prioridad 4: FLEX descubierto
        for headset in descubiertos:
            if 'FLEX' in headset.get('id', '').upper():
                return headset, False
        
        # Prioridad 5: Cualquier descubierto
        if descubiertos:
            return descubiertos[0], False
        
        return None, False

    def crear_sesion_eeg(self):
        """Crea sesión EEG"""
        if self.auth_token and self.headset_id:
            self.send_eeg_request({
                "jsonrpc": "2.0",
                "method": "createSession",
                "params": {
                    "cortexToken": self.auth_token,
                    "headset": self.headset_id,
                    "status": "active"
                },
                "id": 4
            })
            
    def on_eeg_message(self, ws, message):
        try:
            response = json.loads(message)
            
            # Autorización exitosa
            if "result" in response and "cortexToken" in response["result"]:
                self.auth_token = response["result"]["cortexToken"]
                self.gui_queue.put({'type': 'eeg_status', 'data': 'Autorizado - Buscando dispositivo'})
                self.send_eeg_request({"jsonrpc": "2.0", "method": "queryHeadsets", "id": 2})
            
            # queryHeadsets
            elif "result" in response and isinstance(response["result"], list):
                self.last_device_check = time.time()
                
                if response["result"] and len(response["result"]) > 0:
                    dispositivo_seleccionado, ya_conectado = self.seleccionar_dispositivo_eeg(
                        response["result"]
                    )
                    
                    if dispositivo_seleccionado:
                        self.headset_detected = True
                        
                        if not self.headset_id or self.headset_id != dispositivo_seleccionado['id']:
                            self.headset_id = dispositivo_seleccionado['id']
                            dispositivo_tipo = "FLEX" if "FLEX" in self.headset_id else "EPOC"
                            
                            if ya_conectado:
                                print(
                                    f"[EEG] Headset: {self.headset_id} | "
                                    f"battery={dispositivo_seleccionado.get('battery', '?')}% "
                                    f"| firmware={dispositivo_seleccionado.get('firmware', '?')}",
                                    flush=True
                                )
                                # ← "Detectado", NO "Conectado"
                                self.gui_queue.put({
                                    'type': 'eeg_status',
                                    'data': f'Detectado: {dispositivo_tipo}'
                                })
                                self.window.after(500, self.crear_sesion_eeg)
                            else:
                                self.gui_queue.put({
                                    'type': 'eeg_status',
                                    'data': f'Conectando: {dispositivo_tipo}...'
                                })
                                self.send_eeg_request({
                                    "jsonrpc": "2.0",
                                    "method": "controlDevice",
                                    "params": {"command": "connect", "headset": self.headset_id},
                                    "id": 3
                                })
                    else:
                        self.connection_failed()
                else:
                    if self.headset_detected:
                        self.headset_detected = False
                        if self.is_recording:
                            self.gui_queue.put({'type': 'eeg_status', 'data': 'DESCONECTADO - Grabación comprometida'})
                            messagebox.showwarning("Advertencia", "El dispositivo EEG se ha desconectado.")
                        else:
                            self.gui_queue.put({'type': 'eeg_status', 'data': 'Desconectado'})
                            self.handle_disconnection()
                    else:
                        self.connection_failed()
            
            # controlDevice — solo avanzar, NO marcar conectado
            elif "id" in response and response["id"] == 3:
                if "error" not in response:
                    # Comando aceptado → crear sesión, pero aún no estamos "Conectados"
                    self.gui_queue.put({'type': 'eeg_status', 'data': 'Enlazando dispositivo...'})
                    self.window.after(500, self.crear_sesion_eeg)
                else:
                    error = response.get('error', {})
                    if error.get('code') == -32602:
                        # Ya estaba conectado → crear sesión igualmente
                        self.gui_queue.put({'type': 'eeg_status', 'data': 'Enlazando dispositivo...'})
                        self.window.after(500, self.crear_sesion_eeg)
                    else:
                        self.connection_failed()
            
            # createSession ← ÚNICO lugar donde se pone "Conectado"
            elif "id" in response and response["id"] == 4 and "result" in response:
                self.session_id = response["result"]["id"]
                
                # ✅ Aquí sí hay sesión real confirmada por Cortex
                self.gui_queue.put({'type': 'eeg_status', 'data': 'Conectado'})
                if hasattr(self, 'manual_connect_button'):
                    self.manual_connect_button.configure(state='disabled')
                
                self.send_eeg_request({
                    "jsonrpc": "2.0",
                    "method": "subscribe",
                    "params": {
                        "cortexToken": self.auth_token,
                        "session": self.session_id,
                        "streams": ["eeg", "mot", "pow", "fac", "com", "eq", "met", "dev"]
                    },
                    "id": 5
                })
            
            # Datos de streams (la sesión Cortex sigue viva entre grabaciones)
            elif any(stream in response for stream in
                     ["eeg", "mot", "pow", "fac", "com", "eq", "met", "dev"]):
                cortex_time = response.get("time")
                grabando = self.is_recording or self._closing_session

                for signal_type in ["eeg", "mot", "pow", "fac", "com", "eq", "met", "dev"]:
                    if signal_type not in response:
                        continue

                    # Guardar solo durante la grabación (o la cola de cierre)
                    if grabando:
                        self.save_eeg_data(signal_type, response[signal_type], cortex_time)

                    # POW se visualiza siempre, también entre grabaciones
                    if signal_type == "pow" and hasattr(self, 'pow_visualizer'):
                        self.pow_visualizer.add_pow_data(response[signal_type])
        
        except Exception as e:
            print(f"Error en on_eeg_message: {e}")
            # Solo reconectar si es un error de protocolo WebSocket, 
            # NO si es un error de escritura en disco
            if "websocket" in str(type(e).__name__).lower():
                self.connection_failed()
            # Si es otro error, loguear y seguir

    def _close_eeg_files(self):
        """Cierra los CSV EEG sin tocar el WebSocket ni la sesión Cortex."""
        with self._eeg_lock:
            handles = self._eeg_file_handles
            self._eeg_file_handles = {}
            self._eeg_csv_writers  = {}      # desde aquí save_eeg_data ya no escribe

        print("💾 Cerrando archivos EEG (sesión Cortex sigue activa)...")
        for signal_type, fh in handles.items():
            try:
                if not fh.closed:
                    fh.flush()
                    os.fsync(fh.fileno())
                    fh.close()
            except Exception as e:
                print(f"   ⚠️ Error cerrando {signal_type}: {e}")

        total = 0
        for signal_type, filename in self.eeg_csv_files.items():
            try:
                if os.path.exists(filename):
                    with open(filename, 'r') as f:
                        n = sum(1 for _ in f) - 1
                    kb = os.path.getsize(filename) / 1024
                    print(f"   ✅ {signal_type:6s}: {n:6d} registros, {kb:8.1f} KB")
                    total += n
            except Exception as e:
                print(f"   ⚠️ {signal_type}: {e}")
        print(f"   📊 Total registros EEG: {total}")

    def on_eeg_error(self, ws, error):
        """Manejador de errores de conexión WebSocket"""
        # Si ya estábamos conectados, podría ser una desconexión
        if self.headset_detected:
            self.gui_queue.put({'type': 'eeg_status', 'data': 'Error de conexión - Posible desconexión'})
            
            # Verificar dispositivo para confirmar
            self.window.after(1000, lambda: self.send_eeg_request({"jsonrpc": "2.0", "method": "queryHeadsets", "id": 2}))
        else:
            # Error durante el intento inicial de conexión
            self.gui_queue.put({'type': 'eeg_status', 'data': 'Error de conexión'})
            self.connection_failed()

    def handle_disconnection(self):
        """
        Maneja una desconexión inesperada del dispositivo EEG
        """
        # Reiniciar variables de estado para permitir reconexión
        self.headset_id = None
        
        # Actualizar interfaz para mostrar estado actual
        self.gui_queue.put({'type': 'system_status', 'data': 'Dispositivo desconectado'})
        
        # Habilitar botón de reconexión manual
        if hasattr(self, 'manual_connect_button'):
            self.window.after(0, lambda: self.manual_connect_button.configure(state='normal'))
            
    def on_eeg_open(self, ws):
        self.send_eeg_request({
            "jsonrpc": "2.0",
            "method": "requestAccess",
            "params": {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET},
            "id": 0
        })
        
        self.send_eeg_request({
            "jsonrpc": "2.0",
            "method": "authorize",
            "params": {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET, "debit": 1},
            "id": 1
        })

    def send_eeg_request(self, request):
        try:
            if self.ws and self.ws.sock and self.ws.sock.connected:
                self.ws.send(json.dumps(request))
        except Exception as e:
            print(f"Error en send_eeg_request: {e}")

    def get_device_info(self):
        """
        Obtiene información del dispositivo conectado
        
        Returns:
            dict: Información del dispositivo con keys 'type', 'channels', 'channel_count'
        """
        if hasattr(self, 'headset_id') and self.headset_id:
            if 'FLEX' in self.headset_id.upper():
                return {
                    'type': 'FLEX2',
                    'channels': [
                        "LH", "RH", "LG", "RG", "BF", "LK", "RK", "RC", "LC",
                        "LB", "LA", "RA", "RB", "RP", "LP", "RN", "LN", "RL",
                        "LL", "RJ", "LJ", "RM", "LM", "RQ", "LQ", "RO", "LO",
                        "RG", "LG", "RL", "LL", "CMS"
                    ],
                    'channel_count': 32
                }
            elif 'EPOC' in self.headset_id.upper():
                return {
                    'type': 'EPOC',
                    'channels': [
                        "AF3", "F7", "F3", "FC5", "T7", "P7", "O1", 
                        "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"
                    ],
                    'channel_count': 14
                }
        
        # Default a EPOC si no se detecta
        return {
            'type': 'UNKNOWN',
            'channels': [
                "AF3", "F7", "F3", "FC5", "T7", "P7", "O1", 
                "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"
            ],
            'channel_count': 14
        }

    def log_device_info(self):
        """Registra información del dispositivo detectado en el log"""
        device_info = self.get_device_info()
        
        print("\n" + "="*60)
        print("📱 INFORMACIÓN DEL DISPOSITIVO EEG")
        print("="*60)
        print(f"Tipo: {device_info['type']}")
        print(f"ID: {self.headset_id if hasattr(self, 'headset_id') else 'No detectado'}")
        print(f"Canales: {device_info['channel_count']}")
        print(f"Lista de canales: {', '.join(device_info['channels'][:5])}... (mostrando primeros 5)")
        print("="*60 + "\n")
        
    def get_signal_headers(self, signal_type):
        """
        Genera los headers para archivos CSV según el tipo de señal y dispositivo detectado
        Soporta EPOC (14 canales) y FLEX2 (32 canales)
        """
        headers = ["Timestamp"]
        
        # Determinar canales según el dispositivo conectado
        if hasattr(self, 'headset_id') and self.headset_id:
            if 'FLEX' in self.headset_id.upper():
                # FLEX2 - 32 canales
                channels = [
                    "LH", "RH", "LG", "RG", "BF", "LK", "RK", "RC", "LC",
                    "LB", "LA", "RA", "RB", "RP", "LP", "RN", "LN", "RL",
                    "LL", "RJ", "LJ", "RM", "LM", "RQ", "LQ", "RO", "LO",
                    "RG", "LG", "RL", "LL", "CMS"
                ]
            else:
                # EPOC - 14 canales (default)
                channels = [
                    "AF3", "F7", "F3", "FC5", "T7", "P7", "O1", 
                    "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"
                ]
        else:
            # Si no se ha detectado el dispositivo, usar EPOC por defecto
            channels = [
                "AF3", "F7", "F3", "FC5", "T7", "P7", "O1", 
                "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"
            ]
        
        if signal_type == "eeg":
            headers.extend([
                "COUNTER", 
                "INTERPOLATED"
            ])
            # Añadir canales EEG
            headers.extend(channels)
            headers.extend([
                "RAW_CQ", 
                "MARKER_HARDWARE"
            ])
        
        elif signal_type == "mot":
            headers.extend([
                "COUNTER_MEMS", 
                "INTERPOLATED_MEMS",
                "Q0", "Q1", "Q2", "Q3",
                "ACCX", "ACCY", "ACCZ",
                "MAGX", "MAGY", "MAGZ"
            ])
        
        elif signal_type == "pow":
            # Bandas de frecuencia
            bands = ["theta", "alpha", "betaL", "betaH", "gamma"]
            
            # Generar headers: Canal/Banda
            for channel in channels:
                for band in bands:
                    headers.append(f"{channel}/{band}")
        
        elif signal_type == "fac":
            headers.extend([
                "eyeAct", 
                "uAct", 
                "uPow", 
                "lAct", 
                "lPow"
            ])
        
        elif signal_type == "com":
            headers.extend([
                "act", 
                "pow"
            ])
        
        elif signal_type == "eq":
            headers.extend([
                "batteryPercent",
                "overall",
                "sampleRateQuality"
            ])
            # Añadir canales para calidad
            headers.extend(channels)
        
        elif signal_type == "met":
            metrics = ["eng", "exc", "lex", "str", "rel", "int", "foc"]
            for metric in metrics:
                headers.append(f"{metric}.isActive")
                headers.append(metric)
        
        elif signal_type == "dev":
            headers.extend([
                "Battery",
                "Signal",
                "BatteryPercent"
            ])
            # Añadir canales para estado del dispositivo
            headers.extend(channels)
            headers.append("OVERALL")
        
        return headers

    # save_eeg_data reescrito:
    def save_eeg_data(self, signal_type, data, cortex_time=None):
        if not self.is_recording and not self._closing_session:
            return
        try:
            with self._eeg_lock:
                writer = self._eeg_csv_writers.get(signal_type)
                if writer is None:          # archivos ya cerrados
                    return

                formatted_time, epoch_time = self.get_synchronized_timestamp(cortex_time)
                writer.writerow([formatted_time, epoch_time] + data)
                self._eeg_write_count[signal_type] = self._eeg_write_count.get(signal_type, 0) + 1

                now = time.time()
                if now - self._eeg_last_flush >= 1.0:
                    do_fsync = (now - self._eeg_last_fsync) >= 10.0
                    for fh in self._eeg_file_handles.values():
                        if not fh.closed:
                            fh.flush()
                            if do_fsync:
                                os.fsync(fh.fileno())
                    self._eeg_last_flush = now
                    if do_fsync:
                        self._eeg_last_fsync = now
        except Exception as e:
            print(f"⚠️ Error save_eeg_data [{signal_type}]: {e}")

    def show_temporary_warning(self, message, duration=0.5):
        """
        Muestra un mensaje de advertencia temporal que desaparece automáticamente.
        
        Args:
            message (str): El mensaje a mostrar
            duration (float): Duración en segundos antes de que desaparezca
        """
        # Crear ventana temporal
        temp_window = tk.Toplevel(self.window)
        temp_window.title("")
        
        # Quitar bordes y hacer inicialmente transparente
        temp_window.overrideredirect(True)
        temp_window.attributes('-alpha', 0.0)
        temp_window.attributes('-topmost', True)
        
        # Configurar ventana
        width = 400
        height = 100
        screen_width = temp_window.winfo_screenwidth()
        screen_height = temp_window.winfo_screenheight()
        x = (screen_width - width) // 2
        # Posicionar la ventana en la parte inferior de la pantalla, con un margen de 150px
        y = screen_height - height - 150
        temp_window.geometry(f"{width}x{height}+{x}+{y}")
        
        # Frame principal con esquinas redondeadas y color de advertencia
        main_frame = tk.Frame(temp_window, bg='#FFA500')  # Color naranja para advertencia
        main_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        
        # Icono de advertencia
        warning_label = tk.Label(main_frame, text="⚠️", font=("Arial", 24), bg='#FFA500', fg='black')
        warning_label.pack(pady=(10, 5))
        
        # Mensaje
        msg_label = tk.Label(main_frame, text=message, font=("Segoe UI", 10), 
                            bg='#FFA500', fg='black', wraplength=380)
        msg_label.pack(pady=5)
        
        # Funciones para aparecer y desaparecer gradualmente
        def fade_in():
            alpha = temp_window.attributes('-alpha')
            if alpha < 0.9:
                temp_window.attributes('-alpha', alpha + 0.1)
                temp_window.after(20, fade_in)
            else:
                # Programar el fade out después de la duración especificada
                temp_window.after(int(duration * 1000), fade_out)
        
        def fade_out():
            alpha = temp_window.attributes('-alpha')
            if alpha > 0.1:
                temp_window.attributes('-alpha', alpha - 0.1)
                temp_window.after(20, fade_out)
            else:
                temp_window.destroy()
        
        # Iniciar animación
        fade_in()

    # NUEVO: Método para activar/desactivar visualización POW
    def toggle_pow_visualization(self):
        """Activa o desactiva la visualización de potencia EEG"""
        enabled = self.pow_visualization_var.get()
        
        if hasattr(self, 'pow_visualizer'):
            self.pow_visualizer.toggle_visualization(enabled)
            
            # Actualizar label de estado
            if enabled:
                self.pow_status_label.config(text="🟢 Activo", foreground="green")
            else:
                self.pow_status_label.config(text="⚪ Inactivo", foreground="gray")


    def start_acquisition(self):
        """Inicia la adquisición de forma no-bloqueante"""
        try:
            # Validaciones RÁPIDAS en hilo principal
            if self.is_recording or self._stopping:
                return

            name = self.validated_name.get().strip()
            if not name:
                messagebox.showerror("Error", "Por favor ingrese el nombre del participante")
                return

            # ─────────────────────────────────────────────────────
            # LANZAR HILO PARA EL TRABAJO PESADO
            # ─────────────────────────────────────────────────────
            self.gui_queue.put({'type': 'system_status', 'data': 'Iniciando grabación...'})
            
            setup_thread = threading.Thread(
                target=self._do_start_acquisition,
                args=(name,),
                daemon=True,
                name="StartAcquisitionWorker"
            )
            setup_thread.start()

        except Exception as e:
            messagebox.showerror("Error", f"Error al iniciar: {str(e)}")


    def _do_start_acquisition(self, name):
        """Trabajo pesado en hilo secundario"""
        try:
            self._fin_click_epoch     = None
            self._fin_click_formatted = None
            self._fin_event_written   = False
            # ── Timestamps ────────────────────────────────────────────
            click_epoch = getattr(self, '_pending_inicio_epoch', None) or time.time()
            click_dt = datetime.fromtimestamp(click_epoch)
            click_fmt = click_dt.strftime('%Y-%m-%d %H:%M:%S.%f')
            click_str = f"{click_epoch:.6f}"

            setup_epoch = time.time()
            setup_dt = datetime.fromtimestamp(setup_epoch)
            setup_fmt = setup_dt.strftime('%Y-%m-%d %H:%M:%S.%f')
            setup_str = f"{setup_epoch:.6f}"

            self.start_formatted_time = click_fmt
            self.start_epoch_time = click_str
            self.start_time = click_dt
            self._pending_inicio_epoch = None

            # Actualizar UI desde el hilo ← IMPORTANTE
            self.window.after(0, lambda: self.start_time_label.config(
                text=click_fmt.split()[1]))
            self.window.after(0, lambda: self.date_label.config(
                text=click_dt.strftime("%d/%m/%Y")))

            # ── Carpetas ───────────────────────────────────────────────
            modo = self.test_mode.get()
            parent_folder = "Pruebas_realizadas_vehiculo2026"
            date_folder_formatted = setup_dt.strftime("%d_%m_%Y")
            date_folder = os.path.join(parent_folder, f"{modo}_{date_folder_formatted}")
            os.makedirs(date_folder, exist_ok=True)

            timestamp = setup_dt.strftime("%Y%m%d_%H%M%S")
            main_folder_name = os.path.join(date_folder, f"{name}_{modo}_{timestamp}")
            os.makedirs(main_folder_name, exist_ok=True)
            self.session_folder = main_folder_name

            # Crear subcarpetas
            eeg_folder = os.path.join(main_folder_name, "EEG")
            video_folder = os.path.join(main_folder_name, "Video")
            ecg_folder = os.path.join(main_folder_name, "ECG")
            events_folder = os.path.join(main_folder_name, "Events")

            for folder in (eeg_folder, video_folder, ecg_folder, events_folder):
                os.makedirs(folder, exist_ok=True)

            self.eeg_folder = eeg_folder
            self.video_folder = video_folder
            self.ecg_folder = ecg_folder
            self.events_folder = events_folder

            # Log del dispositivo
            self.log_device_info()

            # ── INICIAR GRABACIÓN DE VIDEO (sin esperar sincronización) ──
            self.window.after(0, lambda: self._start_video_recording(
                video_folder, name, modo))

            # ── INICIAR LiDAR ──────────────────────────────────────────
            self.window.after(0, lambda: self._start_lidar_recording(
                main_folder_name, name, modo, click_str))

            # ── Archivos EEG ───────────────────────────────────────────
            self._init_eeg_files(eeg_folder, name, modo, timestamp)

            # ── Archivo de eventos ─────────────────────────────────────
            self.window.after(0, lambda: self._write_events_file(
                events_folder, name, modo, timestamp,
                click_fmt, click_str, setup_fmt, setup_str))

            # ── Archivo de temperatura ─────────────────────────────────
            self.window.after(0, lambda: self._init_temp_file(
                events_folder, name, modo, timestamp))

            # ── Archivo eventos ECG ────────────────────────────────────
            self.window.after(0, lambda: self._init_ecg_events(
                click_fmt, click_str, setup_fmt, setup_str))

            # ── Inicializar métricas de video ──────────────────────────
            self.window.after(0, lambda: self.initialize_video_metrics_files(
                video_folder, timestamp))

            # ─────────────────────────────────────────────────────────
            # ACTUALIZAR ESTADO EN EL HILO PRINCIPAL
            # ─────────────────────────────────────────────────────────
            def _finalize():
                self.is_recording = True
                self.start_button.config(state='disabled')
                self.stop_button.config(state='normal')
                self.name_entry.config(state='disabled')
                self.update_button.config(state='disabled')

                if hasattr(self, 'start_all_cameras_btn'):
                    self.start_all_cameras_btn.config(state='disabled')

                for camera_id in ['main', 'secondary1', 'secondary2']:
                    for widget in (f"start_{camera_id}_btn", f"{camera_id}_combo"):
                        if hasattr(self, widget):
                            getattr(self, widget).config(state='disabled')

                if hasattr(self, 'radio_pista'):
                    self.radio_pista.config(state='disabled')
                if hasattr(self, 'radio_ciudad'):
                    self.radio_ciudad.config(state='disabled')

                if hasattr(self, 'pow_visualization_check'):
                    self.pow_visualization_check.config(state='normal')

                self.gui_queue.put({
                    'type': 'system_status',
                    'data': f'Grabando - Participante: {name}'
                })

                # ── Iniciar timers (no bloquean) ───────────────────
                self.update_timer()
                self.save_metrics_periodically()

            self.window.after(0, _finalize)

        except Exception as e:
            print(f"Error en _do_start_acquisition: {e}")
            import traceback
            traceback.print_exc()
            self.window.after(0, lambda: messagebox.showerror(
                "Error", f"Error al iniciar grabación: {str(e)}"))
            self.window.after(0, lambda: self._reset_ui_after_error())


    def _start_video_recording(self, video_folder, name, modo):
        """Inicia grabación de video (ejecutado en hilo principal vía after)"""
        try:
            if hasattr(self, 'multi_camera_manager'):
                for camera_id, camera_info in self.multi_camera_manager.cameras.items():
                    if camera_info['active']:
                        prefix = f"video_{camera_id}_{name}_{modo}"
                        success = self.multi_camera_manager.start_recording(
                            camera_id, video_folder, file_prefix=prefix)
                        if not success:
                            print(f"⚠️ No se pudo iniciar grabación cámara {camera_id}")
        except Exception as e:
            print(f"Error iniciando video: {e}")


    def _start_lidar_recording(self, main_folder_name, name, modo, click_str):
        """Inicia grabación LiDAR (ejecutado en hilo principal vía after)"""
        try:
            if hasattr(self, 'velodyne_manager') and self.lidar_enabled.get():
                success = self.velodyne_manager.iniciar_grabacion(
                    carpeta_base=main_folder_name,
                    nombre_participante=f"{name}_{modo}",
                    start_time=float(click_str)
                )
                if success:
                    print("✅ Grabación LiDAR iniciada")
                    if hasattr(self, 'lidar_enabled_check'):
                        self.lidar_enabled_check.config(state='disabled')
                else:
                    self.show_temporary_warning("No se pudo iniciar grabación LiDAR")
        except Exception as e:
            print(f"Error iniciando LiDAR: {e}")


    def _init_eeg_files(self, eeg_folder, name, modo, timestamp):
        """Inicializa archivos EEG"""
        try:
            self.eeg_csv_files = {
                'eeg': os.path.join(eeg_folder, f"{name}_{modo}_eeg_raw_{timestamp}.csv"),
                'mot': os.path.join(eeg_folder, f"{name}_{modo}_eeg_motion_{timestamp}.csv"),
                'pow': os.path.join(eeg_folder, f"{name}_{modo}_eeg_power_{timestamp}.csv"),
                'fac': os.path.join(eeg_folder, f"{name}_{modo}_eeg_facial_{timestamp}.csv"),
                'com': os.path.join(eeg_folder, f"{name}_{modo}_eeg_commands_{timestamp}.csv"),
                'eq':  os.path.join(eeg_folder, f"{name}_{modo}_eeg_quality_{timestamp}.csv"),
                'met': os.path.join(eeg_folder, f"{name}_{modo}_eeg_metrics_{timestamp}.csv"),
                'dev': os.path.join(eeg_folder, f"{name}_{modo}_eeg_device_{timestamp}.csv"),
            }

            nuevos_handles = {}
            nuevos_writers = {}
            for signal_type, filename in self.eeg_csv_files.items():
                fh = open(filename, mode='w', newline='', buffering=8192)
                writer = csv.writer(fh)
                writer.writerow(['Timestamp', 'EpochTime'] + 
                            self.get_signal_headers(signal_type)[1:])
                fh.flush()
                nuevos_handles[signal_type] = fh
                nuevos_writers[signal_type] = writer

            with self._eeg_lock:
                self._eeg_file_handles = nuevos_handles
                self._eeg_csv_writers = nuevos_writers
            self._eeg_write_count = {}
            self._eeg_last_flush = time.time()
            self._eeg_last_fsync = time.time()
            self._closing_session = False
            print(f"✅ {len(self.eeg_csv_files)} archivos EEG creados")
        except Exception as e:
            print(f"Error inicializando archivos EEG: {e}")


    def _write_events_file(self, events_folder, name, modo, timestamp,
                        click_fmt, click_str, setup_fmt, setup_str):
        """Escribe el archivo de eventos de sesión"""
        try:
            self.session_events_file = os.path.join(
                events_folder, f"{name}_{modo}_events_{timestamp}.txt")
            with open(self.session_events_file, 'w') as f:
                f.write("Timestamp,EpochTime,Event,Details\n")
                f.write(f"{click_fmt},{click_str},CLICK_FISICO,Timestamp ISR ESP32\n")
                f.write(f"{setup_fmt},{setup_str},INICIO_SETUP,Inicialización\n")
                f.write(f"{click_fmt},{click_str},INICIO,\n")
            self.events_file = self.session_events_file
        except Exception as e:
            print(f"Error escribiendo eventos: {e}")


    def _init_temp_file(self, events_folder, name, modo, timestamp):
        """Inicializa archivo de temperatura"""
        try:
            self.temp_csv_file = None
            if self.temp_serial and self.temp_serial.is_open:
                self.temp_csv_file = os.path.join(
                    events_folder, f"{name}_{modo}_temperatura_{timestamp}.csv")
                with open(self.temp_csv_file, 'w', newline='') as f:
                    csv.writer(f).writerow(['Timestamp', 'EpochTime', 'Temperatura_C'])
                print(f"🌡️ Registro de temperatura iniciado")
        except Exception as e:
            print(f"Error inicializando temp: {e}")


    def _init_ecg_events(self, click_fmt, click_str, setup_fmt, setup_str):
        """Inicializa archivo de eventos ECG"""
        try:
            if hasattr(self, 'ecg_handler') and self.ecg_handler.current_folder_path:
                original_ecg_folder = os.path.join(
                    ECG_PATH, self.ecg_handler.current_folder_path)
                if os.path.exists(original_ecg_folder):
                    self.original_ecg_folder = original_ecg_folder
                    self.ecg_folder_name = self.ecg_handler.current_folder_path
                    self.ecg_events_file = os.path.join(
                        original_ecg_folder, "events.txt")
                    with open(self.ecg_events_file, 'w') as f:
                        f.write("Timestamp,EpochTime,Event,Details\n")
                        f.write(f"{click_fmt},{click_str},CLICK_FISICO,Timestamp ISR ESP32\n")
                        f.write(f"{setup_fmt},{setup_str},INICIO_SETUP,\n")
                        f.write(f"{click_fmt},{click_str},INICIO,\n")
        except Exception as e:
            print(f"Error inicializando ECG events: {e}")


    def _reset_ui_after_error(self):
        """Restaura la UI después de un error"""
        self.is_recording = False
        self.start_button.config(state='normal')
        self.stop_button.config(state='disabled')
        self.name_entry.config(state='normal')
        self.update_button.config(state='normal')
        
    def save_session_info(self, end_formatted_time, end_epoch_time):
        """Save session information to a file with improved error handling."""
        try:
            # Verify required attributes exist
            if not hasattr(self, 'start_epoch_time') or not hasattr(self, 'start_formatted_time'):
                raise ValueError("Missing start time information")
            
            if not hasattr(self, 'session_folder') or not self.session_folder:
                raise ValueError("Session folder not initialized")
                
            if not os.path.exists(self.session_folder):
                raise ValueError(f"Session folder does not exist: {self.session_folder}")

            # Calculate total duration with error handling
            try:
                start_time = datetime.fromtimestamp(float(self.start_epoch_time))
                end_time = datetime.fromtimestamp(float(end_epoch_time))
                duration = end_time - start_time
                hours, remainder = divmod(duration.total_seconds(), 3600)
                minutes, seconds = divmod(remainder, 60)
            except ValueError as e:
                raise ValueError(f"Error calculating duration: {str(e)}")

            # Get participant name with validation
            participant_name = self.validated_name.get().strip()
            if not participant_name:
                raise ValueError("Participant name is missing")

            # Get ECG folder information
            ecg_folder_name = "N/A"
            ecg_folder_path = "N/A"
            if hasattr(self, 'ecg_handler') and hasattr(self.ecg_handler, 'current_folder_path'):
                if self.ecg_handler.current_folder_path:
                    ecg_folder_name = self.ecg_handler.current_folder_path
                    ecg_folder_path = os.path.join(ECG_PATH, self.ecg_handler.current_folder_path)

            # Obtener información del dispositivo
            device_info = self.get_device_info()

            # System information dictionary with safe attribute access
            system_info = {
                "Session Information": {
                    "Participant": participant_name,
                    "Folder": os.path.basename(self.session_folder)
                },
                "Time Information": {
                    "Start Time": self.start_formatted_time,
                    "Start Epoch": self.start_epoch_time,
                    "End Time": end_formatted_time,
                    "End Epoch": end_epoch_time,
                    "Duration": f"{int(hours):02d}:{int(minutes):02d}:{seconds:.6f}"
                },
                "Device Status": {
                    "EEG Device Type": device_info['type'],  # NUEVO
                    "EEG Channels": device_info['channel_count'],  # NUEVO
                    "EEG Connected": bool(self.auth_token and self.headset_id),
                    "EEG Device ID": self.headset_id if hasattr(self, 'headset_id') else 'N/A',  # NUEVO
                    "EEG Session ID": getattr(self, 'session_id', 'N/A'),
                    "Serial Port": self.selected_port.get() if hasattr(self, 'selected_port') else 'N/A',
                    "ECG Status": self.ecg_handler.current_state.name if hasattr(self.ecg_handler, 'current_state') else 'N/A',
                    "ECG Folder Name": ecg_folder_name,
                    "ECG Folder Path": ecg_folder_path,
                    "BIOPAC Status":   self.biopac_sm.current_state
                                    if (hasattr(self, 'biopac_sm') and self.biopac_sm) else 'N/A',
                    "BIOPAC File":     os.path.basename(self.biopac_acq_file)
                                    if hasattr(self, 'biopac_acq_file') else 'N/A',
                    
                    "Camera Active": any(cam['active'] for cam in self.multi_camera_manager.cameras.values()),
                    "Face Detection": self.face_detection_var.get() if hasattr(self, 'face_detection_var') else False
                },
                "Files Generated": {
                    "Events File": os.path.basename(self.events_file) if hasattr(self, 'events_file') else 'N/A',
                    "EEG Files": [],
                    "Video File": os.path.basename(self.multi_camera_manager.recording_path) if 
                            (hasattr(self.multi_camera_manager, 'recording_path') and self.multi_camera_manager.recording_path) else 'N/A',
                    "Screenshots": []
                }
            }

            # Safely add EEG files
            if hasattr(self, 'eeg_csv_files'):
                for file_type, file_path in self.eeg_csv_files.items():
                    if os.path.exists(file_path):
                        system_info["Files Generated"]["EEG Files"].append(
                            f"{file_type}: {os.path.basename(file_path)}"
                        )

            # Add screenshot files if available
            screenshots_folder = os.path.join(self.session_folder, "Screenshots")
            if os.path.exists(screenshots_folder):
                screenshots = [f for f in os.listdir(screenshots_folder) if f.endswith('.png')]
                if screenshots:
                    for screenshot in screenshots[:5]:  # Limit to first 5 for readability
                        system_info["Files Generated"]["Screenshots"].append(screenshot)
                    if len(screenshots) > 5:
                        system_info["Files Generated"]["Screenshots"].append(f"... y {len(screenshots)-5} más")

            # Create info filename with sanitized timestamp
            safe_timestamp = self.start_formatted_time.replace(':', '').replace(' ', '_')
            modo = self.test_mode.get()
            info_filename = f"{participant_name}_{modo}_session_info_{safe_timestamp}.txt"
            info_filepath = os.path.join(self.session_folder, info_filename)

            # Write information to file with error handling
            try:
                with open(info_filepath, 'w', encoding='utf-8') as f:
                    f.write("=== SISTEMA DE ADQUISICIÓN EEG-ECG ===\n\n")
                    
                    for section, data in system_info.items():
                        f.write(f"\n=== {section} ===\n")
                        if isinstance(data, dict):
                            for key, value in data.items():
                                if isinstance(value, list):
                                    f.write(f"{key}:\n")
                                    for item in value:
                                        f.write(f"  - {item}\n")
                                else:
                                    f.write(f"{key}: {value}\n")
                        else:
                            f.write(f"{data}\n")

                    f.write("\n=== Additional Notes ===\n")
                    f.write(f"Report generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Software version: 2.0.0\n")

                # Verify file was created successfully
                if not os.path.exists(info_filepath):
                    raise FileNotFoundError("Session info file was not created successfully")

                return True

            except (IOError, OSError) as e:
                raise IOError(f"Error writing session info file: {str(e)}")

        except Exception as e:
            print(f"Error saving session info: {str(e)}")
            # Re-raise the exception to be handled by the calling function
            raise

    def process_ecg_data(self, folder_path):
        """
        Procesa los datos ECG y guarda a archivos CSV en la subcarpeta ECG de la sesión.
        
        Args:
            folder_path (str): Ruta a la carpeta ECG relativa a ECG_PATH
            
        Returns:
            bool: True si el procesamiento fue exitoso, False en caso contrario
        """
        try:
            if not folder_path:
                raise ValueError("No se encontró carpeta ECG activa")
                
            print(f"\nIniciando procesamiento de datos ECG para carpeta: {folder_path}")
            
            # Verificar que existe la carpeta de sesión y la subcarpeta ECG
            if not hasattr(self, 'session_folder') or not os.path.exists(self.session_folder):
                raise ValueError("Carpeta de sesión no encontrada")
                
            if not hasattr(self, 'ecg_folder') or not os.path.exists(self.ecg_folder):
                raise ValueError("Subcarpeta ECG no encontrada")
                
            xml_file = fr"C:\XMLConvert\{folder_path}.xml"
            events_file = os.path.join(ECG_PATH, folder_path, "events.txt")
            
            # Usar la subcarpeta ECG de la sesión como carpeta de salida
            output_folder = self.ecg_folder
            print(f"Carpeta de salida: {output_folder}")
            
            # Definir rutas de archivos CSV
            modo = self.test_mode.get()
            name = self.validated_name.get().strip()
            original_csv = os.path.join(output_folder, f"{name}_{modo}_ecg_dataS.csv")
            
            # Esperar archivo XML
            if not os.path.exists(xml_file):
                print(f"Archivo XML no encontrado en: {xml_file}")
                print("Iniciando espera de archivo XML...")
                if not self.wait_for_xml(xml_file, folder_path):
                    return False
            
            t_start = time.time()
            
            # Leer archivo de eventos
            print("\nLeyendo archivo de eventos...")
            if not os.path.exists(events_file):
                raise FileNotFoundError(f"Archivo de eventos no encontrado en: {events_file}")
            
            event_start, event_end = self.read_events(events_file)
            if event_start is None or event_end is None:
                raise ValueError("No se encontraron eventos INICIO y FIN en el archivo")
            
            print(f"\nPeriodo de tiempo a filtrar:")
            print(f"Inicio: {datetime.fromtimestamp(event_start)}")
            print(f"Fin: {datetime.fromtimestamp(event_end)}")
            
            # Procesar XML y crear DataFrame
            print("\nLeyendo archivo XML...")
            ecg_data, xml_start_time = self.read_xml_ecg(xml_file)
            
            print("\nProcesando datos...")
            df = self.create_dataframe(ecg_data, xml_start_time)
            
            # Guardar CSV original (el recorte INICIO→FIN se aplica automáticamente
            # al finalizar la sesión mediante apply_session_trim)
            print("\nGuardando archivo CSV...")
            if not self.save_csv(df, original_csv):
                raise Exception("Error al guardar archivo CSV original")
            
            # Copiar el archivo XML a la carpeta de salida para tener todo junto
            try:
                xml_dest = os.path.join(output_folder, f"{folder_path}.xml")
                shutil.copy2(xml_file, xml_dest)
                print(f"Archivo XML copiado a: {xml_dest}")
            except Exception as e:
                print(f"Advertencia: No se pudo copiar el archivo XML: {e}")
            
            process_time = time.time() - t_start
            print(f"\nEstadísticas:")
            print(f"Tiempo total de procesamiento: {process_time:.6f} segundos")
            print("Procesamiento ECG completado exitosamente")
            
            return True
            
        except Exception as e:
            print(f"\nError en el procesamiento de datos ECG: {str(e)}")
            messagebox.showerror("Error", 
                f"Error al procesar datos ECG:\n{str(e)}\n\nPor favor, verifique los archivos y vuelva a intentar.")
            return False

    def stop_acquisition(self):
        """
        Detiene la adquisición de datos.
        
        ORDEN CRÍTICO PARA EVITAR PÉRDIDA DE DATOS:
        1. Marcar _closing_session (permite que save_eeg_data siga escribiendo)
        2. Marcar is_recording = False (detiene nuevos flujos)
        3. Actualizar UI
        4. Detener video
        5. CERRAR SESIÓN EEG (detiene flujo de datos) ← CRÍTICO
        6. ESPERAR 2-3 segundos para últimos paquetes ← CRÍTICO
        7. Cerrar WebSocket
        8. FLUSH FINAL EXPLÍCITO de archivos EEG ← NUEVO
        9. Detener buffer de métricas
        10. Continuar con guardado
        """
        if not self.is_recording:
            return
    
        # ══════════════════════════════════════════════════════════════
        # PASO 0: FLAG DE CIERRE (permite capturar datos finales)
        # ══════════════════════════════════════════════════════════════
        self._stopping        = True
        self._closing_session = True     # sigue escribiendo la "cola" de paquetes
        self.is_recording     = False
        print("🔄 Iniciando cierre de grabación (enlace EEG se mantiene)...")
    
        # ══════════════════════════════════════════════════════════════
        # PASO 1: MARCAR QUE YA NO ESTAMOS GRABANDO
        # ══════════════════════════════════════════════════════════════
        self.is_recording = False
        self.temp_csv_file = None      # deja de registrar temperatura
        # ══════════════════════════════════════════════════════════════
        # PASO 2: ACTUALIZAR UI INMEDIATAMENTE
        # ══════════════════════════════════════════════════════════════
        self.start_button.config(state='normal')
        self.stop_button.config(state='disabled')
        self.gui_queue.put({'type': 'system_status', 'data': 'Finalizando grabación...'})
    
        # Rehabilitar controles de nombre
        self.name_entry.config(state='normal')
        self.update_button.config(state='normal')
    
        # Rehabilitar controles de cámara
        if hasattr(self, 'start_all_cameras_btn'):
            self.start_all_cameras_btn.config(state='normal')
        for camera_id in ['main', 'secondary1', 'secondary2']:
            start_btn_name = f"start_{camera_id}_btn"
            combo_name     = f"{camera_id}_combo"
            if hasattr(self, start_btn_name):
                cam_active = (hasattr(self, 'multi_camera_manager') and
                            self.multi_camera_manager.cameras[camera_id]['active'])
                state = 'disabled' if cam_active else 'normal'
                getattr(self, start_btn_name).config(state=state)
            if hasattr(self, combo_name):
                cam_active = (hasattr(self, 'multi_camera_manager') and
                            self.multi_camera_manager.cameras[camera_id]['active'])
                if not cam_active:
                    getattr(self, combo_name).config(state='readonly')
    
        # Deshabilitar POW
        if hasattr(self, 'pow_visualization_check'):
            self.pow_visualization_check.config(state='disabled')
            self.pow_visualization_var.set(False)
        if hasattr(self, 'pow_status_label'):
            self.pow_status_label.config(text="⚪ Inactivo", foreground="gray")
        if hasattr(self, 'pow_visualizer'):
            self.pow_visualizer.toggle_visualization(False)
    
        # Detener screenshot handler
        if hasattr(self, 'screenshot_handler'):
            self.screenshot_handler.stop_monitoring()
        if hasattr(self, 'lidar_enabled_check'):
            self.lidar_enabled_check.config(state='normal')

        # Rehabilitar selector de modo
        if hasattr(self, 'radio_pista'):
            self.radio_pista.config(state='normal')
        if hasattr(self, 'radio_ciudad'):
            self.radio_ciudad.config(state='normal')
    
        # ══════════════════════════════════════════════════════════════
        # PASO 3: OBTENER TIMESTAMPS DE FIN
        # ══════════════════════════════════════════════════════════════
        if getattr(self, '_fin_click_epoch', None) is not None:
            # Usar el click físico real del botón ESP32 (ya corregido con SYNC)
            end_epoch_time     = f"{self._fin_click_epoch:.6f}"
            end_formatted_time = self._fin_click_formatted
            print(f"[Stop] Usando click físico FIN como cierre oficial: {end_formatted_time}")
        else:
            # Fallback: FIN sin botón físico (p.ej. cierre manual desde la GUI)
            end_formatted_time, end_epoch_time = self.get_synchronized_timestamp()
            print("[Stop] Sin click físico FIN registrado — usando tiempo de sistema")

        self.end_epoch_time = end_epoch_time

        print(f"⏱️  Timestamp final: {end_formatted_time}")
        # ══════════════════════════════════════════════════════════════
        # NUEVO: Escribir evento FIN de forma garantizada
        # ══════════════════════════════════════════════════════════════
        if not getattr(self, '_fin_event_written', False):
            fin_line = f"{end_formatted_time},{end_epoch_time},FIN,\n"
            for attr in ('session_events_file', 'ecg_events_file'):
                if hasattr(self, attr):
                    try:
                        with open(getattr(self, attr), 'a') as f:
                            f.write(fin_line)
                    except Exception as e:
                        print(f"Error escribiendo evento FIN en {attr}: {e}")
            self._fin_event_written = True
    
        # ══════════════════════════════════════════════════════════════
        # PASO 4: DETENER VÍDEO (rápido, sólo cierra el writer)
        # ══════════════════════════════════════════════════════════════
        if hasattr(self, 'multi_camera_manager'):
            for camera_id, camera_info in self.multi_camera_manager.cameras.items():
                if camera_info.get('recording'):
                    self.multi_camera_manager.stop_recording(camera_id)
    
                # ══════════════════════════════════════════════════════════════
        # PASO 5: DETENER BUFFER DE MÉTRICAS DE VIDEO
        # ══════════════════════════════════════════════════════════════
        if hasattr(self, 'metrics_buffer') and self.metrics_buffer.is_active:
            self.metrics_buffer.stop()

        # ══════════════════════════════════════════════════════════════
        # PASO 6: GUARDADO EN HILO (la sesión EEG y el WebSocket
        #         permanecen abiertos para la siguiente grabación)
        # ══════════════════════════════════════════════════════════════
        progress_dialog = self._show_saving_dialog()
        save_thread = threading.Thread(
            target=self._do_stop_work,
            args=(end_formatted_time, end_epoch_time, progress_dialog),
            daemon=True,
            name="StopAcquisitionWorker"
        )
        save_thread.start()


    def _show_saving_dialog(self):
        """
        Crea y muestra una ventana modal de progreso mientras se guardan los datos.
        Devuelve un dict con referencias a los widgets para actualizar desde el hilo.
        """
        dialog = tk.Toplevel(self.window)
        dialog.title("Guardando datos...")
        dialog.geometry("420x160")
        dialog.resizable(False, False)
        dialog.grab_set()                     # modal
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # no cierra con X
        dialog.transient(self.window)

        # Centrar
        dialog.update_idletasks()
        x = self.window.winfo_x() + (self.window.winfo_width()  - 420) // 2
        y = self.window.winfo_y() + (self.window.winfo_height() - 160) // 2
        dialog.geometry(f"420x160+{x}+{y}")

        ttk.Label(dialog, text="💾 Guardando datos de sesión…",
                font=('Segoe UI', 11, 'bold')).pack(pady=(18, 6))

        status_var = tk.StringVar(value="Iniciando…")
        status_lbl = ttk.Label(dialog, textvariable=status_var, font=('Segoe UI', 9))
        status_lbl.pack()

        pb = ttk.Progressbar(dialog, mode='indeterminate', length=360)
        pb.pack(pady=12)
        pb.start(12)

        dialog.update()

        return {'window': dialog, 'status_var': status_var, 'progressbar': pb}


    # ─────────────────────────────────────────────────────────────
    #  TRABAJO PESADO EN HILO SECUNDARIO (nuevo método auxiliar)
    # ─────────────────────────────────────────────────────────────

    def _do_stop_work(self, end_formatted_time, end_epoch_time, progress_dialog):

        results = {
            'session_info_ok': False,
            'ecg_processed':   False,
            'lidar_csv_ok':    False,
            'trim_ok':         0,
            'trim_fail':       0,
            'pcap_trim_ok':    0,
            'pcap_trim_fail':  0,
            'error_msg':       None,
            'carpetas_info':   '',   # ← único campo nuevo, reemplaza todo lo demás
        }

        def update_status(msg):
            try:
                progress_dialog['status_var'].set(msg)
                progress_dialog['window'].update_idletasks()
            except Exception:
                pass

        def run_in_main(fn):
            result_holder = [None]
            done_event    = threading.Event()
            def wrapper():
                try:
                    result_holder[0] = fn()
                except Exception as e:
                    result_holder[0] = None
                    print(f"Error en run_in_main: {e}")
                finally:
                    done_event.set()
            self.window.after(0, wrapper)
            done_event.wait()
            return result_holder[0]

        try:
            # ── EEG: capturar últimos paquetes y cerrar archivos ──────
            update_status("Capturando últimos paquetes EEG…")
            time.sleep(1.5)                 # en el hilo worker, la GUI no se congela
            self._closing_session = False
            self._close_eeg_files()


            # ── LiDAR ────────────────────────────────────────────────
            if hasattr(self, 'velodyne_manager') and self.velodyne_manager.grabando:
                update_status("Guardando datos LiDAR…")
                try:
                    self.velodyne_manager.detener_grabacion(end_time=end_epoch_time)
                    if hasattr(self, 'session_folder'):
                        self.velodyne_manager.guardar_csv(carpeta_base=self.session_folder)
                    else:
                        self.velodyne_manager.guardar_csv()
                    results['lidar_csv_ok'] = True
                except Exception as e:
                    print(f"Error guardando CSV LiDAR: {e}")

                update_status("Recortando PCAP LiDAR…")
                try:
                    pcap_ok, pcap_fail = self.apply_pcap_trim(
                        session_folder=self.session_folder,
                        start_time=self.start_epoch_time,
                        end_time=self.end_epoch_time
                    )
                    results['pcap_trim_ok']   = pcap_ok
                    results['pcap_trim_fail'] = pcap_fail
                except Exception as e:
                    print(f"Error recortando PCAP: {e}")

            # ── Session info ──────────────────────────────────────────
            update_status("Guardando información de sesión…")
            try:
                results['session_info_ok'] = self.save_session_info(
                    end_formatted_time, end_epoch_time
                )
            except Exception as e:
                print(f"Error guardando info de sesión: {e}")
                results['error_msg'] = str(e)

            # ── ECG ───────────────────────────────────────────────────
            update_status("Procesando datos ECG…")
            if hasattr(self, 'ecg_handler') and self.ecg_handler.current_folder_path:
                try:
                    results['ecg_processed'] = self.process_ecg_data(
                        self.ecg_handler.current_folder_path
                    )
                except Exception as e:
                    print(f"Error procesando ECG: {e}")

            # ── BIOPAC ────────────────────────────────────────────────
            update_status("Procesando datos BIOPAC…")
            biopac_fue_detectado = (
                hasattr(self, 'biopac_sm') and
                self.biopac_sm is not None and
                self.biopac_sm.current_state not in ("WAITING", "DISCONNECTED")
            )
            if biopac_fue_detectado:
                def _ask_biopac():
                    return messagebox.askyesno(
                        "Archivo BIOPAC",
                        "¿Ya guardó la sesión en el software BSL "
                        "y el archivo .acq está disponible?"
                    )
                acq_existe = run_in_main(_ask_biopac)
                if acq_existe:
                    acq_path = run_in_main(self.wait_for_acq)
                    if acq_path:
                        try:
                            run_in_main(lambda: self.process_biopac_data(acq_path))
                        except Exception as e:
                            print(f"Error procesando BIOPAC: {e}")
                    else:
                        run_in_main(lambda: messagebox.showwarning(
                            "BIOPAC omitido",
                            "No se procesó el archivo BIOPAC.\n"
                            "Puede exportarlo manualmente desde la carpeta de sesión."
                        ))

            # ── Recorte CSV ───────────────────────────────────────────
            update_status("Recortando datos de la sesión (INICIO→FIN)…")
            try:
                trim_ok, trim_fail = self.apply_session_trim(
                    session_folder=self.session_folder,
                    start_time=self.start_epoch_time,
                    end_time=self.end_epoch_time
                )
                results['trim_ok']   = trim_ok
                results['trim_fail'] = trim_fail
            except Exception as e:
                print(f"Error en recorte: {e}")

            # ── Métricas y resumen ────────────────────────────────────
            update_status("Guardando métricas y resumen…")
            try:
                self.save_video_info()
            except Exception as e:
                print(f"Error save_video_info: {e}")
            try:
                self.generate_metrics_summary()
            except Exception as e:
                print(f"Error generate_metrics_summary: {e}")

            # ── Recopilar TODO desde disco ← CLAVE ───────────────────
            update_status("Recopilando archivos guardados…")
            try:
                results['carpetas_info'] = self._recopilar_carpetas_sesion()
            except Exception as e:
                print(f"Error recopilando carpetas: {e}")

            update_status("¡Listo!")

        except Exception as e:
            results['error_msg'] = str(e)
            print(f"Error general en _do_stop_work: {e}")

        finally:
            self.window.after(0, lambda: self._finish_stop(progress_dialog, results))


    def _recopilar_carpetas_sesion(self) -> str:
        """
        Lee el disco y construye un resumen de TODAS las carpetas
        y archivos de la sesión. Independiente de variables en memoria.
        """
        if not hasattr(self, 'session_folder') or \
        not os.path.exists(self.session_folder):
            return "⚠️ Carpeta de sesión no encontrada."

        lines = []
        # Orden de presentación
        orden = ['EEG', 'Video', 'ECG', 'Events', 'LIDAR', 'Screenshots']
        ya_listadas = set()

        # Carpetas en orden conocido primero
        for nombre in orden:
            ruta = os.path.join(self.session_folder, nombre)
            if not os.path.isdir(ruta):
                continue
            ya_listadas.add(nombre)

            archivos = sorted([
                f for f in os.listdir(ruta)
                if os.path.isfile(os.path.join(ruta, f))
                and not f.startswith('.')
                and f != 'originales'
            ])

            total_mb = sum(
                os.path.getsize(os.path.join(ruta, f))
                for f in archivos
            ) / (1024 * 1024)

            lines.append(
                f"\n📁 {nombre}/  "
                f"({len(archivos)} archivo(s) | {total_mb:.1f} MB)"
            )

            for f in archivos:
                kb = os.path.getsize(os.path.join(ruta, f)) / 1024
                lines.append(f"   • {f}  ({kb:.0f} KB)")

        # Cualquier carpeta extra no esperada
        for nombre in sorted(os.listdir(self.session_folder)):
            ruta = os.path.join(self.session_folder, nombre)
            if not os.path.isdir(ruta) or nombre in ya_listadas:
                continue
            archivos = sorted([
                f for f in os.listdir(ruta)
                if os.path.isfile(os.path.join(ruta, f))
            ])
            lines.append(f"\n📁 {nombre}/  ({len(archivos)} archivo(s))")
            for f in archivos:
                kb = os.path.getsize(os.path.join(ruta, f)) / 1024
                lines.append(f"   • {f}  ({kb:.0f} KB)")

        # Archivos en la raíz (session_info.txt, etc.)
        raiz = sorted([
            f for f in os.listdir(self.session_folder)
            if os.path.isfile(os.path.join(self.session_folder, f))
        ])
        if raiz:
            lines.append("\n📄 Raíz de sesión:")
            for f in raiz:
                kb = os.path.getsize(
                    os.path.join(self.session_folder, f)
                ) / 1024
                lines.append(f"   • {f}  ({kb:.0f} KB)")

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────
    #  FINALIZACIÓN EN HILO PRINCIPAL (nuevo método auxiliar)
    # ─────────────────────────────────────────────────────────────
    def _abrir_carpeta_sesion(self):
        """Abre la carpeta de la sesión actual en el explorador de archivos."""
        try:
            if hasattr(self, 'session_folder') and os.path.exists(self.session_folder):
                folder = os.path.abspath(self.session_folder)
                if sys.platform == 'win32':
                    os.startfile(folder)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', folder])
                else:
                    subprocess.Popen(['xdg-open', folder])
            else:
                messagebox.showwarning(
                    "Carpeta no encontrada",
                    "No se encontró la carpeta de la sesión."
                )
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo abrir la carpeta:\n{e}")

    def _show_completion_dialog(self, message):
        """Muestra el resumen final con botón para abrir la carpeta de sesión."""
        dialog = tk.Toplevel(self.window)
        dialog.title("Completado")
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.transient(self.window)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        text_widget = tk.Text(frame, wrap='word', width=65, height=18)
        text_widget.insert('1.0', message)
        text_widget.config(state='disabled')
        text_widget.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X)

        ttk.Button(
            btn_frame,
            text="📂 Abrir carpeta de sesión",
            command=self._abrir_carpeta_sesion,
            style='Green.TButton'
        ).pack(side=tk.LEFT)

        ttk.Button(
            btn_frame,
            text="Aceptar",
            command=dialog.destroy,
            style='Green.TButton'
        ).pack(side=tk.RIGHT)

        dialog.update_idletasks()
        x = self.window.winfo_x() + (self.window.winfo_width() - dialog.winfo_width()) // 2
        y = self.window.winfo_y() + (self.window.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")
        
    def _finish_stop(self, progress_dialog, results):
        self._stopping = False

        try:
            progress_dialog['progressbar'].stop()
            progress_dialog['window'].grab_release()
            progress_dialog['window'].destroy()
        except Exception:
            pass

        self.gui_queue.put({'type': 'system_status', 'data': 'Listo'})

        if results.get('error_msg'):
            messagebox.showerror(
                "Error",
                f"Error al guardar información de la sesión:\n"
                f"{results['error_msg']}\n\n"
                "Los datos de sensores sí fueron guardados."
            )

        # ── Construir mensaje ─────────────────────────────────────────
        msg = "✅ Adquisición finalizada.\n"

        # Recortes
        if results.get('trim_ok') or results.get('trim_fail'):
            msg += (
                f"\n✂️  Recorte CSV (INICIO→FIN): "
                f"{results['trim_ok']} archivo(s) recortados"
                + (f", {results['trim_fail']} con problemas"
                if results['trim_fail'] else "")
                + ".\n"
            )
        if results.get('pcap_trim_ok') or results.get('pcap_trim_fail'):
            msg += (
                f"📡 Recorte PCAP LiDAR: "
                f"{results['pcap_trim_ok']} archivo(s) recortados"
                + (f", {results['pcap_trim_fail']} con problemas"
                if results['pcap_trim_fail'] else "")
                + ".\n"
            )

        # Contenido real de disco ← reemplaza todos los bloques anteriores
        msg += f"\n{'─' * 55}\n"
        msg += f"📂 Archivos guardados en sesión:"
        msg += results.get('carpetas_info', '\n   (sin información)')
        msg += f"\n{'─' * 55}\n"
        msg += f"\n📍 {getattr(self, 'session_folder', 'N/A')}"

        self._show_completion_dialog(msg)

    def reconnect_eeg(self):
        try:
            if self.ws:
                self.ws.close()
            
            time.sleep(1)  # Esperar un momento antes de reconectar
            
            self.ws = None
            self.auth_token = None
            self.headset_id = None
            self.session_id = None
            
            self.connect_eeg()
            
            self.gui_queue.put({'type': 'eeg_status', 'data': 'Reconectando...'})
        except Exception as e:
            print(f"Error en reconnect_eeg: {e}")
            self.window.after(2000, self.reconnect_eeg)  # Reintentar en 2 segundos

    def check_eeg_connection(self):
        """
        Verifica periódicamente si el dispositivo EEG sigue conectado
        Este método se ejecuta cada 10 segundos para detectar posibles desconexiones
        """
        # Solo verificar si creemos que estamos conectados
        if self.headset_detected and self.headset_id and self.auth_token:
            # Verificar tiempo desde la última comprobación para no saturar
            current_time = time.time()
            if current_time - self.last_device_check > 8:  # Verificar máximo cada 8 segundos
                self.last_device_check = current_time
                
                # Consultar el estado del dispositivo
                self.send_eeg_request({"jsonrpc": "2.0", "method": "queryHeadsets", "id": 99})
        
        # Programar próxima verificación
        if not self.stop_event.is_set():
            self.window.after(10000, self.check_eeg_connection)

    def process_gui_queue(self):
        try:
            # Verificar que la ventana sigue existiendo
            if not self.window.winfo_exists():
                return
        
            while True:
                message = self.gui_queue.get_nowait()
                msg_type = message.get('type')
                data = message.get('data')
                
                # Actualizar estado EEG
                if msg_type == 'eeg_status':
                    self.eeg_status.config(text=f"EEG: {data}")
                    
                    # Determinar el color apropiado basado en el estado
                    if isinstance(data, str):
                        if 'Conectado' in data:
                            color = 'green'
                        elif 'Autorizado' in data or 'Detectado' in data:
                            color = 'blue'
                        elif 'Conectando' in data:
                            color = 'yellow'
                        elif 'Desconectado' in data or 'Error' in data:
                            color = 'red'
                        else:
                            color = 'gray'
                            
                        # Actualizar indicador visual
                        self.update_status_indicator(
                            self.status_indicators['eeg'],
                            self.eeg_status,
                            (data, color)
                        )
                    else:
                        # Si data ya es una tupla (texto, color)
                        self.update_status_indicator(
                            self.status_indicators['eeg'],
                            self.eeg_status,
                            data
                        )
                
                # Actualizar estado Serial
                elif msg_type == 'serial_status':
                    self.serial_status.config(text=f"Serial: {data}")
                    
                    # Determinar color apropiado
                    if isinstance(data, str):
                        if data == 'Conectado':
                            color = 'green'
                        elif 'Verificando' in data:
                            color = 'yellow'
                        elif 'Error' in data or data == 'Desconectado':
                            color = 'red'
                        else:
                            color = 'gray'
                            
                        self.update_status_indicator(
                            self.status_indicators['serial'],
                            self.serial_status,
                            (data, color)
                        )
                    else:
                        self.update_status_indicator(
                            self.status_indicators['serial'],
                            self.serial_status,
                            data == 'Conectado'
                        )
                
                # Actualizar estado ECG
                elif msg_type == 'ecg_status':
                    if isinstance(data, tuple):
                        # Formato: (estado, color)
                        status, color = data
                        self.ecg_status.config(text=f"ECG: {status}")
                        self.update_status_indicator(
                            self.status_indicators['ecg'],
                            self.ecg_status,
                            data
                        )
                    else:
                        # Formato antiguo
                        self.ecg_status.config(text=f"ECG: {data}")
                        is_connected = data == 'Archivo temporal detectado'
                        self.update_status_indicator(
                            self.status_indicators['ecg'],
                            self.serial_status,
                            is_connected
                        )

                elif msg_type == 'lidar_status':
                    self.lidar_status.config(text=f"LiDAR: {data[0] if isinstance(data, tuple) else data}")
                    
                    # Determinar color apropiado
                    if isinstance(data, tuple):
                        # Formato: (estado, color)
                        status, color = data
                        self.update_status_indicator(
                            self.status_indicators['lidar'],
                            self.lidar_status,
                            data
                        )
                    else:
                        # Formato legacy string
                        if 'Conectado' in data or 'Capturando' in data:
                            color = 'green'
                        elif 'Sin datos' in data or 'Iniciando' in data:
                            color = 'yellow'
                        elif 'Error' in data or 'Inactivo' in data:
                            color = 'red'
                        else:
                            color = 'gray'
                            
                        self.update_status_indicator(
                            self.status_indicators['lidar'],
                            self.lidar_status,
                            (data, color)
                        )
                elif msg_type == 'biopac_status':
                    if isinstance(data, tuple):
                        status, color = data
                        self.biopac_status.config(text=f"BIOPAC: {status}")
                        self.update_status_indicator(
                            self.status_indicators['biopac'],
                            self.biopac_status,
                            (status, color)
                        )
                    else:
                        self.biopac_status.config(text=f"BIOPAC: {data}")
                        self.update_status_indicator(
                            self.status_indicators['biopac'],
                            self.biopac_status,
                            (data, 'gray')
                        )
                elif msg_type == 'temperature':
                    if data == 'error':
                        self.temp_label.config(text="error sensor", foreground='#ff6b6b')
                    elif data is None:
                        self.temp_label.config(text="-- °C", foreground='gray')
                    else:
                        if data < 30:
                            color = '#1dd1a1'
                        elif data < 40:
                            color = '#ff9f43'
                        else:
                            color = '#ff6b6b'
                        self.temp_label.config(text=f"{data:.1f} °C", foreground=color)

        except Empty:
            pass
        
        except Exception as e:
            # Ignorar errores si la ventana ya no existe
            if self.window and self.window.winfo_exists():
                print(f"Error en process_gui_queue: {e}")
        finally:
            # Solo programar siguiente actualización si la ventana existe y no estamos cerrando
            if (not self.stop_event.is_set() and 
                hasattr(self, 'window') and 
                self.window and 
                self.window.winfo_exists()):
                try:
                    self.window.after(100, self.process_gui_queue)
                except:
                    pass

    def update_timer(self):
        if self.is_recording and self.start_time:
            try:
                elapsed_time = datetime.now() - self.start_time
                hours = int(elapsed_time.total_seconds() // 3600)
                minutes = int((elapsed_time.total_seconds() % 3600) // 60)
                seconds_total = elapsed_time.total_seconds() % 60
                
                # Separar los segundos en parte entera y decimal
                seconds_int = int(seconds_total)
                seconds_decimal = seconds_total - seconds_int
                
                # Formatear correctamente: HH:MM:SS.DDDD con ceros iniciales garantizados
                formatted_time = f"{hours:02d}:{minutes:02d}:{seconds_int:02d}.{int(seconds_decimal*1_000_000):06d}"
                self.time_label.config(text=formatted_time)
                
                self.window.after(100, self.update_timer)
            except Exception as e:
                print(f"Error en update_timer: {e}")

    def on_closing(self):
        """Limpia los recursos al cerrar la aplicación"""
        try:
            # Detener todos los callbacks periódicos ANTES de cerrar
            self.stop_event.set()

            # Detener visualizador POW primero
            if hasattr(self, 'pow_visualizer'):
                self.pow_visualizer.close_window()

            # Detener monitoreo de capturas de pantalla
            if hasattr(self, 'screenshot_handler'):
                self.screenshot_handler.stop_monitoring()

            def _limpiar():
                """Limpia todos los recursos comunes"""
                self.disconnect_serial()
                        # Desconectar sensor de temperatura
                try:
                    self.disconnect_temp_serial()
                except Exception as e:
                    print(f"Error desconectando sensor de temperatura: {e}")
                if self.ecg_observer:
                    self.ecg_observer.stop()
                    self.ecg_observer.join()
                self.cleanup_resources()
                self.window.update()
                time.sleep(0.1)

            def _cerrar():
                self.window.quit()
                self.window.destroy()

            def _mostrar_dialogo():
                dialog = tk.Toplevel(self.window)
                dialog.title("Cerrar aplicación")
                dialog.resizable(False, False)
                dialog.grab_set()

                self.window.update_idletasks()
                x = self.window.winfo_x() + self.window.winfo_width()  // 2 - 200
                y = self.window.winfo_y() + self.window.winfo_height() // 2 - 80
                dialog.geometry(f"400x140+{x}+{y}")

                tk.Label(
                    dialog,
                    text="¿Qué desea hacer?",
                    font=("Segoe UI", 11, "bold"),
                    pady=10
                ).pack()

                btn_frame = tk.Frame(dialog)
                btn_frame.pack(pady=10)

                def salir():
                    dialog.destroy()
                    _limpiar()
                    _cerrar()

                def volver_selector():
                    dialog.destroy()
                    _limpiar()
                    _cerrar()
                    import subprocess, sys
                    subprocess.Popen([sys.executable, "selector_modo.py"])

                def cancelar():
                    dialog.destroy()

                tk.Button(
                    btn_frame, text="🚪 Salir",
                    width=14, command=salir,
                    bg="#e74c3c", fg="white", font=("Segoe UI", 10, "bold")
                ).grid(row=0, column=0, padx=8)

                tk.Button(
                    btn_frame, text="🔙 Volver al selector",
                    width=18, command=volver_selector,
                    bg="#2980b9", fg="white", font=("Segoe UI", 10, "bold")
                ).grid(row=0, column=1, padx=8)

                tk.Button(
                    btn_frame, text="Cancelar",
                    width=10, command=cancelar,
                    font=("Segoe UI", 10)
                ).grid(row=0, column=2, padx=8)

            # Verifica si hay grabación en curso
            if self.is_recording:
                if messagebox.askokcancel("Salir", "¿Está seguro de que desea salir? La adquisición se detendrá."):
                    self.send_serial_command("FIN")   # libera el TTL del VBOX
                    time.sleep(0.05)
                    self.stop_acquisition()
                    _limpiar()
                    _cerrar()
            else:
                _mostrar_dialogo()

        except Exception as e:
            print(f"Error al cerrar la aplicación: {e}")
            try:
                self.window.quit()
                self.window.destroy()
            except:
                pass
    
    def cleanup_resources(self):
        """Limpia todos los recursos del sistema antes de cerrar"""
        print("🧹 Limpiando recursos del sistema...")
        
        # Establecer flag de detención
        self.stop_event.set()
        
        # Desconectar puerto serial
        self.disconnect_serial()
        # Desconectar sensor de temperatura
        try:
            self.disconnect_temp_serial()
        except Exception as e:
            print(f"Error desconectando sensor de temperatura: {e}")

        # Detener observador ECG
        if self.ecg_observer and self.ecg_observer.is_alive():
            self.ecg_observer.stop()
            self.ecg_observer.join(timeout=2.0)  # ← timeout para no bloquear

        if hasattr(self, 'biopac_observer') and self.biopac_observer:
            if self.biopac_observer.is_alive():
                self.biopac_observer.stop()
                self.biopac_observer.join(timeout=2.0)  # ← timeout
        
        # Limpiar recursos de cámaras
        if hasattr(self, 'multi_camera_manager'):
            try:
                self.multi_camera_manager.cleanup()
            except Exception as e:
                print(f"Error limpiando cámaras: {e}")
        
        # Detener LiDAR
        if hasattr(self, 'velodyne_manager'):
            try:
                self.velodyne_manager.detener()
            except Exception as e:
                print(f"Error deteniendo LiDAR: {e}")
        
        # Detener monitoreo de capturas de pantalla
        if hasattr(self, 'screenshot_handler'):
            try:
                self.screenshot_handler.stop_monitoring()
            except Exception as e:
                print(f"Error deteniendo screenshot handler: {e}")
        
        # Detener visualizador POW
        if hasattr(self, 'pow_visualizer'):
            try:
                self.pow_visualizer.close_window()
            except Exception as e:
                print(f"Error cerrando visualizador POW: {e}")

        # Detener gestor de buffers
        if hasattr(self, 'metrics_buffer'):
            try:
                self.metrics_buffer.stop()
            except Exception as e:
                print(f"Error deteniendo metrics_buffer: {e}")
                # Cerrar conexión WebSocket
                # 1) Cerrar sesión Cortex (necesita el WebSocket abierto)
        if self.ws and self.session_id:
            try:
                self.send_eeg_request({
                    "jsonrpc": "2.0",
                    "method": "updateSession",
                    "params": {
                        "cortexToken": self.auth_token,
                        "session":     self.session_id,
                        "status":      "close"
                    },
                    "id": 6
                })
                time.sleep(0.3)
                print("✅ Sesión Cortex cerrada")
            except Exception as e:
                print(f"⚠️ Error cerrando sesión Cortex: {e}")
            self.session_id = None

        # 2) Ahora sí cerrar el WebSocket
        if self.ws:
            try:
                self.ws.close()
            except Exception as e:
                print(f"Error cerrando WebSocket: {e}")
            self.ws = None
        
        # Limpiar cola GUI
        try:
            while not self.gui_queue.empty():
                try:
                    self.gui_queue.get_nowait()
                except:
                    break
        except:
            pass
        
        print("✅ Recursos limpiados correctamente")

    def read_xml_ecg(self, xml_path):
        """Lee el archivo XML de ECG y extrae los datos"""
        try:
            print(f"Leyendo archivo XML: {xml_path}")
            tree = ET.parse(xml_path)
            root = tree.getroot()
            ns = {'ns': 'urn:hl7-org:v3'}
            
            # Extraer timestamp del XML
            center_time = root.find(".//ns:effectiveTime/ns:center", ns)
            if center_time is not None:
                time_str = center_time.get('value')
                dt = datetime.strptime(time_str, '%Y%m%d%H%M%S')
                start_time = dt.timestamp()
            else:
                start_time = time.time()
                print("Advertencia: No se encontró marca de tiempo en el XML")
            
            ecg_data = {}
            for sequence in root.findall(".//ns:sequence", ns):
                code = sequence.find("ns:code", ns)
                if code is None or 'code' not in code.attrib:
                    continue
                    
                lead_code = code.attrib['code']
                if not lead_code.startswith('MDC_ECG_LEAD'):
                    continue
                    
                digits = sequence.find(".//ns:digits", ns)
                if digits is None or not digits.text:
                    continue
                    
                samples = np.fromstring(digits.text.strip(), sep=' ', dtype=int)
                if len(samples) > 0:
                    std = np.std(samples)
                    if std == 0:
                        samples = samples - np.mean(samples)
                    else:
                        samples = (samples - np.mean(samples)) / std
                    ecg_data[lead_code] = samples
            
            if not ecg_data:
                raise ValueError("No se encontraron datos ECG válidos en el archivo XML")
                
            print(f"Datos ECG extraídos exitosamente: {len(ecg_data)} derivaciones encontradas")
            return ecg_data, start_time
            
        except Exception as e:
            print(f"Error al leer archivo XML {xml_path}: {str(e)}")
            raise

    def create_dataframe(self, ecg_data, start_time, dt=0.001):
        """Crea el DataFrame con los datos de ECG y timestamps"""
        try:
            print("Creando DataFrame con los datos ECG...")
            n_samples = max(len(data) for data in ecg_data.values())
            print(f"Número total de muestras: {n_samples}")
            
            timestamps = []
            unix_times = []
            current_time = start_time
            
            for i in range(n_samples):
                formatted_time, epoch_time = self.get_synchronized_timestamp(current_time)
                timestamps.append(formatted_time)
                unix_times.append(epoch_time)
                current_time += dt
            
            df = pd.DataFrame()
            df['timestamp'] = timestamps
            df['unix_timestamp'] = unix_times
            
            for lead, data in ecg_data.items():
                padded_data = np.pad(data, (0, n_samples - len(data)), 'constant', constant_values=0)
                df[lead] = padded_data
            
            df = df.fillna(0)
            print(f"DataFrame creado exitosamente con {len(df)} filas y {len(df.columns)} columnas")
            return df
            
        except Exception as e:
            print(f"Error al crear DataFrame: {str(e)}")
            raise

    def save_csv(self, df, output_path):
        """Guarda el DataFrame a CSV"""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            df.to_csv(output_path, index=False)
            
            if os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                print(f"\nArchivo guardado exitosamente:")
                print(f"Ruta: {os.path.abspath(output_path)}")
                print(f"Tamaño: {file_size/1024:.2f} KB")
                print("\nPrimeras líneas del archivo:")
                print(df.head(3))
                return True
        except Exception as e:
            print(f"Error al guardar: {str(e)}")
            return False

    def read_events(self, events_file):
        """
        Lee el archivo de eventos y extrae los timestamps del primer INICIO y FIN
        
        Args:
            events_file (str): Ruta al archivo de eventos
            
        Returns:
            tuple: (start_time, end_time) en formato epoch
        """
        start_time = None
        end_time = None
        
        try:
            with open(events_file, 'r') as f:
                # Skip header if present
                first_line = f.readline()
                if not first_line.startswith('20'):  # If it's not a timestamp, it's a header
                    pass
                else:
                    f.seek(0)  # Reset to start if first line was a timestamp
                
                for line in f:
                    parts = line.strip().split(',')
                    if len(parts) < 3:
                        continue
                        
                    _, epoch_time, event_type = parts[:3]
                    
                    # Solo guardar el primer INICIO que encontremos
                    if event_type == 'INICIO' and start_time is None:
                        start_time = float(epoch_time)
                    # Guardar el FIN
                    elif event_type == 'FIN':
                        end_time = float(epoch_time)
                        break  # No necesitamos seguir leyendo después de encontrar FIN
            
            if start_time is None:
                raise ValueError("No se encontró evento INICIO en el archivo")
            if end_time is None:
                raise ValueError("No se encontró evento FIN en el archivo")
                
            print(f"\nEventos encontrados:")
            print(f"INICIO: {datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S.%f')[:-2]}")
            print(f"FIN: {datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S.%f')[:-2]}")
            
            return start_time, end_time
            
        except Exception as e:
            print(f"Error leyendo eventos: {str(e)}")
            raise

    def filter_and_save_data(self, df, output_path, start_time, end_time):
        """
        Filtra por rango de tiempo con criterio "hacia adentro" (mismo que
        _trim_and_save_csv / apply_session_trim):

            INICIO -> exacto si existe, si no el más cercano POR ARRIBA (Δ ≥ 0)
            FIN    -> exacto si existe, si no el más cercano POR ABAJO  (Δ ≤ 0)

        Esto garantiza que el rango resultante quede SIEMPRE contenido dentro
        de [start_time, end_time] — nunca se cuela nada fuera de la ventana
        real de la prueba, a costa de perder como máximo una muestra en cada
        extremo si no hay coincidencia exacta.
        """
        df = df.copy()
        df['unix_timestamp'] = pd.to_numeric(df['unix_timestamp'], errors='coerce')
        df = df.dropna(subset=['unix_timestamp'])

        start_time = float(start_time)
        end_time   = float(end_time)

        # INICIO: exacto, si no el más cercano por arriba
        exact_start = df[df['unix_timestamp'] == start_time]
        if not exact_start.empty:
            actual_start = start_time
        else:
            after_start = df[df['unix_timestamp'] >= start_time]
            actual_start = float(after_start['unix_timestamp'].min()) if not after_start.empty else start_time

        # FIN: exacto, si no el más cercano por abajo
        exact_end = df[df['unix_timestamp'] == end_time]
        if not exact_end.empty:
            actual_end = end_time
        else:
            before_end = df[df['unix_timestamp'] <= end_time]
            actual_end = float(before_end['unix_timestamp'].max()) if not before_end.empty else end_time

        mask        = (df['unix_timestamp'] >= actual_start) & (df['unix_timestamp'] <= actual_end)
        filtered_df = df[mask]

        if not filtered_df.empty:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            filtered_df.to_csv(output_path, index=False)
            print(f"Filtrado: {len(filtered_df)} registros | {actual_start:.6f} → {actual_end:.6f}")
        else:
            print(f"⚠️ Sin datos en rango [{start_time:.6f} → {end_time:.6f}]")
            print(f"   Rango disponible: [{df['unix_timestamp'].min():.6f} → {df['unix_timestamp'].max():.6f}]")

    def wait_for_xml(self, xml_file, folder_path):
        """
        Espera hasta que el archivo XML exista o el usuario cierre la terminal
        
        Args:
            xml_file (str): Ruta completa al archivo XML
            folder_path (str): Nombre de la carpeta/número de registro
        
        Returns:
            bool: True si el archivo fue encontrado, False si el usuario canceló
        """
        print(f"\nEsperando archivo XML: {folder_path}.xml")
        print("Para continuar, exporte el registro en formato XML...\n")
        
        while True:  # Bucle infinito hasta que aparezca el archivo
            if os.path.exists(xml_file):
                print(f"\nArchivo {folder_path}.xml detectado! Continuando el proceso...")
                return True
                
            response = messagebox.askretrycancel(
                "Archivo XML no encontrado",
                f"El archivo {folder_path}.xml no se encuentra en la ruta:\n"
                f"{os.path.dirname(xml_file)}\n\n"
                "Por favor:\n"
                "1. Abra el software del ECG\n"
                "2. Seleccione el registro {folder_path}\n"
                "3. Expórtelo en formato XML\n\n"
                "- Presione 'Reintentar' para verificar nuevamente\n"
                "- Presione 'Cancelar' para terminar el proceso"
            )
            
            if not response:  # Si el usuario presiona Cancelar
                print("\nProceso cancelado por el usuario.")
                return False
                
            # Esperar 2 segundos antes del siguiente intento
            time.sleep(2)

    def run(self):
        try:
            self.window.protocol("WM_DELETE_WINDOW", self.on_closing)
            self.window.after(150, lambda: show_calibration_window(self.window))
            self.window.mainloop()
        except KeyboardInterrupt:
            print("\n⚠️ Interrupción del usuario detectada")
            self.cleanup_resources()
        except Exception as e:
            print(f"Error en run: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Asegurar limpieza final
            try:
                if hasattr(self, 'window') and self.window:
                    self.window.quit()
            except:
                pass

try:
    from multi_camera_manager_mp3 import MultiCameraManagerMP as MultiCameraManagerOptimized
    
    # Reemplazar con la versión multi-proceso
    MultiCameraManager = MultiCameraManagerOptimized
    
    print("✅ MultiCameraManager reemplazado con versión multi-proceso (aislamiento real)")
except ImportError as e:
    print(f"⚠️ No se pudo cargar versión multi-proceso: {e}")
    print("   Usando MultiCameraManager original")


if __name__ == "__main__":
    # Necesario para multiprocessing en Windows (y para .exe empaquetados
    # con PyInstaller/similares): sin freeze_support(), cada proceso hijo
    # que se spawnea (uno por cámara) puede intentar re-ejecutar todo el
    # script principal desde cero. spawn ya es el default en Windows,
    # se fija explícito para que el comportamiento sea el mismo en
    # Linux/Mac durante pruebas.
    import multiprocessing as _mp
    _mp.freeze_support()
    try:
        _mp.set_start_method('spawn', force=False)
    except RuntimeError:
        pass  # ya estaba fijado (p.ej. si este módulo se importa más de una vez)

    try:
        system = CombinedAcquisitionSystem()
        system.run()
    except Exception as e:
        print(f"Error principal: {e}")