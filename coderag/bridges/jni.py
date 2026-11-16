"""
JNI: Kotlin `external fun` / Java `native` methods -> the C/C++ functions that
implement them, named Java_<package>_<Class>_<method>.

JNI escapes names: "_" -> "_1", ";" -> "_2", "[" -> "_3", other characters ->
"_0xxxx" (hex), "." and "/" -> "_", and an overloaded method gets "__<signature>".
"exact": package, class and method all agree. "pattern": the class and method
agree but the declaring file has no package (or another one).
"""

import re

from . import BridgeEdge
from ._source import SourceFiles, is_comment, line_of
from ..languages import language_of

_KOTLIN = re.compile(r"\bexternal\s+fun\s+(?:<[^>]*>\s*)?(\w+)\s*\(")
_JAVA = re.compile(r"\bnative\b[^;{()=]*?\b(\w+)\s*\(")
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.M)


def decode(name):
    """
    'Java_com_example_Native_format_1name' -> ('com.example.Native', 'format_name');
    None if `name` isn't a JNI symbol. An overload signature (after '__') is dropped.
    """
    if not name.startswith("Java_"):
        return None
    parts, current, body, i = [], [], name[5:], 0
    while i < len(body):
        ch = body[i]
        if ch != "_":
            current.append(ch)
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in ("1", "2", "3"):
            current.append({"1": "_", "2": ";", "3": "["}[nxt])
            i += 2
        elif nxt == "0" and re.fullmatch(r"[0-9a-fA-F]{4}", body[i + 2:i + 6] or ""):
            current.append(chr(int(body[i + 2:i + 6], 16)))
            i += 6
        elif nxt == "_":                          # overloaded: Java_pkg_C_m__ILjava_lang_String_2
            break
        else:
            parts.append("".join(current))
            current = []
            i += 1
    parts.append("".join(current))
    if len(parts) < 2 or not all(parts):
        return None
    return ".".join(parts[:-1]), parts[-1]


def _owner(chunk):
    """Class a declaration belongs to: the chunk itself if it's a class, else its parent."""
    return chunk["name"] if chunk["type"] == "class" else chunk.get("parent") or ""


def detect(chunks):
    files = SourceFiles(chunks)
    natives = {}                                  # (class simple name, method) -> [(C chunk, class)]
    for i, c in enumerate(chunks):
        if language_of(c) in ("c", "cpp") and c["type"] == "function":
            decoded = decode(c["name"])
            if decoded:
                cls, method = decoded
                natives.setdefault((cls.rsplit(".", 1)[-1], method), []).append((i, cls))

    edges = []
    for path in files.files("kotlin", "java"):
        text = files.text(path)
        found = _PACKAGE.search(text)
        package = found.group(1) if found else ""
        pattern = _KOTLIN if path.endswith((".kt", ".kts")) else _JAVA
        for m in pattern.finditer(text):
            if is_comment(text, m.start()):
                continue
            line = line_of(text, m.start())
            src = files.chunk_at(path, line)
            if src is None:
                continue
            cls, method = _owner(chunks[src]), m.group(1)
            for dst, full_class in natives.get((cls, method), ()):
                exact = bool(package) and full_class == f"{package}.{cls}"
                edges.append(BridgeEdge(src, dst, "jni", "exact" if exact else "pattern", {
                    "label": f"JNI {cls}.{method}",
                    "matched": [" ".join(m.group(0).rstrip("(").split()), chunks[dst]["name"]],
                    "src": files.where(path, line),
                    "dst": files.where(chunks[dst]["file"], chunks[dst]["start_line"]),
                }))
    return edges
