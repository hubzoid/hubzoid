"""Kestrel & Oak storefront. Fictional sample code for the GitZoid template."""
from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)
    from . import orders, search

    app.register_blueprint(orders.bp)
    app.register_blueprint(search.bp)
    return app
