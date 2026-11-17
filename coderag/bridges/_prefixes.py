"""
Route prefixes added where a router is mounted, for the HTTP bridge.

  FastAPI  router = APIRouter(prefix="/items")            -> every @router.get(...) starts with /items
           app.include_router(items.router, prefix="/v1")  -> ... and with /v1 before that
  Flask    shop = Blueprint("shop", __name__, url_prefix="/shop")
           app.register_blueprint(shop, url_prefix="/x")  -> url_prefix at registration replaces the blueprint's
  Express  const users = require("./routes/users"); app.use("/api/users", users)
           -> every router.get(...) in routes/users.js starts with /api/users

A router is found by its variable: in the same file, as `module.var` (module =
the defining file's name), or through an import (`from .routers.items import
router`, require/import of a relative path in JS). A mount that can't be
resolved adds no prefix; the router keeps its own.
"""

import os
import re

from ._source import call_args, is_comment, keyword_literal, literal

_PY_ROUTER = re.compile(r"(?P<var>\w+)\s*=\s*(?:\w+\.)*(?P<kind>APIRouter|Blueprint)\s*\(")
_PY_MOUNT = re.compile(r"(?P<parent>\w+)\.(?P<how>include_router|register_blueprint)\s*\(")
_PY_IMPORT = re.compile(r"^\s*from\s+(?P<module>[.\w]+)\s+import\s+\(?(?P<names>[^)\n]+)", re.M)
_JS_ROUTER = re.compile(r"(?:const|let|var)\s+(?P<var>\w+)\s*=\s*(?:express\s*\.\s*)?Router\s*\(")
_JS_REQUIRE = re.compile(r"(?:const|let|var)\s+(?P<var>\w+)\s*=\s*require\(\s*[\"'](?P<spec>[^\"']+)[\"']\s*\)")
_JS_IMPORT = re.compile(r"import\s+(?P<var>\w+)\s+from\s+[\"'](?P<spec>[^\"']+)[\"']")
_JS_USE = re.compile(r"(?<![\w$.])(?P<parent>\w+)\.use\s*\(")


def join(*parts):
    """'/v1' + '/items' + '/{id}' -> '/v1/items/{id}'."""
    joined = "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return joined


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def _combine(parents, *more):
    return sorted({join(p, *more) for p in parents})


# =============================================================================
# PYTHON: FastAPI APIRouter / Flask Blueprint
# =============================================================================
def python_prefixes(files):
    """{(file, var): [prefix, ...]} for every APIRouter / Blueprint variable."""
    defs, imports, mounts = {}, {}, []
    for path in files.files("python"):
        text = files.text(path)
        for m in _PY_ROUTER.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            own = keyword_literal(args or [], "prefix" if m.group("kind") == "APIRouter" else "url_prefix")
            defs[(path, m.group("var"))] = (m.group("kind"), own or "")
        aliases = {}
        for m in _PY_IMPORT.finditer(text):
            module = m.group("module").lstrip(".").split(".")[-1] if m.group("module").strip(".") else ""
            for name in m.group("names").split(","):
                parts = name.strip().split(" as ")
                if parts[0].strip():
                    aliases[(parts[-1] if len(parts) > 1 else parts[0]).strip()] = (module, parts[0].strip())
        imports[path] = aliases
        for m in _PY_MOUNT.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            if args:
                key = "prefix" if m.group("how") == "include_router" else "url_prefix"
                mounts.append((path, m.group("parent"), args[0].strip(), keyword_literal(args, key), m.group("how")))

    def resolve(path, expr):
        """The router definition `expr` refers to in `path`, or None."""
        if re.fullmatch(r"\w+\.\w+", expr):                       # items.router
            module, var = expr.split(".")
            found = [k for k in defs if k[1] == var and _stem(k[0]) == module]
        elif re.fullmatch(r"\w+", expr):
            if (path, expr) in defs:
                return (path, expr)
            module, var = imports.get(path, {}).get(expr, (None, None))
            found = [k for k in defs if module is not None and k[1] == var and _stem(k[0]) == module]
            found = found or [k for k in defs if k[1] == var and module and module in k[0].replace("\\", "/").split("/")]
        else:
            return None
        return found[0] if len(found) == 1 else None

    mounted = {}                                                    # def -> [(mount file, parent var, prefix, how)]
    for path, parent, expr, prefix, how in mounts:
        target = resolve(path, expr)
        if target:
            mounted.setdefault(target, []).append((path, parent, prefix, how))

    cache = {}

    def prefixes(key, depth=0):
        if key in cache:
            return cache[key]
        kind, own = defs[key]
        if key not in mounted or depth > 6:
            result = [join(own)]
        else:
            result = []
            for path, parent, prefix, how in mounted[key]:
                above = prefixes((path, parent), depth + 1) if (path, parent) in defs else ["/"]
                if how == "register_blueprint" and prefix is not None:
                    result += _combine(above, prefix)               # replaces the blueprint's own prefix
                else:
                    result += _combine(above, prefix or "", own)
        cache[key] = sorted(set(result))
        return cache[key]

    return {key: prefixes(key) for key in defs}


# =============================================================================
# JAVASCRIPT: Express routers mounted with app.use("/prefix", router)
# =============================================================================
def js_prefixes(files):
    """{(file, var): [prefix, ...]} for every Express Router variable."""
    paths = files.files("javascript", "typescript")
    by_norm = {os.path.normcase(os.path.abspath(p)): p for p in paths}
    routers, modules, uses = {}, {}, []
    for path in paths:
        text = files.text(path)
        for m in _JS_ROUTER.finditer(text):
            routers[(path, m.group("var"))] = True
        names = {}
        for rx in (_JS_REQUIRE, _JS_IMPORT):
            for m in rx.finditer(text):
                if m.group("spec").startswith("."):
                    base = os.path.join(os.path.dirname(path), m.group("spec"))
                    for candidate in (base, *(base + e for e in (".js", ".ts", ".mjs", ".cjs")),
                                      *(os.path.join(base, "index" + e) for e in (".js", ".ts"))):
                        hit = by_norm.get(os.path.normcase(os.path.abspath(candidate)))
                        if hit:
                            names[m.group("var")] = hit
                            break
        modules[path] = names
        for m in _JS_USE.finditer(text):
            if is_comment(text, m.start()):
                continue
            args, _ = call_args(text, m.end() - 1)
            if args and len(args) >= 2:
                prefix = literal(args[0])
                child = args[-1].strip()
                if prefix is not None and prefix.startswith("/") and re.fullmatch(r"\w+", child):
                    uses.append((path, m.group("parent"), prefix, child))

    mounted = {}
    for path, parent, prefix, child in uses:
        if (path, child) in routers:
            targets = [(path, child)]
        elif child in modules.get(path, {}):
            target_file = modules[path][child]
            targets = [k for k in routers if k[0] == target_file]
        else:
            targets = []
        for target in targets:
            mounted.setdefault(target, []).append((path, parent, prefix))

    cache = {}

    def prefixes(key, depth=0):
        if key in cache:
            return cache[key]
        if key not in mounted or depth > 6:
            result = ["/"]
        else:
            result = []
            for path, parent, prefix in mounted[key]:
                above = prefixes((path, parent), depth + 1) if (path, parent) in routers else ["/"]
                result += _combine(above, prefix)
        cache[key] = sorted(set(result))
        return cache[key]

    return {key: prefixes(key) for key in routers}
