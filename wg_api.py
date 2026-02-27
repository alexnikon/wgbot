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
        """Execute an HTTP request to the WGDashboard API."""
        url = f"{self.base_url}{endpoint}"
        
        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=self.headers)
            elif method.upper() == "POST":
                response = requests.post(url, json=data, headers=self.headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error for {url}: {e}")
            raise Exception(f"API error: {e}")
    
    def handshake(self) -> Dict[str, Any]:
        """Check connectivity with WGDashboard."""
        return self._make_request("GET", "/api/handshake")
    
    def add_peer(self, name: str) -> Dict[str, Any]:
        """
        Create a new peer in the WireGuard configuration.
        
        Args:
            name: Peer name
            
        Returns:
            Created peer info
        """
        data = {"name": name}
        result = self._make_request("POST", f"/api/addPeers/{self.config_name}", data)
        
        # Extract peer data from the list
        if result and 'data' in result and len(result['data']) > 0:
            peer_data = result['data'][0]
            return {
                'id': peer_data.get('id'),  # This is public_key
                'name': peer_data.get('name'),
                'private_key': peer_data.get('private_key'),
                'public_key': peer_data.get('id'),  # id = public_key
                'allowed_ip': peer_data.get('allowed_ip'),
                'status': peer_data.get('status')
            }
        
        return result
    
    def delete_peer(self, peer_id: str) -> Dict[str, Any]:
        """
        Delete a peer from the WireGuard configuration.
        
        Args:
            peer_id: Peer ID to delete
            
        Returns:
            Operation result
        """
        data = {"peers": [peer_id]}
        return self._make_request("POST", f"/api/deletePeers/{self.config_name}", data)

    def allow_access_peer(self, peer_id: str) -> Dict[str, Any]:
        """
        Remove restriction for a peer (allow access).

        Args:
            peer_id: Peer ID (public_key)

        Returns:
            Operation result
        """
        data = {"peers": [peer_id]}
        return self._make_request("POST", f"/api/allowAccessPeers/{self.config_name}", data)
    
    def create_restrict_job(self, peer_id: str, expire_date_str: str = None) -> Tuple[Dict[str, Any], str, str]:
        """
        Create a restriction job for a peer.
        
        Args:
            peer_id: Peer ID
            expire_date_str: Expiration date (if None, use +30 days)
            
        Returns:
            Tuple: (API result, job_id, expiration date)
        """
        job_id = str(uuid.uuid4())
        
        if expire_date_str is None:
            # If no date provided, use +30 days
            expire_date = (datetime.datetime.now() + datetime.timedelta(days=PEER_EXPIRY_DAYS))
            expire_date_str = expire_date.strftime("%Y-%m-%d %H:%M:%S")
        else:
            # If date provided, ensure it's in the future
            try:
                expire_date = datetime.datetime.strptime(expire_date_str, "%Y-%m-%d %H:%M:%S")
                now = datetime.datetime.now()
                if expire_date <= now:
                    # If date is in the past, move to +30 days
                    expire_date = now + datetime.timedelta(days=PEER_EXPIRY_DAYS)
                    expire_date_str = expire_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                # If date format is invalid, use +30 days
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
        Delete a peer job.
        
        Args:
            job_data: Job data for deletion
            
        Returns:
            Operation result
        """
        return self._make_request("POST", "/api/deletePeerScheduleJob", {"Job": job_data})
    
    def update_job_expire_date(self, job_id: str, peer_id: str, new_expire_date_str: str) -> Dict[str, Any]:
        """
        Update the expiration date of an existing job.
        
        Args:
            job_id: Existing job ID
            peer_id: Peer ID
            new_expire_date_str: New expiration date in "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            Operation result
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
        Get peer info.
        
        Args:
            peer_id: Peer ID
            
        Returns:
            Peer info
        """
        # This may require additional implementation depending on available endpoints
        pass
    
    def check_peer_exists(self, peer_id: str) -> bool:
        """
        Check whether a peer exists on the server.
        
        Args:
            peer_id: Peer ID (public_key)
            
        Returns:
            True if the peer exists, False otherwise
        """
        try:
            if not peer_id:
                return False
                
            import urllib.parse
            encoded_peer_id = urllib.parse.quote(peer_id, safe='')
            url = f"{self.base_url}/api/downloadPeer/{self.config_name}?id={encoded_peer_id}"
            
            response = requests.get(url, headers=self.headers)
            
            if response.status_code == 200:
                # Inspect response content
                result = response.json()
                if result and result.get('status') == True and result.get('data') is not None:
                    return True
                else:
                    logger.info(f"Peer {peer_id[:20]}... does not exist: {result}")
                    return False
            else:
                logger.info(f"Peer {peer_id[:20]}... does not exist, status: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to check peer existence {peer_id}: {e}")
            return False
    
    def get_configuration_info(self) -> Dict[str, Any]:
        """Get WireGuard configuration info."""
        return self._make_request("GET", f"/api/getWireguardConfigurationInfo?configurationName={self.config_name}")
    
    def get_available_ips(self) -> Dict[str, Any]:
        """Get available IPs in the configuration."""
        return self._make_request("GET", f"/api/getAvailableIPs/{self.config_name}")
    
    def download_peer_config(self, peer_id: str) -> bytes:
        """
        Download a peer configuration.
        
        Args:
            peer_id: Peer ID (public_key)
            
        Returns:
            Configuration as bytes
        """
        if not peer_id:
            raise Exception("Peer ID cannot be empty")
            
        import urllib.parse
        encoded_peer_id = urllib.parse.quote(peer_id, safe='')
        url = f"{self.base_url}/api/downloadPeer/{self.config_name}?id={encoded_peer_id}"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            
            # API returns JSON containing config
            result = response.json()
            logger.info(f"API response for peer {peer_id[:20]}...: {result}")
            
            if result and result.get('data') and 'file' in result['data']:
                config_content = result['data']['file']
                return config_content.encode('utf-8')
            else:
                logger.error(f"Invalid API response format or peer not ready: {result}")
                raise Exception(f"API Error: {result.get('message', 'Unknown error')}")
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download peer config {peer_id}: {e}")
            raise Exception(f"Failed to download config: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while downloading config: {e}")
            raise Exception(f"Failed to download config: {e}")
