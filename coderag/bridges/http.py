"""
HTTP: client calls -> the server routes that handle them.

Clients                                     Servers
  JS/TS   fetch(), axios(), axios.get() ...   FastAPI / Flask   @app.get("/x"), @bp.route("/x", methods=[...])
          wrappers named *fetch* (fetchJSON)  Express           app.get("/x", handler), router.post(...)
          jQuery $.ajax/$.get/$.post,         Django            urls.py path()/re_path(), include() prefixes,
          XMLHttpRequest .open("POST", url)                     function views, .as_view() -> get/post methods,
  Python  requests.get(), httpx.post() ...                      DRF router.register (always a pattern)
  Kotlin/ Retrofit @GET("users/{id}"),        Spring            @GetMapping("/x"), @RequestMapping (class prefix too)
  Java    OkHttp Request.Builder().url(...)   Go net/http       http.HandleFunc("/x", h), mux.HandleFunc("POST /x", h),
  HTML    <form action method>, htmx                            r.HandleFunc(...).Methods("POST"), gin/chi r.GET / r.Get
          hx-get/hx-post/..., literal or
          Django {% url 'app:name' %}

A template's {% url 'name' %} is resolved through the Django route of that
name (exact); any other template expression in a URL makes it dynamic.

Only literal paths (or literals joined with "+") are used; a URL built from a
variable, a call or a template with values is skipped, never guessed. Paths are
normalised: scheme and host, query and trailing "/" dropped, and every path
parameter ({id}, {id:int}, :id, <id>, <int:id>) becomes {}. A client and a
route match on the method (a route without one accepts any) and on the path,
segment by segment, where {} matches any segment.

"exact": same method and the same path with no parameter on either side, from
fetch/axios/requests/... directly. "pattern": a parameter matched a segment, the
route takes any method, or the call went through a wrapper such as fetchJSON.
Prefixes from mounting are applied (_prefixes.py): FastAPI APIRouter(prefix=)
and include_router(prefix=), Flask Blueprint(url_prefix=) and
register_blueprint(url_prefix=), Express app.use("/p", router).

A literal route beats a parametrised one: GET /api/repos/mine matches
@app.get("/api/repos/mine"), not also @app.get("/api/repos/{repo_id}").
"""

import re
from dataclasses import dataclass

from . import BridgeEdge
from ._django import routes as django_routes
from ._prefixes import join, js_prefixes, python_prefixes
from ._source import SourceFiles, call_args, is_comment, keyword_literal, line_of, literal, url_template

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
    # a parameter, or any segment with an interpolation in it ("x-{}"), matches any one segment
    segments = [("{}" if _PARAM.fullmatch(s) or "{" in s else s) for s in path.split("/") if s]
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
    names: tuple = ()    # server: the route's names (Django name=, "app:name")
    ref: str = ""        # client: a route referred to by name ({% url 'app:name' %}), no path
    route_at: str = ""   # server: where the route is declared, when not at the handler (urls.py)
    pattern: bool = False   # server: generated or regex route (DRF, re_path): never "exact"


def _keyword(args, name):
    """The expression passed as keyword `name` (url=...), unparsed."""
    for arg in args:
        m = re.match(rf"\s*{name}\s*=\s*(.+)$", arg, re.S)
        if m:
            return m.group(1)
    return None


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
_JQUERY = re.compile(r"(?<![\w$])(?:\$|jQuery)\.(?P<fn>ajax|get|post|getJSON)\s*\(")
_OBJ_URL = re.compile(r"""\burl\s*:\s*(?P<v>(["'`])[^"'`]*\2(?:\s*\+\s*(["'`])[^"'`]*\3)*)\s*(?=[,}\n])""")
_OBJ_TYPE = re.compile(r"""\b(?:type|method)\s*:\s*(["'`])(\w+)\1""")
_XHR_OPEN = re.compile(r"\.open\s*\(")
_FORM = re.compile(r"<form\b(?P<attrs>[^>]*)>", re.I)
_HTMX = re.compile(r"""\bhx-(?P<verb>get|post|put|patch|delete)\s*=\s*(?:"(?P<d>[^"]*)"|'(?P<s>[^']*)')""", re.I)
_URL_TAG = re.compile(r"""^\s*\{%\s*url\s+(["'])(?P<name>[^"']+)\1[^%]*%\}\s*$""")


def _attr(attrs, name):
    m = re.search(rf"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)')""", attrs, re.I)
    return None if m is None else (m.group(1) if m.group(1) is not None else m.group(2))


def template_target(value):
    """A template URL: ('ref', 'app:name') for {% url %}, ('url', '/x') for a literal, else None."""
    if value is None:
        return None
    tag = _URL_TAG.match(value)
    if tag:
        return ("ref", tag.group("name"))
    value = value.strip()
    if not value or "{{" in value or "{%" in value or "${" in value or not value.startswith(("/", "http")):
        return None                                   # dynamic, empty (same page) or relative
    return ("url", value)


def _clients(files):
    found = []

    def url_of(expr):
        """(url, interpolated) for a client's URL argument, if it is one we can read."""
        found = url_template(expr)
        if found is None or not found[0].startswith(("/", "http://", "https://")):
            return None, False                    # dynamic (a variable, base + path), or not a URL
        host = re.match(r"^[a-zA-Z]+://([^/]*)", found[0])
        if host and "{" in host.group(1):
            return None, False                    # the host itself is interpolated
        return found

    def add(path, offset, method, url, weak=False, ref=""):
        norm = "" if ref else normalize(url)
        if norm is None or method not in VERBS:
            return
        line = line_of(files.text(path), offset)
        chunk = files.chunk_at(path, line)
        if chunk is not None:
            found.append(End(method, norm, "" if ref else display(url), path, line, chunk, weak, ref=ref))

    for path in files.files("javascript", "typescript"):
        text = files.text(path)
        for m in _JS_CLIENT.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            url, interpolated = url_of(args[0]) if args else (None, False)
            if url is None:
                continue
            if m.group("verb"):
                method = m.group("verb").upper()
            else:
                options = next((_JS_METHOD.search(a) for a in args[1:] if _JS_METHOD.search(a)), None)
                method = options.group(2).upper() if options else "GET"
            add(path, m.start(), method, url,
                weak=interpolated or m.group("fn") not in (None, "fetch", "axios"))
        for m in _JQUERY.finditer(text):                  # $.ajax({url, type}) / $.get(url) / $.post(url)
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            if not args:
                continue
            fn = m.group("fn")
            if fn == "ajax" and args[0].startswith("{"):
                found_url = _OBJ_URL.search(args[0])
                url, interpolated = url_of(found_url.group("v")) if found_url else (None, False)
                options = args[0]
            else:
                (url, interpolated), options = url_of(args[0]), " ".join(args[1:])
            kind = _OBJ_TYPE.search(options) if fn == "ajax" else None
            method = kind.group(2).upper() if kind else ("POST" if fn == "post" else "GET")
            if url is not None:
                add(path, m.start(), method, url, weak=interpolated)
        for m in _XHR_OPEN.finditer(text):                # xhr.open("POST", "/x")
            args, _ = call_args(text, m.end() - 1)
            if not args or len(args) < 2 or is_comment(text, m.start()):
                continue
            method, (url, interpolated) = literal(args[0]), url_of(args[1])
            if method and method.upper() in VERBS and url:
                add(path, m.start(), method.upper(), url, weak=interpolated)

    for path in files.files("html"):                      # templates: forms and htmx requests
        text = files.text(path)
        for m in _FORM.finditer(text):
            method = (_attr(m.group("attrs"), "method") or "GET").upper()
            target = template_target(_attr(m.group("attrs"), "action"))
            if target and method in VERBS:
                add(path, m.start(), method, target[1], ref=target[1] if target[0] == "ref" else "")
        for m in _HTMX.finditer(text):
            target = template_target(m.group("d") if m.group("d") is not None else m.group("s"))
            if target:
                add(path, m.start(), m.group("verb").upper(), target[1],
                    ref=target[1] if target[0] == "ref" else "")

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
                url_arg = args[1] if len(args) > 1 else _keyword(args, "url")
            else:
                method = m.group("verb")
                url_arg = args[0] if "=" not in args[0].split("(")[0] else _keyword(args, "url")
            url, interpolated = url_of(url_arg) if url_arg else (None, False)
            if method and url:
                add(path, m.start(), method.upper(), url, weak=interpolated)

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
    py_prefix, js_prefix = python_prefixes(files), js_prefixes(files)
    for r in django_routes(files, chunks):
        c = chunks[r["chunk"]]
        found.append(End(r["method"], normalize(r["path"]), r["path"], c["file"], c["start_line"], r["chunk"],
                         names=r["names"], route_at=r["route_at"], pattern=r["pattern"]))

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
            for prefix in py_prefix.get((path, m.group("obj")), ["/"]):
                for method in methods:
                    add(path, m.start(), method, join(prefix, route) if prefix != "/" else route)

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
            for prefix in js_prefix.get((path, m.group("obj")), ["/"]):
                add(path, m.start(), "*" if verb == "ALL" else verb,
                    join(prefix, route) if prefix != "/" else route, chunk)

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
        if client.ref:                                    # {% url 'app:name' %}: by the route's name
            named = [s for s in servers if client.ref in s.names and s.method in (client.method, "*")]
            bare = client.ref.split(":")[-1]
            matches = named or [s for s in servers if bare in s.names and s.method in (client.method, "*")]
        else:
            matches = [s for s in servers if s.method in (client.method, "*")
                       and _segments_match(client.path, s.path)]
            if "{}" in client.path:     # /api/repos/{} is /api/repos/{repo_id}, not also /api/repos/mine
                matches = [s for s in matches if s.path == client.path] or matches
            else:                       # /api/repos/mine is /api/repos/mine, not also /api/repos/{repo_id}
                matches = [s for s in matches if "{}" not in s.path] or matches
        for server in matches:
            if client.ref:
                exact, written = not server.pattern, f"{{% url '{client.ref}' %}}"
            else:
                exact = (not client.weak and not server.pattern and server.method == client.method
                         and "{}" not in client.path and "{}" not in server.path)
                written = client.raw
            if server.chunk == client.chunk:
                continue
            evidence = {
                "label": f"HTTP {client.method} {client.raw or server.raw}",
                "matched": [f"{client.method} {written}", f"{server.method} {server.raw}"],
                "src": files.where(client.file, client.line),
                "dst": files.where(server.file, server.line),
            }
            if server.route_at:
                evidence["route"] = server.route_at
            edges.append(BridgeEdge(client.chunk, server.chunk, "http", "exact" if exact else "pattern", evidence))
    return edges
