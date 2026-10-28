import React from "react";
import { Container, Row, Col } from "react-bootstrap";

const Footer = () => {
  return (
    <footer style={{ 
      background: 'linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)', 
      color: '#FFFFFF', 
      padding: '30px 0', 
      marginTop: '40px',
      boxShadow: '0 -4px 8px rgba(114, 47, 55, 0.3)',
      borderTop: '3px solid var(--ipn-oro)'
    }}>
      <Container>
        <Row>
          <Col md={6} className="text-start">
            <h6 style={{ 
              color: '#FFFFFF', 
              fontWeight: '900',
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
              textShadow: '2px 2px 4px rgba(0,0,0,0.7)',
              fontSize: '1.1rem',
              marginBottom: '15px'
            }}>
              <i className="fas fa-university me-2" style={{ color: 'var(--ipn-oro)' }}></i>
              Instituto Politécnico Nacional
            </h6>
            <p className="mb-2" style={{ 
              fontSize: '0.95em',
              fontWeight: '600',
              textShadow: '1px 1px 2px rgba(0,0,0,0.5)',
              lineHeight: '1.4'
            }}>
              CICATA Querétaro - Centro de Investigación y de Estudios Avanzados en Tecnología Aplicada
            </p>
            <p className="mb-0" style={{ 
              fontSize: '0.85em', 
              opacity: '0.9',
              fontWeight: '500',
              fontStyle: 'italic',
              color: 'var(--ipn-oro)'
            }}>
              Maestría en Tecnología Avanzada
            </p>
          </Col>
          <Col md={6} className="text-end">
            <h6 style={{ 
              color: '#FFFFFF', 
              fontWeight: '900',
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
              textShadow: '2px 2px 4px rgba(0,0,0,0.7)',
              fontSize: '1.1rem',
              marginBottom: '15px'
            }}>
              <i className="fas fa-user-graduate me-2" style={{ color: 'var(--ipn-oro)' }}></i>
              Desarrollado por:
            </h6>
            <p className="mb-2" style={{ 
              fontSize: '0.95em',
              fontWeight: '700',
              textShadow: '1px 1px 2px rgba(0,0,0,0.5)'
            }}>
              Ing. Bryan Alexis Jiménez Aguilar
            </p>
            <p className="mb-0" style={{ 
              fontSize: '0.85em', 
              opacity: '0.9',
              fontWeight: '500',
              fontStyle: 'italic',
              color: 'var(--ipn-oro)'
            }}>
              Estudiante de Maestría en Tecnología Avanzada
            </p>
          </Col>
        </Row>
        <hr style={{ 
          borderColor: 'var(--ipn-oro)', 
          opacity: '0.5', 
          margin: '20px 0 15px 0',
          borderWidth: '2px'
        }} />
        <Row>
          <Col className="text-center">
            <p className="mb-0" style={{ 
              fontSize: '0.85em', 
              opacity: '0.8',
              fontWeight: '500',
              textShadow: '1px 1px 2px rgba(0,0,0,0.5)'
            }}>
              Copyright &copy; {new Date().getFullYear()} | CICATA Querétaro - IPN | 
              <span style={{ 
                color: 'var(--ipn-oro)', 
                fontWeight: '900', 
                marginLeft: '8px',
                textTransform: 'uppercase',
                letterSpacing: '0.5px'
              }}>
                La Técnica al Servicio de la Patria
              </span>
            </p>
          </Col>
        </Row>
      </Container>
    </footer>
  );
};

export default Footer;