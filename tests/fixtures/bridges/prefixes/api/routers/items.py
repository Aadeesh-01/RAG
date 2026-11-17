"""Items: its own prefix /items, mounted under /v1, so /v1/items/..."""

from fastapi import APIRouter

router = APIRouter(prefix="/items", tags=["items"])


@router.get("/{item_id}")
def read_item(item_id: int):
    """One item by id, with its name and stock level."""
    return {"id": item_id, "name": f"item {item_id}", "stock": 3}


@router.post("/")
def create_item(item: dict):
    """Add an item; it gets the next id and starts with no stock."""
    return {"id": 99, "name": item.get("name"), "stock": 0}
