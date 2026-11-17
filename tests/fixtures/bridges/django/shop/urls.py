from django.urls import path

from . import views

app_name = "shop"

urlpatterns = [
    path("items/", views.ItemListView.as_view(), name="item-list"),
    path("items/<int:pk>/", views.item_detail, name="item-detail"),
    path("items/<int:pk>/edit/", views.ItemEditView.as_view(), name="item-edit"),
]
