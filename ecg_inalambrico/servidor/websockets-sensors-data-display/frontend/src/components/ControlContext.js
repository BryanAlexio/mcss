import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';

// ==================== BUFFER CIRCULAR DE DERIVACIONES ECG ====================
// Cada derivación guarda sus muestras indexadas por sample_num (no por orden de llegada):
//  - llegadas desordenadas o repetidas no corrompen la señal
//  - las muestras perdidas quedan como NaN (hueco visible, no se "pegan" los tramos)
//  - todas las derivaciones comparten la misma escala de tiempo (sample_num)
//  - recibir datos NO provoca re-render de React: la gráfica lee el buffer por su cuenta
export const ECG_SAMPLE_RATE = 1000;          // Hz
export const ECG_RING_CAPACITY = 16384;       // 16.4 s a 1 kHz (potencia de 2)
const RING_MASK = ECG_RING_CAPACITY - 1;
const SESSION_RESTART_JUMP = 2000;            // si sample_num retrocede más de esto = sesión nueva
const DERIVATION_IDS = ['I', 'II', 'III', 'aVR', 'aVL', 'aVF', 'V1', 'V2', 'V3', 'V4', 'V5', 'V6'];

const createRings = () => {
  const rings = {};
  DERIVATION_IDS.forEach((id) => {
    rings[id] = {
      buf: new Float32Array(ECG_RING_CAPACITY).fill(NaN),
      head: -1,        // mayor sample_num recibido
      samples: 0,
      batches: 0
    };
  });
  return rings;
};

const createEmptyStats = () => {
  const stats = {};
  DERIVATION_IDS.forEach((id) => {
    stats[id] = { batches: 0, samples: 0, lastSample: -1 };
  });
  return stats;
};

// Buffer circular por electrodo (señal cruda en voltios, indexada por sample_num)
const createSlaveRing = () => ({
  buf: new Float32Array(ECG_RING_CAPACITY).fill(NaN),
  head: -1,
  samples: 0
});

// Escribe una muestra; devuelve false si es demasiado vieja o inválida
const ringWrite = (ring, s, v) => {
  if (!Number.isInteger(s) || s < 0 || typeof v !== 'number') return false;

  if (s > ring.head) {
    const gap = s - ring.head - 1;
    if (gap > 0) {
      const n = gap < ECG_RING_CAPACITY ? gap : ECG_RING_CAPACITY;
      for (let k = 1; k <= n; k++) ring.buf[(s - k) & RING_MASK] = NaN;
    }
    ring.head = s;
  } else if (s <= ring.head - ECG_RING_CAPACITY) {
    return false;
  }

  ring.buf[s & RING_MASK] = v;
  ring.samples++;
  return true;
};

const ControlContext = createContext();

export const useControlContext = () => {
  const context = useContext(ControlContext);
  if (!context) {
    throw new Error('useControlContext debe ser usado dentro de ControlProvider');
  }
  return context;
};

export const ControlProvider = ({ children }) => {
  const [ws, setWs] = useState(null);
  const [wsConnected, setWsConnected] = useState(false);
  const reconnectTimeoutRef = useRef(null);
  const heartbeatIntervalRef = useRef(null);
  const wsRef = useRef(null);
  
  // ========== OPTIMIZACIÓN: Usar useRef para deviceData ==========
  const deviceDataRef = useRef(new Map());
  const slaveRingsRef = useRef(new Map());   // slave_id -> buffer circular de la señal cruda
  const [deviceDataVersion, setDeviceDataVersion] = useState(0);
  // ================================================================
  
  // ========== ESTADO PARA DERIVACIONES ECG (BUFFER CIRCULAR EN REF) ==========
  const ringsRef = useRef(createRings());
  const globalHeadRef = useRef(-1);
  const [derivationStats, setDerivationStats] = useState(createEmptyStats);

  // Las estadísticas se publican 2 veces por segundo (no por cada lote recibido)
  useEffect(() => {
    const timer = setInterval(() => {
      const next = {};
      let changed = false;
      DERIVATION_IDS.forEach((id) => {
        const r = ringsRef.current[id];
        next[id] = { batches: r.batches, samples: r.samples, lastSample: r.head };
      });
      setDerivationStats((prev) => {
        for (const id of DERIVATION_IDS) {
          if (prev[id]?.samples !== next[id].samples || prev[id]?.batches !== next[id].batches) {
            changed = true;
            break;
          }
        }
        return changed ? next : prev;
      });
    }, 500);
    return () => clearInterval(timer);
  }, []);
  
  const [raspberryStatus, setRaspberryStatus] = useState({
    connected: false,
    acquiring: false,
    slaves_active: [],
    slaves_finished: [],
    session_folder: null,
    last_command: null,
    timestamp: null,
    registered_electrodes_count: 0,
    registered_electrodes: []
  });
  
  const [registeredSlaves, setRegisteredSlaves] = useState(new Map());
  const [connectionStatus, setConnectionStatus] = useState('disconnected');
  const [recordingStatus, setRecordingStatus] = useState('stopped');
  const [isRecording, setIsRecording] = useState(false);
  const [currentTime, setCurrentTime] = useState(new Date());
  const [connectedDevices, setConnectedDevices] = useState(new Map());

  // ========== OPTIMIZACIÓN: Throttling para actualizaciones de UI ==========
  const lastUpdateTimeRef = useRef({});
  const UPDATE_THROTTLE_MS = 100;
  // ==========================================================================

  useEffect(() => {
    wsRef.current = ws;
  }, [ws]);

  useEffect(() => {
    const timer = setInterval(() => {
      setCurrentTime(new Date());
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  // ==================== BEFOREUNLOAD HANDLER ====================
  useEffect(() => {
    const handleBeforeUnload = (event) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        console.log("🔄 Enviando RESET debido a cierre/recarga de página...");
        
        const message = {
          type: 'raspberry_command',
          command: 'RESET',
          params: {},
          timestamp: Date.now()
        };
        
        wsRef.current.send(JSON.stringify(message));
      }
    };

    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload);
    };
  }, []);

  // ==================== WEBSOCKET CONNECTION ====================
  
  useEffect(() => {
    let websocket = null;

    const connectWebSocket = () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }

      if (websocket && websocket.readyState === WebSocket.OPEN) {
        console.log("WebSocket ya está conectado");
        return;
      }

      console.log("🔄 Conectando al servidor WebSocket...");
      
      try {
        websocket = new WebSocket('ws://localhost:8080/request');
      } catch (error) {
        console.error("Error creando WebSocket:", error);
        scheduleReconnect();
        return;
      }

      websocket.onopen = () => {
        console.log("✅ WebSocket conectado exitosamente");
        setWsConnected(true);
        
        if (heartbeatIntervalRef.current) {
          clearInterval(heartbeatIntervalRef.current);
        }
        
        heartbeatIntervalRef.current = setInterval(() => {
          if (websocket.readyState === WebSocket.OPEN) {
            websocket.send(JSON.stringify({ type: 'ping', timestamp: Date.now() }));
          }
        }, 15000);
        
        setTimeout(() => {
          if (websocket.readyState === WebSocket.OPEN) {
            console.log("📡 Solicitando estado inicial del servidor...");
            websocket.send(JSON.stringify({
              type: 'request_status',
              timestamp: Date.now()
            }));
          }
        }, 500);
      };

      websocket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          
          if (data.type === 'pong') {
            return;
          }
          
          handleWebSocketMessage(data);
        } catch (error) {
          console.error("Error procesando mensaje WebSocket:", error);
        }
      };

      websocket.onerror = (error) => {
        console.error("❌ WebSocket error:", error);
      };

      websocket.onclose = (event) => {
        console.log("⚠️ WebSocket desconectado", event.code, event.reason);
        setWsConnected(false);
        
        if (heartbeatIntervalRef.current) {
          clearInterval(heartbeatIntervalRef.current);
          heartbeatIntervalRef.current = null;
        }
        
        scheduleReconnect();
      };

      setWs(websocket);
    };

    const scheduleReconnect = () => {
      if (reconnectTimeoutRef.current) return;
      
      console.log("⏱️ Reintentando conexión en 3 segundos...");
      reconnectTimeoutRef.current = setTimeout(() => {
        reconnectTimeoutRef.current = null;
        connectWebSocket();
      }, 3000);
    };

    connectWebSocket();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (heartbeatIntervalRef.current) {
        clearInterval(heartbeatIntervalRef.current);
      }
      if (websocket) {
        websocket.close();
      }
    };
  }, []);

  // ==================== MESSAGE HANDLERS ====================

  const handleWebSocketMessage = useCallback((data) => {
    switch (data.type) {
      case 'device_list':
        updateDevicesFromList(data.devices);
        break;
        
      case 'device_connected':
        handleDeviceConnected(data.device);
        break;
        
      case 'device_disconnected':
        handleDeviceDisconnected(data.deviceId);
        break;
        
      case 'raspberry_status':
        handleRaspberryStatus(data.status);
        break;
        
      case 'raspberry_command_response':
        handleCommandResponse(data);
        break;
        
      case 'slave_register':
        handleSlaveRegister(data);
        break;
        
      case 'slave_status':
        handleSlaveStatus(data);
        break;
        
      case 'mqtt_data_sample':
        handleMqttDataSample(data);
        break;
        
      case 'data_progress':
        handleDataProgress(data);
        break;
        
      case 'slave_finished':
        handleSlaveFinished(data);
        break;
        
      case 'electrode_list':
        handleElectrodeList(data.electrodes);
        break;
        
      case 'slave_disconnected':
        handleSlaveDisconnected(data);
        break;
        
      case 'raspberry_output':
        console.log("Raspberry Pi output:", data.output);
        break;
      
      // ========== NUEVO: MANEJAR DERIVACIONES ECG ==========
      case 'ecg_derivation_batch':
        handleECGDerivationBatch(data);
        break;
      // ======================================================
        
      default:
        if (data.sensorData !== undefined) {
          updateDeviceData(data);
        } else {
          console.log("⚠️ Tipo de mensaje no manejado:", data.type);
        }
    }
  }, []); // Sin dependencias porque usa refs y setters

  // ========== HANDLER DE DERIVACIONES ECG ==========
  const resetRings = useCallback(() => {
    Object.values(ringsRef.current).forEach((r) => {
      r.buf.fill(NaN);
      r.head = -1;
      r.samples = 0;
      r.batches = 0;
    });
    globalHeadRef.current = -1;
    setDerivationStats(createEmptyStats());
  }, []);

  const handleECGDerivationBatch = useCallback((data) => {
    const { derivation, samples } = data;
    const ring = ringsRef.current[derivation];

    if (!ring || !Array.isArray(samples) || samples.length === 0) {
      return;
    }

    // La numeración volvió a empezar (nueva sesión sin RESET intermedio)
    if (ring.head >= 0 && samples[0].sample_num < ring.head - SESSION_RESTART_JUMP) {
      console.log(`[ECG_BATCH] Nueva sesión detectada en ${derivation}, reiniciando buffers`);
      resetRings();
    }

    const buf = ring.buf;

    for (let i = 0; i < samples.length; i++) {
      const s = samples[i].sample_num;
      const v = samples[i].value;

      if (!Number.isInteger(s) || s < 0 || typeof v !== 'number') continue;

      if (s > ring.head) {
        // Muestras que no llegaron entre head y s quedan como hueco (NaN)
        const gap = s - ring.head - 1;
        if (gap > 0) {
          const n = gap < ECG_RING_CAPACITY ? gap : ECG_RING_CAPACITY;
          for (let k = 1; k <= n; k++) {
            buf[(s - k) & RING_MASK] = NaN;
          }
        }
        ring.head = s;
      } else if (s <= ring.head - ECG_RING_CAPACITY) {
        continue; // más vieja que lo que cabe en el buffer
      }

      buf[s & RING_MASK] = v;
      ring.samples++;
    }

    ring.batches++;
    if (ring.head > globalHeadRef.current) {
      globalHeadRef.current = ring.head;
    }
  }, [resetRings]);

  const updateDevicesFromList = useCallback((devices) => {
    const devicesMap = new Map();
    devices.forEach(device => {
      devicesMap.set(device.deviceId, device);
    });
    setConnectedDevices(devicesMap);
  }, []);

  const handleDeviceConnected = useCallback((device) => {
    setConnectedDevices(prev => {
      const newMap = new Map(prev);
      newMap.set(device.deviceId, device);
      return newMap;
    });
    console.log(`Dispositivo conectado: ${device.deviceId}`);
  }, []);

  const handleDeviceDisconnected = useCallback((deviceId) => {
    setConnectedDevices(prev => {
      const newMap = new Map(prev);
      const device = newMap.get(deviceId);
      if (device) {
        device.status = 'disconnected';
        newMap.set(deviceId, device);
      }
      return newMap;
    });
    console.log(`Dispositivo desconectado: ${deviceId}`);
  }, []);

  const handleRaspberryStatus = useCallback((status) => {
    console.log("📊 Estado Raspberry Pi actualizado:", status);
    
    if (!status.connected) {
      console.log("⚠️ Raspberry Pi desconectada - Limpiando estado completo");
      setRaspberryStatus({ 
        connected: false,
        acquiring: false,
        slaves_active: [],
        slaves_finished: [],
        session_folder: null,
        last_command: null,
        timestamp: null,
        registered_electrodes_count: 0,
        registered_electrodes: []
      });
      setRegisteredSlaves(new Map());
      deviceDataRef.current.clear();
      slaveRingsRef.current.clear();
      resetRings();
      setConnectionStatus('disconnected');
      setRecordingStatus('stopped');
      setIsRecording(false);
      setDeviceDataVersion(0);
      return;
    }

    setRaspberryStatus(status);
    
    if (status.registered_electrodes && Array.isArray(status.registered_electrodes)) {
      setRegisteredSlaves(prev => {
        const newMap = new Map();  // Always new instance prevents mutations[web:6]
        status.registered_electrodes.forEach(electrode => {
          const existing = prev.get(electrode.slave_id);
          const updated = {
            ...existing,
            ...electrode,
            status: electrode.status || 'active',  // Merge status
            last_update: status.timestamp || Date.now()
          };
          newMap.set(electrode.slave_id, updated);
        });
        return newMap;
      });
    }
    
    if (status.acquiring !== undefined) {
      setIsRecording(status.acquiring);
      setRecordingStatus(status.acquiring ? 'recording' : 'stopped');
    }
    
    if (status.acquiring) {
      setConnectionStatus('found');
    } else {
      if (status.slaves_active && status.slaves_active.length > 0) {
        setConnectionStatus('found');
      } else if (!status.connected) {
        setConnectionStatus('disconnected');
      }
    }
  }, []);

  const handleElectrodeList = useCallback((electrodes) => {
    console.log("📋 Lista de electrodos recibida:", electrodes);
    
    if (!electrodes || electrodes.length === 0) {
      console.log("🔭 Lista de electrodos vacía - limpiando");
      setRegisteredSlaves(new Map());
      return;
    }
    
    const now = Date.now();
    
    setRegisteredSlaves(prev => {
      const newMap = new Map();
      
      electrodes.forEach(electrode => {
        const existing = prev.get(electrode.slave_id);
        newMap.set(electrode.slave_id, {
          ...electrode,
          ...(existing || {}),
          last_update: now
        });
      });
      
      return newMap;
    });
    
    if (electrodes.length > 0) {
      setConnectionStatus('found');
    }
  }, []);

  const handleSlaveRegister = useCallback((data) => {
    const slaveId = data.slave_id;
    const now = Date.now();
    
    const electrodeInfo = {
      slave_id: slaveId,
      electrode_name: data.electrode_name || 'Unknown',
      electrode_position: data.electrode_position || 'Unknown',
      electrode_color: data.electrode_color || '#666666',
      mac: data.mac,
      ip: data.ip,
      rssi: data.rssi,
      registered_at: now,
      last_update: now
    };
    
    console.log(`✅ Electrodo registrado: ${electrodeInfo.electrode_name} (ID: ${slaveId})`);
    
    setRegisteredSlaves(prev => {
      const newMap = new Map(prev);
      newMap.set(slaveId, electrodeInfo);
      return newMap;
    });
    
    setConnectionStatus('found');
  }, []);

  const handleSlaveStatus = useCallback((data) => {
    const electrodeName = data.electrode_name || `ID ${data.slave_id}`;
    console.log(`📊 Estado ${electrodeName}: ${data.status}`);
    
    setRegisteredSlaves(prev => {
      const newMap = new Map(prev);
      const slave = newMap.get(data.slave_id);
      if (slave) {
        slave.last_status = data.status;
        slave.last_update = Date.now();
        newMap.set(data.slave_id, slave);
      }
      return newMap;
    });
  }, []);

  const handleMqttDataSample = useCallback((data) => {
    const { slave_id, electrode_name, packet_id } = data;

    let ring = slaveRingsRef.current.get(slave_id);
    if (!ring) {
      ring = createSlaveRing();
      slaveRingsRef.current.set(slave_id, ring);
    }

    if (Array.isArray(data.voltages) && Number.isInteger(data.first_sample)) {
      // Formato compacto: muestras consecutivas desde first_sample
      const first = data.first_sample;
      const volts = data.voltages;
      if (volts.length === 0) return;

      if (ring.head >= 0 && first < ring.head - SESSION_RESTART_JUMP) {
        ring.buf.fill(NaN);          // la numeración volvió a empezar (nueva sesión)
        ring.head = -1;
        ring.samples = 0;
      }
      for (let i = 0; i < volts.length; i++) {
        ringWrite(ring, first + i, volts[i]);
      }
    } else if (Array.isArray(data.samples) && data.samples.length > 0) {
      // Formato con lista de muestras {sample_num, voltage}
      if (ring.head >= 0 && data.samples[0].sample_num < ring.head - SESSION_RESTART_JUMP) {
        ring.buf.fill(NaN);
        ring.head = -1;
        ring.samples = 0;
      }
      for (let i = 0; i < data.samples.length; i++) {
        ringWrite(ring, data.samples[i].sample_num, data.samples[i].voltage);
      }
    } else {
      return;
    }

    // La gráfica lee el buffer por su cuenta; esto solo refresca tarjetas/contadores
    const now = Date.now();
    const lastUpdate = lastUpdateTimeRef.current[slave_id] || 0;

    if (now - lastUpdate >= UPDATE_THROTTLE_MS * 2) {
      lastUpdateTimeRef.current[slave_id] = now;
      setDeviceDataVersion(v => v + 1);

      if (packet_id % 50 === 0) {
        console.log(`📊 ${electrode_name}: ${ring.samples} muestras, Paquete #${packet_id}`);
      }

      setRegisteredSlaves(prev => {
        const newMap = new Map(prev);
        const slave = newMap.get(slave_id);

        if (slave && !slave.locked) {
          slave.last_update = now;
          newMap.set(slave_id, slave);
        }

        return newMap;
      });
    }
  }, []);

  const handleDataProgress = useCallback((data) => {
    const electrodeName = data.electrode_name || `ID ${data.slave_id}`;
    
    if (data.packets % 20 === 0) {
      console.log(`📈 Progreso ${electrodeName}: ${data.packets} paquetes, ${data.samples} muestras`);
    }
    
    setRegisteredSlaves(prev => {
      const newMap = new Map(prev);
      const slave = newMap.get(data.slave_id);
      
      if (slave && !slave.locked) {
        slave.packets = data.packets;
        slave.samples = data.samples;
        slave.last_update = Date.now();
        newMap.set(data.slave_id, slave);
      }
      
      return newMap;
    });
  }, []);

  const handleSlaveFinished = useCallback((data) => {
    const electrodeName = data.electrode_name || `ID ${data.slave_id}`;
    console.log(`✅ ${electrodeName} finalizado: ${data.total_samples} muestras totales`);
    
    setRegisteredSlaves(prev => {
      const newMap = new Map(prev);
      const slave = newMap.get(data.slave_id);
      if (slave) {
        slave.finished = true;
        slave.total_packets = data.total_packets;
        slave.total_samples = data.total_samples;
        slave.last_update = Date.now();
        slave.locked = true;
        
        newMap.set(data.slave_id, slave);
      }
      return newMap;
    });
    
    setDeviceDataVersion(v => v + 1);
  }, []);

  const handleSlaveDisconnected = useCallback((data) => {
    const electrodeName = data.electrode_name || `ID ${data.slave_id}`;
    console.log(`⚠️ ${electrodeName} DESCONECTADO`);
    
    setRegisteredSlaves(prev => {
      const newMap = new Map(prev);
      const slave = newMap.get(data.slave_id);
      if (slave) {
        slave.status = 'disconnected';
        slave.last_update = Date.now();
        slave.disconnection_reason = data.reason;
        newMap.set(data.slave_id, slave);
      }
      return newMap;
    });
  }, []);

  const handleCommandResponse = useCallback((data) => {
    console.log(`Comando ${data.command} ${data.success ? 'exitoso' : 'fallido'}: ${data.message}`);
    
    if (data.success) {
      if (data.command === 'FINALIZAR') {
        setRecordingStatus('stopped');
        setIsRecording(false);
        console.log("✅ Grabación finalizada exitosamente");
      } else if (data.command === 'INICIAR') {
        setRecordingStatus('recording');
        setIsRecording(true);
        console.log("✅ Grabación iniciada exitosamente");
      } else if (data.command === 'RESET') {
        console.log("✅ Comando RESET confirmado por servidor");
        
        // Limpiar datos de electrodos
        deviceDataRef.current.clear();
      slaveRingsRef.current.clear();
        lastUpdateTimeRef.current = {};
        setDeviceDataVersion(0);
        
        // Limpiar derivaciones
        resetRings();
        
        console.log("🗑️ Estado local limpiado completamente");
      }
    }
    
    if (!data.success) {
      alert(`Error en comando ${data.command}: ${data.message}`);
    }
  }, []);

  const updateDeviceData = useCallback((data) => {
    let deviceHistory = deviceDataRef.current.get(data.deviceId) || [];
    
    deviceHistory.push({
      timestamp: data.date,
      value: data.sensorData
    });
    
    if (deviceHistory.length > 10000) {
      deviceHistory.shift();
    }
    
    deviceDataRef.current.set(data.deviceId, deviceHistory);
    
    const now = Date.now();
    const lastUpdate = lastUpdateTimeRef.current[data.deviceId] || 0;
    
    if (now - lastUpdate >= UPDATE_THROTTLE_MS) {
      lastUpdateTimeRef.current[data.deviceId] = now;
      setDeviceDataVersion(v => v + 1);
    }
  }, []);

  // ========== GETTERS ==========
  
  // ----- Señal cruda por electrodo (buffer circular) -----
  const getSlaveHead = useCallback((slaveId) => {
    const ring = slaveRingsRef.current.get(slaveId);
    return ring ? ring.head : -1;
  }, []);

  const getSlaveStats = useCallback((slaveId) => {
    const ring = slaveRingsRef.current.get(slaveId);
    if (!ring || ring.head < 0) return { samples: 0, head: -1, last: null };
    const v = ring.buf[ring.head & RING_MASK];
    return { samples: ring.samples, head: ring.head, last: v === v ? v : null };
  }, []);

  // Igual que fillDerivationWindow, pero para la señal cruda de un electrodo
  const fillSlaveWindow = useCallback((slaveId, out, endSample) => {
    const ring = slaveRingsRef.current.get(slaveId);
    const W = out.length;

    if (!ring || ring.head < 0) {
      out.fill(NaN);
      return;
    }

    const start = endSample - W + 1;
    const head = ring.head;
    const oldest = head - ECG_RING_CAPACITY + 1;

    for (let i = 0; i < W; i++) {
      const s = start + i;
      out[i] = (s < 0 || s > head || s < oldest) ? NaN : ring.buf[s & RING_MASK];
    }
  }, []);

  const getDeviceData = useCallback((slaveId) => {
    return deviceDataRef.current.get(slaveId) || [];
  }, [deviceDataVersion]);
  
  // Mayor sample_num recibido en cualquier derivación (referencia de tiempo común)
  const getDerivationHead = useCallback(() => globalHeadRef.current, []);

  // Llena `out` (Float32Array) con las últimas out.length muestras que terminan en
  // `endSample`. Muestras ausentes = NaN. No asigna memoria: es seguro llamarlo por frame.
  const fillDerivationWindow = useCallback((derivationId, out, endSample) => {
    const ring = ringsRef.current[derivationId];
    const W = out.length;

    if (!ring || ring.head < 0) {
      out.fill(NaN);
      return;
    }

    const start = endSample - W + 1;
    const head = ring.head;
    const oldest = head - ECG_RING_CAPACITY + 1;
    const buf = ring.buf;

    for (let i = 0; i < W; i++) {
      const s = start + i;
      out[i] = (s < 0 || s > head || s < oldest) ? NaN : buf[s & RING_MASK];
    }
  }, []);

  const clearDerivations = useCallback(() => {
    console.log("[CONTEXT] 🗑️ Limpiando todas las derivaciones...");
    resetRings();
  }, [resetRings]);

  // ==================== RASPBERRY PI COMMANDS ====================

  const sendRaspberryCommand = useCallback((command, params = {}) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.error("❌ WebSocket no está conectado");
      alert("No hay conexión con el servidor");
      return false;
    }

    const message = {
      type: 'raspberry_command',
      command: command,
      params: params,
      timestamp: Date.now()
    };

    ws.send(JSON.stringify(message));
    console.log(`📤 Comando enviado a Raspberry Pi: ${command}`, params);
    return true;
  }, [ws]);

  // ==================== CONTROL FUNCTIONS ====================

  const handleFind = useCallback(() => {
    console.log("🔍 Iniciando búsqueda de electrodos (ENCONTRAR)...");
    setConnectionStatus('searching');
    
    setRegisteredSlaves(new Map());
    deviceDataRef.current.clear();
      slaveRingsRef.current.clear();
    lastUpdateTimeRef.current = {};
    setDeviceDataVersion(0);
    
    const success = sendRaspberryCommand('ENCONTRAR');
    
    if (success) {
      setTimeout(() => {
        setConnectionStatus('disconnected');
        console.log("✅ Comando ENCONTRAR enviado, esperando registros...");
      }, 1500);
    } else {
      setConnectionStatus('disconnected');
    }
  }, [sendRaspberryCommand]);

  const handleStartRecording = useCallback(() => {
    if (registeredSlaves.size === 0) {
      alert("Primero debe encontrar los dispositivos (botón ENCONTRAR)");
      return;
    }

    console.log("▶️ Iniciando grabación (INICIAR)...");
    
    // Limpiar datos de electrodos
    deviceDataRef.current.clear();
      slaveRingsRef.current.clear();
    lastUpdateTimeRef.current = {};
    setDeviceDataVersion(0);
    
    // Limpiar derivaciones al iniciar nueva grabación
    clearDerivations();
    
    sendRaspberryCommand('INICIAR');
  }, [registeredSlaves, sendRaspberryCommand, clearDerivations]);

  const handleStopRecording = useCallback(() => {
    console.log("⏹️ Finalizando grabación (FINALIZAR)...");
    sendRaspberryCommand('FINALIZAR');
  }, [sendRaspberryCommand]);

  const handleReset = useCallback(() => {
    if (window.confirm("¿Está seguro de que desea reiniciar el sistema completo?")) {
      console.log("🔄 Enviando RESET a Raspberry Pi...");
      
      const success = sendRaspberryCommand('RESET');
      
      if (!success) {
        alert("Error al enviar comando RESET");
        return;
      }
      
      setConnectionStatus('resetting');
      
      setTimeout(() => {
        console.log("🔄 Fase 1: Limpiando estado local...");
        setRecordingStatus('stopped');
        setIsRecording(false);
        setConnectedDevices(new Map());
        deviceDataRef.current.clear();
      slaveRingsRef.current.clear();
        lastUpdateTimeRef.current = {};
        setDeviceDataVersion(0);
        
        // Limpiar derivaciones
        clearDerivations();
      }, 2000);
      
      setTimeout(() => {
        console.log("🔄 Fase 2: Limpiando electrodos...");
        setRegisteredSlaves(new Map());
      }, 4000);
      
      setTimeout(() => {
        console.log("✅ Reset completado");
        setConnectionStatus('disconnected');
      }, 6000);
    }
  }, [sendRaspberryCommand, clearDerivations]);

  const handleExport = useCallback(() => {
    console.log("💾 Exportando datos...");
    alert("Función de exportación en desarrollo");
  }, []);

  const handleStatus = useCallback(() => {
    console.log("📊 Solicitando estado del sistema...");
    sendRaspberryCommand('STATUS');
  }, [sendRaspberryCommand]);

  const clearDisconnectedSlaves = useCallback(() => {
    setRegisteredSlaves(prev => {
      const newMap = new Map();
      
      for (const [slaveId, electrode] of prev.entries()) {
        if (electrode.status !== 'disconnected') {
          newMap.set(slaveId, electrode);
        } else {
          deviceDataRef.current.delete(slaveId);
          slaveRingsRef.current.delete(slaveId);
          delete lastUpdateTimeRef.current[slaveId];
        }
      }
      
      console.log(`🗑️ Limpiados ${prev.size - newMap.size} electrodos desconectados`);
      setDeviceDataVersion(v => v + 1);
      return newMap;
    });
  }, []);

  // ==================== CONTEXT VALUE ====================

  const contextValue = {
    // Estados básicos
    connectionStatus,
    recordingStatus,
    isRecording,
    currentTime,
    wsConnected,
    
    // Dispositivos y electrodos
    connectedDevices,
    registeredSlaves,
    raspberryStatus,
    
    // Datos de electrodos
    deviceData: deviceDataRef.current,
    deviceDataVersion,
    getDeviceData,
    getSlaveHead,
    getSlaveStats,
    fillSlaveWindow,
    
    // ========== DERIVACIONES ECG ==========
    derivationStats,
    getDerivationHead,
    fillDerivationWindow,
    clearDerivations,
    
    // Setters
    setConnectionStatus,
    setRecordingStatus,
    setIsRecording,
    
    // Comandos
    handleFind,
    handleStartRecording,
    handleStopRecording,
    handleReset,
    handleExport,
    handleStatus,
    clearDisconnectedSlaves,
    sendRaspberryCommand,
    
    // Otros
    updateConnectedDevices: setConnectedDevices,
  };

  return (
    <ControlContext.Provider value={contextValue}>
      {children}
    </ControlContext.Provider>
  );
};

export default ControlContext;