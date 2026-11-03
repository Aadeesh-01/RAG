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


def test_line_numbers_include_decorators(tmp_path):
    (tmp_path / "m.py").write_text(
        "import functools\n"
        "\n"
        "@functools.lru_cache(maxsize=None)\n"
        "@staticmethod\n"
        "def cached(value):\n"
        "    return [value for _ in range(100) if value is not None]\n"
    )
    [chunk] = parse_python_file(str(tmp_path / "m.py"))
    assert (chunk["start_line"], chunk["end_line"]) == (3, 6)
    assert chunk["content"].startswith("@functools.lru_cache(maxsize=None)\n@staticmethod\ndef")
    assert chunk["decorators"] == ["lru_cache", "staticmethod"]


def test_sample_line_numbers(sample_repo):
    chunks = {c["name"]: c for c in load_chunks(sample_repo, get_config("v5"), verbose=False)}
    assert (chunks["Animal"]["start_line"], chunks["Animal"]["end_line"]) == (1, 9)
    assert (chunks["run_zoo"]["start_line"], chunks["run_zoo"]["end_line"]) == (54, 57)


def test_fixed_chunk_spans_map_back_to_files(tmp_path):
    (tmp_path / "a.txt").write_text("line1\nline2\nline3\n")
    (tmp_path / "b.txt").write_text("x\n" * 50)
    chunks = load_chunks(str(tmp_path), get_config("v0", extensions=(".txt",),
                                                   chunk_size=60, chunk_overlap=0),
                         verbose=False)
    a = str(tmp_path / "a.txt")
    b = str(tmp_path / "b.txt")
    spans = [s for c in chunks for s in c["spans"]]
    assert (a, 1, 3) in spans
    assert all(path in (a, b) for path, *_ in spans)
    b_lines = sorted({line for path, lo, hi in spans if path == b for line in range(lo, hi + 1)})
    assert b_lines == list(range(1, 51))


CLASS_SOURCE = '''\
class Store(Base):
    """Keeps things."""

    limit = make_limit(10)

    def __init__(self, path):
        self.path = path
        self.items = load(path)

    @property
    def size(self): return len(self.items)

    def save(
        self, item,
    ):
        """Persist one item."""
        # validate first
        self.items.append(validate(item))
        write(self.path, self.items)
'''


def test_chunk_methods_outline_and_methods(tmp_path):
    (tmp_path / "store.py").write_text(CLASS_SOURCE)
    chunks = parse_python_file(str(tmp_path / "store.py"), str(tmp_path),
                               min_segment_chars=10, chunk_methods=True)
    by_name = {c["name"]: c for c in chunks}
    outline = by_name["Store"]

    assert outline["type"] == "class"
    assert outline["content"] == (
        'class Store(Base):\n'
        '    """Keeps things."""\n'
        '\n'
        '    limit = make_limit(10)\n'
        '\n'
        '    def __init__(self, path):\n'
        '        ...\n'
        '\n'
        '    @property\n'
        '    def size(self): return len(self.items)\n'
        '\n'
        '    def save(\n'
        '        self, item,\n'
        '    ):\n'
        '        ...'
    )
    assert outline["calls"] == ["make_limit"]          # only calls outside methods
    assert outline["methods"] == ["__init__", "size", "save"]
    assert (outline["start_line"], outline["end_line"]) == (1, 19)

    save = by_name["save"]
    assert save["type"] == "method"
    assert save["parent"] == "Store"
    assert save["qname"] == "store.py::Store.save"
    assert save["content"].startswith("def save(\n    self, item,\n):\n")   # dedented
    assert save["calls"] == ["append", "validate", "write"]
    assert save["docstring"] == "Persist one item."
    assert (save["start_line"], save["end_line"]) == (13, 19)

    assert by_name["size"]["content"].startswith("@property\ndef size")


def test_chunk_methods_off_keeps_whole_class(tmp_path):
    (tmp_path / "store.py").write_text(CLASS_SOURCE)
    [chunk] = parse_python_file(str(tmp_path / "store.py"), min_segment_chars=10)
    assert chunk["content"] == CLASS_SOURCE.rstrip("\n")


def test_missing_repo_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_chunks(str(tmp_path / "nope"), get_config("v1"), verbose=False)
