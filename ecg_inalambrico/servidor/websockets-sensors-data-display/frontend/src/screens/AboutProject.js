import React from "react";
import { Container, Row, Col, Card, Badge, Button } from "react-bootstrap";

const AboutProject = () => {
  const technologies = [
    { name: "React.js", icon: "fab fa-react", color: "#61DAFB" },
    { name: "Node.js", icon: "fab fa-node-js", color: "#339933" },
    { name: "WebSockets", icon: "fas fa-plug", color: "#FF6B6B" },
    { name: "ESP32", icon: "fas fa-microchip", color: "#FF9500" },
    { name: "Bootstrap", icon: "fab fa-bootstrap", color: "#7952B3" },
    { name: "Recharts", icon: "fas fa-chart-line", color: "#8884D8" }
  ];

  const features = [
    {
      icon: "fas fa-wifi",
      title: "Comunicación en Tiempo Real",
      description: "Sistema WebSocket para transmisión instantánea de datos ECG desde múltiples dispositivos ESP32"
    },
    {
      icon: "fas fa-microchip",
      title: "Múltiples Dispositivos",
      description: "Soporte para hasta 6 electrodos simultáneos con identificación automática y gestión de conexiones"
    },
    {
      icon: "fas fa-chart-pulse",
      title: "Visualización Avanzada",
      description: "Gráficas interactivas con estadísticas en tiempo real, navegación entre dispositivos y análisis de datos"
    },
    {
      icon: "fas fa-shield-alt",
      title: "Arquitectura Robusta",
      description: "Sistema tolerante a fallos con reconexión automática, validación de datos y manejo de errores"
    }
  ];

  const specifications = [
    { label: "Dispositivos Soportados", value: "ESP32 (AR, AL, LL, RL, V1, V2)" },
    { label: "Protocolo de Comunicación", value: "WebSocket (ws://)" },
    { label: "Frecuencia de Muestreo", value: "Configurable hasta 1kHz" },
    { label: "Resolución ADC", value: "12 bits (4096 niveles)" },
    { label: "Latencia", value: "< 100ms end-to-end" },
    { label: "Visualización", value: "Tiempo real + histórico" }
  ];

  return (
    <div style={{ minHeight: '100vh', backgroundColor: '#FAFAFA', paddingTop: '20px' }}>
      <Container fluid className="p-4">
        
        {/* Header Principal */}
        <Row className="justify-content-center mb-5">
          <Col xs={12}>
            <Card className="text-center shadow-lg border-0" style={{ 
              background: 'linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)',
              borderRadius: '15px'
            }}>
              <Card.Body className="py-5">
                <div className="mb-4">
                  <i className="fas fa-heartbeat" style={{ 
                    fontSize: '4rem', 
                    color: 'var(--ipn-oro)',
                    filter: 'drop-shadow(2px 2px 4px rgba(0,0,0,0.5))'
                  }}></i>
                </div>
                <h1 className="main-title mb-3" style={{ 
                  fontSize: '2.8rem', 
                  fontWeight: '900',
                  textShadow: '3px 3px 6px rgba(0,0,0,0.8)',
                  letterSpacing: '-0.5px',
                  color: '#ffffff'
                }}>
                  Sistema de Adquisición ECG IoT
                </h1>
                <p className="main-subtitle mb-4" style={{ 
                  fontSize: '1.3rem', 
                  fontWeight: '600',
                  textShadow: '2px 2px 4px rgba(0,0,0,0.6)',
                  color: '#f8f9fa'
                }}>
                  Monitoreo Cardiaco en Tiempo Real con Tecnología ESP32
                </p>
                <Badge 
                  style={{ 
                    backgroundColor: 'var(--ipn-oro)', 
                    fontSize: '1rem',
                    padding: '10px 20px',
                    fontWeight: 'bold',
                    textTransform: 'uppercase',
                    letterSpacing: '0.5px'
                  }}
                >
                  Maestría en Tecnología Avanzada - CICATA Querétaro
                </Badge>
              </Card.Body>
            </Card>
          </Col>
        </Row>

        {/* Descripción del Proyecto */}
        <Row className="mb-5">
          <Col lg={8} className="mx-auto">
            <Card className="card-ipn shadow-lg">
              <Card.Header className="card-header-ipn">
                <h3 className="text-white mb-0" style={{ fontWeight: '900' }}>
                  <i className="fas fa-project-diagram me-3"></i>
                  Descripción del Proyecto
                </h3>
              </Card.Header>
              <Card.Body className="p-4">
                <p style={{ fontSize: '1.1rem', lineHeight: '1.8', textAlign: 'justify' }}>
                  Este proyecto desarrolla un <strong>sistema de adquisición de señales electrocardiográficas (ECG)</strong> 
                  distribuido utilizando tecnología Internet of Things (IoT) basada en microcontroladores ESP32. El sistema 
                  permite el <strong>monitoreo cardiaco en tiempo real</strong> de múltiples derivaciones ECG simultáneamente, 
                  transmitiendo los datos a través de protocolos WebSocket para su visualización, análisis y almacenamiento 
                  en una interfaz web responsiva desarrollada con React.js.
                </p>
                <p style={{ fontSize: '1.1rem', lineHeight: '1.8', textAlign: 'justify' }}>
                  La arquitectura del sistema está diseñada para ser <strong>escalable, robusta y de bajo costo</strong>, 
                  facilitando su implementación en entornos clínicos, de investigación biomédica o aplicaciones de telemedicina. 
                  Cada dispositivo ESP32 funciona como un nodo sensor independiente que captura señales analógicas de electrodos 
                  específicos (AR, AL, LL, RL, V1, V2) con resolución de 12 bits, las digitaliza y transmite de forma 
                  sincronizada al servidor central mediante comunicación inalámbrica Wi-Fi.
                </p>
                <p style={{ fontSize: '1.1rem', lineHeight: '1.8', textAlign: 'justify' }}>
                  Como parte de la <strong>investigación de maestría en Tecnología Avanzada</strong>, este trabajo contribuye 
                  al desarrollo de soluciones de <em>healthcare technology</em> accesibles, integrando conceptos de 
                  sistemas embebidos, procesamiento de señales biomédicas, comunicaciones inalámbricas y desarrollo 
                  de aplicaciones web para el sector salud.
                </p>
              </Card.Body>
            </Card>
          </Col>
        </Row>

        {/* Características Principales */}
        <Row className="mb-5">
          <Col xs={12}>
            <Card className="card-ipn shadow-lg">
              <Card.Header className="card-header-ipn">
                <h3 className="text-white mb-0" style={{ fontWeight: '900' }}>
                  <i className="fas fa-star me-3"></i>
                  Características Principales
                </h3>
              </Card.Header>
              <Card.Body className="p-4">
                <Row>
                  {features.map((feature, index) => (
                    <Col md={6} key={index} className="mb-4">
                      <div className="d-flex align-items-start">
                        <div 
                          className="rounded-circle d-flex align-items-center justify-content-center me-3"
                          style={{ 
                            width: '60px', 
                            height: '60px', 
                            backgroundColor: 'var(--ipn-guinda)',
                            color: 'white',
                            flexShrink: 0
                          }}
                        >
                          <i className={feature.icon} style={{ fontSize: '1.5rem' }}></i>
                        </div>
                        <div>
                          <h5 style={{ fontWeight: 'bold', color: 'var(--ipn-guinda)' }}>
                            {feature.title}
                          </h5>
                          <p style={{ fontSize: '0.95rem', lineHeight: '1.6', marginBottom: 0 }}>
                            {feature.description}
                          </p>
                        </div>
                      </div>
                    </Col>
                  ))}
                </Row>
              </Card.Body>
            </Card>
          </Col>
        </Row>

        {/* Tecnologías y Especificaciones */}
        <Row className="mb-5">
          <Col lg={6} className="mb-4">
            <Card className="card-ipn shadow-lg h-100">
              <Card.Header className="card-header-ipn">
                <h4 className="text-white mb-0" style={{ fontWeight: '900' }}>
                  <i className="fas fa-code me-3"></i>
                  Tecnologías Utilizadas
                </h4>
              </Card.Header>
              <Card.Body className="p-4">
                <Row>
                  {technologies.map((tech, index) => (
                    <Col xs={6} key={index} className="mb-3">
                      <div className="d-flex align-items-center">
                        <i 
                          className={tech.icon} 
                          style={{ 
                            fontSize: '2rem', 
                            color: tech.color,
                            marginRight: '12px',
                            width: '40px'
                          }}
                        ></i>
                        <span style={{ fontWeight: 'bold', fontSize: '0.95rem' }}>
                          {tech.name}
                        </span>
                      </div>
                    </Col>
                  ))}
                </Row>
              </Card.Body>
            </Card>
          </Col>

          <Col lg={6} className="mb-4">
            <Card className="card-ipn shadow-lg h-100">
              <Card.Header className="card-header-ipn">
                <h4 className="text-white mb-0" style={{ fontWeight: '900' }}>
                  <i className="fas fa-cogs me-3"></i>
                  Especificaciones Técnicas
                </h4>
              </Card.Header>
              <Card.Body className="p-4">
                {specifications.map((spec, index) => (
                  <div key={index} className="d-flex justify-content-between align-items-center mb-3 pb-2" style={{
                    borderBottom: index < specifications.length - 1 ? '1px solid #e0e0e0' : 'none'
                  }}>
                    <strong style={{ color: 'var(--ipn-guinda)', fontSize: '0.9rem' }}>
                      {spec.label}:
                    </strong>
                    <span style={{ fontWeight: '600', fontSize: '0.9rem', textAlign: 'right' }}>
                      {spec.value}
                    </span>
                  </div>
                ))}
              </Card.Body>
            </Card>
          </Col>
        </Row>

        {/* Autor y Contacto */}
        <Row className="mb-5">
          <Col lg={8} className="mx-auto">
            <Card className="card-ipn shadow-lg">
              <Card.Header className="card-header-ipn">
                <h3 className="text-white mb-0" style={{ fontWeight: '900' }}>
                  <i className="fas fa-user-graduate me-3"></i>
                  Desarrollador
                </h3>
              </Card.Header>
              <Card.Body className="p-5">
                <Row className="align-items-center">
                  <Col md={8}>
                    <h4 style={{ fontWeight: '900', color: 'var(--ipn-guinda)', marginBottom: '15px' }}>
                      Ing. Bryan Alexis Jiménez Aguilar
                    </h4>
                    <p style={{ fontSize: '1.1rem', lineHeight: '1.7', marginBottom: '20px' }}>
                      <strong>Estudiante de Maestría en Tecnología Avanzada</strong><br/>
                      CICATA Querétaro - Instituto Politécnico Nacional<br/>
                      <span style={{ color: 'var(--ipn-oro)', fontWeight: 'bold' }}>
                        <i className="fas fa-envelope me-2"></i>
                        mcbajimenez2400@alumno.ipn.mx
                      </span>
                    </p>
                    <p style={{ fontSize: '1rem', lineHeight: '1.6', color: '#666' }}>
                      Ingeniero .
                    </p>
                    <div className="mt-3">
                      <Badge style={{ backgroundColor: 'var(--ipn-guinda)', marginRight: '8px', padding: '6px 12px' }}>
                        IoT & Embedded Systems
                      </Badge>
                      <Badge style={{ backgroundColor: 'var(--ipn-oro)', marginRight: '8px', padding: '6px 12px' }}>
                        Full-Stack Development
                      </Badge>
                      <Badge style={{ backgroundColor: '#28a745', padding: '6px 12px' }}>
                        Healthcare Technology
                      </Badge>
                    </div>
                  </Col>
                  <Col md={4} className="text-center">
                    <div className="mb-3">
                      <i className="fas fa-user-circle" style={{ 
                        fontSize: '5rem', 
                        color: 'var(--ipn-guinda)' 
                      }}></i>
                    </div>
                    <div className="d-flex flex-column gap-2">
                      <Button 
                        href="https://bryanalexio.github.io/BryanAlexio/" 
                        target="_blank"
                        className="btn-ipn-secondary"
                        style={{ 
                          fontWeight: 'bold',
                          textTransform: 'uppercase',
                          fontSize: '0.85rem'
                        }}
                      >
                        <i className="fas fa-globe me-2"></i>
                        Portafolio
                      </Button>
                      <Button 
                        href="https://www.linkedin.com/in/bryan-alexis-jimenez-aguilar-873889274" 
                        target="_blank"
                        variant="outline-primary"
                        style={{ 
                          fontWeight: 'bold',
                          textTransform: 'uppercase',
                          fontSize: '0.85rem',
                          borderColor: '#0077B5',
                          color: '#0077B5'
                        }}
                      >
                        <i className="fab fa-linkedin me-2"></i>
                        LinkedIn
                      </Button>
                    </div>
                  </Col>
                </Row>
              </Card.Body>
            </Card>
          </Col>
        </Row>

        {/* Call to Action */}
        <Row>
          <Col xs={12} className="text-center">
            <Card style={{ 
              background: 'linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%)',
              border: '2px solid var(--ipn-oro)',
              borderRadius: '15px'
            }}>
              <Card.Body className="p-4">
                <h4 style={{ fontWeight: '900', color: 'var(--ipn-guinda)', marginBottom: '20px' }}>
                  ¿Interesado en el Proyecto?
                </h4>
                <p style={{ fontSize: '1.1rem', marginBottom: '25px', color: '#666' }}>
                  Este sistema está en desarrollo activo como parte de mi investigación de maestría en CICATA Querétaro. 
                  ¡Contacta para colaboraciones, información académica o consultas técnicas!
                </p>
                <div className="d-flex flex-wrap justify-content-center gap-3">
                  <Button 
                    href="mailto:mcbajimenez2400@alumno.ipn.mx?subject=Consulta sobre Sistema ECG IoT&body=Hola Bryan,%0D%0A%0D%0AMe interesa conocer más sobre tu proyecto de Sistema de Adquisición ECG con ESP32.%0D%0A%0D%0ASaludos." 
                    className="btn-ipn-primary"
                    style={{ fontWeight: 'bold', textTransform: 'uppercase' }}
                  >
                    <i className="fas fa-envelope me-2"></i>
                    Contactar Vía Email
                  </Button>
                  <Button 
                    href="https://www.linkedin.com/in/bryan-alexis-jimenez-aguilar-873889274" 
                    target="_blank"
                    style={{ 
                      fontWeight: 'bold', 
                      textTransform: 'uppercase',
                      backgroundColor: '#0077B5',
                      borderColor: '#0077B5',
                      color: 'white'
                    }}
                  >
                    <i className="fab fa-linkedin me-2"></i>
                    LinkedIn
                  </Button>
                  <Button 
                    href="/" 
                    className="btn-ipn-secondary"
                    style={{ fontWeight: 'bold', textTransform: 'uppercase' }}
                  >
                    <i className="fas fa-chart-line me-2"></i>
                    Ver Dashboard ECG
                  </Button>
                </div>
                
                {/* Información adicional de contacto */}
                <div className="mt-4 pt-3" style={{ borderTop: '1px solid #dee2e6' }}>
                  <Row>
                    <Col md={4} className="mb-2">
                      <div style={{ fontSize: '0.9rem', color: '#666' }}>
                        <i className="fas fa-university me-2" style={{ color: 'var(--ipn-guinda)' }}></i>
                        <strong>Institución:</strong> CICATA Querétaro - IPN
                      </div>
                    </Col>
                    <Col md={4} className="mb-2">
                      <div style={{ fontSize: '0.9rem', color: '#666' }}>
                        <i className="fas fa-graduation-cap me-2" style={{ color: 'var(--ipn-oro)' }}></i>
                        <strong>Programa:</strong> Maestría en Tecnología Avanzada
                      </div>
                    </Col>
                    <Col md={4} className="mb-2">
                      <div style={{ fontSize: '0.9rem', color: '#666' }}>
                        <i className="fas fa-map-marker-alt me-2" style={{ color: '#28a745' }}></i>
                        <strong>Ubicación:</strong> Querétaro, México
                      </div>
                    </Col>
                  </Row>
                </div>
              </Card.Body>
            </Card>
          </Col>
        </Row>

      </Container>
    </div>
  );
};

export default AboutProject;