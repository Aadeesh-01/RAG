"""Multi-language indexing (v9): tree-sitter chunks, name-matched edges, and Python left as it was."""

import os
import shutil

import pytest

pytest.importorskip("tree_sitter_language_pack")

from coderag import RAGCodebaseAgent, get_config  # noqa: E402
from coderag.chunking import load_chunks  # noqa: E402
from coderag.config import RAGConfig  # noqa: E402
from coderag.context import display_name, format_chunk  # noqa: E402
from coderag.graph import iter_call_edges  # noqa: E402
from coderag.languages import LANGUAGES, language_of, parser_for, registered  # noqa: E402
from coderag.resolve import CodeGraph  # noqa: E402
from conftest import ROOT, FakeIndex, SAMPLE_REPO  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
V9 = get_config("v9")

# per fixture: its base class, the subclass and what it inherits, and the pair of functions
# where the first calls the second (C has structs but no inheritance)
EXPECTED = {
    "javascript": dict(classes={"Animal": [], "Dog": ["Animal"]}, call=("formatGreeting", "shout"),
                       method_calls=("Dog.speak", "shout")),
    "typescript": dict(classes={"Animal": [], "Dog": ["Animal"]}, call=("formatGreeting", "shout"),
                       method_calls=("Animal.speak", "formatGreeting")),
    "java": dict(classes={"Animal": [], "Dog": ["Animal"], "Zoo": []},
                 call=("Zoo.formatGreeting", "Zoo.shout"), method_calls=("Animal.speak", "Zoo.formatGreeting")),
    "kotlin": dict(classes={"Animal": [], "Dog": ["Animal"]}, call=("formatGreeting", "shout"),
                   method_calls=("Animal.speak", "formatGreeting")),
    "c": dict(classes={"Animal": [], "Dog": []}, call=("format_greeting", "shout"), method_calls=None),
    "cpp": dict(classes={"Animal": [], "Dog": ["Animal"]}, call=("formatGreeting", "shout"),
                method_calls=("Dog.speak", "shout")),
    "go": dict(classes={"Animal": [], "Dog": ["Animal"]}, call=("FormatGreeting", "Shout"),
               method_calls=("Animal.Speak", "FormatGreeting")),
    "rust": dict(classes={"Speak": [], "Animal": ["Speak"], "Dog": ["Speak"]}, call=("format_greeting", "shout"),
                 method_calls=("Animal.speak", "format_greeting")),
}


@pytest.fixture(scope="module", autouse=True)
def grammars():
    """Grammars are downloaded on first use; without them (offline, no cache) skip."""
    try:
        for name in registered():
            for ext in LANGUAGES[name].extensions:
                parser_for(f"x{ext}")
    except Exception as e:                                  # noqa: BLE001 (DownloadError, OSError ...)
        pytest.skip(f"tree-sitter grammars unavailable: {e}")


def chunks_of(lang, config=V9):
    return load_chunks(os.path.join(FIXTURES, lang), config, verbose=False)


def graph_of(lang):
    root = os.path.join(FIXTURES, lang)
    chunks = load_chunks(root, V9, verbose=False)
    return chunks, CodeGraph(chunks, root, V9.max_ambiguous, V9.ignore_assert_calls)


def test_every_registered_language_has_a_fixture():
    assert set(EXPECTED) == set(registered()) == {"javascript", "typescript", "java", "kotlin",
                                                  "c", "cpp", "go", "rust"}


@pytest.mark.parametrize("lang", sorted(EXPECTED))
def test_chunks_follow_the_python_schema(lang):
    chunks = chunks_of(lang)
    python = load_chunks(SAMPLE_REPO, get_config("v8"), verbose=False)[0]
    for c in chunks:
        assert set(python) <= set(c), set(python) - set(c)
        assert c["language"] == lang == language_of(c)
        assert c["type"] in ("class", "method", "function")
        # the lines a chunk claims are the lines of its code
        with open(c["file"], encoding="utf-8") as f:
            lines = f.read().splitlines()
        body = c["content"].splitlines()
        assert lines[c["start_line"] - 1].strip().startswith(body[0].strip()[:20])
        assert lines[c["end_line"] - 1].strip() == body[-1].strip() or c["type"] == "class"


@pytest.mark.parametrize("lang", sorted(EXPECTED))
def test_classes_functions_bases_and_docstrings(lang):
    chunks = chunks_of(lang)
    by_name = {display_name(c): c for c in chunks}
    classes = {c["name"]: c for c in chunks if c["type"] == "class"}
    for name, bases in EXPECTED[lang]["classes"].items():
        assert name in classes, (lang, sorted(by_name))
        assert classes[name]["base_classes"] == bases
    animal = classes["Animal"]
    assert "animal" in animal["docstring"].lower() and "*" not in animal["docstring"]
    caller, callee = EXPECTED[lang]["call"]
    assert caller in by_name and callee in by_name
    assert callee.split(".")[-1] in by_name[caller]["calls"]
    assert by_name[caller]["docstring"].lower().startswith(("build", "formatgreeting build"))


@pytest.mark.parametrize("lang", sorted(EXPECTED))
def test_call_and_inheritance_edges(lang):
    chunks, graph = graph_of(lang)
    names = [display_name(c) for c in chunks]
    calls = {(names[i], names[j]) for i, edges in enumerate(graph.callees) for j, _ in edges}
    bases = {(names[i], names[j]) for i, js in enumerate(graph.bases) for j in js}
    assert EXPECTED[lang]["call"] in calls
    if EXPECTED[lang]["method_calls"]:
        assert EXPECTED[lang]["method_calls"] in calls
    expected_bases = {(cls, base) for cls, bs in EXPECTED[lang]["classes"].items() for base in bs}
    assert bases == expected_bases
    # every edge in another language is a name match; methods know their class
    assert {how for edges in graph.callees for _, how in edges} == {"name_match"}
    assert all(graph.parent[i] is not None for i, c in enumerate(chunks)
               if c["type"] == "method" and c["parent"] in {k["name"] for k in chunks})
    # a method calling the one it overrides doesn't link back to its own class
    assert not any(a.split(".")[0] == b for a, b in calls)


def test_methods_defined_away_from_their_type_are_attached():
    go = {display_name(c): c for c in chunks_of("go")}
    assert go["Dog"]["methods"] == ["Speak"] and go["Dog.Speak"]["parent"] == "Dog"   # receiver
    rust = {display_name(c): c for c in chunks_of("rust")}
    assert rust["Dog"]["methods"] == ["speak", "repeats"] and rust["Dog.speak"]["parent"] == "Dog"   # impl
    cpp = {display_name(c): c for c in chunks_of("cpp")}
    assert cpp["Dog.speak"]["start_line"] > cpp["Dog"]["end_line"]                    # Dog::speak
    js = {display_name(c): c for c in chunks_of("javascript")}
    assert js["shout"]["content"].startswith("export const shout = ")                # arrow function


def test_class_outlines_elide_method_bodies():
    dog = next(c for c in chunks_of("java") if c["name"] == "Dog" and c["type"] == "class")
    assert "{ ... }" in dog["content"] and "mail carrier" not in dog["content"]
    whole = next(c for c in chunks_of("java", get_config("v9", chunk_methods=False))
                 if c["name"] == "Dog")
    assert "mail carrier" in whole["content"]


def mixed_repo(tmp_path):
    """Python, JavaScript and Go side by side, sharing the names Animal, speak, shout ..."""
    for lang, name in (("javascript", "zoo.js"), ("go", "zoo.go")):
        shutil.copy(os.path.join(FIXTURES, lang, name), tmp_path)
    (tmp_path / "zoo.py").write_text(
        "class Animal:\n    \"\"\"A Python animal that shares names with the others.\"\"\"\n\n"
        "    def speak(self):\n        return shout(formatGreeting(self.name, 'makes a sound in python'))\n\n\n"
        "def shout(text):\n    \"\"\"Upper-case a line of text, the Python way, with emphasis.\"\"\"\n"
        "    return text.upper() + '!'\n\n\n"
        "def formatGreeting(name, sound):\n    \"\"\"Build a greeting from a name and a sound, then shout it.\"\"\"\n"
        "    return shout(f'{name} {sound}')\n")
    return str(tmp_path)


def test_no_edge_ever_crosses_languages(tmp_path):
    root = mixed_repo(tmp_path)
    chunks = load_chunks(root, V9, verbose=False)
    langs = [language_of(c) for c in chunks]
    assert set(langs) == {"python", "javascript", "go"}
    graph = CodeGraph(chunks, root)
    edges = ([(i, j) for i, e in enumerate(graph.callees) for j, _ in e]
             + [(i, j) for i, b in enumerate(graph.bases) for j in b]
             + [(i, p) for i, p in enumerate(graph.parent) if p is not None])
    assert edges and all(langs[i] == langs[j] for i, j in edges)
    # the name-based graph (v1-v6 style) keeps languages apart too
    named = list(iter_call_edges(chunks))
    assert named and all(langs[i] == langs[j] for i, j in named)
    # Python edges are still resolved; the others are name matches
    hows = {(langs[i], how) for i, e in enumerate(graph.callees) for _, how in e}
    assert ("python", "exact") in hows and ("javascript", "name_match") in hows
    assert not any(lang == "python" and how == "name_match" for lang, how in hows)


def test_v0_to_v8_stay_python_only(tmp_path):
    root = mixed_repo(tmp_path)
    for version in ("v0", "v1", "v4", "v7", "v8"):
        cfg = get_config(version)
        assert cfg.languages == ()
    v8 = load_chunks(root, get_config("v8"), verbose=False)
    assert {c["file"].rsplit(".", 1)[1] for c in v8} == {"py"} and all("language" not in c for c in v8)
    assert get_config("v8").source_extensions() == (".py",)
    # v9 is v8 plus languages, nothing else
    assert {k: v for k, v in vars(get_config("v9")).items() if k not in ("name", "description", "languages")} \
        == {k: v for k, v in vars(get_config("v8")).items() if k not in ("name", "description", "languages")}
    # indexes saved before v9 have no `languages`: they were Python-only
    old = {k: v for k, v in vars(get_config("v8")).items() if k != "languages"}
    assert RAGConfig.from_dict(old).languages == ()
    assert not RAGConfig.from_dict(old).index_mismatch(get_config("v8"))


def test_context_fences_use_the_chunk_language(tmp_path):
    root = mixed_repo(tmp_path)
    cfg = get_config("v9", prompt_style="cited")
    fences = {language_of(c): format_chunk(c["content"], c, 0, "q", cfg, 1).split("\n")
              for c in load_chunks(root, V9, verbose=False)}
    assert "```go" in fences["go"] and "```javascript" in fences["javascript"]
    assert "```python" in fences["python"]


def test_agent_sources_graph_and_saved_index(tmp_path):
    from coderag.visualize import graph_data

    (tmp_path / "repo").mkdir()
    root = mixed_repo(tmp_path / "repo")
    agent = RAGCodebaseAgent(root, V9, index=FakeIndex(), load_llm=False, verbose=False)
    first = next(agent.stream("shout", llm=None))
    assert {s["language"] for s in first["sources"]} <= {"python", "javascript", "go"}
    assert all(s["confidence"] in ("resolved", "name_match") for s in first["sources"] if s["hop"])
    data = graph_data(agent)
    langs = {n["id"]: n["language"] for n in data["nodes"]}
    for link in data["links"]:
        assert langs[link["source"]] == langs[link["target"]]
        if link["type"] == "calls":
            assert link["confidence"] == ("resolved" if langs[link["source"]] == "python" else "name_match")
    # the saved index keeps languages, edges and its v9 config
    agent.index.save = lambda directory: None
    from coderag import persist
    persist.save_agent(agent, str(tmp_path / "index"))
    meta = persist.read_meta(str(tmp_path / "index"))
    assert RAGConfig.from_dict(meta["config"]).languages == registered()
    saved = persist._read_json(str(tmp_path / "index"), persist.CHUNKS_FILE)
    assert [c.get("language") for c in saved] == [c.get("language") for c in agent.documents]


def test_unknown_or_disabled_languages_are_not_indexed(tmp_path):
    root = mixed_repo(tmp_path)
    only_go = load_chunks(root, get_config("v9", languages=("go",)), verbose=False)
    assert {language_of(c) for c in only_go} == {"python", "go"}


# ── web: indexing a repository that isn't (only) Python ─────────────────────
def test_web_indexes_other_languages_with_v9(tmp_path):
    pytest.importorskip("fastapi")
    from coderag.gitfetch import _source_patterns
    from coderag.web.jobs import check_limits, version_for
    from test_indexing import FakeGitHub, make_app, wait

    assert {"*.py", "*.go", "*.rs", "*.ts", "*.java", "*.kt", "*.cpp", "*.c", "*.js"} <= set(_source_patterns())

    class PolyglotGitHub(FakeGitHub):
        def fetch(self, url, sha, dest, timeout=120, python_only=False, token=None):
            assert python_only
            self.fetched.append(url)
            os.makedirs(dest)
            for lang in ("go", "rust"):
                for name in os.listdir(os.path.join(FIXTURES, lang)):
                    shutil.copy(os.path.join(FIXTURES, lang, name), dest)
            shutil.copy(os.path.join(SAMPLE_REPO, "test_inheritance.py"), os.path.join(dest, "zoo_py.py"))
            return dest

    found = check_limits(FIXTURES, 1000, 25)
    assert found["languages"]["go"] == 1 and found["files"] == sum(found["languages"].values())
    assert version_for({"python": 3}) == "v8" and version_for({"python": 3, "go": 1}) == "v9"

    client, _, _ = make_app(tmp_path, github=PolyglotGitHub())
    job = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])
    assert job["status"] == "ready", job
    info = client.get(f"/api/repos/{job['repo_id']}").json()
    assert info["versions"] == ["v9"]
    assert info["languages"] == {"Go": 1, "Python": 1, "Rust": 1}
    graph = client.get(f"/api/repos/{job['repo_id']}/graph").json()
    assert {n["language"] for n in graph["nodes"]} == {"go", "rust", "python"}
    assert any(l.get("confidence") == "name_match" for l in graph["links"])
    sources = client.post(f"/api/repos/{job['repo_id']}/retrieve", json={"question": "shout"}).json()["sources"]
    assert all("language" in s for s in sources)


def test_limits_cover_every_source_file(tmp_path):
    from coderag.web.jobs import IndexFailure, check_limits

    for lang in ("go", "c"):
        shutil.copy(os.path.join(FIXTURES, lang, os.listdir(os.path.join(FIXTURES, lang))[0]), tmp_path)
    with pytest.raises(IndexFailure, match="Too many source files"):
        check_limits(str(tmp_path), 1, 25)
