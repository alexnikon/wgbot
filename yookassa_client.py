import logging
import hashlib
import hmac
import json
import uuid
from typing import Dict, Any, Optional
from datetime import datetime
import httpx
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY

logger = logging.getLogger(__name__)

class YooKassaClient:
    """Client for the YooKassa API."""
    
    def __init__(self):
        self.shop_id = YOOKASSA_SHOP_ID
        self.secret_key = YOOKASSA_SECRET_KEY
        self.base_url = "https://api.yookassa.ru/v3"
        self.headers = {
            "Authorization": f"Basic {self._get_auth_token()}",
            "Content-Type": "application/json",
            "Idempotence-Key": ""
        }
    
    def _get_auth_token(self) -> str:
        """Create API auth token."""
        import base64
        auth_string = f"{self.shop_id}:{self.secret_key}"
        return base64.b64encode(auth_string.encode()).decode()
    
    def _generate_idempotence_key(self) -> str:
        """Generate a unique idempotence key."""
        return str(uuid.uuid4())
    
    async def create_payment(self, amount: int, currency: str, description: str, 
                           return_url: str, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Create a YooKassa payment.
        
        Args:
            amount: Amount in kopeks
            currency: Currency (RUB)
            description: Payment description
            return_url: Return URL after payment
            metadata: Additional metadata
            
        Returns:
            Payment data or None on error
        """
        try:
            payment_data = {
                "amount": {
                    "value": f"{amount / 100:.2f}",
                    "currency": currency
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": return_url
                },
                "capture": True,
                "description": description,
                "metadata": metadata
            }
            
            headers = self.headers.copy()
            headers["Idempotence-Key"] = self._generate_idempotence_key()
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments",
                    headers=headers,
                    json=payment_data,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Payment created: {result.get('id')}")
                    return result
                else:
                    logger.error(f"Payment creation error: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Payment creation error: {e}")
            return None
    
    async def get_payment(self, payment_id: str) -> Optional[Dict[str, Any]]:
        """
        Get payment info.
        
        Args:
            payment_id: Payment ID
            
        Returns:
            Payment data or None on error
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/payments/{payment_id}",
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Payment fetch error: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Failed to fetch payment {payment_id}: {e}")
            return None
    
    def verify_webhook_signature(self, body: str, signature: str) -> bool:
        """
        Verify YooKassa webhook signature.

        Note: YooKassa docs don't specify the exact signature algorithm.
        This method uses standard HMAC-SHA256 validation.
        
        Args:
            body: Request body
            signature: Signature from header
            
        Returns:
            True if signature is valid
        """
        try:
            if not signature:
                logger.warning("Missing webhook signature")
                return False
                
            # Compute expected signature (HMAC-SHA256)
            expected_signature = hmac.new(
                self.secret_key.encode(),
                body.encode(),
                hashlib.sha256
            ).hexdigest()
            
            # Compare signatures securely
            is_valid = hmac.compare_digest(signature, expected_signature)
            
            if not is_valid:
                logger.warning(f"Invalid webhook signature. Expected: {expected_signature[:8]}..., got: {signature[:8]}...")
            
            return is_valid
            
        except Exception as e:
            logger.error(f"Webhook signature verification error: {e}")
            return False
    
    def parse_webhook(self, body: str) -> Optional[Dict[str, Any]]:
        """
        Parse YooKassa webhook.
        
        Args:
            body: Request body
            
        Returns:
            Webhook data or None on error
        """
        try:
            data = json.loads(body)
            
            # Validate webhook structure
            if not self.validate_webhook_structure(data):
                return None
                
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Webhook parse error: {e}")
            return None
    
    def validate_webhook_structure(self, data: Dict[str, Any]) -> bool:
        """
        Validate webhook structure according to YooKassa docs.
        
        Args:
            data: Parsed webhook payload
            
        Returns:
            True if structure is valid or can be handled
        """
        try:
            logger.debug(f"Validating webhook structure: keys={list(data.keys())}")
            
            # Check type (may be 'notification' or missing)
            notification_type = data.get('type', '')
            
            # If type present, ensure it's 'notification'
            if notification_type and notification_type != 'notification':
                logger.warning(f"Unexpected notification type: {notification_type}, continuing")
            
            # Check event or event_type
            event = data.get('event') or data.get('event_type', '')
            if not event:
                # If no event, maybe direct payment object
                if 'id' in data and 'status' in data:
                    logger.info("Webhook contains direct payment object without notification wrapper")
                    return True
                logger.error(f"Missing 'event' in webhook. Keys: {list(data.keys())}")
                return False
            
            # Check object or payment field
            event_object = data.get('object') or data.get('payment', data)
            if not event_object or not isinstance(event_object, dict):
                # If object missing but payment data is at root
                if 'id' in data and 'status' in data:
                    logger.info("Webhook contains payment data at root")
                    return True
                logger.error(f"Missing or invalid 'object' in webhook. Type: {type(event_object)}")
                return False
            
            logger.debug(f"Webhook structure valid: type={notification_type}, event={event}")
            return True
            
        except Exception as e:
            logger.error(f"Webhook structure validation error: {e}", exc_info=True)
            return False
    
    def is_payment_succeeded(self, payment_data: Dict[str, Any]) -> bool:
        """
        Check if payment is successful.
        
        Args:
            payment_data: Payment data
            
        Returns:
            True if successful
        """
        return payment_data.get("status") == "succeeded"
    
    def is_payment_canceled(self, payment_data: Dict[str, Any]) -> bool:
        """
        Check if payment is canceled.
        
        Args:
            payment_data: Payment data
            
        Returns:
            True if canceled
        """
        return payment_data.get("status") == "canceled"
    
    def is_payment_waiting_for_capture(self, payment_data: Dict[str, Any]) -> bool:
        """
        Check if payment is waiting for capture.
        
        Args:
            payment_data: Payment data
            
        Returns:
            True if waiting for capture
        """
        return payment_data.get("status") == "waiting_for_capture"
    
    def is_refund_succeeded(self, refund_data: Dict[str, Any]) -> bool:
        """
        Check if refund succeeded.
        
        Args:
            refund_data: Refund data
            
        Returns:
            True if successful
        """
        return refund_data.get("status") == "succeeded"
    
    def get_payment_amount(self, payment_data: Dict[str, Any]) -> int:
        """
        Get payment amount in kopeks.
        
        Args:
            payment_data: Payment data
            
        Returns:
            Amount in kopeks
        """
        try:
            amount_value = payment_data.get("amount", {}).get("value", "0")
            return int(float(amount_value) * 100)
        except (ValueError, TypeError):
            return 0
    
    def get_payment_metadata(self, payment_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get payment metadata.
        
        Args:
            payment_data: Payment data
            
        Returns:
            Payment metadata
        """
        return payment_data.get("metadata", {})
