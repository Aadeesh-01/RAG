"""
Command-line interface.

    python -m coderag versions
    python -m coderag chunks   [--repo PATH] [--version v7]
    python -m coderag index    --out DIR [--repo PATH] [--version v7] [--force]
    python -m coderag ask      [QUESTION] [--repo PATH] [--version v7] [--index DIR] [--retrieve-only] ...
    python -m coderag demo     [--repo PATH] [--version v7] [--index DIR]
    python -m coderag graph    [--repo PATH] [-o chunk_graph.html]
"""

import argparse
import os
import sys

from .config import DEFAULT_VERSION, VERSIONS, get_config
from .llm import LLMError

DEFAULT_REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sample_repo")


def _add_common(p):
    p.add_argument("--repo", default=DEFAULT_REPO,
                   help="folder to index (default: bundled sample_repo)")
    p.add_argument("--version", default=DEFAULT_VERSION, choices=list(VERSIONS),
                   help=f"pipeline preset (default: {DEFAULT_VERSION})")
    p.add_argument("-q", "--quiet", action="store_true", help="hide pipeline logging")


def _add_index_dir(p):
    p.add_argument("--index", metavar="DIR",
                   help="reuse the saved index in DIR (built and saved there if missing or stale)")


def _add_retrieval_overrides(p):
    p.add_argument("--top-k", type=int, help="vector-search hits")
    p.add_argument("--max-hops", type=int, help="graph BFS depth (0 = off)")
    p.add_argument("--max-expansion", type=int, help="max graph-expanded chunks")
    p.add_argument("--include-callers", action="store_true", default=None,
                   help="also follow called_by edges")


def _add_llm_options(p):
    p.add_argument("--llm", choices=["local", "groq", "openrouter", "gemini", "none"],
                   default=os.environ.get("CODERAG_LLM"),
                   help="answer with the preset's local model (default), a remote API "
                        "(key from GROQ_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY), "
                        "or none (sources only). Default can be set with CODERAG_LLM.")
    p.add_argument("--model", help="model id for --llm (default: per provider)")
    p.add_argument("--prompt", choices=["legacy", "cited"],
                   help="cited = numbered sources the answer must cite (default for remote "
                        "models; small local models can't follow it, see eval/README.md)")


def _llm_overrides(args):
    from .llm import DEFAULT_REMOTE_MODELS

    llm = getattr(args, "llm", None)
    overrides = {"prompt_style": getattr(args, "prompt", None)}
    if llm == "none":
        overrides["llm_backend"] = "none"
    elif llm in DEFAULT_REMOTE_MODELS:
        model = args.model or DEFAULT_REMOTE_MODELS[llm]
        overrides.update(llm_backend="remote", llm_provider=llm, llm_model=model,
                         max_new_tokens=1024,
                         reasoning_effort="low" if "gpt-oss" in model else None,
                         prompt_style=overrides["prompt_style"] or "cited")
    elif getattr(args, "model", None):
        overrides["llm_model"] = args.model
    return overrides


def _overrides(args):
    return {
        "top_k": args.top_k,
        "max_hops": args.max_hops,
        "max_expansion": args.max_expansion,
        "include_callers": args.include_callers,
    }


def _print_sources(sources):
    print("\nSources:")
    for s in sources:
        hop = "" if s["hop"] == 0 else f"  (call graph, hop {s['hop']})"
        print(f"  [S{s['id']}] {s['file']}:L{s['start_line']}-L{s['end_line']}  {s['name']}{hop}")


def _make_agent(args, overrides=None, load_llm=True):
    from .agent import RAGCodebaseAgent

    config = get_config(args.version, **{**_llm_overrides(args), **(overrides or {})})
    if args.index:
        return RAGCodebaseAgent.load_or_build(args.repo, args.index, config,
                                              load_llm=load_llm, verbose=not args.quiet)
    return RAGCodebaseAgent(args.repo, config, load_llm=load_llm, verbose=not args.quiet)


def cmd_index(args):
    from .agent import RAGCodebaseAgent
    from .persist import check_index

    config = get_config(args.version)
    usable, reason = check_index(args.out, config, args.repo)
    if usable and not args.force:
        print(f"Index in {args.out} is up to date (use --force to rebuild).")
        return
    agent = RAGCodebaseAgent(args.repo, config, load_llm=False, verbose=not args.quiet)
    agent.save(args.out)
    print(f"Indexed {len(agent.documents)} chunks -> {args.out}")


def cmd_versions(args):
    for name, cfg in VERSIONS.items():
        marker = "*" if name == DEFAULT_VERSION else " "
        print(f"{marker} {name}  {cfg.description}")


def cmd_chunks(args):
    from .chunking import load_chunks
    from .graph import build_reverse_index

    config = get_config(args.version)
    chunks = load_chunks(args.repo, config, verbose=not args.quiet)
    if config.structural:
        build_reverse_index(chunks, verbose=not args.quiet)

    for c in chunks:
        where = os.path.relpath(c["file"], args.repo) if c["file"] else ""
        print(f"\n{c['type']:<8} {c['name']:<24} {where}  ({len(c['content'])} chars)")
        for field in ("base_classes", "methods", "calls", "called_by", "also_in"):
            if c.get(field):
                print(f"    {field:<12}: {', '.join(c[field])}")
    print(f"\n{len(chunks)} chunks")


def cmd_ask(args):
    agent = _make_agent(args, _overrides(args), load_llm=not args.retrieve_only)

    def answer(question):
        if args.retrieve_only:
            print(agent.build_context(question))
            return
        sources = []
        for event in agent.stream(question):
            if event["type"] == "sources":
                sources = event["sources"]
                print("\nAnswer:\n", flush=True)
            elif event["type"] == "token":
                print(event["text"], end="", flush=True)
            elif event["type"] == "error":
                print(f"[{event['code']}] {event['message']}")
            elif event["type"] == "done":
                print(f"\n\n(answered by {event['backend']} in {event['llm_ms'] / 1000:.1f}s)")
        _print_sources(sources)

    if args.question:
        answer(args.question)
        return

    print("Interactive mode - empty line or Ctrl+C to quit.")
    try:
        while True:
            question = input("\n? ").strip()
            if not question:
                break
            answer(question)
    except (KeyboardInterrupt, EOFError):
        print()


def cmd_demo(args):
    from .demos import DEMO_QUERIES

    agent = _make_agent(args)
    for question, overrides in DEMO_QUERIES[args.version]:
        answer = agent.query(question, **overrides)
        if args.quiet:
            print(f"\nQ: {question}\nA: {answer}")


def cmd_graph(args):
    from .visualize import write_graph

    write_graph(args.repo, args.output, verbose=not args.quiet)
    print(f"Open {args.output} in a browser.")


def build_parser():
    parser = argparse.ArgumentParser(prog="coderag",
                                     description="Code-aware RAG over a Python repository")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("versions", help="list pipeline presets")
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("chunks", help="show chunks and structural metadata (no models)")
    _add_common(p)
    p.set_defaults(func=cmd_chunks)

    p = sub.add_parser("index", help="build an index and save it for reuse")
    _add_common(p)
    p.add_argument("-o", "--out", required=True, metavar="DIR", help="directory to save into")
    p.add_argument("--force", action="store_true", help="rebuild even if up to date")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("ask", help="ask a question (interactive if none given)")
    p.add_argument("question", nargs="?")
    _add_common(p)
    _add_index_dir(p)
    _add_llm_options(p)
    _add_retrieval_overrides(p)
    p.add_argument("--retrieve-only", action="store_true",
                   help="print the LLM context instead of generating (skips the LLM)")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("demo", help="run the version's original demo queries")
    _add_common(p)
    _add_index_dir(p)
    _add_llm_options(p)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("graph", help="export the interactive chunk graph HTML")
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("-o", "--output", default="chunk_graph.html")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_graph)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (FileNotFoundError, ValueError, LLMError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
