"""Flask blueprint for items."""

from flask import Blueprint

bp = Blueprint("items", __name__)


@bp.route("/items/<int:item_id>", methods=["GET", "DELETE"])
def item(item_id):
    """Read or delete one item by its numeric id."""
    return {"id": item_id, "name": f"item {item_id}", "deleted": False}
