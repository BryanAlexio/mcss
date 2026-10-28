import React from "react";
import { Container, Nav, Navbar, NavDropdown } from "react-bootstrap";
import { LinkContainer } from "react-router-bootstrap";
import { useAuth } from "./AuthProvider";
import { useControlContext } from "./ControlContext";

const Header = () => {
  const { user, logout, isAuthenticated } = useAuth();
  const { sendRaspberryCommand } = useControlContext();

  const handleLogout = () => {
    if (window.confirm('¿Estás seguro que deseas cerrar sesión?')) {
      // Enviar comando RESET a la Raspberry Pi antes de cerrar sesión
      console.log("🔄 Enviando RESET a Raspberry Pi antes de cerrar sesión...");
      sendRaspberryCommand('RESET');
      
      // Pequeño delay para que el comando se envíe antes del logout
      setTimeout(() => {
        const result = logout();
        if (result.success) {
          console.log('Sesión cerrada correctamente');
        } else {
          alert('Error al cerrar sesión. Intenta nuevamente.');
        }
      }, 300);
    }
  };

  const formatUserName = () => {
    if (!user) return '';
    return `${user.nombres} ${user.apellidoPaterno}`;
  };

  const getUserInitials = () => {
    if (!user) return 'U';
    const nombres = user.nombres || '';
    const apellido = user.apellidoPaterno || '';
    return `${nombres.charAt(0)}${apellido.charAt(0)}`.toUpperCase();
  };

  const formatLastAccess = () => {
    if (!user?.ultimoAcceso && !user?.fechaUltimoUso) return '';
    // Priorizar fechaUltimoUso (viene de Raspberry Pi) sobre ultimoAcceso
    const dateStr = user.fechaUltimoUso || user.ultimoAcceso;
    const date = new Date(dateStr);
    return date.toLocaleDateString('es-MX', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric'
    });
  };

  return (
    <header>
      <Navbar 
        bg="dark" 
        variant="dark" 
        expand="lg" 
        collapseOnSelect 
        style={{ 
          background: 'linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)',
          boxShadow: '0 4px 8px rgba(114, 47, 55, 0.3)',
          borderBottom: '3px solid var(--ipn-oro)',
          padding: '10px 0'
        }}
      >
        <Container>
          <LinkContainer to={"/"}>
            <Navbar.Brand style={{ 
              color: '#FFFFFF', 
              fontWeight: '900',
              fontSize: '1.4rem',
              textShadow: '2px 2px 4px rgba(0,0,0,0.7)',
              letterSpacing: '0.5px',
              textTransform: 'uppercase'
            }}>
              <i className="fas fa-graduation-cap me-2" style={{ color: 'var(--ipn-oro)' }}></i>
              CICATA Querétaro - IPN
            </Navbar.Brand>
          </LinkContainer>
          
          <Navbar.Toggle aria-controls="basic-navbar-nav" />
          
          <Navbar.Collapse id="basic-navbar-nav">
            <Nav className="me-auto">
              <LinkContainer to={"/"}>
                <Nav.Link style={{ 
                  color: '#FFFFFF',
                  fontWeight: 'bold',
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                  padding: '8px 16px',
                  borderRadius: '8px',
                  transition: 'all 0.3s ease',
                  textShadow: '1px 1px 2px rgba(0,0,0,0.5)'
                }}
                className="nav-link-hover"
                >
                  <i className="fas fa-chart-line me-2" style={{ color: 'var(--ipn-oro)' }}></i>
                  Dashboard
                </Nav.Link>
              </LinkContainer>
              
              <LinkContainer to={"/derivaciones"}>
                <Nav.Link style={{ 
                  color: '#FFFFFF',
                  fontWeight: 'bold',
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                  padding: '8px 16px',
                  borderRadius: '8px',
                  transition: 'all 0.3s ease',
                  textShadow: '1px 1px 2px rgba(0,0,0,0.5)'
                }}
                className="nav-link-hover"
                >
                  <i className="fas fa-heartbeat me-2" style={{ color: 'var(--ipn-oro)' }}></i>
                  12 Derivaciones ECG
                </Nav.Link>
              </LinkContainer>
              
              <LinkContainer to={"/about"}>
                <Nav.Link style={{ 
                  color: '#FFFFFF',
                  fontWeight: 'bold',
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                  padding: '8px 16px',
                  borderRadius: '8px',
                  transition: 'all 0.3s ease',
                  textShadow: '1px 1px 2px rgba(0,0,0,0.5)'
                }}
                className="nav-link-hover"
                >
                  <i className="fas fa-user-graduate me-2" style={{ color: 'var(--ipn-oro)' }}></i>
                  Acerca del Proyecto
                </Nav.Link>
              </LinkContainer>
            </Nav>

            {/* Información del usuario */}
            {isAuthenticated && user && (
              <Nav className="ms-auto">
                <NavDropdown 
                  title={
                    <span style={{ color: '#FFFFFF' }}>
                      <div 
                        className="d-inline-flex align-items-center justify-content-center rounded-circle me-2"
                        style={{
                          width: '35px',
                          height: '35px',
                          backgroundColor: 'var(--ipn-oro)',
                          color: '#333',
                          fontWeight: 'bold',
                          fontSize: '0.9rem'
                        }}
                      >
                        {getUserInitials()}
                      </div>
                      <span style={{ 
                        fontWeight: 'bold',
                        textShadow: '1px 1px 2px rgba(0,0,0,0.5)'
                      }}>
                        {formatUserName()}
                      </span>
                    </span>
                  } 
                  id="user-dropdown"
                  align="end"
                  menuVariant="light"
                  style={{ color: '#FFFFFF' }}
                >
                  <NavDropdown.Header style={{ 
                    backgroundColor: '#f8f9fa',
                    borderBottom: '2px solid var(--ipn-oro)',
                    fontWeight: 'bold',
                    color: 'var(--ipn-guinda)'
                  }}>
                    <div className="d-flex align-items-center">
                      <div 
                        className="rounded-circle d-flex align-items-center justify-content-center me-3"
                        style={{
                          width: '50px',
                          height: '50px',
                          backgroundColor: 'var(--ipn-guinda)',
                          color: 'white',
                          fontWeight: 'bold',
                          fontSize: '1.2rem'
                        }}
                      >
                        {getUserInitials()}
                      </div>
                      <div>
                        <div style={{ fontWeight: '900', fontSize: '1rem' }}>
                          {user.nombreCompleto}
                        </div>
                        <small style={{ color: '#666' }}>
                          {user.edad} años • {user.sexo}
                        </small>
                      </div>
                    </div>
                  </NavDropdown.Header>
                  
                  <NavDropdown.Item disabled style={{ 
                    backgroundColor: '#fff',
                    color: '#666',
                    fontSize: '0.85rem'
                  }}>
                    <i className="fas fa-calendar me-2" style={{ color: 'var(--ipn-oro)' }}></i>
                    Último acceso: {formatLastAccess()}
                  </NavDropdown.Item>
                  
                  <NavDropdown.Item disabled style={{ 
                    backgroundColor: '#fff',
                    color: '#666',
                    fontSize: '0.85rem'
                  }}>
                    <i className="fas fa-clock me-2" style={{ color: 'var(--ipn-guinda)' }}></i>
                    Registrado: {new Date(user.fechaRegistro).toLocaleDateString('es-MX')}
                  </NavDropdown.Item>
                  
                  <NavDropdown.Divider />
                  
                  <NavDropdown.Item 
                    onClick={handleLogout}
                    style={{ 
                      color: '#dc3545',
                      fontWeight: 'bold',
                      cursor: 'pointer'
                    }}
                  >
                    <i className="fas fa-sign-out-alt me-2"></i>
                    Cerrar Sesión
                  </NavDropdown.Item>
                </NavDropdown>
              </Nav>
            )}
          </Navbar.Collapse>
        </Container>
      </Navbar>
    </header>
  );
};

export default Header;
