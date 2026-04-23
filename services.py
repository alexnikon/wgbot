from config import CLIENTS_JSON_PATH, CUSTOM_CLIENTS_PATH
from custom_clients import CustomClientsManager
from database import Database
from utils import ClientsJsonManager
from wg_api import WGDashboardAPI
from yookassa_client import YooKassaClient


db = Database()
wg_api = WGDashboardAPI()
yookassa_client = YooKassaClient()
clients_manager = ClientsJsonManager(CLIENTS_JSON_PATH)
custom_clients_manager = CustomClientsManager(CUSTOM_CLIENTS_PATH)


async def close_shared_services() -> None:
    """Close shared clients used across bot polling and webhook processing."""
    await yookassa_client.aclose()
    wg_api.close()
