"""
Windows PC - Gestor de Usuarios y Carpetas
Gestiona la estructura de carpetas para cada usuario del sistema ECG
"""

import os
import json
from datetime import datetime
from pathlib import Path

# Configuración de rutas - RUTA CORRECTA PARA WINDOWS
BASE_DIR = "/home/bryan/usuariosecg"
LOG_FILENAME = "ultimasesion.log"

class UserManager:
    def __init__(self, base_dir=BASE_DIR):
        self.base_dir = Path(base_dir)
        self._ensure_base_directory()
    
    def _ensure_base_directory(self):
        """Crea el directorio base si no existe"""
        if not self.base_dir.exists():
            self.base_dir.mkdir(parents=True, exist_ok=True)
            print(f"[USER_MGR] Directorio base creado: {self.base_dir}")
    
    def sanitize_username(self, nombre_completo):
        """
        Convierte nombre completo en nombre de carpeta válido
        Ejemplo: "Juan Pérez García" -> "juan_perez_garcia"
        """
        # Reemplazar espacios y caracteres especiales
        username = nombre_completo.lower()
        username = username.replace(' ', '_')
        
        # Remover acentos y caracteres especiales
        replacements = {
            'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
            'ñ': 'n', 'ü': 'u'
        }
        for old, new in replacements.items():
            username = username.replace(old, new)
        
        # Remover caracteres no alfanuméricos excepto guiones bajos
        username = ''.join(c for c in username if c.isalnum() or c == '_')
        
        return username
    
    def get_user_directory(self, user_data):
        """Obtiene el path del directorio del usuario"""
        nombre_completo = user_data.get('nombreCompleto')
        if not nombre_completo:
            nombre_completo = f"{user_data.get('nombres', '')} {user_data.get('apellidoPaterno', '')} {user_data.get('apellidoMaterno', '')}".strip()
        
        username = self.sanitize_username(nombre_completo)
        return self.base_dir / username
    
    def user_exists(self, user_data):
        """Verifica si el usuario ya tiene carpeta"""
        user_dir = self.get_user_directory(user_data)
        return user_dir.exists()
    
    def get_next_session_number(self, user_dir):
        """Obtiene el siguiente número de sesión disponible"""
        session_num = 1
        while (user_dir / f"sesion{session_num}").exists():
            session_num += 1
        return session_num
    
    def create_user_structure(self, user_data):
        """
        Crea la estructura completa de carpetas para un nuevo usuario
        
        Retorna:
            dict con información de la creación
        """
        user_dir = self.get_user_directory(user_data)
        username = user_dir.name
        
        try:
            # Verificar si el usuario ya existe
            if user_dir.exists():
                print(f"[USER_MGR] Usuario '{username}' ya existe")
                # Actualizar log de última sesión
                self.update_last_session_log(user_data, 0)
                
                # Leer información existente para retornar datos correctos
                info_file = user_dir / "usuario_info.json"
                fecha_registro_original = None
                if info_file.exists():
                    try:
                        with open(info_file, 'r', encoding='utf-8') as f:
                            existing_info = json.load(f)
                            fecha_registro_original = existing_info.get('fecha_registro')
                    except:
                        pass
                
                return {
                    'success': True,
                    'existed': True,
                    'username': username,
                    'user_dir': str(user_dir),
                    'fecha_registro': fecha_registro_original,
                    'message': f"Usuario existente: {username}"
                }
            
            # Crear directorio del usuario
            user_dir.mkdir(parents=True, exist_ok=True)
            print(f"[USER_MGR] Creado directorio: {user_dir}")
            
            # Crear primera sesión
            session_dir = user_dir / "sesion1"
            session_dir.mkdir(exist_ok=True)
            
            # Crear subcarpetas
            derivaciones_dir = session_dir / "derivaciones"
            electrodos_dir = session_dir / "electrodos"
            
            derivaciones_dir.mkdir(exist_ok=True)
            electrodos_dir.mkdir(exist_ok=True)
            
            print(f"[USER_MGR] Creadas subcarpetas en sesion1")
            
            # Guardar información del usuario (captura fecha de registro)
            fecha_registro = datetime.now().isoformat()
            self.save_user_info(user_data, fecha_registro)
            
            # Crear archivo de log
            self.update_last_session_log(user_data, 1)
            
            print(f"[USER_MGR] ✓ Usuario '{username}' creado exitosamente")
            
            return {
                'success': True,
                'existed': False,
                'username': username,
                'user_dir': str(user_dir),
                'session_dir': str(session_dir),
                'fecha_registro': fecha_registro,
                'message': f"Usuario creado: {username}"
            }
        
        except Exception as e:
            print(f"[USER_MGR ERROR] Error creando usuario: {e}")
            return {
                'success': False,
                'error': str(e),
                'username': username
            }
    
    def update_last_session_log(self, user_data, session_num=None):
        """Actualiza el archivo ultimasesion.log con timestamp preciso"""
        user_dir = self.get_user_directory(user_data)
        
        if session_num is None:
            # Buscar última sesión existente
            session_num = self.get_next_session_number(user_dir) - 1
            if session_num < 1:
                session_num = 1
        
        log_file = user_dir / LOG_FILENAME
        
        # Capturar timestamp actual
        now = datetime.now()
        
        log_data = {
            'ultima_sesion': session_num,
            'fecha_acceso': now.strftime("%Y-%m-%d"),
            'hora_acceso': now.strftime("%H:%M:%S"),
            'timestamp': now.isoformat(),  # Formato ISO completo
            'usuario': user_data.get('nombreCompleto', ''),
            'edad': user_data.get('edad', ''),
            'sexo': user_data.get('sexo', '')
        }
        
        try:
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            
            print(f"[USER_MGR] Log actualizado: sesión {session_num} - {now.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
        
        except Exception as e:
            print(f"[USER_MGR ERROR] Error actualizando log: {e}")
            return False
    
    def save_user_info(self, user_data, fecha_registro=None):
        """Guarda información del usuario en un archivo JSON con fecha de registro"""
        user_dir = self.get_user_directory(user_data)
        info_file = user_dir / "usuario_info.json"
        
        # Si no se proporciona fecha de registro, usar la actual
        if fecha_registro is None:
            fecha_registro = datetime.now().isoformat()
        
        user_info = {
            'nombres': user_data.get('nombres', ''),
            'apellido_paterno': user_data.get('apellidoPaterno', ''),
            'apellido_materno': user_data.get('apellidoMaterno', ''),
            'nombre_completo': user_data.get('nombreCompleto', ''),
            'edad': user_data.get('edad', ''),
            'sexo': user_data.get('sexo', ''),
            'fecha_registro': fecha_registro,  # Guardamos la fecha de registro
            'id_sistema': user_data.get('id', '')
        }
        
        try:
            with open(info_file, 'w', encoding='utf-8') as f:
                json.dump(user_info, f, indent=2, ensure_ascii=False)
            
            print(f"[USER_MGR] Info de usuario guardada - Registrado: {fecha_registro}")
            return True
        
        except Exception as e:
            print(f"[USER_MGR ERROR] Error guardando info: {e}")
            return False
    
    def list_all_users(self):
        """
        Lista todos los usuarios registrados con información detallada
        
        Retorna:
            Lista de diccionarios con información de cada usuario
        """
        users = []
        
        try:
            if not self.base_dir.exists():
                return users
            
            for user_folder in self.base_dir.iterdir():
                if user_folder.is_dir():
                    # Leer información del usuario
                    info_file = user_folder / "usuario_info.json"
                    log_file = user_folder / LOG_FILENAME
                    
                    user_data = {
                        'username': user_folder.name,
                        'user_dir': str(user_folder),
                        'exists': True
                    }
                    
                    # Leer info del usuario si existe
                    if info_file.exists():
                        try:
                            with open(info_file, 'r', encoding='utf-8') as f:
                                user_info = json.load(f)
                                user_data['nombres'] = user_info.get('nombres', '')
                                user_data['apellido_paterno'] = user_info.get('apellido_paterno', '')
                                user_data['apellido_materno'] = user_info.get('apellido_materno', '')
                                user_data['nombre_completo'] = user_info.get('nombre_completo', user_folder.name)
                                user_data['edad'] = user_info.get('edad', 0)
                                user_data['sexo'] = user_info.get('sexo', 'no especificado')
                                user_data['id_sistema'] = user_info.get('id_sistema', '')
                                
                                # FECHA DE REGISTRO desde usuario_info.json
                                user_data['fecha_registro'] = user_info.get('fecha_registro', '')
                        except Exception as e:
                            print(f"[ERROR] Leyendo {info_file}: {e}")
                    
                    # Leer log de última sesión si existe
                    if log_file.exists():
                        try:
                            with open(log_file, 'r', encoding='utf-8') as f:
                                log_data = json.load(f)
                                user_data['ultima_sesion'] = log_data.get('ultima_sesion', 0)
                                user_data['fecha_acceso'] = log_data.get('fecha_acceso', '')
                                user_data['hora_acceso'] = log_data.get('hora_acceso', '')
                                
                                # TIMESTAMP COMPLETO desde ultimasesion.log
                                user_data['timestamp'] = log_data.get('timestamp', '')
                        except Exception as e:
                            print(f"[ERROR] Leyendo {log_file}: {e}")
                    
                    # Contar sesiones
                    session_count = sum(1 for item in user_folder.iterdir() 
                                      if item.is_dir() and item.name.startswith('sesion'))
                    user_data['total_sesiones'] = session_count
                    
                    users.append(user_data)
            
            # Ordenar por timestamp de último acceso (más reciente primero)
            users.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            print(f"[USER_MGR] Usuarios encontrados: {len(users)}")
            for u in users:
                print(f"  - {u.get('nombre_completo', u['username'])}")
                print(f"    Registrado: {u.get('fecha_registro', 'N/A')}")
                print(f"    Último uso: {u.get('timestamp', 'N/A')}")
            
            return users
        
        except Exception as e:
            print(f"[USER_MGR ERROR] Error listando usuarios: {e}")
            return []
    
    def create_new_session(self, user_data):
        """
        Crea una nueva sesión para un usuario existente
        
        Retorna:
            dict con información de la sesión creada
        """
        user_dir = self.get_user_directory(user_data)
        
        if not user_dir.exists():
            return {
                'success': False,
                'error': 'Usuario no existe'
            }
        
        try:
            # Obtener siguiente número de sesión
            session_num = self.get_next_session_number(user_dir)
            session_dir = user_dir / f"sesion{session_num}"
            
            # Crear carpeta de sesión
            session_dir.mkdir(exist_ok=True)
            
            # Crear subcarpetas
            derivaciones_dir = session_dir / "derivaciones"
            electrodos_dir = session_dir / "electrodos"
            
            derivaciones_dir.mkdir(exist_ok=True)
            electrodos_dir.mkdir(exist_ok=True)
            
            # Actualizar log
            self.update_last_session_log(user_data, session_num)
            
            print(f"[USER_MGR] Nueva sesión creada: sesion{session_num}")
            
            return {
                'success': True,
                'session_num': session_num,
                'session_dir': str(session_dir),
                'derivaciones_dir': str(derivaciones_dir),
                'electrodos_dir': str(electrodos_dir)
            }
        
        except Exception as e:
            print(f"[USER_MGR ERROR] Error creando sesión: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def get_user_session_path(self, user_data, session_num=None):
        """
        Obtiene el path de una sesión específica del usuario
        Si no se especifica sesión, retorna la última
        """
        user_dir = self.get_user_directory(user_data)
        
        if not user_dir.exists():
            return None
        
        if session_num is None:
            # Obtener última sesión
            log_file = user_dir / LOG_FILENAME
            if log_file.exists():
                try:
                    with open(log_file, 'r', encoding='utf-8') as f:
                        log_data = json.load(f)
                        session_num = log_data.get('ultima_sesion', 1)
                except:
                    session_num = 1
            else:
                session_num = 1
        
        session_dir = user_dir / f"sesion{session_num}"
        
        if session_dir.exists():
            return {
                'session_dir': str(session_dir),
                'derivaciones_dir': str(session_dir / "derivaciones"),
                'electrodos_dir': str(session_dir / "electrodos"),
                'session_num': session_num
            }
        
        return None


# Funciones de utilidad para integración con el código existente
def init_user_manager():
    """Inicializa el gestor de usuarios"""
    return UserManager()


def handle_user_registration(user_data):
    """
    Maneja el registro de un nuevo usuario
    Para integrar con el código existente
    """
    manager = UserManager()
    result = manager.create_user_structure(user_data)
    
    return result


def handle_user_login(user_data):
    """
    Maneja el inicio de sesión de un usuario existente
    Actualiza el log y prepara la sesión
    """
    manager = UserManager()
    
    if manager.user_exists(user_data):
        manager.update_last_session_log(user_data)
        return {
            'success': True,
            'user_dir': str(manager.get_user_directory(user_data)),
            'message': 'Usuario autenticado'
        }
    else:
        # Si el usuario no existe, crearlo
        return handle_user_registration(user_data)


def get_all_users():
    """Obtiene lista de todos los usuarios para el frontend"""
    manager = UserManager()
    return manager.list_all_users()


# Para pruebas
if __name__ == "__main__":
    print("="*60)
    print("  GESTOR DE USUARIOS - PRUEBA")
    print("="*60)
    
    # Crear usuario de prueba
    test_user = {
        'nombres': 'Juan Carlos',
        'apellidoPaterno': 'Pérez',
        'apellidoMaterno': 'García',
        'nombreCompleto': 'Juan Carlos Pérez García',
        'edad': 25,
        'sexo': 'masculino',
        'id': 'test_123',
        'fechaRegistro': datetime.now().isoformat()
    }
    
    manager = UserManager()
    
    print("\n1. Creando estructura de usuario...")
    result = manager.create_user_structure(test_user)
    print(f"   Resultado: {result}")
    
    print("\n2. Listando usuarios...")
    users = manager.list_all_users()
    for user in users:
        print(f"   - {user.get('nombre_completo', user.get('username'))}")
    
    print("\n3. Creando nueva sesión...")
    session_result = manager.create_new_session(test_user)
    print(f"   Resultado: {session_result}")
    
    print("\n" + "="*60)