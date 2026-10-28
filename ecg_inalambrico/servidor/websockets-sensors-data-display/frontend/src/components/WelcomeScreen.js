import React, { useState } from "react";
import { Container, Row, Col, Card, Button, Modal, Badge } from "react-bootstrap";

const WelcomeScreen = ({ onProceedToRegister }) => {
  const [isTransitioning, setIsTransitioning] = useState(false);
  const [showAbout, setShowAbout] = useState(false);

  const handleProceed = () => {
    setIsTransitioning(true);
    setTimeout(() => {
      onProceedToRegister();
    }, 300);
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "20px",
        opacity: isTransitioning ? 0.5 : 1,
        transition: "opacity 0.3s ease",
      }}
    >
      <Container>
        <Row className="justify-content-center">
          <Col xs={12} lg={8}>
            <Card
              className="shadow-lg text-center"
              style={{
                borderRadius: "25px",
                border: "4px solid var(--ipn-oro)",
                overflow: "hidden",
                animation: "fadeInUp 0.8s ease-out",
              }}
            >
              <Card.Body className="p-5">
                {/* Logo */}
                <div className="mb-4">
                  <div
                    className="mx-auto rounded-circle d-flex align-items-center justify-content-center"
                    style={{
                      width: "120px",
                      height: "120px",
                      background: "linear-gradient(45deg, var(--ipn-guinda), #5a252b)",
                      color: "white",
                      fontSize: "2.5rem",
                      fontWeight: "900",
                      boxShadow: "0 10px 20px rgba(114, 47, 55, 0.4)",
                      animation: "pulse 2s infinite, rotate 20s linear infinite",
                    }}
                  >
                    IPN
                  </div>
                </div>

                {/* Títulos */}
                <h1
                  style={{
                    color: "var(--ipn-guinda)",
                    fontWeight: "900",
                    fontSize: "2rem",
                  }}
                >
                  Instituto Politécnico Nacional
                </h1>
                <h2
                  style={{
                    color: "var(--ipn-oro)",
                    fontWeight: "700",
                    fontSize: "1.3rem",
                    fontStyle: "italic",
                  }}
                >
                  La Técnica al Servicio de la Patria
                </h2>

                {/* Subtítulo del sistema */}
                <h3
                  style={{
                    color: "var(--ipn-guinda)",
                    fontWeight: "800",
                    marginTop: "25px",
                    marginBottom: "30px",
                  }}
                >
                  Sistema ECG IoT
                </h3>

                {/* Botón principal */}
                <div className="d-flex flex-column gap-3 align-items-center">
                  <Button
                    onClick={handleProceed}
                    disabled={isTransitioning}
                    style={{
                      background:
                        "linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)",
                      border: "none",
                      fontWeight: "900",
                      padding: "15px 50px",
                      fontSize: "1.2rem",
                      borderRadius: "25px",
                      color: "white",
                    }}
                    className="btn-glow"
                  >
                    {isTransitioning ? (
                      <>
                        <span
                          className="spinner-border spinner-border-sm me-2"
                          role="status"
                          aria-hidden="true"
                        ></span>
                        Cargando...
                      </>
                    ) : (
                      <>
                        <i className="fas fa-rocket me-2"></i>
                        Acceder al Sistema
                      </>
                    )}
                  </Button>

                  {/* Botón para abrir modal */}
                  <Button
                    variant="outline-secondary"
                    onClick={() => setShowAbout(true)}
                    style={{
                      borderRadius: "20px",
                      fontWeight: "600",
                      padding: "10px 25px",
                    }}
                  >
                    <i className="fas fa-info-circle me-2"></i>
                    Acerca del Proyecto
                  </Button>
                </div>
              </Card.Body>
            </Card>
          </Col>
        </Row>
      </Container>

      {/* Modal con información adicional */}
      <Modal
        show={showAbout}
        onHide={() => setShowAbout(false)}
        centered
        size="lg"
      >
        <Modal.Header closeButton>
          <Modal.Title>Acerca del Proyecto</Modal.Title>
        </Modal.Header>
        <Modal.Body>
          <p>
            Bienvenido al <strong>sistema de monitoreo cardiaco</strong>{" "}
            desarrollado como parte del programa de{" "}
            <strong>Maestría en Tecnología Avanzada</strong> en CICATA Querétaro.
          </p>
          <p>
            Este innovador proyecto utiliza tecnología{" "}
            <strong>Internet of Things (IoT)</strong> basada en ESP32 para la
            adquisición de señales electrocardiográficas en tiempo real.
          </p>

          <h5 className="mt-4">Características principales</h5>
          <ul>
            <li>📡 Comunicación inalámbrica mediante WebSocket</li>
            <li>⚡ Soporte para hasta 6 electrodos simultáneos</li>
            <li>📊 Visualización avanzada con gráficas interactivas</li>
            <li>🛡️ Sistema robusto con reconexión automática</li>
          </ul>

          <div
            className="mt-4 p-3 rounded"
            style={{ backgroundColor: "#f8f9fa", border: "1px solid #ddd" }}
          >
            <h6>Desarrollador</h6>
            <p className="mb-0">
              <strong>Ing. Bryan Alexis Jiménez Aguilar</strong>
              <br />
              Estudiante de Maestría en Tecnología Avanzada
              <br />
              CICATA Querétaro - IPN
            </p>
          </div>
        </Modal.Body>
      </Modal>

      {/* Estilos animaciones */}
      <style jsx>{`
        @keyframes pulse {
          0% {
            transform: scale(1);
          }
          50% {
            transform: scale(1.05);
          }
          100% {
            transform: scale(1);
          }
        }

        @keyframes rotate {
          from {
            transform: rotate(0deg);
          }
          to {
            transform: rotate(360deg);
          }
        }

        .btn-glow:hover {
          transform: translateY(-3px) !important;
          box-shadow: 0 12px 20px rgba(114, 47, 55, 0.5) !important;
        }
      `}</style>
    </div>
  );
};

export default WelcomeScreen;
