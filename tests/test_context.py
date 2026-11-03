from coderag.config import get_config
from coderag.context import (build_rich_header, char_budget, clean_chunk, format_chunk,
                             keyword_filter)

META = {
    "type": "class", "name": "Zoo", "file": "/x/zoo.py",
    "base_classes": ["Base"], "methods": ["add"], "calls": ["append"],
    "called_by": ["run_zoo"], "decorators": [], "docstring": "Holds animals.", "also_in": [],
}


def test_keyword_filter_falls_back_when_nothing_matches():
    results = [("alpha code", {}, 0), ("beta code", {}, 0)]
    assert keyword_filter("beta", results) == [results[1]]
    assert keyword_filter("gamma", results) == results


def test_clean_chunk_keeps_signatures_returns_and_keywords():
    chunk = "def f(x):\n    y = x * 2\n    z = store(y)\n    return z"
    assert clean_chunk(chunk, "store") == "def f(x):\nz = store(y)\nreturn z"


def test_clean_chunk_falls_back_to_raw_lines():
    chunk = "x = 1\ny = 2"
    assert clean_chunk(chunk, "nothing") == chunk


def test_char_budget():
    flat = get_config("v4")
    decay = get_config("v5")
    assert [char_budget(flat, h) for h in range(3)] == [400, 400, 400]
    assert [char_budget(decay, h) for h in range(4)] == [400, 200, 100, 80]
    assert char_budget(get_config("v0"), 0) is None


def test_rich_header():
    header = build_rich_header(META, hop=2, hop_label=True)
    assert header.splitlines()[0] == "# class: Zoo  [file: zoo.py]  [retrieval: hop-2]"
    assert "# called_by: run_zoo" in header
    assert "# doc: Holds animals." in header
    assert "retrieval" not in build_rich_header(META, hop=0)


def test_header_styles():
    body = "class Zoo:\n    pass"
    assert format_chunk(body, META, 0, "q", get_config("v0")) == body
    assert format_chunk(body, META, 0, "q", get_config("v1")) == "# class: Zoo (zoo.py)\n" + body
    assert format_chunk(body, META, 0, "q", get_config("v3")).startswith("# class: Zoo\n")
