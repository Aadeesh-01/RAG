"""HTTP bridges, continued: Django URLconfs, DRF routers, templates (forms, htmx), jQuery and XHR."""

import os

import pytest

pytest.importorskip("tree_sitter_language_pack")

from coderag import get_config  # noqa: E402
from coderag.bridges import detect  # noqa: E402
from coderag.bridges._django import regex_to_path  # noqa: E402
from coderag.bridges.http import template_target  # noqa: E402
from coderag.chunking import load_chunks, parse_template_file  # noqa: E402
from coderag.context import display_name, format_chunk, public_name  # noqa: E402
from conftest import ROOT  # noqa: E402

DJANGO = os.path.join(ROOT, "tests", "fixtures", "bridges", "django")
V10 = get_config("v10")


@pytest.fixture(scope="module")
def django():
    chunks = load_chunks(DJANGO, V10, verbose=False)
    edges = {(display_name(chunks[e.src]), display_name(chunks[e.dst]), e.evidence["matched"][0]): e
             for e in detect(chunks, ("http",))}
    return chunks, edges


def test_regex_routes_become_paths():
    assert regex_to_path(r"^legacy/items/(?P<pk>[0-9]+)/$") == "legacy/items/{pk}/"
    assert regex_to_path(r"^files/([^/]+)/raw$") == "files/{}/raw"
    assert regex_to_path(r"^search/(?:a|b)+$") is None          # not a plain path: skipped


def test_template_urls():
    assert template_target("{% url 'shop:item-edit' item.pk %}") == ("ref", "shop:item-edit")
    assert template_target('{% url "home" %}') == ("ref", "home")
    assert template_target("/shop/items/") == ("url", "/shop/items/")
    for dynamic in ["/shop/items/{{ item.pk }}/", "{{ next }}", "", "edit/", "#", None,
                    "{% if x %}/a{% endif %}"]:
        assert template_target(dynamic) is None, dynamic


def test_templates_are_chunks_only_when_they_send_requests():
    form = parse_template_file(os.path.join(DJANGO, "shop", "templates", "shop", "item_form.html"), DJANGO)
    assert [(c["type"], c["language"], c["name"]) for c in form] == [("template", "html", "item_form.html")]
    assert public_name(form[0]) == "item_form.html (template)"
    assert "```html" in format_chunk(form[0]["content"], form[0], 0, "q", get_config("v10", prompt_style="cited"), 1)
    assert parse_template_file(os.path.join(DJANGO, "shop", "templates", "shop", "base.html"), DJANGO) == []
    # templates are read only with HTTP bridges: v9 and older never see them
    assert not any(c["type"] == "template" for c in load_chunks(DJANGO, get_config("v9"), verbose=False))
    assert ".html" in V10.source_extensions() and ".html" not in get_config("v9").source_extensions()


def test_django_routes_reach_views(django):
    chunks, edges = django
    found = {(src, dst): (e.confidence, e.evidence) for (src, dst, _), e in edges.items()}
    # function view: any method, so a pattern; class-based view: the method for the verb, exact
    assert found[("loadItems", "ItemListView.get")][0] == "exact"
    assert found[("loadItems", "ItemListView.get")][1]["route"] == "shop/urls.py:8"   # include("shop.urls") prefix
    assert found[("loadItems", "ItemListView.get")][1]["dst"] == "shop/views.py:10"
    assert found[("legacy", "legacy_item")][0] == "pattern"                          # re_path
    assert found[("legacy", "legacy_item")][1]["matched"] == ["POST /legacy/items/9/", "* /legacy/items/{pk}/"]


def test_drf_router_routes_are_patterns(django):
    _, edges = django
    create = edges[("createOrder", "OrderViewSet.create", "POST /api/orders/")]
    assert create.confidence == "pattern" and create.evidence["route"] == "api/urls.py:9"
    # retrieve isn't defined on the viewset (ModelViewSet provides it): the class stands for it
    retrieve = edges[("readOrder", "OrderViewSet", "GET /api/orders/42/")]
    assert retrieve.confidence == "pattern" and retrieve.evidence["matched"][1] == "GET /api/orders/{pk}"


def test_templates_forms_and_htmx(django):
    _, edges = django
    by_url = {(src, dst): e for (src, dst, _), e in edges.items() if src == "item_form.html"}
    edit = by_url[("item_form.html", "ItemEditView.post")]                # {% url 'shop:item-edit' %} POST
    assert edit.confidence == "exact"
    assert edit.evidence["matched"] == ["POST {% url 'shop:item-edit' %}", "POST /shop/items/<int:pk>/edit/"]
    assert edit.evidence["src"] == "shop/templates/shop/item_form.html:6"
    assert by_url[("item_form.html", "ItemListView.get")].confidence == "exact"     # hx-get literal
    assert by_url[("item_form.html", "home")].evidence["matched"][0] == "GET {% url 'home' %}"
    assert by_url[("item_form.html", "legacy_item")].confidence == "pattern"       # literal form action
    # the {% url %} names are resolved, not guessed: GET on the edit form isn't the POST handler
    assert ("item_form.html", "ItemEditView.get") not in by_url


def test_dynamic_and_non_request_urls_are_never_matched(django):
    chunks, edges = django
    sources = {src for src, _, _ in edges}
    assert "saveItem" not in sources                                     # $.ajax url with a variable
    assert not any("DELETE" in matched for _, _, matched in edges)       # hx-delete with {{ item.pk }}
    assert not any(src == "legacy" and dst == "ItemListView.get" for src, dst, _ in edges)   # window.open
    assert len([1 for src, _, _ in edges if src == "item_form.html"]) == 4


def test_jquery_and_xhr_clients(django):
    _, edges = django
    methods = {src: matched.split()[0] for src, _, matched in edges if src != "item_form.html"}
    assert methods == {"loadItems": "GET", "createOrder": "POST", "readOrder": "GET", "legacy": "POST"}
