"""Settings, read from the environment. Secrets never live in this file."""
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///storefront.db")
SUPPLIERS_API_URL = os.environ.get("SUPPLIERS_API_URL", "https://suppliers.example.com")
PAYMENTS_API_URL = os.environ.get("PAYMENTS_API_URL", "https://payments.example.com")
PAYMENTS_API_KEY = os.environ["PAYMENTS_API_KEY"]
