"""Flask blueprints: shop keeps its url_prefix; admin's is replaced when it is registered."""

from flask import Blueprint

shop = Blueprint("shop", __name__, url_prefix="/shop")
admin = Blueprint("admin", __name__, url_prefix="/admin")


@shop.route("/cart", methods=["POST"])
def add_to_cart():
    """Put an item in the visitor's cart and return the cart's size."""
    return {"items": 1, "total": 12, "currency": "EUR"}


@admin.route("/stats")
def stats():
    """Sales numbers for the staff dashboard, today and this week."""
    return {"today": 4, "week": 31, "refunds": 0}
