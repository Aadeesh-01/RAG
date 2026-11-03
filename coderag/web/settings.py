"""Web server settings, read from CODERAG_* environment variables."""

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Settings:
    data_dir: str = "data"                      # built demo indexes live in <data_dir>/demos
    # Server-side LLM (key from the provider's env var, e.g. GROQ_API_KEY). Models are
    # tried in order; each has its own rate limit on the provider side.
    server_llm_provider: str = "groq"
    server_llm_models: tuple = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
    # Rate limits
    ask_per_ip_per_hour: int = 30               # questions answered with the server key
    own_key_ask_per_ip_per_hour: int = 300      # questions answered with the visitor's key
    retrieve_per_ip_per_hour: int = 600         # retrieval-only requests
    server_daily_cap: int = 800                 # all visitors, server key; keep below quota
    trusted_proxy_hops: int = 0                 # 1 behind one reverse proxy (HF Spaces)
    answer_cache_size: int = 500

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        values = {}
        for f in fields(cls):
            raw = environ.get(f"CODERAG_{f.name.upper()}")
            if raw is None:
                continue
            if f.type in (int, "int"):
                values[f.name] = int(raw)
            elif f.type in (tuple, "tuple"):
                values[f.name] = tuple(v.strip() for v in raw.split(",") if v.strip())
            else:
                values[f.name] = raw
        return cls(**values)
