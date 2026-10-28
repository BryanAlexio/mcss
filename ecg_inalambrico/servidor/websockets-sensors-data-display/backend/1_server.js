import { createServer } from "http";
import { parse } from "url";
import { WebSocketServer } from "ws";

const server = createServer();
const wss1 = new WebSocketServer({ noServer: true }); // Clientes web
const wss2 = new WebSocketServer({ noServer: true }); // ESP32 devices
const wss3 = new WebSocketServer({ noServer: true }); // Raspberry Pi control

const users = new Set();
const connectedDevices = new Map();

let raspberryPiWs = null;
let raspberryPiStatus = {
  connected: false,
  acquiring: false,
  slaves_active: [],
  slaves_finished: [],
  session_folder: null,
  electrodos_registrados: {},
  last_command: null,
  timestamp: null,
  // NUEVO: Info de usuario actual
  current_user: null
};

const registeredElectrodes = new Map();

// NUEVO: Lista de usuarios registrados en Raspberry Pi
let registeredUsers = [];

const HEARTBEAT_INTERVAL = 15000;
const HEARTBEAT_TIMEOUT = 30000;

// ==================== CLIENTES WEB ====================
wss1.on("connection", function connection(socket) {
  console.log("wss1:: User connected (Client viewer)");
  
  socket.isAlive = true;
  
  const userRef = {
    socket: socket,
    connectionDate: Date.now(),
    type: 'client'
  };
  users.add(userRef);

  socket.on('pong', () => {
    socket.isAlive = true;
  });

  sendInitialStateToClient(socket);

  socket.on('message', function(data) {
    try {
      const message = JSON.parse(data);
      
      if (message.type === 'ping') {
        socket.send(JSON.stringify({ type: 'pong', timestamp: Date.now() }));
        return;
      }
      
      if (message.type === 'request_status') {
        console.log("📡 Cliente solicita estado actual del sistema");
        sendInitialStateToClient(socket);
        return;
      }
      
      // NUEVO: Solicitud de lista de usuarios
      if (message.type === 'request_users_list') {
        console.log("📋 Cliente solicita lista de usuarios");
        if (raspberryPiWs && raspberryPiWs.readyState === 1) {
          raspberryPiWs.send(JSON.stringify({
            type: 'request_users_list',
            timestamp: Date.now()
          }));
        } else {
          socket.send(JSON.stringify({
            type: 'users_list',
            users: registeredUsers,
            from_cache: true,
            timestamp: Date.now()
          }));
        }
        return;
      }
      
      // NUEVO: Autenticación de usuario
      if (message.type === 'user_authentication') {
        console.log("🔐 Autenticando usuario:", message.user_data?.nombreCompleto);
        if (raspberryPiWs && raspberryPiWs.readyState === 1) {
          raspberryPiWs.send(JSON.stringify({
            type: 'user_authentication',
            user_data: message.user_data,
            timestamp: Date.now()
          }));
        } else {
          socket.send(JSON.stringify({
            type: 'user_auth_response',
            success: false,
            error: 'Raspberry Pi no conectada',
            timestamp: Date.now()
          }));
        }
        return;
      }
      
      if (message.type === 'raspberry_command') {
        handleRaspberryCommand(message.command, message.params);
      }
    } catch (error) {
      console.error("Error processing client message:", error);
    }
  });

  socket.on('close', () => {
    console.log("Client disconnected");
    users.delete(userRef);
  });

  socket.on('error', (error) => {
    console.error("Client socket error:", error);
    users.delete(userRef);
  });
});

function sendInitialStateToClient(socket) {
  const now = Date.now();
  
  // Enviar estado de Raspberry Pi
  socket.send(JSON.stringify({
    type: 'raspberry_status',
    status: {
      ...raspberryPiStatus,
      connected: raspberryPiWs !== null && raspberryPiWs.readyState === 1,
      registered_electrodes_count: registeredElectrodes.size,
      registered_electrodes: Array.from(registeredElectrodes.values()),
      timestamp: now
    },
    timestamp: now
  }));

  // Enviar lista de electrodos
  const electrodeList = Array.from(registeredElectrodes.values());
  socket.send(JSON.stringify({
    type: 'electrode_list',
    electrodes: electrodeList,
    timestamp: now
  }));
  
  // NUEVO: Enviar lista de usuarios si está disponible
  if (registeredUsers.length > 0) {
    socket.send(JSON.stringify({
      type: 'users_list',
      users: registeredUsers,
      timestamp: now
    }));
  }
  
  // Enviar lista de dispositivos
  const deviceList = Array.from(connectedDevices.values());
  if (deviceList.length > 0) {
    socket.send(JSON.stringify({
      type: 'device_list',
      devices: deviceList,
      timestamp: now
    }));
  }
  
  console.log(`📤 Estado inicial enviado: Raspberry=${raspberryPiStatus.connected}, Electrodos=${electrodeList.length}, Usuarios=${registeredUsers.length}`);
}

// ==================== ESP32 DEVICES (sin cambios) ====================
wss2.on("connection", function connection(ws) {
  console.log("wss2:: ESP32 device connected");
  ws.isAlive = true;
  let deviceInfo = null;

  ws.on('pong', () => {
    ws.isAlive = true;
  });

  ws.on('message', function message(data) {
    try {
      const parseData = JSON.parse(data);
      const now = Date.now();

      if (parseData.type === 'device_register') {
        deviceInfo = {
          deviceId: parseData.deviceId,
          electrode: parseData.electrode,
          leadName: parseData.leadName,
          slot: parseData.slot,
          connectionTime: now,
          lastSeen: now,
          status: 'connected'
        };
        
        connectedDevices.set(parseData.deviceId, deviceInfo);
        console.log(`Device registered: ${parseData.deviceId} - ${parseData.leadName}`);

        const registrationMessage = JSON.stringify({
          type: 'device_connected',
          device: deviceInfo,
          timestamp: now
        });
        sendMessageToClients(registrationMessage);

      } else {
        const deviceId = parseData.deviceId || deviceInfo?.deviceId;
        
        if (!deviceId || deviceId === 'Unknown') {
          return;
        }
        
        if (connectedDevices.has(deviceId)) {
          const device = connectedDevices.get(deviceId);
          device.lastSeen = now;
          connectedDevices.set(deviceId, device);
        }

        let message = { 
          date: now, 
          sensorData: parseData.value,
          deviceId: deviceId
        };

        if (deviceInfo) {
          message.electrode = deviceInfo.electrode;
          message.leadName = deviceInfo.leadName;
          message.slot = deviceInfo.slot;
        }

        const jsonMessage = JSON.stringify(message);
        sendMessageToClients(jsonMessage);
      }
    } catch (error) {
      console.error("Error parsing ESP32 message:", error);
    }
  });

  ws.on('close', () => {
    if (deviceInfo) {
      console.log(`ESP32 device disconnected: ${deviceInfo.deviceId}`);
      
      if (connectedDevices.has(deviceInfo.deviceId)) {
        const device = connectedDevices.get(deviceInfo.deviceId);
        device.status = 'disconnected';
        device.disconnectionTime = Date.now();
        connectedDevices.set(deviceInfo.deviceId, device);

        const disconnectionMessage = JSON.stringify({
          type: 'device_disconnected',
          deviceId: deviceInfo.deviceId,
          timestamp: Date.now()
        });
        sendMessageToClients(disconnectionMessage);
      }
    }
  });
});

// ==================== RASPBERRY PI CONTROL ====================
wss3.on("connection", function connection(ws) {
  console.log("wss3:: Raspberry Pi control connected");
  
  ws.isAlive = true;
  raspberryPiWs = ws;
  raspberryPiStatus.connected = true;
  raspberryPiStatus.timestamp = Date.now();
  
  ws.on('pong', () => {
    ws.isAlive = true;
    raspberryPiStatus.timestamp = Date.now();
    console.log("💗 Raspberry Pi heartbeat received");
  });

  broadcastRaspberryStatus();

  ws.on('message', function(data) {
    try {
      const message = JSON.parse(data);
      
      raspberryPiStatus.timestamp = Date.now();
      
      if (message.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now() }));
        return;
      }
      
      console.log("Raspberry Pi message:", message.type);

      // Handler para derivaciones ECG
      if (message.type === 'ecg_derivation_batch') {
        console.log(`📊 Reenviando batch ECG: ${message.derivation} (${message.batch_size} muestras)`);
        
        const ecgMessage = JSON.stringify({
          type: 'ecg_derivation_batch',
          derivation: message.derivation,
          samples: message.samples,
          batch_size: message.batch_size,
          first_sample: message.first_sample,
          last_sample: message.last_sample,
          timestamp: Date.now()
        });
        
        sendMessageToClients(ecgMessage);
        return;
      }
      
      // NUEVO: Lista de usuarios
      if (message.type === 'users_list') {
        registeredUsers = message.users || [];
        console.log(`📋 Lista de usuarios recibida: ${registeredUsers.length} usuarios`);
        
        // Broadcast a todos los clientes
        const usersMessage = JSON.stringify({
          type: 'users_list',
          users: registeredUsers,
          timestamp: Date.now()
        });
        sendMessageToClients(usersMessage);
        return;
      }
      
      // NUEVO: Respuesta de autenticación de usuario
      if (message.type === 'user_auth_response') {
        console.log("🔐 Respuesta de autenticación recibida");
        raspberryPiStatus.current_user = message.username;
        
        // Reenviar a clientes
        const authMessage = JSON.stringify({
          type: 'user_auth_response',
          success: message.success,
          username: message.username,
          timestamp: Date.now()
        });
        sendMessageToClients(authMessage);
        
        broadcastRaspberryStatus();
        return;
      }
      
      if (message.type === 'raspberry_status') {
        raspberryPiStatus = {
          ...raspberryPiStatus,
          connected: true,
          ...message.status,
          timestamp: Date.now()
        };
        
        if (message.status.electrodos_registrados) {
          Object.entries(message.status.electrodos_registrados).forEach(([slaveId, electrodeInfo]) => {
            registeredElectrodes.set(parseInt(slaveId), electrodeInfo);
          });
        }
        
        broadcastRaspberryStatus();
      }
      
      if (message.type === 'command_response') {
        console.log(`Raspberry Pi command response: ${message.command} - ${message.success ? 'OK' : 'FAILED'}`);
        
        const response = JSON.stringify({
          type: 'raspberry_command_response',
          command: message.command,
          success: message.success,
          message: message.message,
          timestamp: Date.now()
        });
        sendMessageToClients(response);
      }

      if (message.type === 'slave_register') {
        const slaveId = message.slave_id;
        const electrodeInfo = {
          slave_id: slaveId,
          electrode_name: message.electrode_name || 'Unknown',
          electrode_position: message.electrode_position || 'Unknown',
          electrode_color: message.electrode_color || '#666666',
          mac: message.mac,
          ip: message.ip,
          rssi: message.rssi,
          registered_at: Date.now()
        };
        
        registeredElectrodes.set(slaveId, electrodeInfo);
        
        console.log(`Electrodo registrado: ${electrodeInfo.electrode_name} (ID: ${slaveId})`);
        
        const registerMessage = JSON.stringify({
          type: 'slave_register',
          ...electrodeInfo,
          timestamp: Date.now()
        });
        sendMessageToClients(registerMessage);
      }

      if (message.type === 'slave_status') {
        const slaveId = message.slave_id;
        const electrode = registeredElectrodes.get(slaveId);
        
        const statusMessage = JSON.stringify({
          type: 'slave_status',
          slave_id: slaveId,
          electrode_name: message.electrode_name || electrode?.electrode_name,
          status: message.status,
          timestamp: Date.now()
        });
        sendMessageToClients(statusMessage);
      }

      if (message.type === 'data_progress') {
        const slaveId = message.slave_id;
        const electrode = registeredElectrodes.get(slaveId);
        
        const progressMessage = JSON.stringify({
          type: 'data_progress',
          slave_id: slaveId,
          electrode_name: message.electrode_name || electrode?.electrode_name,
          packets: message.packets,
          samples: message.samples,
          timestamp: Date.now()
        });
        sendMessageToClients(progressMessage);
      }

      if (message.type === 'slave_finished') {
        const slaveId = message.slave_id;
        const electrode = registeredElectrodes.get(slaveId);
        
        console.log(`Electrodo ${electrode?.electrode_name || `ID ${slaveId}`} finalizado: ${message.total_samples} muestras`);
        
        const finishedMessage = JSON.stringify({
          type: 'slave_finished',
          slave_id: slaveId,
          electrode_name: message.electrode_name || electrode?.electrode_name,
          total_packets: message.total_packets,
          total_samples: message.total_samples,
          timestamp: Date.now()
        });
        sendMessageToClients(finishedMessage);
      }
      
      if (message.type === 'slave_disconnected') {
        const slaveId = message.slave_id;
        const electrode = registeredElectrodes.get(slaveId);
        
        console.log(`Electrodo ${message.electrode_name || `ID ${slaveId}`} DESCONECTADO`);
        
        if (electrode) {
          electrode.status = 'disconnected';
          electrode.last_seen = Date.now();
          registeredElectrodes.set(slaveId, electrode);
        }
        
        const disconnectedMessage = JSON.stringify({
          type: 'slave_disconnected',
          slave_id: slaveId,
          electrode_name: message.electrode_name || electrode?.electrode_name,
          reason: message.reason,
          discovery_count: message.discovery_count,
          timestamp: Date.now()
        });
        sendMessageToClients(disconnectedMessage);
      }

      if (message.type === 'graph_data_batch') {
        console.log(`📊 Reenviando batch de gráfica: ${message.electrode_name} (${message.batch_num}/${message.total_batches})`);
        
        const graphMessage = JSON.stringify({
          type: 'graph_data_batch',
          slave_id: message.slave_id,
          electrode_name: message.electrode_name,
          batch_num: message.batch_num,
          total_batches: message.total_batches,
          data: message.data,
          timestamp: Date.now()
        });
        sendMessageToClients(graphMessage);
      }

      if (message.type === 'mqtt_data_sample') {
        const realtimeMessage = JSON.stringify({
          type: 'mqtt_data_sample',
          slave_id: message.slave_id,
          electrode_name: message.electrode_name,
          samples: message.samples,
          packet_id: message.packet_id,
          timestamp: Date.now()
        });
        sendMessageToClients(realtimeMessage);
      }
      
    } catch (error) {
      console.error("Error processing Raspberry Pi message:", error);
    }
  });

  ws.on('close', () => {
    console.log("⚠️ Raspberry Pi control disconnected");
    raspberryPiWs = null;
    
    raspberryPiStatus = {
      connected: false,
      acquiring: false,
      slaves_active: [],
      slaves_finished: [],
      session_folder: null,
      electrodos_registrados: {},
      last_command: null,
      timestamp: Date.now(),
      current_user: null
    };
    
    registeredElectrodes.clear();
    
    console.log("🧹 Limpiando estado y notificando a clientes...");
    broadcastRaspberryStatus();
    
    const emptyElectrodeMessage = JSON.stringify({
      type: 'electrode_list',
      electrodes: [],
      timestamp: Date.now()
    });
    sendMessageToClients(emptyElectrodeMessage);
  });

  ws.on('error', (error) => {
    console.error("Raspberry Pi socket error:", error);
  });
});

// ==================== HEARTBEAT INTERVALS ====================

const clientHeartbeat = setInterval(() => {
  for (const user of users) {
    if (user.socket.isAlive === false) {
      console.log("Client heartbeat timeout, terminating connection");
      user.socket.terminate();
      users.delete(user);
      continue;
    }
    
    user.socket.isAlive = false;
    user.socket.ping();
  }
}, HEARTBEAT_INTERVAL);

const raspberryHeartbeat = setInterval(() => {
  if (raspberryPiWs) {
    if (raspberryPiWs.isAlive === false) {
      console.log("⚠️ Raspberry Pi heartbeat timeout, terminating connection");
      raspberryPiWs.terminate();
      return;
    }
    
    raspberryPiWs.isAlive = false;
    raspberryPiWs.ping();
  }
}, HEARTBEAT_INTERVAL);

// ==================== FUNCIONES DE CONTROL ====================

function handleRaspberryCommand(command, params = {}) {
  console.log(`Executing Raspberry Pi command: ${command}`);
  
  if (!raspberryPiWs || raspberryPiWs.readyState !== 1) {
    console.log("ERROR: Raspberry Pi not connected");
    sendMessageToClients(JSON.stringify({
      type: 'raspberry_command_response',
      command: command,
      success: false,
      message: 'Raspberry Pi not connected',
      timestamp: Date.now()
    }));
    return;
  }
  
  if (command === 'RESET') {
    console.log("🧹 RESET: Limpiando electrodos registrados");
    registeredElectrodes.clear();
    
    const emptyMessage = JSON.stringify({
      type: 'electrode_list',
      electrodes: [],
      timestamp: Date.now()
    });
    sendMessageToClients(emptyMessage);
  }
  
  const message = {
    type: 'raspberry_command',
    command: command,
    params: params,
    timestamp: Date.now()
  };
  
  raspberryPiWs.send(JSON.stringify(message));
  console.log(`Command sent to Raspberry Pi: ${command}`);
  
  raspberryPiStatus.last_command = command;
  raspberryPiStatus.timestamp = Date.now();
  broadcastRaspberryStatus();
}

function broadcastRaspberryStatus() {
  const statusMessage = JSON.stringify({
    type: 'raspberry_status',
    status: {
      ...raspberryPiStatus,
      connected: raspberryPiWs !== null && raspberryPiWs.readyState === 1,
      registered_electrodes_count: registeredElectrodes.size,
      registered_electrodes: Array.from(registeredElectrodes.values()),
      timestamp: Date.now()
    },
    timestamp: Date.now()
  });
  
  const clientCount = sendMessageToClients(statusMessage);
  console.log(`📢 Estado Raspberry broadcast a ${clientCount} clientes: connected=${raspberryPiStatus.connected}, electrodos=${registeredElectrodes.size}, usuario=${raspberryPiStatus.current_user || 'ninguno'}`);
}

function sendMessageToClients(message) {
  let sentCount = 0;
  for (const user of users) {
    if (user.socket.readyState === 1) {
      user.socket.send(message);
      sentCount++;
    }
  }
  
  for (const user of users) {
    if (user.socket.readyState !== 1) {
      users.delete(user);
    }
  }
  
  return sentCount;
}

// ==================== SERVER SETUP ====================

server.on("upgrade", function upgrade(request, socket, head) {
  const { pathname } = parse(request.url);
  console.log(`WebSocket upgrade request: ${pathname}`);

  if (pathname === "/request") {
    wss1.handleUpgrade(request, socket, head, function done(ws) {
      wss1.emit("connection", ws, request);
    });
  } else if (pathname === "/sendSensorData") {
    wss2.handleUpgrade(request, socket, head, function done(ws) {
      wss2.emit("connection", ws, request);
    });
  } else if (pathname === "/raspberryControl") {
    wss3.handleUpgrade(request, socket, head, function done(ws) {
      wss3.emit("connection", ws, request);
    });
  } else {
    socket.destroy();
  }
});

server.listen(8080, '0.0.0.0', () => {
  console.log("==================================================");
  console.log("  WebSocket Server - Sistema ECG con Usuarios");
  console.log("==================================================");
  console.log("Server running on port 8080");
  console.log("Listening on all network interfaces (0.0.0.0)");
  console.log("");
  console.log("Endpoints:");
  console.log("  - Client: ws://<YOUR_IP>:8080/request");
  console.log("  - ESP32: ws://<YOUR_IP>:8080/sendSensorData");
  console.log("  - Raspberry Pi: ws://<YOUR_IP>:8080/raspberryControl");
  console.log("");
  console.log("Features:");
  console.log("  - Heartbeat: 15s interval, 30s timeout");
  console.log("  - Gestión de usuarios con carpetas personalizadas");
  console.log("  - Gráficas individuales con datos CSV");
  console.log("  - Soporte para múltiples discoveries");
  console.log("==================================================");
});

setInterval(() => {
  const now = Date.now();
  const TIMEOUT = 60000;

  for (const [deviceId, device] of connectedDevices.entries()) {
    if (device.status === 'connected' && (now - device.lastSeen) > TIMEOUT) {
      device.status = 'timeout';
      device.disconnectionTime = now;
      console.log(`Device ${deviceId} timed out`);
      
      const timeoutMessage = JSON.stringify({
        type: 'device_timeout',
        deviceId: deviceId,
        timestamp: now
      });
      sendMessageToClients(timeoutMessage);
    }
  }
}, 10000);

process.on('SIGINT', () => {
  console.log('\n==================================================');
  console.log('Shutting down server...');
  console.log('==================================================');
  
  clearInterval(clientHeartbeat);
  clearInterval(raspberryHeartbeat);
  
  if (raspberryPiWs) {
    raspberryPiWs.close();
  }
  
  registeredElectrodes.clear();
  
  process.exit(0);
});