import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';

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
  const [deviceDataVersion, setDeviceDataVersion] = useState(0);
  // ================================================================
  
  // ========== ESTADO PARA DERIVACIONES ECG (OPTIMIZADO CON REF) ==========
  const derivationDataRef = useRef({
    I: [], II: [], III: [], aVR: [], aVL: [], aVF: [],
    V1: [], V2: [], V3: [], V4: [], V5: [], V6: []
  });
  
  const [derivationStats, setDerivationStats] = useState({
    I: { batches: 0, samples: 0, lastSample: -1 },
    II: { batches: 0, samples: 0, lastSample: -1 },
    III: { batches: 0, samples: 0, lastSample: -1 },
    aVR: { batches: 0, samples: 0, lastSample: -1 },
    aVL: { batches: 0, samples: 0, lastSample: -1 },
    aVF: { batches: 0, samples: 0, lastSample: -1 },
    V1: { batches: 0, samples: 0, lastSample: -1 },
    V2: { batches: 0, samples: 0, lastSample: -1 },
    V3: { batches: 0, samples: 0, lastSample: -1 },
    V4: { batches: 0, samples: 0, lastSample: -1 },
    V5: { batches: 0, samples: 0, lastSample: -1 },
    V6: { batches: 0, samples: 0, lastSample: -1 }
  });
  
  const [derivationDataVersion, setDerivationDataVersion] = useState(0);
  const MAX_POINTS_PER_DERIVATION = 1500;
  // ========================================================================
  
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

  // ========== HANDLER DE DERIVACIONES ECG (OPTIMIZADO) ==========
  const handleECGDerivationBatch = useCallback((data) => {
    const { derivation, samples, batch_size, first_sample, last_sample } = data;
    
    // Validación
    if (!derivation || !samples || samples.length === 0) {
      console.warn("[ECG_BATCH] ⚠️ Batch inválido:", data);
      return;
    }

    // Validar que la derivación existe
    if (!derivationDataRef.current.hasOwnProperty(derivation)) {
      console.warn(`[ECG_BATCH] ⚠️ Derivación desconocida: ${derivation}`);
      return;
    }

    console.log(`[ECG_BATCH] 📊 ${derivation}: ${batch_size} muestras (${first_sample}-${last_sample})`);
    
    // Obtener datos actuales de la derivación
    let currentData = derivationDataRef.current[derivation] || [];
    
    // Agregar nuevas muestras
    const newSamples = samples.map(sample => ({
      sample_num: sample.sample_num,
      value: sample.value,
      timestamp: Date.now()
    }));
    
    currentData = [...currentData, ...newSamples];
    
    // Limitar a MAX_POINTS_PER_DERIVATION (mantener últimos)
    if (currentData.length > MAX_POINTS_PER_DERIVATION) {
      currentData = currentData.slice(-MAX_POINTS_PER_DERIVATION);
      console.log(`[ECG_BATCH] ✂️ ${derivation}: Recortado a ${MAX_POINTS_PER_DERIVATION} muestras`);
    }
    
    // Actualizar ref (sin causar re-render)
    derivationDataRef.current[derivation] = currentData;
    
    // Actualizar estadísticas (esto SÍ causa re-render)
    setDerivationStats(prev => ({
      ...prev,
      [derivation]: {
        batches: (prev[derivation]?.batches || 0) + 1,
        samples: (prev[derivation]?.samples || 0) + samples.length,
        lastSample: last_sample
      }
    }));
    
    // Forzar re-render para que los componentes se actualicen
    setDerivationDataVersion(v => v + 1);
    
    // Log cada 10 batches para no saturar consola
    const currentBatches = (derivationStats[derivation]?.batches || 0) + 1;
    if (currentBatches % 10 === 0) {
      console.log(`[ECG_BATCH] 📈 ${derivation}: ${currentBatches} batches, ${currentData.length} muestras en memoria`);
    }
    
  }, [derivationStats]);
  // ===============================================================

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
      Object.keys(derivationDataRef.current).forEach(key => {
        derivationDataRef.current[key] = [];
      });
      setDerivationStats({ /* reset to initial zeros */ });
      setDerivationDataVersion(0);
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
    const { slave_id, electrode_name, samples, packet_id } = data;
    
    if (!samples || samples.length === 0) {
      return;
    }
    
    let deviceHistory = deviceDataRef.current.get(slave_id) || [];
    
    samples.forEach(sample => {
      deviceHistory.push({
        timestamp: sample.timestamp_us / 1000,
        value: sample.voltage,
        adc_value: sample.adc_value,
        sample_num: sample.sample_num
      });
    });
    
    const MAX_SAMPLES = 10000;
    if (deviceHistory.length > MAX_SAMPLES) {
      deviceHistory = deviceHistory.slice(-MAX_SAMPLES);
    }
    
    deviceDataRef.current.set(slave_id, deviceHistory);
    
    const now = Date.now();
    const lastUpdate = lastUpdateTimeRef.current[slave_id] || 0;
    
    if (now - lastUpdate >= UPDATE_THROTTLE_MS) {
      lastUpdateTimeRef.current[slave_id] = now;
      setDeviceDataVersion(v => v + 1);
      
      if (packet_id % 10 === 0) {
        console.log(`📊 ${electrode_name}: ${deviceHistory.length} muestras, Paquete #${packet_id}`);
      }
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
        lastUpdateTimeRef.current = {};
        setDeviceDataVersion(0);
        
        // Limpiar derivaciones
        derivationDataRef.current = {
          I: [], II: [], III: [], aVR: [], aVL: [], aVF: [],
          V1: [], V2: [], V3: [], V4: [], V5: [], V6: []
        };
        setDerivationStats({
          I: { batches: 0, samples: 0, lastSample: -1 },
          II: { batches: 0, samples: 0, lastSample: -1 },
          III: { batches: 0, samples: 0, lastSample: -1 },
          aVR: { batches: 0, samples: 0, lastSample: -1 },
          aVL: { batches: 0, samples: 0, lastSample: -1 },
          aVF: { batches: 0, samples: 0, lastSample: -1 },
          V1: { batches: 0, samples: 0, lastSample: -1 },
          V2: { batches: 0, samples: 0, lastSample: -1 },
          V3: { batches: 0, samples: 0, lastSample: -1 },
          V4: { batches: 0, samples: 0, lastSample: -1 },
          V5: { batches: 0, samples: 0, lastSample: -1 },
          V6: { batches: 0, samples: 0, lastSample: -1 }
        });
        setDerivationDataVersion(0);
        
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
  
  const getDeviceData = useCallback((slaveId) => {
    return deviceDataRef.current.get(slaveId) || [];
  }, [deviceDataVersion]);
  
  const getDerivationData = useCallback((derivationId) => {
    const data = derivationDataRef.current[derivationId];
    if (!data) {
      console.warn(`[CONTEXT] Derivación no encontrada: ${derivationId}`);
      return [];
    }
    return data;
  }, [derivationDataVersion]);

  const getAllDerivationData = useCallback(() => {
    return { ...derivationDataRef.current };
  }, [derivationDataVersion]);
  
  const clearDerivations = useCallback(() => {
    console.log("[CONTEXT] 🗑️ Limpiando todas las derivaciones...");
    
    derivationDataRef.current = {
      I: [], II: [], III: [], aVR: [], aVL: [], aVF: [],
      V1: [], V2: [], V3: [], V4: [], V5: [], V6: []
    };
    
    setDerivationStats({
      I: { batches: 0, samples: 0, lastSample: -1 },
      II: { batches: 0, samples: 0, lastSample: -1 },
      III: { batches: 0, samples: 0, lastSample: -1 },
      aVR: { batches: 0, samples: 0, lastSample: -1 },
      aVL: { batches: 0, samples: 0, lastSample: -1 },
      aVF: { batches: 0, samples: 0, lastSample: -1 },
      V1: { batches: 0, samples: 0, lastSample: -1 },
      V2: { batches: 0, samples: 0, lastSample: -1 },
      V3: { batches: 0, samples: 0, lastSample: -1 },
      V4: { batches: 0, samples: 0, lastSample: -1 },
      V5: { batches: 0, samples: 0, lastSample: -1 },
      V6: { batches: 0, samples: 0, lastSample: -1 }
    });
    
    setDerivationDataVersion(v => v + 1);
    
    console.log("[CONTEXT] ✅ Derivaciones limpiadas");
  }, []);

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
    
    // ========== DERIVACIONES ECG ==========
    derivationData: derivationDataRef.current,
    derivationStats,
    derivationDataVersion,
    getDerivationData,
    getAllDerivationData,
    clearDerivations,
    // ======================================
    
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