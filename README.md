# MCSS — Sistema de adquisición multisensor sincronizado

Código fuente del MCSS (EEG, ECG, BIOPAC, cámaras, LiDAR Velodyne HDL-64E y VBOX),
del módulo de sincronización ESP32-C3, del prototipo de ECG inalámbrico y de los
scripts de análisis de la tesis.

## Estructura

| Carpeta / archivo | Contenido |
|---|---|
| `firmware/` | Firmware del módulo de sincronización ESP32-C3 (`MASTER_ESP32_fixed`; `IMT_INICIO_FIN2_previo` es la versión anterior). |
| `mcss/` | Aplicación central: GUI (`sistema_multisensor.py`), cámaras en procesos independientes (`multi_camera_manager_mp3.py`), driver LiDAR (`velodyne_manager_karen.py`), visualizador 3D (`lidar_visualizer2.py`) y calibración (`dbkaren.xml`). |
| `fusion/` | Fusión multisensor y métricas de calidad temporal (`fusion_sensores_v7.py`). |
| `analisis/` | Scripts de análisis: `sync_analyzer_unified.py`, `vbo_trigger_batch.py`, `vbo_trigger_sync.py`. |
| `ecg_inalambrico/` | Nodos ESP32-S3 (`nodos/`), maestro CYD (`maestro_control/`), controlador Raspberry Pi (`maestro_datos/`), servidor web React/Node (`servidor/`) y piezas 3D (`hardware/`). |
| `config.example.py` | Plantilla de configuración, sin credenciales. |
| `requirements.txt` | Dependencias de Python. |

## Instalación

```bash
git clone https://github.com/USUARIO/mcss.git
cd mcss
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.py config.py                       # y complete sus credenciales Cortex
```

## Uso

```bash
python mcss/sistema_multisensor.py        # adquisición
python fusion/fusion_sensores_v7.py       # fusión multisensor
python analisis/vbo_trigger_batch.py RAIZ # métricas de telemetría VBOX
python analisis/sync_analyzer_unified.py  # análisis de sincronización
```

Firmware: abrir los `.ino` con Arduino IDE / PlatformIO. En
`ecg_inalambrico/nodos/esclavo_ecg_mqtt/` reemplace `TU_WIFI_SSID` y `TU_WIFI_PASSWORD`.
Servidor: `cd ecg_inalambrico/servidor/websockets-sensors-data-display && npm install`.

## Credenciales

Las credenciales de la API Cortex **no** forman parte del repositorio. Copie
`config.example.py` a `config.py` (ignorado por git).

## Licencia

MIT — ver `LICENSE`.

## Cita

Si usa este software, cite la versión archivada en Zenodo: DOI `10.5281/zenodo.XXXXXXX`
(se completa al archivar la primera versión).
