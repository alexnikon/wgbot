import json
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from config import YOOKASSA_SECRET_KEY, YOOKASSA_SHOP_ID

logger = logging.getLogger(__name__)


class YooKassaError(RuntimeError):
    """Base error raised by the YooKassa integration."""


class YooKassaUnavailable(YooKassaError):
    """YooKassa could not be reached or returned a transient error."""


class YooKassaNotFound(YooKassaError):
    """The requested YooKassa object does not exist."""


class YooKassaClient:
    """Client for the YooKassa API."""
    
    def __init__(self):
        self.shop_id = YOOKASSA_SHOP_ID
        self.secret_key = YOOKASSA_SECRET_KEY
        self.base_url = "https://api.yookassa.ru/v3"
        self.timeout = httpx.Timeout(30.0, connect=10.0)
        self._client: httpx.AsyncClient | None = None
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

    def _get_client(self) -> httpx.AsyncClient:
        """Return a shared async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the shared async HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
    
    async def create_payment(self, amount: int, currency: str, description: str, 
                           return_url: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
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
            
            response = await self._get_client().post(
                f"{self.base_url}/payments",
                headers=headers,
                json=payment_data,
            )
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Payment created: {result.get('id')}")
                return result

            logger.error(f"Payment creation error: {response.status_code} - {response.text}")
            return None
                    
        except Exception as e:
            logger.error(f"Payment creation error: {e}")
            return None
    
    async def _fetch_resource(self, resource: str, object_id: str) -> dict[str, Any]:
        """Fetch an authoritative YooKassa object and preserve failure semantics."""
        try:
            response = await self._get_client().get(
                f"{self.base_url}/{resource}/{object_id}",
                headers=self.headers,
            )
        except httpx.HTTPError as exc:
            raise YooKassaUnavailable(
                f"Failed to fetch {resource}/{object_id}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise YooKassaNotFound(f"YooKassa {resource}/{object_id} was not found")
        if response.status_code >= 500 or response.status_code in {408, 425, 429}:
            raise YooKassaUnavailable(
                f"YooKassa returned {response.status_code} for {resource}/{object_id}"
            )
        if response.status_code != 200:
            raise YooKassaError(
                f"YooKassa returned {response.status_code} for {resource}/{object_id}: "
                f"{response.text[:300]}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise YooKassaUnavailable("YooKassa returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise YooKassaUnavailable("YooKassa returned an invalid object")
        return result

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch authoritative payment data from YooKassa."""
        return await self._fetch_resource("payments", payment_id)

    async def get_refund(self, refund_id: str) -> dict[str, Any]:
        """Fetch authoritative refund data from YooKassa."""
        return await self._fetch_resource("refunds", refund_id)
    
    def parse_webhook(self, body: str) -> dict[str, Any] | None:
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
    
    def validate_webhook_structure(self, data: dict[str, Any]) -> bool:
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
    
    def get_payment_amount(self, payment_data: dict[str, Any]) -> int:
        """
        Get payment amount in kopeks.
        
        Args:
            payment_data: Payment data
            
        Returns:
            Amount in kopeks
        """
        try:
            amount_value = Decimal(str(payment_data.get("amount", {}).get("value", "0")))
            return int(amount_value * 100)
        except (InvalidOperation, ValueError, TypeError):
            return 0
    
    def get_payment_metadata(self, payment_data: dict[str, Any]) -> dict[str, Any]:
        """
        Get payment metadata.
        
        Args:
            payment_data: Payment data
            
        Returns:
            Payment metadata
        """
        return payment_data.get("metadata", {})
