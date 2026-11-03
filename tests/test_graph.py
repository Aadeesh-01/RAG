from coderag.chunking import load_chunks
from coderag.config import get_config
from coderag.graph import build_graph, build_name_index, build_reverse_index, expand_bfs


def structural_chunks(repo, version="v5", **overrides):
    chunks = load_chunks(repo, get_config(version, **overrides), verbose=False)
    build_reverse_index(chunks, verbose=False)
    return chunks


def by_name(chunks):
    return {c["name"]: c for c in chunks}


def test_reverse_index(sample_repo):
    c = by_name(structural_chunks(sample_repo))
    assert c["Bat"]["called_by"] == ["Zoo", "create_bat"]   # Zoo via animal.speak()
    assert "Zoo" in c["Animal"]["called_by"]            # via animal.info()
    # BaseProcessor.process -> self.transform(), which DataProcessor overrides
    assert c["DataProcessor"]["called_by"] == ["BaseProcessor", "run_pipeline"]


def test_zoo_graph_matches_original(zoo_repo):
    """The edges the original chunk_graph.html showed for test_inheritance.py."""
    chunks = structural_chunks(zoo_repo, "v4", min_chunk_chars=10)
    nodes, edges = build_graph(chunks)
    name = {n["id"]: n["name"] for n in nodes}
    got = sorted((name[e["source"]], e["kind"], name[e["target"]]) for e in edges)
    assert got == sorted([
        ("Mammal", "inherits", "Animal"), ("Bird", "inherits", "Animal"),
        ("Bat", "calls", "Bird"), ("Bat", "calls", "Mammal"),
        ("Bat", "inherits", "Mammal"), ("Bat", "inherits", "Bird"),
        ("Zoo", "calls", "Animal"), ("Zoo", "calls", "Mammal"),
        ("Zoo", "calls", "Bird"), ("Zoo", "calls", "Bat"),
        ("create_bat", "calls", "Bat"),
        ("run_zoo", "calls", "Zoo"), ("run_zoo", "calls", "create_bat"),
    ])


def test_bfs_hops(zoo_repo):
    chunks = structural_chunks(zoo_repo)
    idx = {c["name"]: i for i, c in enumerate(chunks)}
    name_index = build_name_index(chunks)

    reached = expand_bfs(chunks, name_index, {idx["run_zoo"]}, max_hops=3, max_expansion=10)
    hops = {chunks[i]["name"]: h for i, h in reached}
    assert hops == {"Zoo": 1, "create_bat": 1, "Bat": 2, "Mammal": 3, "Bird": 3}

    one_hop = expand_bfs(chunks, name_index, {idx["run_zoo"]}, max_hops=1, max_expansion=10)
    assert {chunks[i]["name"] for i, _ in one_hop} == {"Zoo", "create_bat"}

    capped = expand_bfs(chunks, name_index, {idx["run_zoo"]}, max_hops=3, max_expansion=3)
    assert len(capped) == 3

    assert expand_bfs(chunks, name_index, {idx["run_zoo"]}, max_hops=0) == []


def test_bfs_include_callers(zoo_repo):
    chunks = structural_chunks(zoo_repo)
    idx = {c["name"]: i for i, c in enumerate(chunks)}
    name_index = build_name_index(chunks)

    forward = expand_bfs(chunks, name_index, {idx["create_bat"]}, max_hops=1)
    both = expand_bfs(chunks, name_index, {idx["create_bat"]}, max_hops=1, include_callers=True)
    assert {chunks[i]["name"] for i, _ in forward} == {"Bat"}
    assert {chunks[i]["name"] for i, _ in both} == {"Bat", "run_zoo"}
