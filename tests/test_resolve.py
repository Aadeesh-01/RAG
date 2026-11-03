import textwrap

import pytest

from coderag.chunking import load_chunks
from coderag.config import get_config
from coderag.resolve import AMBIGUOUS, EXACT, IMPORT, UNIQUE, CodeGraph

FILES = {
    "__init__.py": "from .core import Base, helper\n",
    "core.py": """
        import json

        class Base:
            def run(self):
                return self.step()

            def step(self):
                return 1

            def load(self, f):
                return json.load(f)

        def helper(x):
            return x
    """,
    "impl.py": """
        from . import core
        from .core import Base

        class Impl(Base):
            def step(self):
                return super().step() + core.helper(2)

        def make():
            return Impl()

        def go(obj):
            return obj.load(1)
    """,
    "user.py": """
        from pkg import helper

        def use():
            return helper(1)

        def dump(data):
            return data.load(2) + data.run()
    """,
    "other.py": """
        class Other:
            def run(self):
                return 2
    """,
}


@pytest.fixture
def graph(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    for name, body in FILES.items():
        (root / name).write_text(textwrap.dedent(body))
    config = get_config("v7", min_chunk_chars=0, min_segment_chars=0)
    chunks = load_chunks(str(root), config, verbose=False)
    return CodeGraph(chunks, str(root))


def edges(graph, qname):
    by_q = {c["qname"]: i for i, c in enumerate(graph.chunks)}
    return {graph.chunks[j]["qname"]: how for j, how in graph.callees[by_q[qname]]}


def test_external_module_calls_get_no_edge(graph):
    # json.load must not link to Base.load (the bug in name-based resolution)
    assert edges(graph, "core.py::Base.load") == {}


def test_self_call_includes_subclass_override(graph):
    assert edges(graph, "core.py::Base.run") == {
        "core.py::Base.step": EXACT, "impl.py::Impl.step": EXACT}


def test_super_and_module_attribute(graph):
    assert edges(graph, "impl.py::Impl.step") == {
        "core.py::Base.step": EXACT, "core.py::helper": IMPORT}


def test_same_file_class_instantiation(graph):
    assert edges(graph, "impl.py::make") == {"impl.py::Impl": EXACT}


def test_unknown_object_unique_method(graph):
    # only Base defines load -> unique; run is defined by Base and Other -> dropped
    assert edges(graph, "impl.py::go") == {"core.py::Base.load": UNIQUE}
    assert edges(graph, "user.py::dump") == {"core.py::Base.load": UNIQUE}


def test_reexport_through_package_init(graph):
    assert edges(graph, "user.py::use") == {"core.py::helper": IMPORT}


def test_inheritance_resolved_through_import(graph):
    by_q = {c["qname"]: i for i, c in enumerate(graph.chunks)}
    assert graph.bases[by_q["impl.py::Impl"]] == [by_q["core.py::Base"]]
    assert graph.parent[by_q["impl.py::Impl.step"]] == by_q["impl.py::Impl"]


def test_ambiguous_calls_allowed_when_configured(tmp_path, graph):
    lenient = CodeGraph(graph.chunks, str(tmp_path / "pkg"), max_ambiguous=2)
    assert edges(lenient, "user.py::dump") == {
        "core.py::Base.load": UNIQUE,
        "core.py::Base.run": AMBIGUOUS, "other.py::Other.run": AMBIGUOUS}


def test_called_by_uses_display_names(graph):
    graph.apply_called_by()
    by_q = {c["qname"]: c for c in graph.chunks}
    assert by_q["core.py::helper"]["called_by"] == ["Impl.step", "use"]
