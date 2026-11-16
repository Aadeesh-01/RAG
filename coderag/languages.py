"""
Languages other than Python: a registry of tree-sitter grammars plus the node
types the generic chunker (chunking.parse_tree_sitter_file) looks for.

Python is not here: it keeps its own `ast` chunker and the scope-aware
resolver (resolve.py). Every other language is chunked by the same generic
code from the config below, and its calls are linked by name within the same
language (graph.py). Adding a language means adding one Language entry.

Grammars come from tree-sitter-language-pack (pinned in pyproject.toml). It
downloads each grammar on first use into a user cache; `prefetch()` fetches
them all ahead of time (e.g. while building a server image).

Fields of a Language
--------------------
name          registry key; also each chunk's `language`
label         shown to people ("TypeScript")
extensions    file extension -> grammar name in tree-sitter-language-pack
fence         Markdown code-fence tag for the LLM context
functions     top-level function definitions
classes       class-like definitions: class, interface, struct, trait, enum, object.
              A value of {field: (node types)} only counts the node when that
              field's child is one of the types (Go: a type_spec of a struct_type)
methods       member definitions inside a class body
class_bodies  node types holding a class's members when there is no `body` field
containers    nodes whose named children are scanned as if they were top-level
              (export, namespace, module, template, declaration lists)
function_values  {declarator node: (value field, function node types)} for
              functions assigned to names: const f = () => {...}
receiver_methods {node: receiver field}: top-level methods attached to a type
              by a receiver (Go: func (d *Dog) Speak())
impl_blocks   {node: (type field, trait field)}: blocks adding methods to a type,
              optionally implementing a trait (Rust: impl Speak for Dog)
embedded_bases  member node types that, without a `name` field, embed their
              type as a base (Go: an anonymous struct field)
calls         {call node: field holding the callee, or None = first named child}
inheritance   nodes inside a class definition that list its bases
comments      comment node types; comments right above a definition become its docstring
imports       import nodes; their module paths help rank same-name call targets
name_types    leaf node types that hold a name
qualified     node types of a qualified name (a.b, a::b): only the last part counts
skip          node types never searched for names (type and call arguments)
owner_types   name types that name a type: a receiver's or impl block's owner
elide         replaces a method body in a class outline
"""

import os
from dataclasses import dataclass, field

# Grammar node types shared by most C-family grammars; a Language adds its own.
NAME_TYPES = frozenset({"identifier", "type_identifier", "field_identifier", "property_identifier",
                        "simple_identifier", "namespace_identifier", "constant", "name"})
QUALIFIED = frozenset({"member_expression", "field_expression", "selector_expression",
                       "qualified_identifier", "scoped_identifier", "scoped_type_identifier",
                       "navigation_expression", "navigation_suffix", "generic_type",
                       "template_type", "template_function", "generic_function", "nested_identifier"})
SKIP = frozenset({"arguments", "argument_list", "value_arguments", "call_suffix", "type_arguments",
                  "type_parameters", "template_argument_list", "access_specifier",
                  "string", "string_literal", "comment"})


@dataclass(frozen=True)
class Language:
    name: str
    label: str
    extensions: dict
    fence: str
    functions: frozenset = frozenset()
    classes: dict = field(default_factory=dict)
    methods: frozenset = frozenset()
    class_bodies: frozenset = frozenset()
    containers: frozenset = frozenset()
    function_values: dict = field(default_factory=dict)
    receiver_methods: dict = field(default_factory=dict)
    impl_blocks: dict = field(default_factory=dict)
    embedded_bases: frozenset = frozenset()
    calls: dict = field(default_factory=dict)
    inheritance: frozenset = frozenset()
    comments: frozenset = frozenset({"comment"})
    imports: frozenset = frozenset()
    name_types: frozenset = NAME_TYPES
    qualified: frozenset = QUALIFIED
    skip: frozenset = SKIP
    owner_types: frozenset = frozenset({"type_identifier"})
    elide: str = "{ ... }"


def _s(*names):
    return frozenset(names)


def _classes(*names, **conditional):
    """Class node types; a keyword maps a node type to {field: (allowed child types)}."""
    return {**{n: None for n in names}, **conditional}


_JS_LIKE = dict(
    functions=_s("function_declaration", "generator_function_declaration"),
    methods=_s("method_definition"),
    containers=_s("export_statement", "lexical_declaration", "variable_declaration"),
    function_values={"variable_declarator": ("value", ("arrow_function", "function_expression",
                                                       "function", "generator_function"))},
    calls={"call_expression": "function", "new_expression": "constructor"},
    inheritance=_s("class_heritage"),
    imports=_s("import_statement"),
)

LANGUAGES = {lang.name: lang for lang in (
    Language(
        name="javascript", label="JavaScript", fence="javascript",
        extensions={".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
                    ".jsx": "javascript"},
        classes=_classes("class_declaration", "class"),
        **_JS_LIKE,
    ),
    Language(
        name="typescript", label="TypeScript", fence="typescript",
        extensions={".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx"},
        classes=_classes("class_declaration", "abstract_class_declaration", "interface_declaration",
                         "enum_declaration"),
        **{**_JS_LIKE,
           "methods": _s("method_definition", "method_signature", "abstract_method_signature"),
           "containers": _JS_LIKE["containers"] | _s("ambient_declaration", "internal_module",
                                                       "statement_block"),
           "inheritance": _s("class_heritage", "extends_type_clause")},
    ),
    Language(
        name="java", label="Java", fence="java",
        extensions={".java": "java"},
        classes=_classes("class_declaration", "interface_declaration", "enum_declaration",
                         "record_declaration", "annotation_type_declaration"),
        methods=_s("method_declaration", "constructor_declaration", "compact_constructor_declaration"),
        containers=_s("enum_body_declarations"),
        calls={"method_invocation": "name", "object_creation_expression": "type"},
        inheritance=_s("superclass", "super_interfaces", "extends_interfaces"),
        comments=_s("block_comment", "line_comment"),
        imports=_s("import_declaration"),
    ),
    Language(
        name="kotlin", label="Kotlin", fence="kotlin",
        extensions={".kt": "kotlin", ".kts": "kotlin"},
        functions=_s("function_declaration"),
        classes=_classes("class_declaration", "object_declaration"),
        methods=_s("function_declaration", "secondary_constructor"),
        class_bodies=_s("class_body", "enum_class_body"),
        calls={"call_expression": None},
        inheritance=_s("delegation_specifier"),
        comments=_s("multiline_comment", "line_comment"),
        imports=_s("import_header"),
    ),
    Language(
        name="c", label="C", fence="c",
        extensions={".c": "c", ".h": "c"},
        functions=_s("function_definition"),
        classes=_classes("struct_specifier", "union_specifier", "enum_specifier"),
        containers=_s("declaration", "type_definition", "linkage_specification", "declaration_list",
                      "preproc_if", "preproc_ifdef", "preproc_else"),
        calls={"call_expression": "function"},
        imports=_s("preproc_include"),
    ),
    Language(
        name="cpp", label="C++", fence="cpp",
        extensions={".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
                    ".hxx": "cpp"},
        functions=_s("function_definition"),
        classes=_classes("class_specifier", "struct_specifier", "union_specifier", "enum_specifier"),
        methods=_s("function_definition", "template_declaration"),
        containers=_s("namespace_definition", "declaration_list", "template_declaration",
                      "linkage_specification", "declaration", "type_definition",
                      "preproc_if", "preproc_ifdef", "preproc_else"),
        calls={"call_expression": "function", "new_expression": "type"},
        inheritance=_s("base_class_clause"),
        imports=_s("preproc_include", "using_declaration"),
    ),
    Language(
        name="go", label="Go", fence="go",
        extensions={".go": "go"},
        functions=_s("function_declaration"),
        classes=_classes(type_spec={"type": ("struct_type", "interface_type")}),
        methods=_s("method_elem"),
        class_bodies=_s("field_declaration_list"),
        containers=_s("type_declaration"),
        receiver_methods={"method_declaration": "receiver"},
        embedded_bases=_s("field_declaration"),
        calls={"call_expression": "function", "composite_literal": "type"},
        imports=_s("import_spec"),
    ),
    Language(
        name="rust", label="Rust", fence="rust",
        extensions={".rs": "rust"},
        functions=_s("function_item"),
        classes=_classes("struct_item", "enum_item", "trait_item", "union_item"),
        methods=_s("function_item", "function_signature_item"),
        containers=_s("mod_item", "declaration_list"),
        impl_blocks={"impl_item": ("type", "trait")},
        calls={"call_expression": "function", "struct_expression": "name"},
        comments=_s("line_comment", "block_comment"),
        imports=_s("use_declaration"),
    ),
)}

PYTHON_EXTENSIONS = (".py",)
_BY_EXTENSION = {ext: lang for lang in LANGUAGES.values() for ext in lang.extensions}
_PARSERS = {}


def registered():
    """Names of every registered language (Python not included: it is always on)."""
    return tuple(LANGUAGES)


def extensions_for(names):
    """File extensions of the named languages."""
    return tuple(ext for name in names for ext in LANGUAGES[name].extensions)


def language_for(path, enabled=None):
    """The Language of a file by extension, or None (Python, or not enabled)."""
    lang = _BY_EXTENSION.get(os.path.splitext(path)[1].lower())
    return lang if lang is not None and (enabled is None or lang.name in enabled) else None


def language_of(meta):
    """'python', 'go', ...: a chunk's language (chunks from v0-v8 have no field: Python)."""
    if meta.get("language"):
        return meta["language"]
    path = meta.get("qname", meta.get("file", "")).split("::")[0]
    lang = language_for(path)
    return lang.name if lang else "python"


def label_of(name):
    return "Python" if name == "python" else LANGUAGES[name].label if name in LANGUAGES else name


def fence_of(meta):
    name = language_of(meta)
    return LANGUAGES[name].fence if name in LANGUAGES else "python"


def parser_for(path):
    """A tree-sitter Parser for `path` (grammar picked by its extension)."""
    grammar = _BY_EXTENSION[os.path.splitext(path)[1].lower()].extensions[
        os.path.splitext(path)[1].lower()]
    if grammar not in _PARSERS:
        from tree_sitter_language_pack import get_parser
        _PARSERS[grammar] = get_parser(grammar)
    return _PARSERS[grammar]


def prefetch(names=None):
    """Download the grammars of `names` (default: all) so indexing works offline."""
    from tree_sitter_language_pack import download
    grammars = sorted({g for n in (names or LANGUAGES) for g in LANGUAGES[n].extensions.values()})
    download(grammars)
    return grammars
