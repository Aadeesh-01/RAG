"""Bridge edges across languages and processes (v10): JNI, ctypes/cffi, HTTP."""

import os
import shutil

import pytest

pytest.importorskip("tree_sitter_language_pack")

from coderag import RAGCodebaseAgent, get_config  # noqa: E402
from coderag.bridges import BridgeEdge, detect  # noqa: E402
from coderag.bridges.http import normalize  # noqa: E402
from coderag.bridges.jni import decode  # noqa: E402
from coderag.bridges._source import literal  # noqa: E402
from coderag.chunking import load_chunks  # noqa: E402
from coderag.context import display_name  # noqa: E402
from coderag.resolve import CodeGraph  # noqa: E402
from conftest import ROOT, FakeIndex  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "bridges")
V10 = get_config("v10")


def bridges(sub, mechanisms=None):
    chunks = load_chunks(os.path.join(FIXTURES, sub), V10, verbose=False)
    edges = detect(chunks, mechanisms or V10.bridges)
    return chunks, {(display_name(chunks[e.src]), display_name(chunks[e.dst])): e for e in edges}


# ── JNI ──────────────────────────────────────────────────────────────────────
def test_jni_names_decode_with_escaping():
    assert decode("Java_com_example_zoo_NativeMath_add") == ("com.example.zoo.NativeMath", "add")
    assert decode("Java_com_example_zoo_NativeMath_format_1name") == ("com.example.zoo.NativeMath", "format_name")
    assert decode("Java_com_example_zoo_NativeClock_now_1ms") == ("com.example.zoo.NativeClock", "now_ms")
    assert decode("Java_pkg_Outer_00024Inner_run") == ("pkg.Outer$Inner", "run")         # _0xxxx: "$"
    assert decode("Java_pkg_C_take_3B") == ("pkg.C", "take[B")                            # _3: "["
    assert decode("Java_pkg_C_add__II") == ("pkg.C", "add")                               # overload signature
    assert decode("helper") is None and decode("Java_") is None


def test_jni_links_kotlin_and_java_declarations_to_c():
    chunks, edges = bridges("jni")
    assert set(edges) == {
        ("NativeMath", "Java_com_example_zoo_NativeMath_add"),
        ("NativeMath", "Java_com_example_zoo_NativeMath_format_1name"),
        ("NativeClock", "Java_com_example_zoo_NativeClock_now_1ms"),
    }
    # format (no "_") is not format_name ("format_1name"), so the unused C function gets no edge
    assert not any(dst == "Java_com_example_zoo_NativeMath_format" for _, dst in edges)
    edge = edges[("NativeMath", "Java_com_example_zoo_NativeMath_format_1name")]
    assert edge.confidence == "exact" and edge.mechanism == "jni"
    assert edge.evidence["matched"] == ["external fun format_name", "Java_com_example_zoo_NativeMath_format_1name"]
    assert edge.evidence["src"] == "NativeMath.kt:6" and edge.evidence["dst"] == "native.c:12"
    assert edges[("NativeClock", "Java_com_example_zoo_NativeClock_now_1ms")].evidence["src"] == "NativeClock.java:10"


def test_jni_without_the_right_package_is_only_a_pattern(tmp_path):
    for name in ("NativeMath.kt", "native.c"):
        shutil.copy(os.path.join(FIXTURES, "jni", name), tmp_path)
    kt = tmp_path / "NativeMath.kt"
    kt.write_text(kt.read_text().replace("package com.example.zoo", "package com.other"))
    chunks = load_chunks(str(tmp_path), V10, verbose=False)
    assert {e.confidence for e in detect(chunks, ("jni",))} == {"pattern"}


# ── ctypes / cffi ────────────────────────────────────────────────────────────
def test_ctypes_and_cffi_calls_reach_the_c_functions():
    chunks, edges = bridges("ctypes")
    assert set(edges) == {("add", "zoo_add"), ("Counter.bump", "zoo_bump"), ("scale", "zoo_scale")}
    assert all(e.confidence == "exact" for e in edges.values())     # libzoo.so / "zoo" -> zoo/zoo.c
    assert edges[("add", "zoo_add")].evidence == {
        "label": "ctypes/cffi zoo_add", "matched": ["_lib.zoo_add(", "zoo_add"],
        "src": "zoo_lib.py:11", "dst": "zoo/zoo.c:3"}
    # helper.zoo_unused() / helper.zoo_add() is on an arbitrary object, not a loaded library
    assert not any(src == "not_a_library" for src, _ in edges)
    assert not any(dst == "zoo_unused" for _, dst in edges)


# ── HTTP ─────────────────────────────────────────────────────────────────────
def test_paths_are_normalised():
    assert normalize("https://x.io:8080/api/users/:id/?q=1#top") == "/api/users/{}"
    for route in ("/items/{id}", "/items/<int:id>", "/items/<id>", "/items/:id", "items/{id:path}"):
        assert normalize(route) == "/items/{}", route
    assert normalize("/") == "/" and normalize("./data.json") is None


def test_only_literals_are_paths():
    assert literal('"/api/repos"') == "/api/repos"
    assert literal('"/api/repos/" + "mine"') == "/api/repos/mine"
    assert literal("`/api/health`") == "/api/health"
    for dynamic in ['"/api/repos/" + id', "`/api/repos/${id}`", 'f"/items/{item_id}"', "path",
                    'BASE + "/x"', '"/a".format(x)', '"/items/$id"', 'url_for("x")']:
        assert literal(dynamic) is None, dynamic


def test_http_clients_reach_their_routes():
    chunks, edges = bridges("http")
    expected = {
        ("addRepo", "add_repo"): ("exact", "POST /api/repos", "POST /api/repos"),
        ("loadMine", "my_repos"): ("pattern", "GET /api/repos/mine", "GET /api/repos/mine"),   # wrapper
        ("health", "health"): ("exact", "GET /api/health", "GET /api/health"),             # absolute URL
        ("fetch_item", "item"): ("pattern", "GET /items/42", "GET /items/<int:item_id>"),  # Flask param
        ("ping", "ping"): ("pattern", "POST /ping", "* /ping"),                            # any method
        ("save_order", "saveOrder"): ("pattern", "POST /orders/7", "POST /orders/{id}"),   # Go 1.22
        ("ItemsApi", "ItemController.get"): ("pattern", "GET /api/v1/items/{id}", "GET /api/v1/items/{id}"),
        ("ItemsApi", "ItemController.create"): ("exact", "POST /api/v1/items", "POST /api/v1/items"),
        ("Pinger.ping", "ping"): ("pattern", "GET /ping", "* /ping"),                      # OkHttp
        # interpolated URLs: each interpolation is one wildcard segment, always a pattern
        ("openRepo", "ask"): ("pattern", "POST /api/repos/{}/ask", "POST /api/repos/{repo_id}/ask"),
        ("delete_item", "item"): ("pattern", "DELETE /items/{}", "DELETE /items/<int:item_id>"),
    }
    assert set(edges) == set(expected)
    for key, (confidence, client, server) in expected.items():
        assert edges[key].confidence == confidence, key
        assert edges[key].evidence["matched"] == [client, server], key
    assert edges[("addRepo", "add_repo")].evidence["src"] == "web/client.js:11"
    assert edges[("addRepo", "add_repo")].evidence["dst"] == "server/api.py:8"
    assert edges[("addRepo", "add_repo")].label == "HTTP POST /api/repos"


def test_dynamic_urls_are_never_matched():
    chunks, edges = bridges("http")
    sources = {src for src, _ in edges}
    # a literal plus a variable ("/api/repos/mine/" + id) stays unmatched
    assert "forgetRepo" not in sources
    # a literal route beats a parametrised one: /api/repos/mine is not also /api/repos/{repo_id}
    assert ("loadMine", "repo_info") not in edges
    # templates and f-strings are patterns, never exact
    assert edges[("openRepo", "ask")].confidence == edges[("delete_item", "item")].confidence == "pattern"


# ── in the graph ─────────────────────────────────────────────────────────────
def polyglot(tmp_path):
    for sub in ("jni", "ctypes", "http"):
        shutil.copytree(os.path.join(FIXTURES, sub), tmp_path / sub)
    return str(tmp_path)


def test_v10_is_v9_plus_bridges_and_project_files():
    v9, v10 = get_config("v9"), get_config("v10")
    assert v9.bridges == () and v10.bridges == ("jni", "ctypes_cffi", "http")
    assert v9.project_files is False and v10.project_files is True
    changed = ("name", "description", "bridges", "project_files")
    assert {k: v for k, v in vars(v9).items() if k not in changed} == \
           {k: v for k, v in vars(v10).items() if k not in changed}
    assert v9.index_mismatch(v10) == ["bridges", "project_files"]


def test_expansion_follows_bridges_as_a_hop(tmp_path):
    root = polyglot(tmp_path)
    agent = RAGCodebaseAgent(root, V10, index=FakeIndex(), load_llm=False, verbose=False)
    names = [display_name(d) for d in agent.documents]
    client, server = names.index("addRepo"), names.index("add_repo")
    graph = agent.code_graph
    assert (server, "bridge:http") in graph.typed_neighbors(client)
    assert (client, "bridged by:http") in graph.typed_neighbors(server)
    agent.index.ranking = [client]
    first = next(agent.stream("add a repo", llm=None, top_k=1))
    reached = {s["name"]: s for s in first["sources"]}
    assert reached["add_repo"]["hop"] == 1 and reached["add_repo"]["relation"] == "callee"
    assert reached["add_repo"]["bridge"]["label"] == "HTTP POST /api/repos"
    assert reached["add_repo"]["confidence"] == "exact"
    assert reached["add_repo"]["bridge"]["evidence"]["dst"] == "http/server/api.py:8"
    # v9 has no such edge
    v9 = RAGCodebaseAgent(root, get_config("v9"), index=FakeIndex([client]), load_llm=False, verbose=False)
    assert "add_repo" not in {s["name"] for s in next(v9.stream("add a repo", llm=None, top_k=1))["sources"]}


def test_bridges_are_saved_with_the_index_and_drawn(tmp_path):
    from coderag import persist
    from coderag.visualize import graph_data

    root = polyglot(tmp_path / "repo")
    agent = RAGCodebaseAgent(root, V10, index=FakeIndex(), load_llm=False, verbose=False)
    agent.index.save = lambda directory: None
    persist.save_agent(agent, str(tmp_path / "index"))
    saved = persist._read_json(str(tmp_path / "index"), persist.GRAPH_FILE)
    loaded = CodeGraph.from_dict(agent.documents, saved)
    assert loaded.bridges.edges == agent.code_graph.bridges.edges
    assert set(loaded.bridges.counts()) == {"jni", "ctypes_cffi", "http"}
    links = [l for l in graph_data(agent)["links"] if l["type"] == "bridge"]
    assert {l["mechanism"] for l in links} == {"jni", "ctypes_cffi", "http"}
    for link in links:
        assert link["confidence"] in ("exact", "pattern")
        assert link["evidence"]["src"] and link["evidence"]["dst"] and len(link["evidence"]["matched"]) == 2
    # graphs saved before v10 have no bridges
    del saved["bridges"]
    assert len(CodeGraph.from_dict(agent.documents, saved).bridges) == 0


def test_edges_round_trip():
    edge = BridgeEdge(1, 2, "http", "exact", {"label": "HTTP GET /x", "matched": ["GET /x", "GET /x"],
                                              "src": "a.js:1", "dst": "b.py:2"})
    assert BridgeEdge.from_dict(edge.to_dict()) == edge and edge.label == "HTTP GET /x"
