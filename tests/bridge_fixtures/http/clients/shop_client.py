"""Python clients of the item and order services."""

import httpx
import requests


def fetch_item(item_id):
    """Read item 42's record from the Flask service (a literal id)."""
    return requests.get("http://items.local/items/42").json()


def delete_item(item_id):
    """Dynamic URL built with an f-string: must not be matched."""
    return requests.delete(f"http://items.local/items/{item_id}").status_code == 204


def ping():
    """Health check on the Go service; any method is accepted there."""
    return httpx.post("http://orders.local/ping", timeout=2).text == "pong"


def save_order():
    """Create order 7 on the Go service with an explicit method."""
    return requests.request("POST", "http://orders.local/orders/7", json={"total": 3}).status_code
