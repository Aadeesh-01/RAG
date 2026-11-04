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


HUB_FILES = {
    # two scripts, each with its own main() called from its own __main__ block
    "plan_a.py": """
        def main():
            path = []
            path.append(1)
            return path.copy()

        if __name__ == "__main__":
            main()
    """,
    "plan_b.py": """
        from states import States

        def main():
            s = States()
            s.append(2)
            States.append(s, 3)
            return s

        if __name__ == "__main__":
            main()
    """,
    # the repo's only class with append/copy methods
    "states.py": """
        class States:
            def append(self, x):
                self.items.append(x)

            def copy(self):
                return States()

            def merge(self, other):
                self.append(other)
    """,
}


@pytest.fixture
def hub_graph(tmp_path):
    root = tmp_path / "robots"
    root.mkdir()
    for name, body in HUB_FILES.items():
        (root / name).write_text(textwrap.dedent(body))
    config = get_config("v7", min_chunk_chars=0, min_segment_chars=0)
    return CodeGraph(load_chunks(str(root), config, verbose=False), str(root))


def test_each_script_main_is_its_own_node(hub_graph):
    mains = [c["qname"] for c in hub_graph.chunks if c["name"] == "main"]
    assert sorted(mains) == ["plan_a.py::main", "plan_b.py::main"]
    callers_of = {c["qname"]: {hub_graph.chunks[i]["qname"] for i, _ in hub_graph.callers[j]}
                  for j, c in enumerate(hub_graph.chunks) if c["name"] == "main"}
    # each main is reached only from its own file, never across scripts
    for qname, callers in callers_of.items():
        assert all(c.split("::")[0] == qname.split("::")[0] for c in callers), (qname, callers)


def test_builtin_method_names_on_unknown_objects_get_no_edge(hub_graph):
    # path.append / path.copy are list methods, not the repo's one States class
    assert edges(hub_graph, "plan_a.py::main") == {}


def test_builtin_method_names_still_resolve_when_the_type_is_known(hub_graph):
    # States.append(...) names the class; self.append() is the enclosing class
    assert edges(hub_graph, "plan_b.py::main") == {
        "states.py::States": IMPORT, "states.py::States.append": EXACT}
    assert edges(hub_graph, "states.py::States.merge") == {"states.py::States.append": EXACT}


def test_old_saved_graph_is_migrated_on_load(hub_graph):
    saved = hub_graph.to_dict()
    by_q = {c["qname"]: i for i, c in enumerate(hub_graph.chunks)}
    a_main, append = by_q["plan_a.py::main"], by_q["states.py::States.append"]
    # what resolver 1 saved: plan_a.main guessed into States.append
    saved["resolver"] = 1
    saved["callees"][a_main] = [[append, UNIQUE]]
    saved["callers"][append] = saved["callers"][append] + [[a_main, UNIQUE]]
    loaded = CodeGraph.from_dict(hub_graph.chunks, saved)
    assert loaded.migrated
    assert loaded.callees[a_main] == []
    assert a_main not in [i for i, _ in loaded.callers[append]]
    # exact edges are untouched
    assert loaded.callees[by_q["plan_b.py::main"]] == hub_graph.callees[by_q["plan_b.py::main"]]
    assert not CodeGraph.from_dict(hub_graph.chunks, hub_graph.to_dict()).migrated


def test_called_by_uses_display_names(graph):
    graph.apply_called_by()
    by_q = {c["qname"]: c for c in graph.chunks}
    assert by_q["core.py::helper"]["called_by"] == ["Impl.step", "use"]
