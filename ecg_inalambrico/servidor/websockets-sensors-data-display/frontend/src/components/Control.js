import React from "react";
import { Container, Row, Col, Button, ButtonGroup, OverlayTrigger, Tooltip } from "react-bootstrap";
import { useControlContext } from "./ControlContext";

const Control = () => {
  const {
    connectionStatus,
    recordingStatus,
    registeredSlaves,
    currentTime,
    wsConnected,
    raspberryStatus,
    handleFind,
    handleStartRecording,
    handleStopRecording,
    handleReset,
    handleExport,
    handleStatus
  } = useControlContext();

  // ConfiguraciÃ³n dinÃ¡mica del botÃ³n ENCONTRAR
  const getFindButtonConfig = () => {
    if (connectionStatus === 'resetting') {
      return {
        text: 'Reseteando...',
        disabled: true,
        icon: 'fa-sync fa-spin',
        bgColor: '#e67e22',
        onClick: null
      };
    }
    
    switch (connectionStatus) {
      case 'searching':
        return {
          text: 'Buscando...',
          disabled: true,
          icon: 'fa-spinner fa-spin',
          bgColor: '#f39c12',
          onClick: null
        };
      case 'found':
        return {
          text: 'Encontrado',
          disabled: false,
          icon: 'fa-check-circle',
          bgColor: '#27ae60',
          onClick: handleFind
        };
      default:
        return {
          text: 'Encontrar',
          disabled: !wsConnected || !raspberryStatus.connected,
          icon: 'fa-search',
          bgColor: 'var(--ipn-guinda)',
          onClick: handleFind
        };
    }
  };

  const findButton = getFindButtonConfig();

  // Tooltip para el botÃ³n ENCONTRAR
  const renderFindTooltip = (props) => {
    if (connectionStatus === 'resetting') {
      return (
        <Tooltip id="find-tooltip" {...props}>
          Reiniciando sistema completo...
        </Tooltip>
      );
    }
    
    if (!raspberryStatus.connected) {
      return (
        <Tooltip id="find-tooltip" {...props}>
          Esperando conexiÃ³n de Raspberry Pi
        </Tooltip>
      );
    } else if (connectionStatus === 'found') {
      return (
        <Tooltip id="find-tooltip" {...props}>
          {registeredSlaves.size} esclavo(s) encontrado(s) - Click para buscar nuevamente
        </Tooltip>
      );
    } else if (connectionStatus === 'searching') {
      return (
        <Tooltip id="find-tooltip" {...props}>
          Buscando dispositivos ESP32...
        </Tooltip>
      );
    } else {
      return (
        <Tooltip id="find-tooltip" {...props}>
          Buscar dispositivos esclavos (comando ENCONTRAR)
        </Tooltip>
      );
    }
  };

  // Determinar color del indicador de sistema
  const getSystemIndicatorColor = () => {
    if (connectionStatus === 'resetting') {
      return '#e67e22'; // Naranja - Reseteando
    } else if (!raspberryStatus.connected) {
      return '#e74c3c'; // Rojo - Raspberry no conectada
    } else if (recordingStatus === 'recording') {
      return '#e74c3c'; // Rojo - Grabando (REC)
    } else if (connectionStatus === 'found') {
      return '#27ae60'; // Verde - Sistema listo
    } else {
      return '#7f8c8d'; // Gris - Raspberry conectada pero sin dispositivos
    }
  };

  const getSystemStatus = () => {
    if (connectionStatus === 'resetting') {
      return 'RESET';
    } else if (!raspberryStatus.connected) {
      return 'OFF';
    } else if (recordingStatus === 'recording') {
      return 'REC';
    } else if (connectionStatus === 'found') {
      return 'ON';
    } else {
      return 'IDLE';
    }
  };

  return (
    <div style={{
      background: 'linear-gradient(135deg, #2c3e50 0%, #34495e 100%)',
      borderBottom: '3px solid var(--ipn-oro)',
      padding: '8px 0',
      boxShadow: '0 3px 6px rgba(0,0,0,0.2)'
    }}>
      <Container fluid>
        <Row className="align-items-center justify-content-between">
          {/* SecciÃ³n izquierda - ENCONTRAR y RESET */}
          <Col xs="auto">
            <div className="d-flex align-items-center gap-3">
              <div className="d-flex align-items-center gap-2">
                {/* BotÃ³n ENCONTRAR */}
                <OverlayTrigger
                  placement="bottom"
                  overlay={renderFindTooltip}
                >
                  <Button
                    size="sm"
                    onClick={findButton.onClick}
                    disabled={findButton.disabled}
                    className="control-btn"
                    style={{
                      backgroundColor: findButton.bgColor,
                      border: 'none',
                      borderRadius: '20px',
                      padding: '6px 16px',
                      fontSize: '0.85rem',
                      fontWeight: '600',
                      color: 'white',
                      minWidth: '120px',
                      boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
                      transition: 'all 0.2s ease',
                      cursor: findButton.disabled ? 'not-allowed' : 'pointer'
                    }}
                  >
                    <i className={`fas ${findButton.icon} me-1`}></i>
                    {findButton.text}
                  </Button>
                </OverlayTrigger>

                {/* BotÃ³n RESET */}
                <OverlayTrigger
                  placement="bottom"
                  overlay={
                    <Tooltip id="reset-tooltip">
                      {connectionStatus === 'resetting' 
                        ? 'Reinicio en progreso...' 
                        : 'Reiniciar sistema completo (toma ~6 segundos)'}
                    </Tooltip>
                  }
                >
                  <Button
                    size="sm"
                    onClick={handleReset}
                    disabled={!raspberryStatus.connected || connectionStatus === 'resetting'}
                    className="control-btn reset-btn"
                    style={{
                      backgroundColor: connectionStatus === 'resetting' ? '#e67e22' : '#c0392b',
                      border: 'none',
                      borderRadius: '20px',
                      padding: '6px 16px',
                      fontSize: '0.85rem',
                      fontWeight: '600',
                      color: 'white',
                      minWidth: '90px',
                      boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
                      transition: 'all 0.2s ease',
                      opacity: (!raspberryStatus.connected || connectionStatus === 'resetting') ? 0.6 : 1
                    }}
                  >
                    <i className={`fas ${connectionStatus === 'resetting' ? 'fa-sync fa-spin' : 'fa-redo'} me-1`}></i>
                    {connectionStatus === 'resetting' ? 'Reseteando' : 'Reset'}
                  </Button>
                </OverlayTrigger>
              </div>
              
              <div className="d-flex align-items-center gap-2">
                {/* Contador de esclavos registrados */}
                <OverlayTrigger
                  placement="bottom"
                  overlay={
                    <Tooltip id="slaves-tooltip">
                      Esclavos ESP32 registrados
                      {registeredSlaves.size > 0 && (
                        <div className="mt-1">
                          {Array.from(registeredSlaves.values()).map(slave => (
                            <div key={slave.slave_id} style={{ fontSize: '0.75rem' }}>
                              ID: {slave.slave_id} | RSSI: {slave.rssi}
                            </div>
                          ))}
                        </div>
                      )}
                    </Tooltip>
                  }
                >
                  <span 
                    className="device-counter"
                    style={{
                      backgroundColor: connectionStatus === 'resetting' ? '#e67e22' :
                                     registeredSlaves.size >= 3 ? '#27ae60' : 
                                     registeredSlaves.size > 0 ? '#f39c12' : '#7f8c8d',
                      color: 'white',
                      fontSize: '0.75rem',
                      padding: '4px 8px',
                      borderRadius: '12px',
                      fontWeight: 'bold',
                      transition: 'background-color 0.3s ease'
                    }}
                  >
                    <i className="fas fa-microchip me-1"></i>
                    {registeredSlaves.size}/10
                  </span>
                </OverlayTrigger>

                {/* Indicador de conexiÃ³n WebSocket */}
                <OverlayTrigger
                  placement="bottom"
                  overlay={
                    <Tooltip id="ws-tooltip">
                      Servidor: {wsConnected ? 'Conectado' : 'Desconectado'}
                    </Tooltip>
                  }
                >
                  <span 
                    style={{
                      width: '8px',
                      height: '8px',
                      borderRadius: '50%',
                      backgroundColor: wsConnected ? '#27ae60' : '#e74c3c',
                      display: 'inline-block',
                      animation: wsConnected ? 'none' : 'pulse-red 1.5s infinite'
                    }}
                  />
                </OverlayTrigger>

                {/* Indicador de Raspberry Pi */}
                <OverlayTrigger
                  placement="bottom"
                  overlay={
                    <Tooltip id="rpi-tooltip">
                      Raspberry Pi: {raspberryStatus.connected ? 'Conectada' : 'Desconectada'}
                    </Tooltip>
                  }
                >
                  <span 
                    style={{
                      width: '8px',
                      height: '8px',
                      borderRadius: '50%',
                      backgroundColor: raspberryStatus.connected ? '#27ae60' : '#e74c3c',
                      display: 'inline-block',
                      animation: raspberryStatus.connected ? 'none' : 'pulse-red 1.5s infinite'
                    }}
                  />
                </OverlayTrigger>
              </div>
            </div>
          </Col>

          {/* SecciÃ³n central - Controles de grabaciÃ³n */}
          <Col xs="auto">
            <ButtonGroup>
              <OverlayTrigger
                placement="bottom"
                overlay={
                  <Tooltip id="record-tooltip">
                    {connectionStatus === 'resetting' 
                      ? 'Sistema en reinicio'
                      : registeredSlaves.size === 0
                      ? 'Primero encuentre dispositivos' 
                      : recordingStatus === 'recording' 
                      ? 'GrabaciÃ³n en curso' 
                      : 'Iniciar grabaciÃ³n (comando INICIAR)'}
                  </Tooltip>
                }
              >
                <Button
                  size="sm"
                  onClick={handleStartRecording}
                  disabled={registeredSlaves.size === 0 || recordingStatus === 'recording' || connectionStatus === 'resetting'}
                  className="control-btn record-btn"
                  style={{
                    backgroundColor: recordingStatus === 'recording' ? '#e74c3c' : '#27ae60',
                    border: 'none',
                    borderRadius: '20px 0 0 20px',
                    padding: '6px 16px',
                    fontSize: '0.85rem',
                    fontWeight: '600',
                    color: 'white',
                    minWidth: '100px',
                    boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
                    transition: 'all 0.2s ease',
                    opacity: (registeredSlaves.size === 0 || connectionStatus === 'resetting') ? 0.6 : 1
                  }}
                >
                  <i className={`fas ${recordingStatus === 'recording' ? 'fa-circle pulse-dot' : 'fa-play'} me-1`}></i>
                  {recordingStatus === 'recording' ? 'Grabando' : 'Grabar'}
                </Button>
              </OverlayTrigger>
              
              <OverlayTrigger
                placement="bottom"
                overlay={
                  <Tooltip id="stop-tooltip">
                    {recordingStatus !== 'recording' 
                      ? 'No hay grabaciÃ³n activa' 
                      : 'Finalizar grabaciÃ³n (comando FINALIZAR)'}
                  </Tooltip>
                }
              >
                <Button
                  size="sm"
                  onClick={handleStopRecording}
                  disabled={recordingStatus !== 'recording'}
                  className="control-btn"
                  style={{
                    backgroundColor: '#e67e22',
                    border: 'none',
                    borderRadius: '0 20px 20px 0',
                    padding: '6px 16px',
                    fontSize: '0.85rem',
                    fontWeight: '600',
                    color: 'white',
                    minWidth: '80px',
                    boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
                    transition: 'all 0.2s ease',
                    opacity: recordingStatus !== 'recording' ? 0.6 : 1
                  }}
                >
                  <i className="fas fa-stop me-1"></i>
                  Parar
                </Button>
              </OverlayTrigger>
            </ButtonGroup>
          </Col>

          {/* SecciÃ³n derecha - Estado y exportar */}
          <Col xs="auto">
            <div className="d-flex align-items-center gap-3">
              <div className="d-flex align-items-center gap-2">
                {/* Badge de estado del sistema */}
                <OverlayTrigger
                  placement="bottom"
                  overlay={
                    <Tooltip id="status-tooltip">
                      {connectionStatus === 'resetting'
                        ? 'Reiniciando sistema (ESP32s + Raspberry Pi)'
                        : !raspberryStatus.connected 
                        ? 'Raspberry Pi desconectada' 
                        : recordingStatus === 'recording' 
                        ? 'GrabaciÃ³n en curso' 
                        : connectionStatus === 'found' 
                        ? 'Sistema listo' 
                        : 'Sistema en espera'}
                    </Tooltip>
                  }
                >
                  <span 
                    className="status-badge"
                    style={{
                      backgroundColor: getSystemIndicatorColor(),
                      color: 'white',
                      fontSize: '0.7rem',
                      padding: '3px 8px',
                      borderRadius: '10px',
                      fontWeight: 'bold',
                      animation: (recordingStatus === 'recording' || connectionStatus === 'resetting') ? 'pulse-status 2s infinite' : 'none',
                      border: 'none',
                      cursor: 'default'
                    }}
                  >
                    <i className={`fas ${connectionStatus === 'resetting' ? 'fa-sync fa-spin' :
                                       !raspberryStatus.connected ? 'fa-times' :
                                       recordingStatus === 'recording' ? 'fa-circle' : 
                                       connectionStatus === 'found' ? 'fa-check' : 'fa-pause'} me-1`}></i>
                    {getSystemStatus()}
                  </span>
                </OverlayTrigger>
                
                {/* Reloj */}
                <small className="text-light" style={{ fontSize: '0.75rem', fontFamily: 'monospace' }}>
                  {currentTime.toLocaleTimeString('es-MX', { 
                    hour: '2-digit', 
                    minute: '2-digit',
                    second: '2-digit'
                  })}
                </small>
              </div>
              
              {/* BotÃ³n de estado/info */}
              <OverlayTrigger
                placement="bottom"
                overlay={
                  <Tooltip id="info-tooltip">
                    Ver estadÃ­sticas del sistema
                  </Tooltip>
                }
              >
                <Button
                  size="sm"
                  onClick={handleStatus}
                  disabled={!raspberryStatus.connected || connectionStatus === 'resetting'}
                  className="control-btn info-btn"
                  style={{
                    backgroundColor: '#3498db',
                    border: 'none',
                    borderRadius: '50%',
                    width: '32px',
                    height: '32px',
                    padding: '0',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
                    transition: 'all 0.2s ease',
                    opacity: (!raspberryStatus.connected || connectionStatus === 'resetting') ? 0.6 : 1
                  }}
                >
                  <i className="fas fa-info" style={{ fontSize: '0.85rem' }}></i>
                </Button>
              </OverlayTrigger>
              
              {/* BotÃ³n de exportar */}
              <OverlayTrigger
                placement="bottom"
                overlay={
                  <Tooltip id="export-tooltip">
                    {recordingStatus === 'recording' 
                      ? 'Detenga la grabaciÃ³n primero' 
                      : connectionStatus === 'resetting'
                      ? 'Sistema en reinicio'
                      : 'Exportar datos registrados'}
                  </Tooltip>
                }
              >
                <Button
                  size="sm"
                  onClick={handleExport}
                  disabled={recordingStatus === 'recording' || connectionStatus === 'resetting'}
                  className="control-btn export-btn"
                  style={{
                    backgroundColor: '#8e44ad',
                    border: 'none',
                    borderRadius: '20px',
                    padding: '6px 16px',
                    fontSize: '0.85rem',
                    fontWeight: '600',
                    color: 'white',
                    minWidth: '80px',
                    boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
                    transition: 'all 0.2s ease',
                    opacity: (recordingStatus === 'recording' || connectionStatus === 'resetting') ? 0.6 : 1
                  }}
                >
                  <i className="fas fa-download me-1"></i>
                  Export
                </Button>
              </OverlayTrigger>
            </div>
          </Col>
        </Row>

        {/* Barra de progreso de bÃºsqueda o reset */}
        {(connectionStatus === 'searching' || connectionStatus === 'resetting') && (
          <Row className="mt-2">
            <Col>
              <div className="progress" style={{ height: '2px', borderRadius: '1px', backgroundColor: 'rgba(255,255,255,0.1)' }}>
                <div 
                  className="progress-bar" 
                  style={{ 
                    width: '100%',
                    backgroundColor: connectionStatus === 'resetting' ? '#e67e22' : '#f39c12',
                    animation: 'progress-slide 1.5s ease-in-out infinite'
                  }}
                ></div>
              </div>
              {connectionStatus === 'resetting' && (
                <div style={{
                  backgroundColor: 'rgba(230, 126, 34, 0.2)',
                  borderRadius: '4px',
                  padding: '4px 8px',
                  fontSize: '0.7rem',
                  color: 'rgba(255,255,255,0.9)',
                  marginTop: '4px'
                }}>
                  <i className="fas fa-sync fa-spin me-1"></i>
                  Reiniciando servidor y ESP32s... (~6 segundos)
                </div>
              )}
            </Col>
          </Row>
        )}

        {/* InformaciÃ³n de esclavos registrados (cuando se encuentran) */}
        {connectionStatus === 'found' && registeredSlaves.size > 0 && (
          <Row className="mt-2">
            <Col>
              <div style={{
                backgroundColor: 'rgba(39, 174, 96, 0.2)',
                borderRadius: '4px',
                padding: '4px 8px',
                fontSize: '0.7rem',
                color: 'rgba(255,255,255,0.9)'
              }}>
                <i className="fas fa-check-circle me-1"></i>
                {registeredSlaves.size} esclavo(s) encontrado(s)
                {raspberryStatus.session_folder && (
                  <span className="ms-2">
                    | Sesión: {raspberryStatus.session_folder}
                  </span>
                )}
              </div>
            </Col>
          </Row>
        )}

        {/* Info de Raspberry Pi (solo en desarrollo o debug) */}
        {process.env.NODE_ENV === 'development' && raspberryStatus.connected && (
          <Row className="mt-2">
            <Col>
              <div style={{
                backgroundColor: 'rgba(0,0,0,0.2)',
                borderRadius: '4px',
                padding: '4px 8px',
                fontSize: '0.7rem',
                color: 'rgba(255,255,255,0.7)'
              }}>
                <i className="fas fa-microchip me-1"></i>
                Raspberry Pi: {raspberryStatus.acquiring ? 'Adquiriendo' : 'Listo'} | 
                Esclavos activos: {raspberryStatus.slaves_active?.length || 0} | 
                Esclavos finalizados: {raspberryStatus.slaves_finished?.length || 0}
              </div>
            </Col>
          </Row>
        )}
      </Container>

      {/* Estilos CSS */}
      <style>{`
        .control-btn:hover:not(:disabled) {
          transform: translateY(-1px);
          box-shadow: 0 4px 8px rgba(0,0,0,0.3) !important;
        }
        
        .control-btn:active:not(:disabled) {
          transform: translateY(0);
          box-shadow: 0 2px 4px rgba(0,0,0,0.2) !important;
        }
        
        .control-btn:disabled {
          cursor: not-allowed;
        }
        
        .reset-btn:hover:not(:disabled) {
          background-color: #e74c3c !important;
        }
        
        .pulse-dot {
          animation: pulse-red 1s infinite;
        }
        
        .device-counter {
          transition: background-color 0.3s ease;
        }
        
        .status-badge {
          transition: all 0.3s ease;
        }
        
        .info-btn:hover:not(:disabled) {
          transform: scale(1.1) !important;
        }
        
        @keyframes pulse-red {
          0% { opacity: 1; }
          50% { opacity: 0.4; }
          100% { opacity: 1; }
        }
        
        @keyframes pulse-status {
          0% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.8; transform: scale(1.05); }
          100% { opacity: 1; transform: scale(1); }
        }
        
        @keyframes progress-slide {
          0% { transform: translateX(-100%); }
          50% { transform: translateX(0%); }
          100% { transform: translateX(100%); }
        }
        
        /* Responsive adjustments */
        @media (max-width: 992px) {
          .control-btn {
            min-width: 70px !important;
            padding: 5px 10px !important;
            font-size: 0.75rem !important;
          }
          
          .device-counter,
          .status-badge {
            font-size: 0.65rem !important;
            padding: 3px 6px !important;
          }
        }
        
        @media (max-width: 768px) {
          .control-btn {
            min-width: 60px !important;
            padding: 4px 8px !important;
            font-size: 0.7rem !important;
          }
          
          .info-btn {
            width: 28px !important;
            height: 28px !important;
          }
        }
      `}</style>
    </div>
  );
};

export default Control;