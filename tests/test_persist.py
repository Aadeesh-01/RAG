import json
import os
import shutil
import zlib

import numpy as np
import pytest

from coderag import RAGCodebaseAgent, get_config
from coderag.persist import check_index

faiss = pytest.importorskip("faiss")
from coderag.index import VectorIndex  # noqa: E402

QUESTIONS = ["Which functions call normalize_name?", "How are records stored?",
             "What does a bat do?", "How is the moving average computed?"]


class HashEncoder:
    """Deterministic bag-of-words embedding: no model download, real FAISS search."""

    def __init__(self):
        self.encoded = 0

    def encode(self, texts, show_progress_bar=False):
        self.encoded += len(texts)
        out = np.zeros((len(texts), 64), dtype="float32")
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, zlib.crc32(word.encode()) % 64] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


def build(repo, version="v7", encoder=None, **overrides):
    config = get_config(version, **overrides)
    index = VectorIndex(config.embed_model, model=encoder or HashEncoder())
    return RAGCodebaseAgent(repo, config, index=index, load_llm=False, verbose=False)


def load(directory, config=None, encoder=None):
    return RAGCodebaseAgent.load(directory, config, load_llm=False, verbose=False,
                                 embedder=encoder or HashEncoder())


@pytest.fixture
def repo_copy(tmp_path, sample_repo):
    dest = tmp_path / "repo"
    shutil.copytree(sample_repo, dest)
    return str(dest)


@pytest.mark.parametrize("version", ["v0", "v5", "v7"])
def test_roundtrip_gives_identical_retrieval(tmp_path, repo_copy, version):
    built = build(repo_copy, version)
    built.save(tmp_path / "idx")
    loaded = load(tmp_path / "idx")

    assert loaded.config == built.config
    assert loaded.chunks == built.chunks
    for q in QUESTIONS:
        assert loaded.build_context(q) == built.build_context(q)


def test_saved_graph_matches_built_graph(tmp_path, repo_copy):
    built = build(repo_copy)
    built.save(tmp_path / "idx")
    loaded = load(tmp_path / "idx")
    n = len(built.chunks)
    for i in range(n):
        assert loaded.code_graph.neighbors(i, True) == built.code_graph.neighbors(i, True)
    assert [c["called_by"] for c in loaded.documents] == [c["called_by"] for c in built.documents]


def test_load_works_without_the_repo(tmp_path, repo_copy):
    build(repo_copy).save(tmp_path / "idx")
    shutil.rmtree(repo_copy)
    agent = load(tmp_path / "idx")
    assert "DataProcessor" in agent.build_context("How are records cleaned?")


def test_query_settings_can_change_but_index_settings_cannot(tmp_path, repo_copy):
    build(repo_copy).save(tmp_path / "idx")
    assert load(tmp_path / "idx", get_config("v7", top_k=8, max_hops=0)).config.top_k == 8
    with pytest.raises(ValueError, match="call_resolution"):
        load(tmp_path / "idx", get_config("v6"))


def test_check_index_detects_changes(tmp_path, repo_copy):
    directory = str(tmp_path / "idx")
    config = get_config("v7")
    assert check_index(directory, config, repo_copy) == (False, "no saved index")

    build(repo_copy).save(directory)
    assert check_index(directory, config, repo_copy) == (True, "up to date")
    assert check_index(directory, get_config("v5"), repo_copy)[0] is False

    with open(os.path.join(repo_copy, "pipeline", "storage.py"), "a") as f:
        f.write("\n# changed\n")
    assert check_index(directory, config, repo_copy) == (False, "source files changed")


def test_interrupted_save_is_not_loadable(tmp_path, repo_copy):
    directory = tmp_path / "idx"
    build(repo_copy).save(directory)
    os.remove(directory / "meta.json")
    with pytest.raises(FileNotFoundError):
        load(directory)


def test_load_or_build_reuses_then_rebuilds(tmp_path, repo_copy, monkeypatch):
    encoder = HashEncoder()
    monkeypatch.setattr("coderag.index._load_model", lambda name: encoder)
    directory = str(tmp_path / "idx")

    def run():
        return RAGCodebaseAgent.load_or_build(repo_copy, directory, get_config("v7"),
                                              load_llm=False, verbose=False)

    run()
    built_once = encoder.encoded
    assert built_once > 0
    run()
    assert encoder.encoded == built_once        # loaded: nothing re-embedded

    with open(os.path.join(repo_copy, "test_inheritance.py"), "a") as f:
        f.write("\n\ndef extra_function_for_test():\n"
                "    return 'a new chunk of code, long enough to pass the size filter'\n")
    agent = run()
    assert encoder.encoded > built_once         # stale: rebuilt
    assert "extra_function_for_test" in {c["name"] for c in agent.documents}
    meta = json.load(open(os.path.join(directory, "meta.json")))
    assert meta["n_chunks"] == len(agent.documents)
