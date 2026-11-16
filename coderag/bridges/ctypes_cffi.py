"""
ctypes / cffi: Python calls on a loaded shared library -> the C/C++ function of that name.

    lib = ctypes.CDLL("./libzoo.so")          # also cdll.LoadLibrary, WinDLL, PyDLL
    ffi = cffi.FFI(); lib = ffi.dlopen("zoo")  # cffi ABI mode
    lib.zoo_add(1, 2)                          # -> C function zoo_add

Bindings are found anywhere in the file (often at module level, outside every
chunk), including attributes such as self._lib. "exact": the library's name
(libzoo.so, zoo.dll, "zoo") matches the C file or its folder; "pattern": only
the function name matches.
"""

import os
import re

from . import BridgeEdge
from ._source import SourceFiles, call_args, is_comment, line_of, literal
from ..languages import language_of

_LOAD = re.compile(
    r"(?P<var>[A-Za-z_][\w.]*)\s*=\s*(?:\w+\.)*?"
    r"(?:CDLL|WinDLL|OleDLL|PyDLL|LoadLibrary|dlopen)\s*\(")


def _stem(library):
    """'./build/libzoo.so.1' -> 'zoo', 'zoo.dll' -> 'zoo', 'zoo' -> 'zoo'."""
    name = os.path.basename(library or "")
    name = re.sub(r"(\.(so|dll|dylib|pyd)(\.\d+)*)$", "", name)
    return name[3:] if name.startswith("lib") else name


def detect(chunks):
    files = SourceFiles(chunks)
    c_functions = {}                              # name -> [chunk index]
    for i, c in enumerate(chunks):
        if language_of(c) in ("c", "cpp") and c["type"] in ("function", "method"):
            c_functions.setdefault(c["name"], []).append(i)

    edges = []
    for path in files.files("python"):
        text = files.text(path)
        libraries = {}                            # bound name -> library stem ("" if not literal)
        for m in _LOAD.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            libraries[m.group("var")] = _stem(literal(args[0]) if args else "")
        for var, stem in libraries.items():
            for m in re.finditer(rf"(?<![\w.]){re.escape(var)}\.(\w+)\s*\(", text):
                name = m.group(1)
                if name not in c_functions or is_comment(text, m.start()):
                    continue
                line = line_of(text, m.start())
                src = files.chunk_at(path, line)
                if src is None:
                    continue
                for dst in c_functions[name]:
                    c_file = chunks[dst]["file"]
                    places = {os.path.splitext(os.path.basename(c_file))[0],
                              os.path.basename(os.path.dirname(c_file))}
                    edges.append(BridgeEdge(src, dst, "ctypes_cffi",
                                            "exact" if stem and stem in places else "pattern", {
                        "label": f"ctypes/cffi {name}",
                        "matched": [f"{var}.{name}(", chunks[dst]["name"]],
                        "src": files.where(path, line),
                        "dst": files.where(c_file, chunks[dst]["start_line"]),
                    }))
    return edges
