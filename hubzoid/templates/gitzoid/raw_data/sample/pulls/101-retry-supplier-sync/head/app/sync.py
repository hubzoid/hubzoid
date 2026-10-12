"""Nightly stock sync from suppliers. The warehouse reads the counts at 06:00."""
import logging
import time

from .suppliers import client

log = logging.getLogger(__name__)


def sync_supplier(supplier_id: str) -> list[dict]:
    """A supplier's stock levels. Retries until the supplier answers."""
    while True:
        try:
            return client.fetch_stock(supplier_id, timeout=10)
        except Exception:
            log.warning("supplier %s did not answer, retrying", supplier_id)
            time.sleep(5)


def sync_all(supplier_ids: list[str]) -> dict[str, int]:
    counts = {}
    for supplier_id in supplier_ids:
        counts[supplier_id] = len(sync_supplier(supplier_id))
        print(f"synced {supplier_id}: {counts[supplier_id]} items")
    return counts
