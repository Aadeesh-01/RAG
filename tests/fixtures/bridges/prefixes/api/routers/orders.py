"""Orders: no prefix of its own, mounted under /v2."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/orders")
def list_orders():
    """Every open order, newest first, with its total."""
    return [{"id": 1, "total": 12, "open": True}, {"id": 2, "total": 30, "open": True}]
