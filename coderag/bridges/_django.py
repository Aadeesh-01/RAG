"""
Django URLconfs, for the HTTP bridge: each route with its full path, its view
and its name.

  path("items/<int:pk>/", views.item_detail, name="item-detail")   function view: any method
  re_path(r"^items/(?P<pk>\\d+)/$", ...)                              regex turned into a path (pattern)
  path("shop/", include("shop.urls"))                               prefix for every route in shop/urls.py
  path("items/", ItemView.as_view())                                class-based view: ItemView.get -> GET,
                                                                    ItemView.post -> POST ... (the class if none)
  router.register(r"orders", OrderViewSet); path("api/", include(router.urls))
                                                                    DRF: list/create on /api/orders,
                                                                    retrieve/update/partial_update/destroy
                                                                    on /api/orders/{pk} (pattern)

A route's name is kept as "name" and, with the file's app_name, "app:name", so
templates' {% url 'app:name' %} can be resolved to it.
"""

import os
import re

from ._source import call_args, is_comment, keyword_literal, line_of, literal
from ..languages import language_of

_ROUTE = re.compile(r"(?<![\w.])(?P<fn>path|re_path|url)\s*\(")
_ROUTER = re.compile(r"(?P<var>\w+)\s*=\s*(?:\w+\.)*(?:DefaultRouter|SimpleRouter)\s*\(")
_APP_NAME = re.compile(r"""^\s*app_name\s*=\s*["']([\w-]+)["']""", re.M)
VIEW_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
VIEWSET_ACTIONS = {"list": ("GET", False), "create": ("POST", False), "retrieve": ("GET", True),
                   "update": ("PUT", True), "partial_update": ("PATCH", True), "destroy": ("DELETE", True)}


def regex_to_path(rx):
    """'^items/(?P<pk>[0-9]+)/$' -> 'items/{pk}/'; None if it isn't a plain path with groups."""
    rx = rx.lstrip("^").rstrip("$")
    out = re.sub(r"\(\?P<(\w+)>[^)]*\)", r"{\1}", rx)
    out = re.sub(r"\([^)]*\)", "{}", out).replace("\\.", ".").replace("\\-", "-").replace("\\/", "/")
    return None if re.search(r"[\\^$*+?()\[\]|]", out) else out


def _join(*parts):
    """'shop/' + 'items/<int:pk>/' -> '/shop/items/<int:pk>/' (a route's own trailing slash is kept)."""
    joined = "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    last = next((p for p in reversed(parts) if p and p.strip("/")), "")
    return joined + "/" if last.endswith("/") and joined != "/" else joined


def _target_of(expr):
    """include("shop.urls") / include(router.urls) / view: ('module', m) | ('router', var) | ('view', expr) | None."""
    m = re.match(r"\s*include\s*\(", expr or "")
    if not m:
        return ("view", expr.strip()) if expr else None
    args, _ = call_args(expr, m.end() - 1)
    first = (args or [""])[0]
    if first.startswith("("):                       # include(("shop.urls", "shop"))
        inner, _ = call_args(first, 0)
        first = (inner or [""])[0]
    module = literal(first)
    if module:
        return ("module", module)
    router = re.fullmatch(r"(\w+)\.urls", first.strip())
    return ("router", router.group(1)) if router else None


def routes(files, chunks):
    """[{method, path, chunk, names, route_at, pattern}] for every Django route found."""
    python = [i for i, c in enumerate(chunks) if language_of(c) == "python"]

    def find(name, kinds, near):
        found = [i for i in python if chunks[i]["name"] == name and chunks[i]["type"] in kinds]
        close = [i for i in found if os.path.dirname(chunks[i]["file"]) == os.path.dirname(near)]
        return (close or (found if len(found) == 1 else []))[:1]

    # ── read every URLconf ──────────────────────────────────────────────────
    confs = {}
    for path in files.files("python"):
        text = files.text(path)
        if not (os.path.basename(path) == "urls.py" or "urlpatterns" in text):
            continue
        conf = {"routes": [], "includes": [], "routers": {}, "router_includes": [],
                "app": (_APP_NAME.search(text) or [None, ""])[1]}
        for m in _ROUTE.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            route = literal(args[0]) if args else None
            if route is None:
                continue
            is_re = m.group("fn") != "path"
            if is_re:
                route = regex_to_path(route)
                if route is None:
                    continue
            target = _target_of(args[1] if len(args) > 1 else "")
            line = line_of(text, m.start())
            if target and target[0] == "module":
                conf["includes"].append((route, target[1], is_re))
            elif target and target[0] == "router":
                conf["router_includes"].append((target[1], route))
            elif target:
                conf["routes"].append((route, is_re, target[1], keyword_literal(args, "name"), line))
        for m in _ROUTER.finditer(text):
            var, registered = m.group("var"), []
            for r in re.finditer(rf"(?<![\w.]){re.escape(var)}\.register\s*\(", text):
                args, _ = call_args(text, r.end() - 1)
                prefix = literal(args[0]) if args else None
                if prefix is not None and len(args) > 1:
                    registered.append((prefix, args[1].strip().split(".")[-1], line_of(text, r.start())))
            conf["routers"][var] = registered
            if re.search(rf"urlpatterns\s*\+?=\s*(?:\[[^\]]*)?{re.escape(var)}\.urls", text):
                conf["router_includes"].append((var, ""))
        confs[path] = conf

    # ── prefixes through include() ──────────────────────────────────────────
    def conf_for(module):
        tail = module.replace(".", "/") + ".py"
        hits = [p for p in confs if files.rel(p) == tail or files.rel(p).endswith("/" + tail)]
        return hits[0] if len(hits) == 1 else None

    children = {p: [(conf_for(mod), route, is_re) for route, mod, is_re in c["includes"]] for p, c in confs.items()}
    included = {child for kids in children.values() for child, _, _ in kids if child}
    prefixes = {p: set() for p in confs}
    stack = [(p, "", False, 0) for p in confs if p not in included]
    while stack:
        path, prefix, regex, depth = stack.pop()
        if (prefix, regex) in prefixes[path] or depth > 8:
            continue
        prefixes[path].add((prefix, regex))
        for child, route, is_re in children[path]:
            if child:
                stack.append((child, _join(prefix, route), regex or is_re, depth + 1))

    # ── routes -> views ─────────────────────────────────────────────────────
    out = []
    for path, conf in confs.items():
        app = conf["app"]
        for prefix, prefix_re in prefixes[path] or {("", False)}:
            for route, is_re, view, name, line in conf["routes"]:
                full = _join(prefix, route)
                names = tuple(n for n in (name, f"{app}:{name}" if app and name else None) if n)
                where = files.where(path, line)
                cbv = re.match(r"([\w.]+)\.as_view\s*\(", view)
                if cbv:
                    cls = cbv.group(1).split(".")[-1]
                    for k in find(cls, ("class",), path):
                        methods = [i for i in python if chunks[i]["type"] == "method"
                                   and chunks[i]["parent"] == cls and chunks[i]["file"] == chunks[k]["file"]
                                   and chunks[i]["name"] in VIEW_METHODS]
                        for i in methods or [k]:
                            verb = chunks[i]["name"].upper() if i != k else "*"
                            out.append({"method": verb, "path": full, "chunk": i, "names": names,
                                        "route_at": where, "pattern": is_re or prefix_re})
                elif re.fullmatch(r"[\w.]+", view):
                    for i in find(view.split(".")[-1], ("function",), path):
                        out.append({"method": "*", "path": full, "chunk": i, "names": names,
                                    "route_at": where, "pattern": is_re or prefix_re})
            # DRF routers: always a pattern (the routes are generated)
            for var, include_route in conf["router_includes"]:
                for prefix_lit, viewset, line in conf["routers"].get(var, ()):
                    base = _join(prefix, include_route, prefix_lit)
                    for k in find(viewset, ("class",), path):
                        actions = {chunks[i]["name"]: i for i in python if chunks[i]["type"] == "method"
                                   and chunks[i]["parent"] == viewset and chunks[i]["name"] in VIEWSET_ACTIONS}
                        for action, (verb, detail) in VIEWSET_ACTIONS.items():
                            # an action the viewset doesn't define is inherited: the class stands for it
                            out.append({"method": verb, "path": _join(base, "{pk}") if detail else base,
                                        "chunk": actions.get(action, k), "names": (),
                                        "route_at": files.where(path, line), "pattern": True})
    return out
