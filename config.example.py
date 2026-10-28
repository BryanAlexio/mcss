"""
Plantilla de configuración del MCSS (sin credenciales).

Uso:
    1. Copie este archivo:  cp config.example.py config.py
    2. Complete sus propios valores en config.py
    3. config.py está en .gitignore: NUNCA lo suba al repositorio.

Las credenciales de la API Cortex (Emotiv) se obtienen creando una aplicación en
https://www.emotiv.com/my-account/cortex-apps/
"""

# --- API Cortex (adquisición de EEG con Emotiv) ---
CORTEX_URL = "wss://localhost:6868"
CLIENT_ID = ""        # <-- su Client ID de Cortex
CLIENT_SECRET = ""    # <-- su Client Secret de Cortex
