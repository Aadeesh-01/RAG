import pytest

from coderag import RAGCodebaseAgent, get_config
from coderag.cli import main
from conftest import FakeIndex, FakeLLM


def make_agent(repo, version="v5", ranking=None, **overrides):
    return RAGCodebaseAgent(repo, get_config(version, **overrides), index=FakeIndex(ranking),
                            llm=FakeLLM(), verbose=False)


def test_v5_retrieve_labels_hops(zoo_repo):
    agent = make_agent(zoo_repo)
    run_zoo = agent.chunks.index(next(c for c in agent.chunks if c.startswith("def run_zoo")))
    agent.index.ranking = [run_zoo]

    retrieved = agent.retrieve("how does the zoo run")
    assert [(m["name"], hop) for _, m, hop in retrieved][:3] == [
        ("run_zoo", 0), ("Zoo", 1), ("create_bat", 1)]
    assert "[retrieval: hop-2]" in agent.build_context("how does the zoo run")


def test_query_overrides_apply_per_call(zoo_repo):
    agent = make_agent(zoo_repo)
    assert agent.query("zoo", max_hops=0) == "answer to zoo"
    context, _ = agent.llm.calls[-1]
    assert "hop-" not in context
    assert agent.config.max_hops == 3          # unchanged for later calls


def test_v1_has_no_filter_or_expansion(sample_repo):
    agent = make_agent(sample_repo, "v1")
    retrieved = agent.retrieve("xyz")
    assert len(retrieved) == 4
    assert all(hop == 0 for *_, hop in retrieved)


def test_v0_baseline_pipeline(sample_repo):
    agent = make_agent(sample_repo, "v0")
    assert all(m["type"] == "text" for m in agent.metadata)
    assert len(agent.retrieve("anything")) == 5


def test_retrieve_only_agent_cannot_answer(zoo_repo):
    agent = RAGCodebaseAgent(zoo_repo, get_config("v5"), index=FakeIndex(),
                             load_llm=False, verbose=False)
    with pytest.raises(RuntimeError):
        agent.query("zoo")


def test_unknown_version():
    with pytest.raises(ValueError):
        get_config("v9")


def test_cli_versions_and_chunks(capsys, sample_repo):
    assert main(["versions"]) == 0
    assert "* v8" in capsys.readouterr().out
    assert main(["chunks", "--repo", sample_repo, "-q"]) == 0
    assert "DataProcessor" in capsys.readouterr().out


def test_cli_graph(tmp_path, zoo_repo):
    out = tmp_path / "g.html"
    assert main(["graph", "--repo", zoo_repo, "-o", str(out), "-q"]) == 0
    html = out.read_text(encoding="utf-8")
    assert '"name": "Bat"' in html and "d3@7.9.0" in html


def test_cli_bad_repo(tmp_path):
    assert main(["chunks", "--repo", str(tmp_path / "missing")]) == 1


def test_graph_sources_name_the_path_that_reached_them(zoo_repo):
    agent = make_agent(zoo_repo, "v8")
    run_zoo = agent.chunks.index(next(c for c in agent.chunks if c.startswith("def run_zoo")))
    agent.index.ranking = [run_zoo]
    sources = next(agent.stream("how does the zoo run", llm=None))["sources"]
    by_name = {s["name"]: s for s in sources}
    assert by_name["run_zoo"]["hop"] == 0 and by_name["run_zoo"]["via"] is None
    hop1 = [s for s in sources if s["hop"] == 1]
    assert hop1 and all(s["via"] == ["run_zoo"] for s in hop1)
    for s in (s for s in sources if s["hop"] >= 2):
        assert len(s["via"]) == s["hop"] and s["via"][0] == "run_zoo"
    # the same path as graph node ids, starting at the vector hit
    for s in (s for s in sources if s["hop"]):
        assert s["via_ids"][0] == run_zoo and len(s["via_ids"]) == len(s["via"])
        assert [agent.documents[i]["name"] for i in s["via_ids"]][0] == "run_zoo"
