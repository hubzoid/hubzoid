"""Product catalog, loaded from the YAML files the buying team edits."""
import time
from pathlib import Path

import yaml

CATALOG_DIR = Path(__file__).parent / "catalog"
CACHE_SECONDS = 300
_cache: dict = {"at": 0.0, "products": []}


def load_catalog() -> list[dict]:
    """Every product, re-read from disk at most every five minutes."""
    if time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["products"]
    products = []
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        products += yaml.load(path.read_text(), Loader=yaml.FullLoader) or []
    _cache.update(at=time.monotonic(), products=products)
    return products
