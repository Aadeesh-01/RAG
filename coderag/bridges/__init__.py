"""
Cross-language "bridge" edges (v10): links between code that calls across a
language or process boundary, which neither the Python resolver nor name
matching can see.

One module per mechanism, each with detect(chunks) -> [BridgeEdge]:

    jni           Kotlin/Java `external`/`native` methods -> C/C++ Java_<pkg>_<Class>_<method>
    ctypes_cffi   Python calls on a ctypes/cffi-loaded library -> C/C++ functions of that name
    http          HTTP client calls (fetch/axios, requests/httpx, OkHttp/Retrofit)
                  -> server routes (FastAPI/Flask, Express, Spring, Go net/http)

An edge points from the calling chunk to the chunk that implements the call.
Its confidence is "exact" (names or method + path agree completely) or
"pattern" (a path parameter, a wrapper function or a missing package had to be
assumed). Its evidence holds the matched strings and both file:line locations.

Detectors only use what the chunks record, plus the source files they came
from (package names, module-level bindings, route decorators outside any
definition), so they run while the repository is there, at index time.
"""

from dataclasses import asdict, dataclass, field

MECHANISMS = ("jni", "ctypes_cffi", "http")
LABELS = {"jni": "JNI", "ctypes_cffi": "ctypes/cffi", "http": "HTTP"}


@dataclass(frozen=True)
class BridgeEdge:
    src: int                    # chunk index of the caller
    dst: int                    # chunk index of the implementation
    mechanism: str              # jni | ctypes_cffi | http
    confidence: str             # exact | pattern
    evidence: dict = field(default_factory=dict, compare=False)
    # evidence: {"matched": [caller side, callee side], "src": "path:line", "dst": "path:line"}

    @property
    def label(self):
        """'HTTP POST /api/repos', 'JNI NativeMath.add', 'ctypes/cffi zoo_add'."""
        return self.evidence.get("label") or LABELS.get(self.mechanism, self.mechanism)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: data[k] for k in ("src", "dst", "mechanism", "confidence", "evidence")})


def detect(chunks, mechanisms=MECHANISMS):
    """Every bridge edge the named mechanisms find among `chunks`, without duplicates."""
    from . import ctypes_cffi, http, jni
    modules = {"jni": jni, "ctypes_cffi": ctypes_cffi, "http": http}
    edges, seen = [], set()
    for name in mechanisms:
        for edge in modules[name].detect(chunks):
            key = (edge.src, edge.dst, edge.mechanism, edge.label)
            if edge.src != edge.dst and key not in seen:
                seen.add(key)
                edges.append(edge)
    return edges
