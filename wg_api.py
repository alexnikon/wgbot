import requests
import datetime
import uuid
import logging
from typing import Dict, Any, Optional, Tuple
from config import WG_DASHBOARD_URL, WG_DASHBOARD_API_KEY, WG_CONFIG_NAME, PEER_EXPIRY_DAYS

logger = logging.getLogger(__name__)

class WGDashboardAPI:
    def __init__(self):
        self.base_url = WG_DASHBOARD_URL
        self.api_key = WG_DASHBOARD_API_KEY
        self.config_name = WG_CONFIG_NAME
        self.headers = {
            "Content-Type": "application/json",
            "wg-dashboard-apikey": self.api_key
        }
    
    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """Выполняет HTTP запрос к WGDashboard API"""
        url = f"{self.base_url}{endpoint}"
        
        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=self.headers)
            elif method.upper() == "POST":
                response = requests.post(url, json=data, headers=self.headers)
            else:
                raise ValueError(f"Неподдерживаемый HTTP метод: {method}")
            
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при запросе к {url}: {e}")
            raise Exception(f"Ошибка API: {e}")
    
    def handshake(self) -> Dict[str, Any]:
        """Проверка соединения с WGDashboard"""
        return self._make_request("GET", "/api/handshake")
    
    def add_peer(self, name: str) -> Dict[str, Any]:
        """
        Создает нового пира в WireGuard конфигурации
        
        Args:
            name: Имя пира
            
        Returns:
            Информация о созданном пире
        """
        data = {"name": name}
        result = self._make_request("POST", f"/api/addPeers/{self.config_name}", data)
        
        # Извлекаем данные пира из массива
        if result and 'data' in result and len(result['data']) > 0:
            peer_data = result['data'][0]
            return {
                'id': peer_data.get('id'),  # Это public_key
                'name': peer_data.get('name'),
                'private_key': peer_data.get('private_key'),
                'public_key': peer_data.get('id'),  # id = public_key
                'allowed_ip': peer_data.get('allowed_ip'),
                'status': peer_data.get('status')
            }
        
        return result
    
    def delete_peer(self, peer_id: str) -> Dict[str, Any]:
        """
        Удаляет пира из WireGuard конфигурации
        
        Args:
            peer_id: ID пира для удаления
            
        Returns:
            Результат операции
        """
        data = {"peers": [peer_id]}
        return self._make_request("POST", f"/api/deletePeers/{self.config_name}", data)
    
    def create_restrict_job(self, peer_id: str, expire_date_str: str = None) -> Tuple[Dict[str, Any], str, str]:
        """
        Создает job для ограничения пира
        
        Args:
            peer_id: ID пира
            expire_date_str: Дата истечения (если None, то через 30 дней)
            
        Returns:
            Tuple: (результат API, job_id, дата истечения)
        """
        job_id = str(uuid.uuid4())
        
        if expire_date_str is None:
            # Если дата не указана, создаем через 30 дней
            expire_date = (datetime.datetime.now() + datetime.timedelta(days=PEER_EXPIRY_DAYS))
            expire_date_str = expire_date.strftime("%Y-%m-%d %H:%M:%S")
        else:
            # Если дата указана, проверяем, что она в будущем
            try:
                expire_date = datetime.datetime.strptime(expire_date_str, "%Y-%m-%d %H:%M:%S")
                now = datetime.datetime.now()
                if expire_date <= now:
                    # Если дата в прошлом, создаем новую дату через 30 дней
                    expire_date = now + datetime.timedelta(days=PEER_EXPIRY_DAYS)
                    expire_date_str = expire_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                # Если дата в неправильном формате, создаем новую
                expire_date = datetime.datetime.now() + datetime.timedelta(days=PEER_EXPIRY_DAYS)
                expire_date_str = expire_date.strftime("%Y-%m-%d %H:%M:%S")
        
        creation_date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        data = {
            "Job": {
                "JobID": job_id,
                "Configuration": self.config_name,
                "Peer": peer_id,
                "Field": "date",
                "Operator": "lgt",
                "Value": expire_date_str,
                "CreationDate": creation_date_str,
                "ExpireDate": expire_date_str,
                "Action": "restrict"
            }
        }
        
        result = self._make_request("POST", "/api/savePeerScheduleJob", data)
        return result, job_id, expire_date_str
    
    def delete_job(self, job_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Удаляет job пира
        
        Args:
            job_data: Данные job для удаления
            
        Returns:
            Результат операции
        """
        return self._make_request("POST", "/api/deletePeerScheduleJob", {"Job": job_data})
    
    def update_job_expire_date(self, job_id: str, peer_id: str, new_expire_date_str: str) -> Dict[str, Any]:
        """
        Обновляет дату истечения существующего job
        
        Args:
            job_id: ID существующего job
            peer_id: ID пира
            new_expire_date_str: Новая дата истечения в формате "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            Результат операции
        """
        creation_date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        data = {
            "Job": {
                "JobID": job_id,
                "Configuration": self.config_name,
                "Peer": peer_id,
                "Field": "date",
                "Operator": "lgt",
                "Value": new_expire_date_str,
                "CreationDate": creation_date_str,
                "ExpireDate": new_expire_date_str,
                "Action": "restrict"
            }
        }
        
        result = self._make_request("POST", "/api/savePeerScheduleJob", data)
        return result
    
    def get_peer_info(self, peer_id: str) -> Dict[str, Any]:
        """
        Получает информацию о пире
        
        Args:
            peer_id: ID пира
            
        Returns:
            Информация о пире
        """
        # Этот метод может потребовать дополнительной реализации
        # в зависимости от доступных API endpoints
        pass
    
    def check_peer_exists(self, peer_id: str) -> bool:
        """
        Проверяет, существует ли пир на сервере
        
        Args:
            peer_id: ID пира (public_key)
            
        Returns:
            True если пир существует, False если нет
        """
        try:
            if not peer_id:
                return False
                
            import urllib.parse
            encoded_peer_id = urllib.parse.quote(peer_id, safe='')
            url = f"{self.base_url}/api/downloadPeer/{self.config_name}?id={encoded_peer_id}"
            
            response = requests.get(url, headers=self.headers)
            
            if response.status_code == 200:
                # Проверяем содержимое ответа
                result = response.json()
                if result and result.get('status') == True and result.get('data') is not None:
                    return True
                else:
                    logger.info(f"Пир {peer_id[:20]}... не существует: {result}")
                    return False
            else:
                logger.info(f"Пир {peer_id[:20]}... не существует, статус: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка при проверке существования пира {peer_id}: {e}")
            return False
    
    def get_configuration_info(self) -> Dict[str, Any]:
        """Получает информацию о WireGuard конфигурации"""
        return self._make_request("GET", f"/api/getWireguardConfigurationInfo?configurationName={self.config_name}")
    
    def get_available_ips(self) -> Dict[str, Any]:
        """Получает доступные IP адреса в конфигурации"""
        return self._make_request("GET", f"/api/getAvailableIPs/{self.config_name}")
    
    def download_peer_config(self, peer_id: str) -> bytes:
        """
        Скачивает конфигурацию пира
        
        Args:
            peer_id: ID пира (public_key)
            
        Returns:
            Конфигурация в виде байтов
        """
        if not peer_id:
            raise Exception("Peer ID не может быть пустым")
            
        import urllib.parse
        encoded_peer_id = urllib.parse.quote(peer_id, safe='')
        url = f"{self.base_url}/api/downloadPeer/{self.config_name}?id={encoded_peer_id}"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            
            # API возвращает JSON с конфигурацией
            result = response.json()
            logger.info(f"Ответ API для пира {peer_id[:20]}...: {result}")
            
            if result and result.get('data') and 'file' in result['data']:
                config_content = result['data']['file']
                return config_content.encode('utf-8')
            else:
                logger.error(f"Неверный формат ответа API или пир еще не готов: {result}")
                raise Exception(f"API Error: {result.get('message', 'Unknown error')}")
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при скачивании конфигурации пира {peer_id}: {e}")
            raise Exception(f"Ошибка при скачивании конфигурации: {e}")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при скачивании конфигурации: {e}")
            raise Exception(f"Ошибка при скачивании конфигурации: {e}")
