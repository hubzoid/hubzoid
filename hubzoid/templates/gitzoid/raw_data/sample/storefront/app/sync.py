"""Nightly stock sync from suppliers. The warehouse reads the counts at 06:00."""
import logging

from .suppliers import client

log = logging.getLogger(__name__)


def sync_supplier(supplier_id: str) -> list[dict]:
    """A supplier's stock levels. Raises when the supplier cannot be reached."""
    return client.fetch_stock(supplier_id, timeout=10)


def sync_all(supplier_ids: list[str]) -> dict[str, int]:
    counts = {}
    for supplier_id in supplier_ids:
        try:
            counts[supplier_id] = len(sync_supplier(supplier_id))
        except Exception:
            log.exception("stock sync failed for %s", supplier_id)
    return counts
