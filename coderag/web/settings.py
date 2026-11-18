"""Web server settings, read from CODERAG_* environment variables."""

import os
from dataclasses import dataclass, field, fields
from urllib.parse import urlsplit

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class ConfigError(RuntimeError):
    """The settings are unsafe or incomplete; the server refuses to start."""


@dataclass(frozen=True)
class Settings:
    data_dir: str = "data"                      # visitor indexes, clones, SQLite (a persistent disk)
    demos_dir: str = ""                         # curated demo indexes; "" = <data_dir>/demos
    database_url: str = ""                      # CODERAG_DATABASE_URL; "" = sqlite:///<data_dir>/coderag.db
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
    # Indexing visitors' GitHub repos
    index_per_ip_per_hour: int = 3              # new repos (already indexed ones are free)
    submit_per_ip_per_hour: int = 30            # URL submissions, including cached repos
    index_per_user_per_hour: int = 3            # the same limits per signed-in user
    submit_per_user_per_hour: int = 30
    index_timeout: int = 600                    # seconds a job may take in all (fetch, parse, embed)
    max_index_queue: int = 10
    max_loaded_repos: int = 6                   # visitor repos kept in memory (LRU)
    max_stored_repos: int = 100                 # visitor repo indexes kept on disk
    max_stored_mb: int = 0                      # and their total size on disk (0 = no size cap)
    max_py_files: int = 2000
    max_py_mb: int = 25
    fetch_timeout: int = 120
    index_ignore_dirs: tuple = ("tests", "test", "docs", "examples", "benchmarks")
    default_version: str = "v10"                # preset new visitor repos are indexed with
    live_check: bool = False                    # "Check if live": server-side HEAD to a repo's homepage
    max_concurrent_asks: int = 8                # answer streams at once; more get 503
    ask_timeout: int = 90                       # seconds an answer stream may run in all
    docs: bool = False                          # /docs, /redoc, /openapi.json
    allow_no_proxy: bool = False                # https PUBLIC_URL without TRUSTED_PROXY_HOPS (see validate)
    # GitHub sign-in (all three needed; without them the site works signed out)
    github_client_id: str = ""
    github_client_secret: str = field(default="", repr=False)
    session_secret: str = field(default="", repr=False)    # signs cookies (>= 32 characters)
    token_key: str = field(default="", repr=False)         # encrypts stored GitHub tokens (>= 32 characters)
    public_url: str = ""                                   # e.g. https://coderag.example.com

    @property
    def demos_path(self):
        return self.demos_dir or os.path.join(self.data_dir, "demos")

    def validate(self):
        """
        Refuse unsafe or half-done settings (ConfigError, one line per problem).
        Returns warnings to log for allowed-but-risky ones.
        """
        errors, warnings = self.problems()
        if errors:
            raise ConfigError("coderag will not start:\n  - " + "\n  - ".join(errors))
        return warnings

    def problems(self):
        """(errors, warnings) of the start-up rules (validate; scripts/predeploy_check.py)."""
        errors, warnings = [], []
        github = {"CODERAG_GITHUB_CLIENT_ID": self.github_client_id,
                  "CODERAG_GITHUB_CLIENT_SECRET": self.github_client_secret}
        if any(github.values()):
            errors += [f"{name} is missing (GitHub sign-in needs both GitHub settings)"
                       for name, value in github.items() if not value]
            for name, value in (("CODERAG_SESSION_SECRET", self.session_secret),
                                ("CODERAG_TOKEN_KEY", self.token_key)):
                if len(value) < 32:
                    errors.append(f"{name} must be at least 32 characters with GitHub sign-in "
                                  f"(python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
            if self.token_key and self.token_key == self.session_secret:
                errors.append("CODERAG_TOKEN_KEY must differ from CODERAG_SESSION_SECRET")
        if self.public_url:
            parts = urlsplit(self.public_url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                errors.append(f"CODERAG_PUBLIC_URL is not a URL: {self.public_url!r}")
            elif parts.scheme != "https" and parts.hostname not in LOCAL_HOSTS:
                errors.append("CODERAG_PUBLIC_URL must be https:// (http is only for localhost/127.0.0.1)")
            elif parts.scheme == "https" and self.trusted_proxy_hops <= 0:
                message = ("CODERAG_PUBLIC_URL is https but CODERAG_TRUSTED_PROXY_HOPS is 0: behind a TLS proxy "
                           "every visitor would share the proxy's address and one set of rate limits")
                if self.allow_no_proxy:
                    warnings.append(message + " (allowed by CODERAG_ALLOW_NO_PROXY=1)")
                else:
                    errors.append(message + ". Set CODERAG_TRUSTED_PROXY_HOPS=1, or CODERAG_ALLOW_NO_PROXY=1 "
                                            "if TLS really ends at this server")
        return errors, warnings

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
            elif f.type in (bool, "bool"):
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif f.type in (tuple, "tuple"):
                values[f.name] = tuple(v.strip() for v in raw.split(",") if v.strip())
            else:
                values[f.name] = raw
        return cls(**values)
