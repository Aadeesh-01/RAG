"""
Pipeline configuration.

Every research version (v0 baseline ... v5 multi-hop) is a preset of one
RAGConfig, so comparing versions is a config change instead of a separate
copy of the whole pipeline.
"""

from dataclasses import dataclass, fields, replace

# Directories never worth indexing (pruned in-place during os.walk)
SKIP_DIRS = frozenset({
    "venv", ".venv", "env", ".env",
    ".git", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", "dist", "build", "site-packages",
    ".tox", "eggs", ".eggs",
})

BASELINE_EXTENSIONS = (".py", ".js", ".html", ".css", ".json", ".md", ".txt")

QWEN_CODER = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
FLAN_T5 = "google/flan-t5-base"

FLAN_PROMPT = """
Use ONLY the given context to answer the question.

Context:
{context}

Question:
{question}

Answer:
"""

SYSTEM_PROMPT_BASIC = (
    "You are a senior software engineer. "
    "Answer questions about the codebase using ONLY the provided code context. "
    "Be concise and specific. Do NOT repeat the code verbatim."
)

SYSTEM_PROMPT_STRUCTURAL = (
    "You are a senior software engineer. "
    "Answer questions about the codebase using ONLY the provided code context. "
    "Use the structural annotations (extends, calls, called_by, methods) "
    "to reason about relationships between components. "
    "Be concise and specific. Do NOT repeat the code verbatim."
)

SYSTEM_PROMPT_MULTIHOP = (
    "You are a senior software engineer. "
    "Answer questions about the codebase using ONLY the provided code context. "
    "Use the structural annotations (extends, calls, called_by, methods, retrieval) "
    "to reason about relationships between components. "
    "Chunks marked [retrieval: direct] are the most relevant; "
    "chunks marked [retrieval: hop-N] provide supporting context. "
    "Be concise and specific. Do NOT repeat the code verbatim."
)


SYSTEM_PROMPT_CITED = (
    "You answer questions about a Python codebase using ONLY the numbered sources provided. "
    "Each source is labelled [S<n>] with its file, line range and qualified name; its "
    "'calls' and 'called_by' lines list call relationships resolved from the code.\n"
    "Rules:\n"
    "- Cite the sources that support each claim inline, like [S2] or [S1, S4].\n"
    "- Name functions, methods and classes exactly as written in the sources "
    "(e.g. `Session.send`).\n"
    "- Only state that X calls Y if a source's code or its calls/called_by line shows it.\n"
    "- If the sources do not contain the answer, say so plainly instead of guessing.\n"
    "- Be concise."
)


@dataclass(frozen=True)
class RAGConfig:
    name: str
    description: str = ""

    # ── Loading / chunking ──────────────────────────────────────────────────
    chunker: str = "ast"                    # "fixed" (character windows) | "ast"
    extensions: tuple = (".py",)
    prune_dirs: bool = True                 # skip SKIP_DIRS and dot-directories
    chunk_size: int = 1000                  # fixed chunker only
    chunk_overlap: int = 200                # fixed chunker only
    max_file_chars: int = 30_000            # ast: skip larger (generated) files
    min_segment_chars: int = 60             # ast: skip trivial class/function stubs
    min_chunk_chars: int = 80               # drop chunks with len(content) <= this
    chunk_methods: bool = False             # class outline chunk + one chunk per method
    dedup_content: bool = False             # merge chunks with same name + normalised hash
    structural: bool = False                # reverse index + docstring-aware embeddings
    call_resolution: str = "name"           # "name" (bare-name match) | "scoped" (resolve.py)
    max_ambiguous: int = 0                  # scoped: link obj.foo() to <= this many classes

    # ── Embedding ───────────────────────────────────────────────────────────
    embed_model: str = "all-MiniLM-L6-v2"

    # ── Retrieval ───────────────────────────────────────────────────────────
    top_k: int = 4
    dedup_prefix: bool = True               # drop hits whose first 80 chars repeat
    keyword_filter: bool = False
    max_direct: int | None = None           # cap on vector hits after filtering
    max_hops: int = 0                       # graph BFS depth (0 = no expansion)
    max_expansion: int = 0                  # cap on graph-expanded chunks
    include_callers: bool = False           # also follow called_by edges
    auto_callers: bool = False              # ...but only for "who calls X?" questions

    # ── Context ─────────────────────────────────────────────────────────────
    header: str = "file"                    # "none" | "name" | "file" | "rich"
    hop_label: bool = False                 # rich header shows [retrieval: hop-N]
    clean_chunks: bool = False
    chars_per_chunk: int | None = 350       # None = whole chunk
    hop_budget_decay: bool = False          # halve the budget per hop
    min_chars_per_chunk: int = 80           # floor for the decayed budget

    # ── LLM ─────────────────────────────────────────────────────────────────
    llm_backend: str = "chat"               # "seq2seq" (flan-t5) | "chat" (local) | "remote" | "none"
    llm_provider: str = ""                  # remote: groq | openrouter | gemini
    llm_model: str = QWEN_CODER             # HF id (local) or provider model id (remote)
    reasoning_effort: str | None = None     # remote reasoning models: low | medium | high
    prompt_style: str = "legacy"            # "legacy" (code context) | "cited" (numbered sources)
    system_prompt: str = SYSTEM_PROMPT_BASIC
    max_input_tokens: int = 2048
    max_new_tokens: int = 256
    device: str = "cpu"

    def with_overrides(self, **overrides):
        """Return a copy with the non-None overrides applied."""
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})

    @classmethod
    def from_dict(cls, data):
        """Inverse of dataclasses.asdict after a JSON round trip (lists back to tuples)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: tuple(v) if isinstance(v, list) else v
                      for k, v in data.items() if k in known})

    def index_mismatch(self, other):
        """INDEX_FIELDS on which `other` differs, i.e. would need a different index."""
        return [f for f in INDEX_FIELDS if getattr(self, f) != getattr(other, f)]


# Fields that shape the saved index (chunks, embeddings, call graph). Everything
# else (retrieval depth, context budget, LLM) can change on an existing index.
INDEX_FIELDS = (
    "chunker", "extensions", "prune_dirs", "chunk_size", "chunk_overlap",
    "max_file_chars", "min_segment_chars", "min_chunk_chars", "chunk_methods",
    "dedup_content", "structural", "call_resolution", "max_ambiguous", "embed_model",
)


_V1 = RAGConfig(
    name="v1",
    description="AST chunking (one chunk per top-level class/function) + Qwen2.5-Coder",
)

_V2 = replace(
    _V1, name="v2",
    description="v1 + keyword filter on vector hits, keep top 3",
    keyword_filter=True, max_direct=3,
)

_V3 = replace(
    _V2, name="v3",
    description="v2 + context cleaning (signatures, returns, keyword lines)",
    clean_chunks=True, header="name",
)

_V4 = replace(
    _V3, name="v4",
    description="v3 + structural metadata, dedup, called_by index, 1-hop graph expansion",
    dedup_content=True, structural=True,
    max_hops=1, max_expansion=2,
    header="rich", chars_per_chunk=400,
    system_prompt=SYSTEM_PROMPT_STRUCTURAL,
)

_V5 = replace(
    _V4, name="v5",
    description="v4 + multi-hop BFS expansion with hop-weighted context budget",
    min_segment_chars=10,
    max_hops=3, max_expansion=8,
    hop_label=True, hop_budget_decay=True,
    system_prompt=SYSTEM_PROMPT_MULTIHOP,
)

_V6 = replace(
    _V5, name="v6",
    description="v5 + index large files, method-level chunks, bigger raw context, "
                "caller-aware expansion (fixes measured in eval/)",
    max_file_chars=1_000_000,           # v1-v5 silently skipped files > 30k chars
    chunk_methods=True,
    keyword_filter=False, max_direct=None,
    clean_chunks=False,
    chars_per_chunk=1500,
    auto_callers=True,
    max_input_tokens=8192,              # Qwen2.5 handles 32k; context is trimmed to fit
)

_V7 = replace(
    _V6, name="v7",
    description="v6 + scope-aware call resolution (imports, self/super, external "
                "modules) instead of bare-name matching",
    call_resolution="scoped",
)

_V0 = RAGConfig(
    name="v0",
    description="Baseline: concatenate all files, fixed 1000-char chunks, flan-t5-base",
    chunker="fixed", extensions=BASELINE_EXTENSIONS, prune_dirs=False,
    min_chunk_chars=0,
    top_k=5, dedup_prefix=False,
    header="none", chars_per_chunk=None,
    llm_backend="seq2seq", llm_model=FLAN_T5,
    max_input_tokens=1024, max_new_tokens=100,
)

VERSIONS = {c.name: c for c in (_V0, _V1, _V2, _V3, _V4, _V5, _V6, _V7)}
DEFAULT_VERSION = "v7"


def get_config(version=DEFAULT_VERSION, **overrides):
    try:
        base = VERSIONS[version]
    except KeyError:
        raise ValueError(f"Unknown version {version!r}; choose from {', '.join(VERSIONS)}")
    return base.with_overrides(**overrides)
