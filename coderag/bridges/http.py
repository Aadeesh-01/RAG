"""
HTTP: client calls -> the server routes that handle them.

Clients                                     Servers
  JS/TS   fetch(), axios(), axios.get() ...   FastAPI / Flask   @app.get("/x"), @bp.route("/x", methods=[...])
          wrappers named *fetch* (fetchJSON)  Express           app.get("/x", handler), router.post(...)
  Python  requests.get(), httpx.post() ...    Spring            @GetMapping("/x"), @RequestMapping (class prefix too)
  Kotlin/ Retrofit @GET("users/{id}"),        Go net/http       http.HandleFunc("/x", h), mux.HandleFunc("POST /x", h),
  Java    OkHttp Request.Builder().url(...)                     r.HandleFunc(...).Methods("POST"), gin/chi r.GET / r.Get

Only literal paths (or literals joined with "+") are used; a URL built from a
variable, a call or a template with values is skipped, never guessed. Paths are
normalised: scheme and host, query and trailing "/" dropped, and every path
parameter ({id}, {id:int}, :id, <id>, <int:id>) becomes {}. A client and a
route match on the method (a route without one accepts any) and on the path,
segment by segment, where {} matches any segment.

"exact": same method and the same path with no parameter on either side, from
fetch/axios/requests/... directly. "pattern": a parameter matched a segment, the
route takes any method, or the call went through a wrapper such as fetchJSON.
A literal route beats a parametrised one: GET /api/repos/mine matches
@app.get("/api/repos/mine"), not also @app.get("/api/repos/{repo_id}").
"""

import re
from dataclasses import dataclass

from . import BridgeEdge
from ._source import SourceFiles, call_args, is_comment, keyword_literal, line_of, literal

VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_PARAM = re.compile(r"\{[^}]*\}|:\w+|<[^>]*>|\*\w*")
_PATHLIKE = re.compile(r"[\w\-./{}<>:~%+@!$&'*,;=]*")


def normalize(url):
    """'https://x.io/api/users/:id/?q=1' -> '/api/users/{}'; None if it isn't a path."""
    if url is None:
        return None
    path = re.sub(r"^[a-zA-Z][\w+.-]*://[^/]*", "", url.strip())
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    if not _PATHLIKE.fullmatch(path) or path.startswith("."):
        return None
    segments = [("{}" if _PARAM.fullmatch(s) else s) for s in path.split("/") if s]
    return "/" + "/".join(segments)


def display(url):
    """The path part of a URL as written: 'https://x.io/api/a?b=1' -> '/api/a'."""
    path = re.sub(r"^[a-zA-Z][\w+.-]*://[^/]*", "", url.strip())
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    return path if path.startswith("/") else "/" + path


@dataclass
class End:
    method: str          # GET, POST ... or "*" (route: any method)
    path: str            # normalised
    raw: str             # as written
    file: str
    line: int
    chunk: int
    weak: bool = False   # client through a wrapper (fetchJSON): the method is assumed


def _segments_match(a, b):
    sa, sb = a.split("/"), b.split("/")
    return len(sa) == len(sb) and all(x == y or "{}" in (x, y) for x, y in zip(sa, sb))


# =============================================================================
# CLIENTS
# =============================================================================
_JS_CLIENT = re.compile(r"(?<![\w$.])(?:axios\.(?P<verb>get|post|put|patch|delete|head|options)"
                        r"|(?P<fn>fetch|axios|\w*[Ff]etch\w*))\s*\(")
_JS_METHOD = re.compile(r"""\bmethod\s*:\s*(["'`])(\w+)\1""")
_PY_CLIENT = re.compile(r"(?<![\w.])(?:requests|httpx)\.(?P<verb>get|post|put|patch|delete|head|options|request)\s*\(")
_RETROFIT = re.compile(r"@(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*\(")
_OKHTTP = re.compile(r"\.url\s*\(")
_OKHTTP_VERB = re.compile(r"\.(get|post|put|patch|delete|head)\s*\(")


def _clients(files):
    found = []

    def add(path, offset, method, url, weak=False):
        norm = normalize(url)
        if norm is None or method not in VERBS:
            return
        line = line_of(files.text(path), offset)
        chunk = files.chunk_at(path, line)
        if chunk is not None:
            found.append(End(method, norm, display(url), path, line, chunk, weak))

    for path in files.files("javascript", "typescript"):
        text = files.text(path)
        for m in _JS_CLIENT.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            url = literal(args[0]) if args else None
            if url is None or not url.startswith(("/", "http://", "https://")):
                continue                              # dynamic, or not a URL
            if m.group("verb"):
                method = m.group("verb").upper()
            else:
                options = next((_JS_METHOD.search(a) for a in args[1:] if _JS_METHOD.search(a)), None)
                method = options.group(2).upper() if options else "GET"
            add(path, m.start(), method, url, weak=m.group("fn") not in (None, "fetch", "axios"))

    for path in files.files("python"):
        text = files.text(path)
        for m in _PY_CLIENT.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            if not args:
                continue
            if m.group("verb") == "request":
                method = literal(args[0]) or keyword_literal(args, "method")
                url = literal(args[1]) if len(args) > 1 else keyword_literal(args, "url")
            else:
                method = m.group("verb")
                url = literal(args[0]) if "=" not in args[0] else keyword_literal(args, "url")
            if method and url:
                add(path, m.start(), method.upper(), url)

    for path in files.files("kotlin", "java"):
        text = files.text(path)
        for m in _RETROFIT.finditer(text):
            args, _ = call_args(text, m.end() - 1)
            url = (literal(args[0]) if args and "=" not in args[0] else keyword_literal(args or [], "value"))
            if url is not None and not is_comment(text, m.start()):
                add(path, m.start(), m.group("verb"), url)
        for m in _OKHTTP.finditer(text):
            args, end = call_args(text, m.end() - 1)
            url = literal(args[0]) if args else None
            if url is None or is_comment(text, m.start()):
                continue
            chain = text[end:end + 300].split(".build()")[0]
            verb = _OKHTTP_VERB.search(chain)
            add(path, m.start(), verb.group(1).upper() if verb else "GET", url)
    return found


# =============================================================================
# SERVERS
# =============================================================================
_PY_ROUTE = re.compile(r"@(?P<obj>[\w.]+)\.(?P<verb>get|post|put|patch|delete|head|options|route|api_route)\s*\(")
_JS_ROUTE = re.compile(r"(?<![\w$.])(?P<obj>app|router|server|\w+Router|\w+App)\."
                       r"(?P<verb>get|post|put|patch|delete|head|options|all)\s*\(")
_SPRING = re.compile(r"@(?P<verb>Get|Post|Put|Patch|Delete|Request)Mapping\b")
_GO_ROUTE = re.compile(r"(?<![\w.])(?P<obj>\w+)\.(?P<fn>HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE"
                       r"|Get|Post|Put|Patch|Delete)\s*\(")


def _first_path(args, *keywords):
    """A route's path: the first positional literal, a keyword (value=, path=), or ["/x"] / {"/x"}."""
    if not args:
        return None
    first = args[0]
    if "=" not in first and ":" not in first.split('"')[0]:
        if first[:1] in "[{":
            first = first[1:-1].split(",")[0]
        return literal(first)
    value = keyword_literal(args, *keywords)
    if value is None:
        for arg in args:
            m = re.match(r"\s*(?:\w+)\s*[=:]\s*[\[{]\s*(.+?)\s*[\]}]\s*$", arg, re.S)
            if m and m.group(0).split("=")[0].strip() in keywords:
                value = literal(m.group(1).split(",")[0])
    return value


def _servers(files, chunks):
    found = []

    def add(path, offset, method, route, chunk=None):
        norm = normalize(route)
        if norm is None:
            return
        line = line_of(files.text(path), offset)
        chunk = chunk if chunk is not None else files.chunk_at(path, line)
        if chunk is not None:
            found.append(End(method, norm, display(route), path, line, chunk))

    for path in files.files("python"):
        text = files.text(path)
        for m in _PY_ROUTE.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            route = _first_path(args or [], "path", "rule")
            if route is None:
                continue
            verb = m.group("verb")
            if verb in ("route", "api_route"):
                listed = next((a for a in args if re.match(r"\s*methods\s*=", a)), "")
                methods = [v.upper() for v in re.findall(r"""["'](\w+)["']""", listed)] or ["GET"]
            else:
                methods = [verb.upper()]
            for method in methods:
                add(path, m.start(), method, route)

    for path in files.files("javascript", "typescript"):
        text = files.text(path)
        for m in _JS_ROUTE.finditer(text):
            args, _ = call_args(text, m.end() - 1)
            if not args or len(args) < 2 or is_comment(text, m.start()):
                continue                              # a route needs a handler
            route = literal(args[0])
            if route is None or not route.startswith("/"):
                continue
            handler = re.fullmatch(r"[\w$.]+", args[-1])
            chunk = files.named(path, args[-1].split(".")[-1], "javascript" if path.endswith(
                (".js", ".mjs", ".cjs", ".jsx")) else "typescript") if handler else None
            verb = m.group("verb").upper()
            add(path, m.start(), "*" if verb == "ALL" else verb, route, chunk)

    for path in files.files("kotlin", "java"):
        text = files.text(path)
        prefixes, routes = {}, []                     # class chunk -> prefix; method routes
        for m in _SPRING.finditer(text):
            if is_comment(text, m.start()):
                continue
            paren = m.end() + re.match(r"\s*", text[m.end():]).end()
            args, end = call_args(text, paren) if text[paren:paren + 1] == "(" else ([], m.end())
            route = _first_path(args or [], "value", "path") if args else ""
            if route is None:
                continue
            verb = m.group("verb")
            method = verb.upper() if verb != "Request" else (
                (re.search(r"RequestMethod\.(\w+)", " ".join(args or [])) or [None, "*"])[1])
            following = re.search(r"\b(class|interface|object)\b|\(", text[end + 1:end + 600])
            line = line_of(text, m.start())
            chunk = files.chunk_at(path, line)
            if following and following.group(1) and chunk is not None and chunks[chunk]["type"] == "class":
                prefixes[chunks[chunk]["name"]] = route
            else:
                routes.append((m.start(), method, route, chunk))
        for offset, method, route, chunk in routes:
            owner = chunks[chunk].get("parent") or chunks[chunk]["name"] if chunk is not None else ""
            prefix = prefixes.get(owner, "")
            full = "/".join(p.strip("/") for p in (prefix, route) if p.strip("/"))
            add(path, offset, method, "/" + full, chunk)

    for path in files.files("go"):
        text = files.text(path)
        for m in _GO_ROUTE.finditer(text):
            fn, obj = m.group("fn"), m.group("obj")
            if obj == "http" and fn not in ("HandleFunc", "Handle"):
                continue                              # http.Get(...) is a client call
            args, end = call_args(text, m.end() - 1)
            route = literal(args[0]) if args else None
            if route is None or is_comment(text, m.start()):
                continue
            method = "*"
            if fn not in ("HandleFunc", "Handle"):
                method = fn.upper()
            elif re.match(r"(\w+)\s+/", route):          # Go 1.22: "POST /items/{id}"
                method, route = route.split(None, 1)
            else:
                chained = re.match(r"\s*\.Methods\(\s*\"(\w+)\"", text[end + 1:end + 80])
                method = chained.group(1).upper() if chained else "*"
            handler = args[-1].split(".")[-1] if len(args) > 1 and re.fullmatch(r"[\w.]+", args[-1]) else None
            add(path, m.start(), method.upper(), route, files.named(path, handler, "go") if handler else None)
    return found


# =============================================================================
# MATCHING
# =============================================================================
def detect(chunks):
    files = SourceFiles(chunks)
    servers = _servers(files, chunks)
    edges = []
    for client in _clients(files):
        matches = [s for s in servers if s.method in (client.method, "*")
                   and _segments_match(client.path, s.path)]
        literal_routes = [s for s in matches if "{}" not in s.path]
        for server in (literal_routes or matches):
            exact = (not client.weak and server.method == client.method
                     and "{}" not in client.path and "{}" not in server.path)
            if server.chunk == client.chunk:
                continue
            edges.append(BridgeEdge(client.chunk, server.chunk, "http", "exact" if exact else "pattern", {
                "label": f"HTTP {client.method} {client.raw}",
                "matched": [f"{client.method} {client.raw}", f"{server.method} {server.raw}"],
                "src": files.where(client.file, client.line),
                "dst": files.where(server.file, server.line),
            }))
    return edges
