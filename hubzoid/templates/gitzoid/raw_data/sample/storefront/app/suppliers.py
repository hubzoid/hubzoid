"""Supplier stock API client."""
import requests

from . import config


class SupplierClient:
    def fetch_stock(self, supplier_id: str, timeout: int = 10) -> list[dict]:
        r = requests.get(f"{config.SUPPLIERS_API_URL}/stock/{supplier_id}", timeout=timeout)
        r.raise_for_status()
        return r.json()["items"]


client = SupplierClient()
