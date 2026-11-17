"""FastAPI app: the items router is mounted under /v1, the orders router under /v2."""

from fastapi import FastAPI

from .routers import items
from .routers.orders import router as orders_router

app = FastAPI()
app.include_router(items.router, prefix="/v1")
app.include_router(orders_router, prefix="/v2")


@app.get("/status")
def status():
    """The app's own route: no prefix."""
    return {"ok": True, "routers": ["items", "orders"], "version": 2}
