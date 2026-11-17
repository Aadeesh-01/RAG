"""The Flask app: registers both blueprints; admin moves to /staff."""

from flask import Flask

from .shop import admin, shop

app = Flask(__name__)
app.register_blueprint(shop)
app.register_blueprint(admin, url_prefix="/staff")
