import pytest

from coderag.chunking import chunk_fixed, load_chunks, parse_python_file
from coderag.config import get_config


def names(chunks):
    return {c["name"] for c in chunks}


def test_fixed_chunks_overlap():
    chunks = chunk_fixed("abcdefghij", chunk_size=4, overlap=1)
    assert [c["content"] for c in chunks] == ["abcd", "defg", "ghij", "j"]


def test_fixed_rejects_overlap_not_smaller_than_size():
    with pytest.raises(ValueError):
        chunk_fixed("abc", chunk_size=4, overlap=4)


def test_ast_chunks_are_top_level_units(sample_repo):
    chunks = load_chunks(sample_repo, get_config("v1"), verbose=False)
    got = names(chunks)
    assert {"Animal", "Bat", "Zoo", "run_zoo", "DataProcessor", "SQLiteStore",
            "run_pipeline"} <= got
    # methods live inside their class chunk, not as separate chunks
    assert "speak" not in got and "add_animal" not in got
    # __init__.py has only a docstring -> too small, dropped
    assert "__init__.py" not in got


def test_v5_keeps_smaller_segments_than_v4(tmp_path):
    (tmp_path / "m.py").write_text(
        "def tiny(x):\n    return x + 1\n\n"
        "def bigger(values):\n    total = sum(values)\n    return total / max(len(values), 1) + 0\n"
    )
    v4 = parse_python_file(str(tmp_path / "m.py"), min_segment_chars=60)
    v5 = parse_python_file(str(tmp_path / "m.py"), min_segment_chars=10)
    assert names(v4) == {"bigger"}
    assert names(v5) == {"tiny", "bigger"}


def test_script_without_defs_becomes_file_chunk(tmp_path):
    (tmp_path / "script.py").write_text("import os\nprint(os.getcwd())\n" * 5)
    [chunk] = parse_python_file(str(tmp_path / "script.py"))
    assert chunk["type"] == "file"
    assert chunk["name"] == "script.py"
    assert chunk["calls"] == ["getcwd", "print"]


def test_metadata(sample_repo):
    chunks = {c["name"]: c for c in load_chunks(sample_repo, get_config("v4"), verbose=False)}
    bat = chunks["Bat"]
    assert bat["base_classes"] == ["Mammal", "Bird"]
    assert bat["methods"] == ["speak", "special"]
    assert bat["qname"] == "test_inheritance.py::Bat"
    assert chunks["DataProcessor"]["docstring"].startswith("Cleans people records")


def test_dedup_records_duplicate_file(tmp_path):
    body = "def shared(a, b):\n    # helper\n    result = a * b + a - b\n    return result\n"
    (tmp_path / "a.py").write_text(body)
    (tmp_path / "b.py").write_text(body.replace("# helper", "# other comment"))
    v1 = load_chunks(str(tmp_path), get_config("v1", min_chunk_chars=0), verbose=False)
    v4 = load_chunks(str(tmp_path), get_config("v4", min_chunk_chars=0), verbose=False)
    assert len(v1) == 2
    assert len(v4) == 1 and len(v4[0]["also_in"]) == 1


def test_skip_dirs_pruned(tmp_path):
    for d in ("venv", ".hidden", "src"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "mod.py").write_text(
            f"def in_{d.strip('.')}(x):\n    return [x for _ in range(10) if x is not None]\n"
        )
    chunks = load_chunks(str(tmp_path), get_config("v1", min_chunk_chars=0), verbose=False)
    assert names(chunks) == {"in_src"}


def test_missing_repo_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_chunks(str(tmp_path / "nope"), get_config("v1"), verbose=False)
