"""Shared by the detectors: the source files behind the chunks, and literal-only argument parsing."""

import os
import re
from collections import defaultdict

from ..languages import language_of

_COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*)")


class SourceFiles:
    """The files the chunks came from: their text, and which chunk covers a line."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.by_file = defaultdict(list)          # absolute path -> [chunk index]
        for i, c in enumerate(chunks):
            if c.get("file") and "qname" in c:
                self.by_file[c["file"]].append(i)
        self._text = {}

    def files(self, *languages):
        """Paths of the files in the given languages."""
        return [p for p, idx in self.by_file.items() if language_of(self.chunks[idx[0]]) in languages]

    def text(self, path):
        if path not in self._text:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    self._text[path] = f.read()
            except OSError:
                self._text[path] = ""
        return self._text[path]

    def rel(self, path):
        """Repo-relative path, as the chunks' qnames have it."""
        idx = self.by_file.get(path)
        return self.chunks[idx[0]]["qname"].split("::")[0] if idx else os.path.basename(path)

    def where(self, path, line):
        return f"{self.rel(path)}:{line}"

    def chunk_at(self, path, line):
        """The smallest chunk of `path` whose lines include `line`, or None."""
        best = None
        for i in self.by_file.get(path, ()):
            c = self.chunks[i]
            if c["start_line"] <= line <= c["end_line"]:
                span = c["end_line"] - c["start_line"]
                if best is None or span < best[0]:
                    best = (span, i)
        return best[1] if best else None

    def named(self, path, name, language):
        """A function chunk called `name`: in `path` first, then anywhere in `language`."""
        local = [i for i in self.by_file.get(path, ()) if self.chunks[i]["name"] == name]
        if local:
            return local[0]
        found = [i for i, c in enumerate(self.chunks)
                 if c["name"] == name and c["type"] in ("function", "method") and language_of(c) == language]
        return found[0] if len(found) == 1 else None


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def is_comment(text, offset):
    """True when the line holding `offset` is a comment line."""
    start = text.rfind("\n", 0, offset) + 1
    return bool(_COMMENT_LINE.match(text[start:offset + 1]))


# =============================================================================
# ARGUMENTS
# =============================================================================
_CLOSE = {"(": ")", "[": "]", "{": "}"}


def call_args(text, open_paren):
    """
    Top-level argument texts of the call whose "(" is at `open_paren`, e.g.
    'fetch("/x", {method: "POST"})' -> ['"/x"', '{method: "POST"}']. Quotes and
    nested brackets are respected; None if the call isn't closed.
    """
    args, depth, start, i, n = [], [], open_paren + 1, open_paren + 1, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            i = _skip_string(text, i)
            continue
        if ch in "([{":
            depth.append(_CLOSE[ch])
        elif depth and ch == depth[-1]:
            depth.pop()
        elif not depth and ch == ",":
            args.append(text[start:i].strip())
            start = i + 1
        elif not depth and ch == ")":
            last = text[start:i].strip()
            if last or args:
                args.append(last)
            return args, i
        i += 1
    return None, n


def _skip_string(text, i):
    quote = text[i]
    if text.startswith(quote * 3, i) and quote != "`":
        end = text.find(quote * 3, i + 3)
        return len(text) if end < 0 else end + 3
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote or (text[i] == "\n" and quote != "`"):
            return i + 1
        i += 1
    return i


_LITERAL = re.compile(r"""\s*(?P<prefix>[rRbBuUfF]{0,2})(?P<q>"|'|`)""")


def literal(expr):
    """
    The value of a string literal, or of literals joined with "+", else None.
    Anything dynamic is refused: variables, calls, JS `${}`, Python f"{}",
    Kotlin "$x", format strings with arguments ...
    """
    if not expr:
        return None
    out, pos = [], 0
    while True:
        m = _LITERAL.match(expr, pos)
        if not m:
            return None
        start = m.end("q") - 1
        end = _skip_string(expr, start)
        body = expr[start + 1:end - 1]
        prefix, quote = m.group("prefix").lower(), m.group("q")
        if quote == "`" and "${" in body:                      # JS template with values
            return None
        if "f" in prefix and re.search(r"(?<!\{)\{(?!\{)", body):  # Python f-string with values
            return None
        if quote == '"' and re.search(r"\$[{\w]", body) and not prefix:   # Kotlin "$x" / "${x}"
            return None
        out.append(body)
        rest = expr[end:].lstrip()
        if not rest:
            return "".join(out)
        if not rest.startswith("+"):
            return None
        pos = len(expr) - len(rest) + 1


def keyword_literal(args, *names):
    """A literal keyword argument (method="POST", value = "/x", path: "/x") among `args`."""
    for arg in args:
        m = re.match(r"\s*(\w+)\s*[=:]\s*(.+)$", arg, re.S)
        if m and m.group(1) in names:
            value = literal(m.group(2))
            if value is not None:
                return value
    return None


_FORMAT_CALL = re.compile(r"""^\s*(?P<lit>[rRuU]?(?P<q>"|')(?:\\.|(?!(?P=q)).)*(?P=q))\s*\.format\s*\(.*\)\s*$""", re.S)


def url_template(expr):
    """
    (url, interpolated) for a URL argument: a literal (or literals joined with
    "+") is (value, False); a single JS template literal `/api/x/${id}`, a
    Python f-string f"/items/{pk}" or a literal "/items/{}".format(pk) gives
    the text with each interpolation as {} and True. None for anything else:
    a variable, a call, `base + path`, concatenation with a value.
    """
    value = literal(expr)
    if value is not None:
        return value, False
    text = (expr or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == "`" and _skip_string(text, 0) == len(text):
        body = text[1:-1]
        if "${" in body and "`" not in body:
            return re.sub(r"\$\{[^}]*\}", "{}", body), True
        return None
    f_string = re.match(r"""^(?:[rR]?[fF]|[fF][rR])(?P<q>"|')(?P<body>.*)(?P=q)$""", text, re.S)
    if f_string and _skip_string(text, len(text) - len(text.lstrip("rRfF"))) == len(text):
        body = f_string.group("body").replace("{{", "\0").replace("}}", "\1")
        return re.sub(r"\{[^{}]*\}", "{}", body).replace("\0", "{").replace("\1", "}"), True
    fmt = _FORMAT_CALL.match(text)
    if fmt:
        base = literal(fmt.group("lit"))
        if base is not None:
            return re.sub(r"\{[^{}]*\}", "{}", base), True
    return None
