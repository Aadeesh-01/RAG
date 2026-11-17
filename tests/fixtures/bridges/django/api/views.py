"""The orders API (Django REST framework)."""

from rest_framework import viewsets
from rest_framework.response import Response


class OrderViewSet(viewsets.ModelViewSet):
    """Orders: list and create are customised; the other actions come from ModelViewSet."""

    def list(self, request):
        orders = [{"id": 1, "total": 12}, {"id": 2, "total": 30}]
        return Response({"orders": orders, "count": len(orders)})

    def create(self, request):
        order = {"id": 3, "total": request.data.get("total", 0), "status": "new"}
        return Response(order, status=201)
