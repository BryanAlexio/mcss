import React, { createContext, useContext, useEffect, useState, useRef } from "react";

const AuthContext = createContext(null);

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth debe ser usado dentro de AuthProvider');
  }
  return context;
};

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [userHistory, setUserHistory] = useState([]);
  const wsRef = useRef(null);
  const userListRequestedRef = useRef(false);
  const lastUserListUpdateRef = useRef(0);

  useEffect(() => {
    checkUrlFlow();
    connectToRaspberryPi();
    
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  const connectToRaspberryPi = () => {
    try {
      const ws = new WebSocket('ws://localhost:8080/request');
      
      ws.onopen = () => {
        console.log('🔗 Conectado al servidor para autenticación');
        wsRef.current = ws;
        
        if (!userListRequestedRef.current) {
          requestUsersListInternal();
        }
      };
      
      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          
          if (message.type === 'users_list') {
            console.log('📋 Lista de usuarios recibida:', message.users.length);
            
            const raspberryUsers = message.users.map(rpUser => ({
              id: rpUser.username || rpUser.id_sistema,
              nombres: rpUser.nombres || '',
              apellidoPaterno: rpUser.apellido_paterno || '',
              apellidoMaterno: rpUser.apellido_materno || '',
              nombreCompleto: rpUser.nombre_completo || rpUser.username,
              edad: rpUser.edad || 0,
              sexo: rpUser.sexo || 'no especificado',
              // FECHAS REALES desde Raspberry Pi
              fechaRegistro: rpUser.fecha_registro || '',
              fecha_registro: rpUser.fecha_registro || '',
              fechaUltimoUso: rpUser.timestamp || rpUser.fecha_acceso || '',
              ultimoAcceso: rpUser.timestamp || rpUser.fecha_acceso || '',
              fromRaspberry: true,
              totalSesiones: rpUser.total_sesiones || 0,
              ultimaSesion: rpUser.ultima_sesion || 1
            }));
            
            setUserHistory(raspberryUsers);
            localStorage.setItem('ipnUserHistory', JSON.stringify(raspberryUsers));
            lastUserListUpdateRef.current = Date.now();
            
            // Si hay un usuario autenticado, actualizar sus fechas
            const currentUser = localStorage.getItem('ipnUser');
            if (currentUser) {
              const userData = JSON.parse(currentUser);
              const matchingUser = raspberryUsers.find(u => 
                u.id === userData.id || 
                (u.nombres === userData.nombres && u.apellidoPaterno === userData.apellidoPaterno)
              );
              
              if (matchingUser) {
                const updatedUser = {
                  ...userData,
                  fechaRegistro: matchingUser.fechaRegistro,
                  fecha_registro: matchingUser.fecha_registro,
                  fechaUltimoUso: matchingUser.fechaUltimoUso,
                  ultimoAcceso: matchingUser.ultimoAcceso,
                  totalSesiones: matchingUser.totalSesiones,
                  ultimaSesion: matchingUser.ultimaSesion
                };
                
                localStorage.setItem('ipnUser', JSON.stringify(updatedUser));
                setUser(updatedUser);
                console.log('✅ Fechas del usuario actual sincronizadas');
              }
            }
            
            console.log('✅ Historial sincronizado con Raspberry Pi');
            
            // Debug: Mostrar fechas en consola
            raspberryUsers.forEach(u => {
              console.log(`  Usuario: ${u.nombreCompleto}`);
              console.log(`    Registrado: ${u.fechaRegistro}`);
              console.log(`    Último uso: ${u.fechaUltimoUso}`);
            });
          }
          
          if (message.type === 'user_auth_response') {
            console.log('✅ Respuesta de autenticación:', message);
            
            if (message.success) {
              console.log(`Usuario autenticado: ${message.username}`);
              
              // Solicitar lista actualizada después de autenticación
              setTimeout(() => {
                requestUsersListInternal();
              }, 1500);
            }
          }
        } catch (error) {
          console.error('Error procesando mensaje:', error);
        }
      };
      
      ws.onerror = (error) => {
        console.error('❌ Error WebSocket:', error);
      };
      
      ws.onclose = () => {
        console.log('🔌 WebSocket cerrado, reintentando en 5s...');
        userListRequestedRef.current = false;
        setTimeout(connectToRaspberryPi, 5000);
      };
      
    } catch (error) {
      console.error('Error conectando WebSocket:', error);
    }
  };

  const requestUsersListInternal = () => {
    const now = Date.now();
    const timeSinceLastUpdate = now - lastUserListUpdateRef.current;
    
    if (timeSinceLastUpdate < 2000) {
      console.log('⏳ Ignorando request (throttle 2s)');
      return;
    }
    
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      console.log('📤 Solicitando lista de usuarios...');
      wsRef.current.send(JSON.stringify({
        type: 'request_users_list',
        timestamp: now
      }));
      userListRequestedRef.current = true;
    }
  };

  const authenticateWithRaspberryPi = (userData) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      console.log('📤 Enviando autenticación a Raspberry Pi:', userData.nombreCompleto);
      
      wsRef.current.send(JSON.stringify({
        type: 'user_authentication',
        user_data: {
          nombres: userData.nombres,
          apellidoPaterno: userData.apellidoPaterno,
          apellidoMaterno: userData.apellidoMaterno,
          nombreCompleto: userData.nombreCompleto,
          edad: userData.edad,
          sexo: userData.sexo,
          id: userData.id,
          fechaRegistro: userData.fechaRegistro || userData.fecha_registro
        },
        timestamp: Date.now()
      }));
      
      return true;
    } else {
      console.warn('⚠️ WebSocket no conectado');
      return false;
    }
  };

  const checkUrlFlow = () => {
    try {
      const urlParams = new URLSearchParams(window.location.search);
      const flowParam = urlParams.get('flow');
      
      console.log('🔧 Verificando parámetro de flujo:', flowParam);

      switch (flowParam) {
        case 'welcome':
          forceWelcomeFlow();
          break;
          
        case 'register':
          forceRegisterFlow();
          break;
          
        case 'demo':
          createDemoUser();
          break;
          
        case 'clear':
          clearAllData();
          break;

        case 'auto':
          checkExistingUser();
          break;
          
        default:
          forceWelcomeFlow();
          break;
      }
    } catch (error) {
      console.error('Error en checkUrlFlow:', error);
      forceWelcomeFlow();
    }
  };

  const forceWelcomeFlow = () => {
    localStorage.removeItem('ipnUser');
    localStorage.removeItem('ipnLogoutTime');
    setUser(null);
    setIsAuthenticated(false);
    setIsLoading(false);
  };

  const forceRegisterFlow = () => {
    localStorage.removeItem('ipnUser');
    localStorage.removeItem('ipnLogoutTime');
    setUser(null);
    setIsAuthenticated(false);
    setIsLoading(false);
  };

  const clearAllData = () => {
    localStorage.removeItem('ipnUser');
    localStorage.removeItem('ipnLogoutTime');
    localStorage.removeItem('ipnUserHistory');
    localStorage.removeItem('ipnSessionData');
    setUser(null);
    setIsAuthenticated(false);
    setUserHistory([]);
    setIsLoading(false);
    console.log('💾 Todos los datos locales eliminados');
  };

  const checkExistingUser = () => {
    try {
      const logoutTimestamp = localStorage.getItem('ipnLogoutTime');
      const now = Date.now();
      
      if (logoutTimestamp && (now - parseInt(logoutTimestamp)) < 5000) {
        console.log('Logout reciente detectado');
        localStorage.removeItem('ipnUser');
        localStorage.removeItem('ipnLogoutTime');
        setUser(null);
        setIsAuthenticated(false);
        setIsLoading(false);
        return;
      }

      if (logoutTimestamp) {
        localStorage.removeItem('ipnLogoutTime');
      }

      const savedUser = localStorage.getItem('ipnUser');
      if (savedUser) {
        const userData = JSON.parse(savedUser);
        
        if (userData && userData.id && userData.nombres && userData.apellidoPaterno) {
          setUser(userData);
          setIsAuthenticated(true);
          authenticateWithRaspberryPi(userData);
          console.log('Usuario existente cargado:', userData.nombreCompleto);
        } else {
          localStorage.removeItem('ipnUser');
          setUser(null);
          setIsAuthenticated(false);
        }
      } else {
        setUser(null);
        setIsAuthenticated(false);
      }
    } catch (error) {
      console.error('Error al recuperar datos del usuario:', error);
      localStorage.removeItem('ipnUser');
      localStorage.removeItem('ipnLogoutTime');
      setUser(null);
      setIsAuthenticated(false);
    } finally {
      setIsLoading(false);
    }
  };

  const addToUserHistory = (userData) => {
    console.log('✅ Usuario será agregado automáticamente por Raspberry Pi');
  };

  const loadUserFromHistory = (historicalUser) => {
    try {
      // Usar las fechas REALES del historial (desde Raspberry Pi)
      const userRecord = {
        ...historicalUser,
        id: historicalUser.id || generateUserId(),
        // Mantener las fechas originales de Raspberry Pi
        fechaRegistro: historicalUser.fechaRegistro || historicalUser.fecha_registro,
        fecha_registro: historicalUser.fecha_registro || historicalUser.fechaRegistro,
        ultimoAcceso: new Date().toISOString(), // Actualizar acceso actual
        fechaUltimoUso: new Date().toISOString() // Actualizar acceso actual
      };

      localStorage.setItem('ipnUser', JSON.stringify(userRecord));
      
      setUser(userRecord);
      setIsAuthenticated(true);
      
      authenticateWithRaspberryPi(userRecord);

      console.log('👤 Usuario cargado desde historial:', userRecord.nombreCompleto);
      console.log('   Fecha registro:', userRecord.fechaRegistro);
      console.log('   Último acceso:', userRecord.ultimoAcceso);
      
      return { success: true, user: userRecord };
    } catch (error) {
      console.error('Error cargando usuario desde historial:', error);
      return { success: false, error: error.message };
    }
  };

  const registerUser = (userData) => {
    try {
      if (!validateUserData(userData)) {
        throw new Error('Datos de usuario inválidos');
      }

      localStorage.removeItem('ipnLogoutTime');

      const now = new Date().toISOString();
      const userRecord = {
        ...userData,
        id: generateUserId(),
        fechaRegistro: now,
        fecha_registro: now,
        ultimoAcceso: now,
        fechaUltimoUso: now,
        nombreCompleto: `${userData.nombres} ${userData.apellidoPaterno} ${userData.apellidoMaterno}`
      };

      localStorage.setItem('ipnUser', JSON.stringify(userRecord));
      
      setUser(userRecord);
      setIsAuthenticated(true);
      
      const raspberryAuth = authenticateWithRaspberryPi(userRecord);
      if (raspberryAuth) {
        console.log('✅ Usuario autenticando en Raspberry Pi...');
      } else {
        console.warn('⚠️ No se pudo autenticar en Raspberry Pi');
      }

      console.log('Usuario registrado exitosamente:', userRecord.nombreCompleto);
      console.log('   Fecha registro:', userRecord.fechaRegistro);
      
      return { success: true, user: userRecord };
    } catch (error) {
      console.error('Error al registrar usuario:', error);
      return { success: false, error: error.message };
    }
  };

  const createDemoUser = () => {
    const demoData = {
      nombres: 'Demo',
      apellidoPaterno: 'Usuario',
      apellidoMaterno: 'IPN',
      edad: 25,
      sexo: 'masculino'
    };
    
    const result = registerUser(demoData);
    if (result.success) {
      console.log('🧪 Usuario demo creado exitosamente');
    }
    setIsLoading(false);
  };

  const logout = () => {
    try {
      localStorage.setItem('ipnLogoutTime', Date.now().toString());
      localStorage.removeItem('ipnUser');
      
      setUser(null);
      setIsAuthenticated(false);
      
      console.log('Logout ejecutado correctamente');
      return { success: true };
    } catch (error) {
      console.error('Error al cerrar sesión:', error);
      return { success: false, error: error.message };
    }
  };

  const updateLastAccess = () => {
    if (user && isAuthenticated) {
      const now = new Date().toISOString();
      const updatedUser = {
        ...user,
        ultimoAcceso: now,
        fechaUltimoUso: now
      };
      localStorage.setItem('ipnUser', JSON.stringify(updatedUser));
      setUser(updatedUser);
    }
  };

  const clearSession = () => {
    localStorage.removeItem('ipnUser');
    localStorage.removeItem('ipnLogoutTime');
    localStorage.removeItem('ipnSessionData');
    setUser(null);
    setIsAuthenticated(false);
    setIsLoading(false);
    console.log('Sesión limpiada completamente');
  };

  const forceLogout = () => {
    clearSession();
    window.location.reload();
  };

  const removeFromHistory = (userToRemove) => {
    console.warn('⚠️ Para eliminar un usuario, debe eliminarse su carpeta en Raspberry Pi');
    console.log('Carpeta: /home/bryan/usuariosecg/' + userToRemove.id);
  };

  const validateUserData = (userData) => {
    const required = ['nombres', 'apellidoPaterno', 'apellidoMaterno', 'edad', 'sexo'];
    
    for (const field of required) {
      if (!userData[field]) {
        console.log(`Campo faltante: ${field}`);
        return false;
      }
    }

    if (userData.edad < 15 || userData.edad > 100) {
      console.log('Edad fuera del rango válido');
      return false;
    }

    const namePattern = /^[a-zA-ZáéíóúÁÉÍÓÚñÑ'\s]+$/;
    if (!namePattern.test(userData.nombres) || 
        !namePattern.test(userData.apellidoPaterno) || 
        !namePattern.test(userData.apellidoMaterno)) {
      console.log('Formato de nombres inválido');
      return false;
    }

    return true;
  };

  const generateUserId = () => {
    return 'user_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
  };

  const capitalizeWords = (str) => {
    return str.toLowerCase().replace(/\b\w/g, l => l.toUpperCase());
  };

  const formatUserData = (userData) => {
    return {
      ...userData,
      nombres: capitalizeWords(userData.nombres.trim()),
      apellidoPaterno: capitalizeWords(userData.apellidoPaterno.trim()),
      apellidoMaterno: capitalizeWords(userData.apellidoMaterno.trim())
    };
  };

  const getDebugInfo = () => {
    return {
      user,
      isAuthenticated,
      isLoading,
      userHistoryCount: userHistory.length,
      wsConnected: wsRef.current?.readyState === WebSocket.OPEN,
      lastUpdate: new Date(lastUserListUpdateRef.current).toLocaleTimeString(),
      localStorage: {
        ipnUser: localStorage.getItem('ipnUser'),
        ipnLogoutTime: localStorage.getItem('ipnLogoutTime'),
        ipnUserHistory: localStorage.getItem('ipnUserHistory')
      }
    };
  };

  const contextValue = {
    user,
    isAuthenticated,
    isLoading,
    userHistory,
    registerUser,
    logout,
    clearSession,
    forceLogout,
    updateLastAccess,
    formatUserData,
    capitalizeWords,
    validateUserData,
    loadUserFromHistory,
    removeFromHistory,
    addToUserHistory,
    createDemoUser,
    clearAllData,
    getDebugInfo,
    requestUsersList: requestUsersListInternal
  };

  return (
    <AuthContext.Provider value={contextValue}>
      {children}
    </AuthContext.Provider>
  );
};