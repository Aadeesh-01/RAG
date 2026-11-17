"""Site-wide views."""

from django.shortcuts import redirect, render


def home(request):
    """The landing page, with the latest items from the shop."""
    return render(request, "home.html", {"title": "Zoo shop", "items": []})


def legacy_item(request, pk):
    """Old item links: send them on to the shop's item page."""
    return redirect("shop:item-detail", pk=pk)
