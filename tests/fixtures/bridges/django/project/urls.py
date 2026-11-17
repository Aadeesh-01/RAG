"""Root URLconf: the home page, a legacy regex route, and the shop and API apps."""

from django.urls import include, path, re_path

from . import views as site_views

urlpatterns = [
    path("", site_views.home, name="home"),
    path("shop/", include("shop.urls")),
    re_path(r"^legacy/items/(?P<pk>[0-9]+)/$", site_views.legacy_item, name="legacy-item"),
    path("api/", include(("api.urls", "api"))),
]
