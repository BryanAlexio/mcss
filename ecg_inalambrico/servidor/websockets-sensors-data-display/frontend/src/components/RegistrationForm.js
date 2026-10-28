import React, { useState, useEffect } from "react";
import { Container, Row, Col, Card, Form, Button, Alert, Badge, ListGroup, Modal, ButtonGroup, Tooltip, OverlayTrigger } from "react-bootstrap";
import { useAuth } from "./AuthProvider";
import WelcomeScreen from "./WelcomeScreen";

const RegistrationForm = () => {
  const { 
    registerUser, 
    formatUserData, 
    userHistory, 
    loadUserFromHistory, 
    removeFromHistory,
    clearAllData,
    requestUsersList
  } = useAuth();
  
  const [showWelcome, setShowWelcome] = useState(true);
  const [formData, setFormData] = useState({
    nombres: '',
    apellidoPaterno: '',
    apellidoMaterno: '',
    edad: '',
    sexo: ''
  });
  const [errors, setErrors] = useState({});
  const [showAlert, setShowAlert] = useState(false);
  const [alertMessage, setAlertMessage] = useState('');
  const [alertType, setAlertType] = useState('danger');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showLogoutMessage, setShowLogoutMessage] = useState(false);
  const [showHistoryModal, setShowHistoryModal] = useState(false);
  const [selectedHistoryUser, setSelectedHistoryUser] = useState(null);

  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const flowParam = urlParams.get('flow');
    
    if (urlParams.get('logout') === 'true' || flowParam === 'register') {
      setShowWelcome(false);
      if (urlParams.get('logout') === 'true') {
        setShowLogoutMessage(true);
        setAlertMessage('Sesión cerrada correctamente. Puedes registrar un nuevo usuario.');
        setAlertType('success');
        setShowAlert(true);
        setTimeout(() => {
          setShowLogoutMessage(false);
          setShowAlert(false);
        }, 4000);
      }
      
      window.history.replaceState({}, document.title, window.location.pathname);
    }
    
    // ELIMINADO: No solicitar lista al cargar
    // requestUsersList(); // ❌ Esto causaba requests constantes
  }, []); // Sin dependencias, solo se ejecuta una vez

  if (showWelcome) {
    return (
      <WelcomeScreen onProceedToRegister={() => setShowWelcome(false)} />
    );
  }

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: value
    }));
    if (errors[name]) {
      setErrors(prev => ({ ...prev, [name]: '' }));
    }
  };

  const validateField = (name, value) => {
    switch (name) {
      case 'nombres':
      case 'apellidoPaterno':
      case 'apellidoMaterno':
        if (!value.trim()) return 'Este campo es obligatorio';
        if (value.trim().length < 2) return 'Debe tener al menos 2 caracteres';
        if (!/^[a-zA-ZáéíóúÁÉÍÓÚñÑ\s]+$/.test(value.trim())) return 'Solo se permiten letras y espacios';
        break;
      case 'edad':
        const edad = parseInt(value);
        if (!value) return 'La edad es obligatoria';
        if (isNaN(edad) || edad < 15 || edad > 100) return 'La edad debe estar entre 15 y 100 años';
        break;
      case 'sexo':
        if (!value) return 'Debe seleccionar el sexo';
        break;
      default:
        break;
    }
    return '';
  };

  const validateForm = () => {
    const newErrors = {};
    Object.keys(formData).forEach(field => {
      const error = validateField(field, formData[field]);
      if (error) newErrors[field] = error;
    });
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const showMessage = (message, type = 'info') => {
    setAlertMessage(message);
    setAlertType(type);
    setShowAlert(true);
    setTimeout(() => setShowAlert(false), 5000);
  };

  const clearForm = () => {
    setFormData({
      nombres: '',
      apellidoPaterno: '',
      apellidoMaterno: '',
      edad: '',
      sexo: ''
    });
    setErrors({});
  };

  const handleBackToWelcome = () => {
    setShowWelcome(true);
    clearForm();
    setShowAlert(false);
  };

  const handleLoadFromHistory = (user) => {
    setFormData({
      nombres: user.nombres,
      apellidoPaterno: user.apellidoPaterno,
      apellidoMaterno: user.apellidoMaterno,
      edad: user.edad.toString(),
      sexo: user.sexo
    });
    setErrors({});
    showMessage('Datos cargados desde el historial. Puedes modificarlos si es necesario.', 'info');
  };

  const handleQuickLogin = (user) => {
    const result = loadUserFromHistory(user);
    if (result.success) {
      showMessage('¡Usuario cargado exitosamente!', 'success');
    } else {
      showMessage('Error al cargar usuario: ' + result.error, 'danger');
    }
  };

  // MODIFICADO: Ahora muestra advertencia en lugar de eliminar
  const handleRemoveFromHistory = (user) => {
    showMessage(
      '⚠️ Los usuarios deben eliminarse desde Raspberry Pi. Carpeta: /home/bryan/usuariosecg/' + user.id,
      'warning'
    );
  };

  const handleClearAllData = () => {
    if (window.confirm('¿Estás seguro de eliminar TODOS los datos locales? Los datos en Raspberry Pi NO se eliminarán.')) {
      clearAllData();
      showMessage('Datos locales eliminados. Los usuarios en Raspberry Pi permanecen intactos.', 'info');
      clearForm();
      
      // Solicitar lista actualizada
      setTimeout(() => {
        requestUsersList();
      }, 500);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setIsSubmitting(true);

    try {
      if (!validateForm()) {
        showMessage('Por favor, corrige los errores en el formulario', 'danger');
        return;
      }
      const formattedData = formatUserData(formData);
      const result = registerUser(formattedData);

      if (result.success) {
        showMessage('¡Registro exitoso! Carpeta creada en Raspberry Pi.', 'success');
        clearForm();
        
        // Solicitar lista actualizada después de 1 segundo
        setTimeout(() => {
          requestUsersList();
        }, 1000);
      } else {
        showMessage(result.error || 'Error al registrar usuario', 'danger');
      }
    } catch (error) {
      showMessage('Error inesperado. Por favor, intente nuevamente', 'danger');
      console.error('Error en registro:', error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const formatDate = (dateString) => {
    if (!dateString) return 'N/A';
    return new Date(dateString).toLocaleDateString('es-MX', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  };

  const renderDeleteTooltip = (props) => (
    <Tooltip id="delete-tooltip" {...props}>
      Los usuarios deben eliminarse desde Raspberry Pi
    </Tooltip>
  );

  return (
    <div style={{ 
      minHeight: '100vh',
      background: 'linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)',
      display: 'flex',
      alignItems: 'center',
      padding: '20px'
    }}>
      <Container>
        <Row className="justify-content-center">
          <Col xs={12} md={10} lg={8}>
            
            <div className="text-center mb-3">
              <Button
                variant="link"
                onClick={handleBackToWelcome}
                style={{
                  color: 'white',
                  textDecoration: 'none',
                  fontWeight: 'bold',
                  opacity: 0.8
                }}
                className="p-0"
              >
                <i className="fas fa-arrow-left me-2"></i>
                Volver a Información del Sistema
              </Button>
            </div>

            {showAlert && (
              <Alert variant={alertType} className="text-center mb-4"
                style={{ borderRadius: '10px', fontWeight: 'bold', boxShadow: '0 4px 8px rgba(0,0,0,0.15)' }}>
                {showLogoutMessage && <div><i className="fas fa-check-circle me-2"></i></div>}
                {alertMessage}
              </Alert>
            )}

            <Row>
              <Col md={7}>
                <Card className="shadow-lg" style={{ borderRadius: '20px', border: '3px solid var(--ipn-oro)', overflow: 'hidden' }}>
                  <Card.Header className="text-center py-4"
                    style={{ background: 'linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%)', border: 'none' }}>
                    <div className="mb-3">
                      <div className="mx-auto rounded-circle d-flex align-items-center justify-content-center"
                        style={{
                          width: '80px',
                          height: '80px',
                          background: 'linear-gradient(45deg, var(--ipn-guinda), #5a252b)',
                          color: 'white',
                          fontSize: '1.5rem',
                          fontWeight: 'bold',
                          boxShadow: '0 8px 16px rgba(114, 47, 55, 0.3)',
                          animation: showLogoutMessage ? 'none' : 'pulse 2s infinite'
                        }}>
                        IPN
                      </div>
                    </div>
                    <h1 style={{ color: 'var(--ipn-guinda)', fontWeight: '900', fontSize: '1.5rem', marginBottom: '8px' }}>
                      Instituto Politécnico Nacional
                    </h1>
                    <Badge style={{ backgroundColor: showLogoutMessage ? '#28a745' : 'var(--ipn-oro)', color: 'white', fontSize: '0.8rem', fontWeight: 'bold', padding: '6px 12px' }}>
                      {showLogoutMessage ? 'Nuevo Registro Disponible' : 'Sistema de Registro Estudiantil'}
                    </Badge>
                  </Card.Header>

                  <Card.Body className="p-4">
                    <h3 className="text-center mb-4" style={{ color: 'var(--ipn-guinda)', fontWeight: '900' }}>
                      {showLogoutMessage ? 'Registrar Nuevo Usuario' : 'Registro de Usuario'}
                    </h3>

                    <Form onSubmit={handleSubmit}>
                      <Form.Group className="mb-3">
                        <Form.Label style={{ fontWeight: '600', color: '#333' }}>
                          Nombres <span style={{ color: 'red' }}>*</span>
                        </Form.Label>
                        <Form.Control
                          type="text"
                          name="nombres"
                          value={formData.nombres}
                          onChange={handleChange}
                          placeholder="Ingresa tus nombres"
                          isInvalid={!!errors.nombres}
                        />
                        <Form.Control.Feedback type="invalid">{errors.nombres}</Form.Control.Feedback>
                      </Form.Group>

                      <Row>
                        <Col md={6}>
                          <Form.Group className="mb-3">
                            <Form.Label>Apellido Paterno <span style={{ color: 'red' }}>*</span></Form.Label>
                            <Form.Control
                              type="text"
                              name="apellidoPaterno"
                              value={formData.apellidoPaterno}
                              onChange={handleChange}
                              placeholder="Apellido paterno"
                              isInvalid={!!errors.apellidoPaterno}
                            />
                            <Form.Control.Feedback type="invalid">{errors.apellidoPaterno}</Form.Control.Feedback>
                          </Form.Group>
                        </Col>
                        <Col md={6}>
                          <Form.Group className="mb-3">
                            <Form.Label>Apellido Materno <span style={{ color: 'red' }}>*</span></Form.Label>
                            <Form.Control
                              type="text"
                              name="apellidoMaterno"
                              value={formData.apellidoMaterno}
                              onChange={handleChange}
                              placeholder="Apellido materno"
                              isInvalid={!!errors.apellidoMaterno}
                            />
                            <Form.Control.Feedback type="invalid">{errors.apellidoMaterno}</Form.Control.Feedback>
                          </Form.Group>
                        </Col>
                      </Row>

                      <Row>
                        <Col md={6}>
                          <Form.Group className="mb-3">
                            <Form.Label>Edad <span style={{ color: 'red' }}>*</span></Form.Label>
                            <Form.Control
                              type="number"
                              name="edad"
                              value={formData.edad}
                              onChange={handleChange}
                              placeholder="Tu edad"
                              min="15"
                              max="100"
                              isInvalid={!!errors.edad}
                            />
                            <Form.Control.Feedback type="invalid">{errors.edad}</Form.Control.Feedback>
                          </Form.Group>
                        </Col>
                        <Col md={6}>
                          <Form.Group className="mb-3">
                            <Form.Label>Sexo <span style={{ color: 'red' }}>*</span></Form.Label>
                            <Form.Select
                              name="sexo"
                              value={formData.sexo}
                              onChange={handleChange}
                              isInvalid={!!errors.sexo}
                            >
                              <option value="">Seleccionar...</option>
                              <option value="masculino">Masculino</option>
                              <option value="femenino">Femenino</option>
                              <option value="otro">Otro</option>
                            </Form.Select>
                            <Form.Control.Feedback type="invalid">{errors.sexo}</Form.Control.Feedback>
                          </Form.Group>
                        </Col>
                      </Row>

                      <div className="text-center mt-4">
                        <Button type="submit" disabled={isSubmitting}
                          style={{
                            background: 'linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)',
                            border: 'none',
                            fontWeight: '900',
                            textTransform: 'uppercase',
                            padding: '12px 40px',
                            fontSize: '1.1rem',
                            borderRadius: '10px',
                            width: '100%'
                          }}>
                          {isSubmitting ? (
                            <>
                              <span className="spinner-border spinner-border-sm me-2"></span>
                              Registrando...
                            </>
                          ) : (
                            <>
                              <i className="fas fa-user-plus me-2"></i>
                              {showLogoutMessage ? 'Registrar Nuevo Usuario' : 'Registrarme e Ingresar'}
                            </>
                          )}
                        </Button>
                      </div>

                      <div className="text-center mt-3">
                        <small style={{ color: '#666' }}>
                          {showLogoutMessage
                            ? 'Completa el formulario para registrar un nuevo usuario'
                            : 'Al registrarte, se creará tu carpeta en Raspberry Pi'}
                        </small>
                      </div>
                    </Form>
                  </Card.Body>
                </Card>
              </Col>

              <Col md={5}>
                <Card className="shadow-lg" style={{ borderRadius: '20px', border: '2px solid var(--ipn-oro)' }}>
                  <Card.Header style={{ 
                    background: 'linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)',
                    color: 'white'
                  }}>
                    <h5 className="mb-0" style={{ fontWeight: '900' }}>
                      <i className="fas fa-history me-2"></i>
                      Usuarios en Raspberry Pi ({userHistory.length})
                    </h5>
                  </Card.Header>
                  <Card.Body style={{ maxHeight: '500px', overflowY: 'auto' }}>
                    {userHistory.length === 0 ? (
                      <div className="text-center py-4">
                        <div style={{ fontSize: '3rem', opacity: 0.5 }}>👥</div>
                        <p className="text-muted" style={{ fontWeight: '500', fontSize: '0.9rem' }}>
                          No hay usuarios en Raspberry Pi
                        </p>
                        <small className="text-muted">
                          Los usuarios registrados aparecerán aquí
                        </small>
                      </div>
                    ) : (
                      <>
                        <div className="d-grid gap-2 mb-3">
                          <Button
                            variant="outline-info"
                            size="sm"
                            onClick={() => setShowHistoryModal(true)}
                          >
                            <i className="fas fa-list me-2"></i>
                            Ver Lista Completa
                          </Button>
                          <Button
                            variant="outline-secondary"
                            size="sm"
                            onClick={() => requestUsersList()}
                          >
                            <i className="fas fa-sync me-2"></i>
                            Actualizar Lista
                          </Button>
                        </div>

                        <ListGroup variant="flush">
                          {userHistory.slice(0, 5).map((user, index) => (
                            <ListGroup.Item key={user.id} className="px-0 py-2 border-0">
                              <div className="d-flex align-items-center justify-content-between">
                                <div style={{ fontSize: '0.85rem' }}>
                                  <div style={{ fontWeight: 'bold', color: 'var(--ipn-guinda)' }}>
                                    {user.nombreCompleto}
                                  </div>
                                  <div className="text-muted">
                                    {user.edad} años • {user.sexo}
                                  </div>
                                  {user.fechaUltimoUso && (
                                    <div className="text-muted" style={{ fontSize: '0.75rem' }}>
                                      Último uso: {formatDate(user.fechaUltimoUso)}
                                    </div>
                                  )}
                                  {user.totalSesiones > 0 && (
                                    <div className="text-muted" style={{ fontSize: '0.75rem' }}>
                                      Sesiones: {user.totalSesiones}
                                    </div>
                                  )}
                                </div>
                                <ButtonGroup size="sm">
                                  <Button
                                    variant="outline-primary"
                                    size="sm"
                                    onClick={() => handleLoadFromHistory(user)}
                                    title="Cargar datos en el formulario"
                                  >
                                    <i className="fas fa-edit"></i>
                                  </Button>
                                  <Button
                                    variant="success"
                                    size="sm"
                                    onClick={() => handleQuickLogin(user)}
                                    title="Acceso rápido"
                                  >
                                    <i className="fas fa-sign-in-alt"></i>
                                  </Button>
                                  <OverlayTrigger
                                    placement="top"
                                    overlay={renderDeleteTooltip}
                                  >
                                    <Button
                                      variant="outline-secondary"
                                      size="sm"
                                      onClick={() => handleRemoveFromHistory(user)}
                                      title="Eliminar desde Raspberry Pi"
                                      disabled
                                    >
                                      <i className="fas fa-folder"></i>
                                    </Button>
                                  </OverlayTrigger>
                                </ButtonGroup>
                              </div>
                            </ListGroup.Item>
                          ))}
                        </ListGroup>

                        {userHistory.length > 5 && (
                          <div className="text-center mt-2">
                            <small className="text-muted">
                              y {userHistory.length - 5} usuarios más...
                            </small>
                          </div>
                        )}
                      </>
                    )}
                  </Card.Body>
                </Card>
              </Col>
            </Row>
          </Col>
        </Row>

        <Modal show={showHistoryModal} onHide={() => setShowHistoryModal(false)} centered size="lg">
          <Modal.Header closeButton>
            <Modal.Title>
              <i className="fas fa-server me-2"></i>
              Usuarios en Raspberry Pi
            </Modal.Title>
          </Modal.Header>
          <Modal.Body style={{ maxHeight: '400px', overflowY: 'auto' }}>
            {userHistory.map((user, index) => (
              <Card key={user.id} className="mb-3 border">
                <Card.Body className="py-2">
                  <Row className="align-items-center">
                    <Col md={6}>
                      <h6 className="mb-1" style={{ color: 'var(--ipn-guinda)', fontWeight: 'bold' }}>
                        {user.nombreCompleto}
                      </h6>
                      <div className="text-muted" style={{ fontSize: '0.85rem' }}>
                        {user.edad} años • {user.sexo}
                      </div>
                      {user.fechaRegistro && (
                        <div className="text-muted" style={{ fontSize: '0.75rem' }}>
                          Registrado: {formatDate(user.fechaRegistro)}
                        </div>
                      )}
                      {user.fechaUltimoUso && (
                        <div className="text-muted" style={{ fontSize: '0.75rem' }}>
                          Último uso: {formatDate(user.fechaUltimoUso)}
                        </div>
                      )}
                      {user.totalSesiones > 0 && (
                        <div className="text-muted" style={{ fontSize: '0.75rem' }}>
                          <i className="fas fa-folder me-1"></i>
                          {user.totalSesiones} sesión(es)
                        </div>
                      )}
                      <div className="text-muted" style={{ fontSize: '0.7rem' }}>
                        <i className="fas fa-hdd me-1"></i>
                        /home/bryan/usuariosecg/{user.id}
                      </div>
                    </Col>
                    <Col md={6} className="text-end">
                      <ButtonGroup size="sm">
                        <Button
                          variant="outline-primary"
                          onClick={() => {
                            handleLoadFromHistory(user);
                            setShowHistoryModal(false);
                          }}
                          title="Cargar en formulario"
                        >
                          <i className="fas fa-edit me-1"></i>
                          Editar
                        </Button>
                        <Button
                          variant="success"
                          onClick={() => {
                            handleQuickLogin(user);
                            setShowHistoryModal(false);
                          }}
                          title="Acceso rápido"
                        >
                          <i className="fas fa-sign-in-alt me-1"></i>
                          Ingresar
                        </Button>
                        <OverlayTrigger
                          placement="top"
                          overlay={renderDeleteTooltip}
                        >
                          <Button
                            variant="outline-secondary"
                            onClick={() => handleRemoveFromHistory(user)}
                            title="Ver carpeta en RPi"
                            disabled
                          >
                            <i className="fas fa-folder"></i>
                          </Button>
                        </OverlayTrigger>
                      </ButtonGroup>
                    </Col>
                  </Row>
                </Card.Body>
              </Card>
            ))}
          </Modal.Body>
          <Modal.Footer>
            <small className="text-muted me-auto">
              <i className="fas fa-info-circle me-1"></i>
              Usuarios sincronizados desde Raspberry Pi
            </small>
            <Button variant="secondary" onClick={() => setShowHistoryModal(false)}>
              Cerrar
            </Button>
          </Modal.Footer>
        </Modal>
      </Container>
    </div>
  );
};

export default RegistrationForm;