"""The demo queries each original version script ran, as (question, overrides)."""

_GENERAL = [
    ("What does this project do?", {}),
    ("How is data processed?", {}),
    ("Explain the main architecture", {}),
    ("How does the system store data?", {}),
    ("Why is this approach used?", {}),
]

DEMO_QUERIES = {
    "v0": _GENERAL[:2] + [("Which file handles API requests?", {})] + _GENERAL[2:],
    "v1": _GENERAL,
    "v2": _GENERAL,
    "v3": [
        ("What does this project do?", {}),
        ("Explain the main architecture", {}),
        ("How is data processed?", {}),
        ("Why is this approach used?", {}),
    ],
    "v4": [
        ("What does this project do?", {}),
        ("Explain the main architecture", {}),
        ("How is data processed?", {}),
        ("Which functions call the data processing logic?", {}),
        ("What classes inherit from a base class?", {}),
        ("Why is this approach used?", {}),
    ],
    "v5": [
        # full call-chain traversal
        ("What classes inherit from a base class and what do they call?",
         {"max_hops": 3, "max_expansion": 8}),
        # forward + backward traversal
        ("Which functions call the data processing logic?",
         {"max_hops": 3, "max_expansion": 8, "include_callers": True}),
        # no graph expansion (pure vector RAG)
        ("How is data processed?", {"max_hops": 0}),
        # 1-hop only (v4 equivalent)
        ("Explain the main architecture", {"max_hops": 1, "max_expansion": 2}),
    ],
    "v6": [
        ("How is data processed?", {}),
        ("Which functions call normalize_name?", {}),       # caller-aware expansion
        ("What does DataProcessor inherit from and which method does it override?", {}),
        ("Trace what happens to a CSV row from loading until it is stored.", {}),
    ],
}
