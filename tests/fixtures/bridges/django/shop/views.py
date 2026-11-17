"""Shop views: a function view and two class-based views."""

from django.http import JsonResponse
from django.views import View


class ItemListView(View):
    """All items in the shop; POST adds one."""

    def get(self, request):
        items = [{"id": 1, "name": "bone"}, {"id": 2, "name": "ball"}]
        return JsonResponse({"items": items, "count": len(items)})

    def post(self, request):
        item = {"id": 3, "name": request.POST.get("name", "new item")}
        return JsonResponse(item, status=201)


def item_detail(request, pk):
    """One item's page, by its primary key, for any HTTP method."""
    return JsonResponse({"id": pk, "name": f"item {pk}", "in_stock": True})


class ItemEditView(View):
    """The edit form for one item, and saving it."""

    def get(self, request, pk):
        form = {"id": pk, "fields": ["name", "price"], "action": "save"}
        return JsonResponse(form)

    def post(self, request, pk):
        saved = {"id": pk, "name": request.POST.get("name"), "saved": True}
        return JsonResponse(saved)
