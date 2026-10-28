import React, { useState, useMemo } from "react";
import {
  LineChart,
  Line,
  YAxis,
  XAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { Container, Row, Col, Card, Button, Badge } from "react-bootstrap";
import Control from "../components/Control";
import { useControlContext } from "../components/ControlContext";

const SensorDataChart = () => {
  const { registeredSlaves, getDeviceData } = useControlContext();
  const [currentView, setCurrentView] = useState("overview");
  const [selectedDeviceIndex, setSelectedDeviceIndex] = useState(0);

  const formatTimestamp = (timestamp) => {
    const date = new Date(timestamp);
    return date.toLocaleTimeString("es-MX", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  const slavesArray = Array.from(registeredSlaves.values());
  
  const currentSlave = slavesArray[selectedDeviceIndex];
  const rawData = currentSlave ? getDeviceData(currentSlave.slave_id) : [];
  
  const chartData = useMemo(() => {
    if (!currentSlave || rawData.length === 0) {
      return [];
    }
    
    console.log(`🔄 Recalculando chartData para ${currentSlave.electrode_name} (${rawData.length} muestras)`);
    
    let samplingRate = 1;
    
    if (rawData.length > 5000) {
      samplingRate = Math.ceil(rawData.length / 5000);
      console.log(`📉 Decimación activada: mostrando 1 de cada ${samplingRate} muestras`);
    }
    
    return rawData
      .filter((_, index) => index % samplingRate === 0)
      .map((point, index) => ({
        index: index * samplingRate,
        sample_num: point.sample_num || (index * samplingRate),
        time: new Date(point.timestamp).toLocaleTimeString('es-MX', {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          fractionalSecondDigits: 3
        }),
        voltage: point.value,
        adc: point.adc_value
      }));
  }, [currentSlave?.slave_id, rawData.length]);

  // ----------------------------
  // Vista General (Overview)
  // ----------------------------
  const renderOverviewScreen = () => (
    <Container fluid className="p-4">
      <Row className="justify-content-center mb-4">
        <Col xs={12}>
          <Card
            className="text-center shadow-lg border-0"
            style={{
              background: "linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)",
            }}
          >
            <Card.Body className="py-5">
              <h1
                className="main-title mb-3"
                style={{
                  fontSize: "2.5rem",
                  fontWeight: "900",
                  textShadow: "3px 3px 6px rgba(0,0,0,0.8)",
                  letterSpacing: "-0.5px",
                  color: "#ffffff",
                }}
              >
                Sistema de Adquisición ECG en Tiempo Real
              </h1>
              <p
                className="main-subtitle mb-0"
                style={{
                  fontSize: "1.2rem",
                  fontWeight: "600",
                  textShadow: "2px 2px 4px rgba(0,0,0,0.6)",
                  color: "#f8f9fa",
                }}
              >
                CICATA Querétaro - Instituto Politécnico Nacional
              </p>
            </Card.Body>
          </Card>
        </Col>
      </Row>

      <Row className="justify-content-center">
        <Col xs={12}>
          <Card className="card-ipn shadow-lg">
            <Card.Header className="card-header-ipn py-3">
              <h2
                className="text-white mb-0"
                style={{ fontWeight: "900", fontSize: "1.8rem" }}
              >
                Dispositivos Conectados ({registeredSlaves.size})
              </h2>
            </Card.Header>
            <Card.Body className="p-4">
              {registeredSlaves.size === 0 ? (
                <div className="text-center py-5">
                  <div style={{ fontSize: "4rem" }}>📡</div>
                  <h3
                    className="text-ipn-primary mt-3 mb-2"
                    style={{ fontWeight: "bold" }}
                  >
                    Esperando conexiones...
                  </h3>
                  <p className="text-muted" style={{ fontWeight: "500" }}>
                    Presione el botón "ENCONTRAR" para buscar dispositivos ESP32
                  </p>
                </div>
              ) : (
                <>
                  <Row>
                    {Array.from(registeredSlaves.values()).map((slave) => {
                      const data = getDeviceData(slave.slave_id);
                      const lastValue = data.length > 0 ? data[data.length - 1].value : 0;
                      const lastUpdate = slave.last_update
                        ? formatTimestamp(slave.last_update)
                        : "N/A";

                      return (
                        <Col md={6} lg={4} key={slave.slave_id} className="mb-4">
                          <Card
                            className="h-100 shadow-sm border-2 hover-card"
                            style={{
                              borderColor: slave.electrode_color || "#666666",
                              transition: "all 0.3s ease",
                            }}
                          >
                            <Card.Body>
                              <div className="d-flex justify-content-between align-items-center mb-3">
                                <div className="d-flex align-items-center">
                                  <div
                                    className="rounded-circle me-2"
                                    style={{
                                      width: "20px",
                                      height: "20px",
                                      backgroundColor: slave.electrode_color || "#666666",
                                      border: "2px solid white",
                                    }}
                                  ></div>
                                  <h5
                                    className="mb-0 text-ipn-primary"
                                    style={{ fontWeight: "900" }}
                                  >
                                    {slave.electrode_name || `Slave ${slave.slave_id}`}
                                  </h5>
                                </div>
                                <Badge bg={slave.status === 'disconnected' ? 'danger' : slave.finished ? 'success' : 'primary'}>
                                  {slave.status === 'disconnected' ? 'DESCONECTADO' : slave.finished ? 'FINALIZADO' : 'ACTIVO'}
                                </Badge>
                              </div>

                              <div style={{ fontSize: "0.9rem" }}>
                                <div className="d-flex justify-content-between mb-2">
                                  <strong className="text-muted">Posición:</strong>
                                  <span
                                    style={{ fontWeight: "bold", color: slave.electrode_color || "#666666" }}
                                  >
                                    {slave.electrode_position || "Unknown"}
                                  </span>
                                </div>
                                <div className="d-flex justify-content-between mb-2">
                                  <strong className="text-muted">Slave ID:</strong>
                                  <span style={{ fontWeight: "bold" }}>
                                    {slave.slave_id}
                                  </span>
                                </div>
                                {slave.mac && (
                                  <div className="d-flex justify-content-between mb-2">
                                    <strong className="text-muted">MAC:</strong>
                                    <span style={{ fontWeight: "bold", fontSize: "0.75rem" }}>
                                      {slave.mac}
                                    </span>
                                  </div>
                                )}
                                {slave.ip && (
                                  <div className="d-flex justify-content-between mb-2">
                                    <strong className="text-muted">IP:</strong>
                                    <span style={{ fontWeight: "bold" }}>
                                      {slave.ip}
                                    </span>
                                  </div>
                                )}
                                {slave.rssi && (
                                  <div className="d-flex justify-content-between mb-2">
                                    <strong className="text-muted">RSSI:</strong>
                                    <span style={{ fontWeight: "bold" }}>
                                      {slave.rssi} dBm
                                    </span>
                                  </div>
                                )}
                                {slave.samples !== undefined && (
                                  <div className="d-flex justify-content-between mb-2">
                                    <strong className="text-muted">Muestras:</strong>
                                    <span
                                      style={{
                                        fontWeight: "900",
                                        color: "#007bff",
                                        fontSize: "1.1rem",
                                      }}
                                    >
                                      {slave.finished ? slave.total_samples : slave.samples || 0}
                                    </span>
                                  </div>
                                )}
                                {slave.packets !== undefined && (
                                  <div className="d-flex justify-content-between mb-2">
                                    <strong className="text-muted">Paquetes:</strong>
                                    <span
                                      style={{
                                        fontWeight: "900",
                                        color: "#6f42c1",
                                        fontSize: "1.1rem",
                                      }}
                                    >
                                      {slave.finished ? slave.total_packets : slave.packets || 0}
                                    </span>
                                  </div>
                                )}
                                {data.length > 0 && (
                                  <div className="d-flex justify-content-between mb-2">
                                    <strong className="text-muted">Último Valor:</strong>
                                    <span
                                      style={{
                                        fontWeight: "900",
                                        color: "#28a745",
                                        fontSize: "1.1rem",
                                      }}
                                    >
                                      {lastValue.toFixed(4)} V
                                    </span>
                                  </div>
                                )}
                                <div className="d-flex justify-content-between">
                                  <strong className="text-muted">Actualizado:</strong>
                                  <span style={{ fontWeight: "bold" }}>
                                    {lastUpdate}
                                  </span>
                                </div>
                              </div>
                            </Card.Body>
                          </Card>
                        </Col>
                      );
                    })}
                  </Row>

                  <div className="text-center mt-4">
                    <Button
                      className="btn-ipn-primary shadow-lg"
                      size="lg"
                      onClick={() => {
                        setCurrentView("individual");
                        setSelectedDeviceIndex(0);
                      }}
                      style={{
                        backgroundColor: "var(--ipn-guinda)",
                        border: "none",
                        borderRadius: "25px",
                        padding: "12px 32px",
                        fontSize: "1.1rem",
                        fontWeight: "700",
                        boxShadow: "0 4px 8px rgba(0,0,0,0.3)",
                        transition: "all 0.3s ease"
                      }}
                    >
                      <i className="fas fa-chart-line me-2"></i>
                      Ver Gráficas Individuales →
                    </Button>
                  </div>
                </>
              )}
            </Card.Body>
          </Card>
        </Col>
      </Row>
    </Container>
  );

  // ----------------------------
  // Vista Individual
  // ----------------------------
  const renderIndividualScreen = () => {
    if (slavesArray.length === 0) {
      return (
        <Container fluid className="p-4">
          <Row className="justify-content-center">
            <Col xs={12}>
              <Card className="shadow-lg">
                <Card.Body className="text-center py-5">
                  <div style={{ fontSize: "4rem" }}>📊</div>
                  <h3 className="mt-3 mb-2">No hay dispositivos para mostrar</h3>
                  <p className="text-muted">
                    Primero debe encontrar dispositivos usando el botón ENCONTRAR
                  </p>
                  <Button
                    variant="primary"
                    size="lg"
                    onClick={() => setCurrentView("overview")}
                    className="mt-3"
                  >
                    <i className="fas fa-arrow-left me-2"></i>
                    Volver al Overview
                  </Button>
                </Card.Body>
              </Card>
            </Col>
          </Row>
        </Container>
      );
    }

    return (
      <Container fluid className="p-4">
        {/* Navegación superior */}
        <Row className="mb-3">
          <Col>
            <div className="d-flex justify-content-between align-items-center">
              <Button
                variant="outline-secondary"
                onClick={() => setCurrentView("overview")}
                style={{
                  borderRadius: "20px",
                  fontWeight: "600"
                }}
              >
                <i className="fas fa-arrow-left me-2"></i>
                Volver al Overview
              </Button>

              <div className="d-flex gap-2">
                <Button
                  variant="outline-primary"
                  onClick={() => setSelectedDeviceIndex(Math.max(0, selectedDeviceIndex - 1))}
                  disabled={selectedDeviceIndex === 0}
                  style={{ borderRadius: "20px" }}
                >
                  <i className="fas fa-chevron-left"></i> Anterior
                </Button>
                
                <Badge 
                  bg="secondary" 
                  style={{ 
                    fontSize: "1rem", 
                    padding: "8px 16px",
                    display: "flex",
                    alignItems: "center"
                  }}
                >
                  {selectedDeviceIndex + 1} / {slavesArray.length}
                </Badge>

                <Button
                  variant="outline-primary"
                  onClick={() => setSelectedDeviceIndex(Math.min(slavesArray.length - 1, selectedDeviceIndex + 1))}
                  disabled={selectedDeviceIndex === slavesArray.length - 1}
                  style={{ borderRadius: "20px" }}
                >
                  Siguiente <i className="fas fa-chevron-right"></i>
                </Button>
              </div>
            </div>
          </Col>
        </Row>

        {/* Información del dispositivo */}
        <Row className="mb-4">
          <Col xs={12}>
            <Card 
              className="shadow-lg border-3"
              style={{
                borderColor: currentSlave.electrode_color || "#666666"
              }}
            >
              <Card.Header 
                className="py-3"
                style={{
                  background: `linear-gradient(135deg, ${currentSlave.electrode_color || "#666666"} 0%, ${currentSlave.electrode_color || "#666666"}dd 100%)`,
                  color: "white"
                }}
              >
                <Row className="align-items-center">
                  <Col>
                    <h2 className="mb-0" style={{ fontWeight: "900" }}>
                      <i className="fas fa-heartbeat me-2"></i>
                      {currentSlave.electrode_name || `Slave ${currentSlave.slave_id}`}
                    </h2>
                    <p className="mb-0 mt-1" style={{ fontSize: "0.9rem", opacity: 0.9 }}>
                      {currentSlave.electrode_position} - Slave ID: {currentSlave.slave_id}
                    </p>
                  </Col>
                  <Col xs="auto">
                    <Badge bg={currentSlave.status === 'disconnected' ? 'danger' : currentSlave.finished ? 'success' : 'light'} 
                           className="px-3 py-2"
                           style={{ fontSize: "0.9rem" }}>
                      {currentSlave.status === 'disconnected' ? 'DESCONECTADO' : currentSlave.finished ? 'FINALIZADO' : 'ACTIVO'}
                    </Badge>
                  </Col>
                </Row>
              </Card.Header>

              <Card.Body className="p-4">
                <Row>
                  <Col md={3}>
                    <div className="stat-box text-center p-3 mb-3 mb-md-0">
                      <div className="text-muted small mb-1">Muestras</div>
                      <div className="h3 mb-0" style={{ color: "#007bff", fontWeight: "900" }}>
                        {currentSlave.finished ? currentSlave.total_samples : currentSlave.samples || 0}
                      </div>
                    </div>
                  </Col>
                  <Col md={3}>
                    <div className="stat-box text-center p-3 mb-3 mb-md-0">
                      <div className="text-muted small mb-1">Paquetes</div>
                      <div className="h3 mb-0" style={{ color: "#6f42c1", fontWeight: "900" }}>
                        {currentSlave.finished ? currentSlave.total_packets : currentSlave.packets || 0}
                      </div>
                    </div>
                  </Col>
                  <Col md={3}>
                    <div className="stat-box text-center p-3 mb-3 mb-md-0">
                      <div className="text-muted small mb-1">RSSI</div>
                      <div className="h3 mb-0" style={{ color: "#28a745", fontWeight: "900" }}>
                        {currentSlave.rssi ? `${currentSlave.rssi} dBm` : 'N/A'}
                      </div>
                    </div>
                  </Col>
                  <Col md={3}>
                    <div className="stat-box text-center p-3">
                      <div className="text-muted small mb-1">Actualización</div>
                      <div className="h6 mb-0" style={{ fontWeight: "700" }}>
                        {currentSlave.last_update ? formatTimestamp(currentSlave.last_update) : 'N/A'}
                      </div>
                    </div>
                  </Col>
                </Row>

                {currentSlave.ip && (
                  <Row className="mt-3">
                    <Col>
                      <div className="bg-light p-2 rounded text-center">
                        <small className="text-muted">
                          <strong>IP:</strong> {currentSlave.ip} | <strong>MAC:</strong> {currentSlave.mac || 'N/A'}
                        </small>
                      </div>
                    </Col>
                  </Row>
                )}
              </Card.Body>
            </Card>
          </Col>
        </Row>

        {/* Gráfica */}
        <Row>
          <Col xs={12}>
            <Card className="shadow-lg">
              <Card.Header className="bg-dark text-white py-3">
                <h4 className="mb-0" style={{ fontWeight: "700" }}>
                  <i className="fas fa-chart-area me-2"></i>
                  Señal ECG en Tiempo Real
                </h4>
              </Card.Header>
              <Card.Body className="p-4">
                {chartData.length === 0 ? (
                  <div className="text-center py-5">
                    <div style={{ fontSize: "3rem" }}>📈</div>
                    <h4 className="mt-3 text-muted">Sin datos para mostrar</h4>
                    <p className="text-muted">Los datos aparecerán aquí cuando el dispositivo comience a transmitir</p>
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height={400}>
                    <LineChart data={chartData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
                      <XAxis 
                        dataKey="sample_num"
                        label={{ value: 'Número de Muestra', position: 'insideBottom', offset: -5 }}
                        tick={{ fontSize: 12 }}
                      />
                      <YAxis 
                        domain={[0, 3.3]}
                        label={{ value: 'Voltaje (V)', angle: -90, position: 'insideLeft' }}
                        tick={{ fontSize: 12 }}
                        tickFormatter={(value) => `${value.toFixed(2)}V`}
                      />
                      <Tooltip 
                        contentStyle={{ 
                          backgroundColor: 'rgba(0,0,0,0.8)', 
                          border: 'none',
                          borderRadius: '8px',
                          color: 'white'
                        }}
                        labelFormatter={(value) => `Muestra #${value}`}
                        formatter={(value, name) => {
                          if (name === 'Voltaje') return [`${value.toFixed(4)} V`, 'Voltaje'];
                          return [value, name];
                        }}
                      />
                      <Legend />
                      <Line 
                        type="monotone" 
                        dataKey="voltage"
                        stroke={currentSlave.electrode_color || "#007bff"} 
                        strokeWidth={2}
                        dot={false}
                        name="Voltaje"
                        isAnimationActive={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                )}

                <div className="mt-3 text-center">
                  <small className="text-muted">
                    <strong>📊 Total:</strong> {rawData.length} muestras | 
                    <strong> 📈 Mostrando:</strong> {chartData.length} puntos {rawData.length > 5000 && '(decimado)'} | 
                    <strong> 📏 Rango:</strong> 0-3.3V | 
                    <strong> 🔄 Actualización:</strong> Tiempo real
                  </small>
                </div>
              </Card.Body>
            </Card>
          </Col>
        </Row>

        {/* Selector de dispositivos (thumbnails) */}
        <Row className="mt-4">
          <Col xs={12}>
            <Card className="shadow">
              <Card.Body className="p-3">
                <div className="d-flex justify-content-start align-items-center gap-3 overflow-auto">
                  {slavesArray.map((slave, index) => {
                    const slaveData = getDeviceData(slave.slave_id);
                    const dataPoints = slaveData.length;
                    
                    return (
                      <div
                        key={slave.slave_id}
                        onClick={() => setSelectedDeviceIndex(index)}
                        style={{
                          minWidth: "150px",
                          padding: "12px",
                          borderRadius: "12px",
                          border: `3px solid ${index === selectedDeviceIndex ? slave.electrode_color : '#e0e0e0'}`,
                          backgroundColor: index === selectedDeviceIndex ? `${slave.electrode_color}15` : 'white',
                          cursor: "pointer",
                          transition: "all 0.3s ease",
                          transform: index === selectedDeviceIndex ? 'scale(1.05)' : 'scale(1)'
                        }}
                        className="text-center"
                      >
                        <div
                          className="rounded-circle mx-auto mb-2"
                          style={{
                            width: "30px",
                            height: "30px",
                            backgroundColor: slave.electrode_color || "#666666",
                            border: "2px solid white"
                          }}
                        ></div>
                        <div style={{ fontSize: "0.85rem", fontWeight: "700" }}>
                          {slave.electrode_name}
                        </div>
                        <div style={{ fontSize: "0.7rem", color: "#666" }}>
                          ID: {slave.slave_id}
                        </div>
                        <div style={{ fontSize: "0.65rem", color: "#999", marginTop: "4px" }}>
                          {dataPoints.toLocaleString()} pts
                        </div>
                      </div>
                    );
                  })}
                </div>
              </Card.Body>
            </Card>
          </Col>
        </Row>
      </Container>
    );
  };

  // ----------------------------
  // Render principal
  // ----------------------------
  return (
    <div style={{ minHeight: "100vh", backgroundColor: "#FAFAFA" }}>
      <Control />
      
      {currentView === "overview" ? renderOverviewScreen() : renderIndividualScreen()}
      
      <style>{`
        .hover-card:hover {
          transform: translateY(-5px);
          box-shadow: 0 8px 16px rgba(0,0,0,0.2) !important;
        }
        
        .stat-box {
          background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
          border-radius: 12px;
          border: 1px solid #dee2e6;
        }
        
        .btn-ipn-primary:hover {
          transform: translateY(-2px);
          box-shadow: 0 6px 12px rgba(0,0,0,0.4) !important;
        }
      `}</style>
    </div>
  );
};

export default SensorDataChart;