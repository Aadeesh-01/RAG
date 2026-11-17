"""HTTP bridges: interpolated URLs as patterns, and route prefixes from mounting."""

import os

import pytest

pytest.importorskip("tree_sitter_language_pack")

from coderag import get_config  # noqa: E402
from coderag.bridges import detect  # noqa: E402
from coderag.bridges._source import url_template  # noqa: E402
from coderag.chunking import load_chunks  # noqa: E402
from coderag.context import display_name  # noqa: E402
from conftest import ROOT  # noqa: E402

PREFIXES = os.path.join(ROOT, "tests", "fixtures", "bridges", "prefixes")


def test_interpolations_become_one_wildcard_each():
    assert url_template('"/api/repos"') == ("/api/repos", False)
    assert url_template("`/api/repos/${encodeURIComponent(id)}/ask`") == ("/api/repos/{}/ask", True)
    assert url_template('f"/items/{item_id}/parts/{part}"') == ("/items/{}/parts/{}", True)
    assert url_template('"/items/{}".format(pk)') == ("/items/{}", True)
    assert url_template('"/items/{pk}".format(pk=pk)') == ("/items/{}", True)
    # still dynamic: a variable, base + path, a call
    for expr in ["url", '"/api/repos/" + id', 'BASE + "/x"', "build_url(id)", "`${base}/x` + y"]:
        assert url_template(expr) is None, expr


@pytest.fixture(scope="module")
def edges():
    chunks = load_chunks(PREFIXES, get_config("v10"), verbose=False)
    return {(display_name(chunks[e.src]), display_name(chunks[e.dst])): e for e in detect(chunks, ("http",))}


def test_route_prefixes(edges):
    expected = {
        ("readItem", "read_item"): ("pattern", "GET /v1/items/{item_id}"),     # include_router + APIRouter
        ("createItem", "create_item"): ("exact", "POST /v1/items"),
        ("listOrders", "list_orders"): ("exact", "GET /v2/orders"),  # import alias
        ("addToCart", "add_to_cart"): ("exact", "POST /shop/cart"),             # Blueprint url_prefix
        ("staffStats", "stats"): ("exact", "GET /staff/stats"),                # register_blueprint override
        ("getUser", "getUser"): ("pattern", "GET /api/users/:id"),             # app.use + require
    }
    assert set(edges) == set(expected)
    for key, (confidence, server) in expected.items():
        assert edges[key].confidence == confidence, key
        assert edges[key].evidence["matched"][1] == server, key


def test_unprefixed_or_overridden_paths_do_not_match(edges):
    sources = {src for src, _ in edges}
    assert "readItemWithoutPrefix" not in sources      # /items/{id} is only reachable under /v1
    assert "adminStats" not in sources                 # admin's own /admin prefix was replaced by /staff
