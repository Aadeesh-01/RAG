"""@overload stubs, assert-only calls, and the retrieval debug trace."""

import textwrap

from coderag import RAGCodebaseAgent, get_config
from coderag.chunking import load_chunks, parse_python_file
from coderag.resolve import CodeGraph
from conftest import FakeIndex

OVERLOADED = '''
import typing as t
from ._types import is_ready

@t.overload
def group(name: str) -> "Group": ...

@t.overload
def group(name: None = None) -> "Group": ...

def group(name=None):
    """The real implementation."""
    return make_group(name)

def make_group(name):
    return [name, name, name, name, name, name, name, name, name]

class Group:
    @t.overload
    def add(self, x: int) -> None: ...

    def add(self, x):
        assert is_ready(self)
        return group(x)
'''

TYPES = '''
def is_ready(obj):
    return obj is not None and bool(getattr(obj, "ready", True)) and True
'''


def write_pkg(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "core.py").write_text(textwrap.dedent(OVERLOADED))
    (pkg / "_types.py").write_text(textwrap.dedent(TYPES))
    return str(pkg)


def names(chunks):
    return sorted(f"{c['parent']}.{c['name']}" if c["parent"] else c["name"] for c in chunks)


def test_overload_stubs_dropped(tmp_path):
    path = write_pkg(tmp_path) + "/core.py"
    keep = parse_python_file(path, min_segment_chars=0, chunk_methods=True)
    drop = parse_python_file(path, min_segment_chars=0, chunk_methods=True, skip_overloads=True)
    assert names(keep).count("group") == 3 and names(keep).count("Group.add") == 2
    assert names(drop).count("group") == 1 and names(drop).count("Group.add") == 1
    [impl] = [c for c in drop if c["name"] == "group"]
    assert "real implementation" in impl["content"]


def test_one_edge_per_call_after_dropping_stubs(tmp_path):
    root = write_pkg(tmp_path)
    for skip, expected in ((False, 3), (True, 1)):
        chunks = load_chunks(root, get_config("v7", skip_overloads=skip, min_chunk_chars=0,
                                              min_segment_chars=0), verbose=False)
        g = CodeGraph(chunks, root)
        add = next(i for i, c in enumerate(chunks)
                   if c["name"] == "add" and "real" not in c["content"] and "assert" in c["content"])
        targets = [chunks[j]["name"] for j, _ in g.callees[add]]
        assert targets.count("group") == expected


def test_assert_only_calls_can_be_ignored(tmp_path):
    root = write_pkg(tmp_path)
    config = get_config("v8", min_chunk_chars=0, min_segment_chars=0)
    chunks = load_chunks(root, config, verbose=False)
    add = next(i for i, c in enumerate(chunks) if c["name"] == "add")
    assert chunks[add]["assert_call_sites"] == [["name", "", "is_ready"]]
    with_asserts = CodeGraph(chunks, root)
    without = CodeGraph(chunks, root, ignore_assert_calls=True)
    called = lambda g: sorted(chunks[j]["name"] for j, _ in g.callees[add])  # noqa: E731
    assert called(with_asserts) == ["group", "is_ready"]
    assert called(without) == ["group"]


def test_debug_trace_explains_every_source(zoo_repo):
    agent = RAGCodebaseAgent(zoo_repo, get_config("v8"), index=FakeIndex(),
                             load_llm=False, verbose=False)
    run_zoo = next(i for i, c in enumerate(agent.chunks) if c.startswith("def run_zoo"))
    agent.index.ranking = [run_zoo]
    sources = next(agent.stream("zoo", debug=True))["sources"]

    direct = sources[0]["debug"]
    assert direct["rank"] == 1 and direct["seed"] == "run_zoo" and "path" not in direct
    bat = next(s for s in sources if s["name"] == "Bat")
    path = [(e["from"], e["edge"], e["to"]) for e in bat["debug"]["path"]]
    assert path == [("run_zoo", "calls:exact", "create_bat"), ("create_bat", "calls:exact", "Bat")]
    assert all(s["debug"]["context_chars"] > 0 for s in sources)
    # no debug info unless asked for
    assert next(agent.stream("zoo"))["sources"][0]["debug"] is None
