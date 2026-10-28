import React from "react";
import { BrowserRouter as Router, Routes, Route, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./components/AuthProvider";
import { ControlProvider } from "./components/ControlContext";
import Header from "./components/Header";
import Footer from "./components/Footer";
import SensorChart from "./screens/SensorDataChart";
import AboutProject from "./screens/AboutProject";
import DerivacionesECG from "./screens/DerivacionesECG";
import RegistrationForm from "./components/RegistrationForm";
import { Spinner } from "react-bootstrap";

// Componente para proteger rutas que requieren autenticación
const ProtectedRoute = ({ children }) => {
  const { isAuthenticated, isLoading } = useAuth();
  
  if (isLoading) {
    return (
      <div style={{ 
        minHeight: '100vh', 
        display: 'flex', 
        alignItems: 'center', 
        justifyContent: 'center',
        background: 'linear-gradient(135deg, var(--ipn-guinda) 0%, #5a252b 100%)'
      }}>
        <div className="text-center text-white">
          <Spinner 
            animation="border" 
            style={{ 
              color: 'var(--ipn-oro)',
              width: '3rem', 
              height: '3rem',
              marginBottom: '20px'
            }} 
          />
          <h4 style={{ fontWeight: '600' }}>Cargando Sistema IPN...</h4>
        </div>
      </div>
    );
  }
  
  if (!isAuthenticated) {
    return <RegistrationForm />;
  }
  
  return children;
};

// Componente para actualizar último acceso
const LastAccessUpdater = () => {
  const { isAuthenticated, updateLastAccess } = useAuth();
  const location = useLocation();

  // Actualizar último acceso cuando cambia la ruta
  React.useEffect(() => {
    if (isAuthenticated) {
      updateLastAccess();
    }
  }, [location.pathname]); // Solo cuando cambia la ruta, NO cuando cambia updateLastAccess

  return null;
};

// Componente principal de la aplicación
const AppContent = () => {
  const { isAuthenticated } = useAuth();

  return (
    <div style={{ 
      minHeight: '100vh', 
      display: 'flex', 
      flexDirection: 'column', 
      backgroundColor: '#FAFAFA' 
    }}>
      {isAuthenticated && <Header />}
      
      <LastAccessUpdater />
      
      <main className="py-3" style={{ flex: 1 }}>
        <Routes>
          <Route 
            exact 
            path="/" 
            element={
              <ProtectedRoute>
                <SensorChart />
              </ProtectedRoute>
            }
          />
          <Route 
            path="/derivaciones" 
            element={
              <ProtectedRoute>
                <DerivacionesECG />
              </ProtectedRoute>
            }
          />
          <Route 
            path="/about" 
            element={
              <ProtectedRoute>
                <AboutProject />
              </ProtectedRoute>
            }
          />
          {/* Ruta para registro manual (opcional) */}
          <Route 
            path="/register" 
            element={<RegistrationForm />}
          />
        </Routes>
      </main>
      
      {isAuthenticated && <Footer />}
    </div>
  );
};

// Aplicación principal con proveedores
const App = () => {
  // Configuración de desarrollo
  React.useEffect(() => {
    if (process.env.NODE_ENV === 'development') {
      console.log('Modo desarrollo activo');
      console.log('COMPORTAMIENTO POR DEFECTO: Siempre inicia en pantalla de bienvenida');
      console.log('Query parameters disponibles:');
      console.log('   ?flow=welcome - Forzar pantalla de bienvenida (por defecto)');
      console.log('   ?flow=register - Forzar formulario de registro');
      console.log('   ?flow=demo - Crear usuario demo automáticamente');
      console.log('   ?flow=auto - Cargar usuario existente (comportamiento anterior)');
      console.log('   ?flow=clear - Limpiar todos los datos');
    }
  }, []);

  return (
    <Router>
      <AuthProvider>
        <ControlProvider>
          <AppContent />
        </ControlProvider>
      </AuthProvider>
    </Router>
  );
};

export default App;