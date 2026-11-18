# coderag web server: one container, CPU only (Hugging Face Spaces, Docker SDK, or any host).
#
#   docker build -t coderag .
#   docker run --rm -p 7860:7860 --env-file deploy/local.env coderag
#
# Everything the server needs at runtime is baked in at build time: the locked
# dependencies (CPU-only torch), the embedding model (HF_HOME, then offline), the
# tree-sitter grammars and the demo indexes (CODERAG_DEMOS_DIR, outside the data dir,
# so a volume mounted at /data never hides them). Settings come from the environment
# (README "Deploy"); the server refuses to start with unsafe ones.

# ── build: dependencies, model, grammars, demos ──────────────────────────────
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/tslp PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes -r /tmp/requirements.lock

WORKDIR /app
COPY coderag ./coderag
COPY scripts ./scripts
COPY sample_repo ./sample_repo
COPY demos.yaml pyproject.toml ./

# the embedding model and every grammar, so the server never downloads at runtime
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
 && python -c "from coderag.languages import prefetch; print(len(prefetch()), 'grammars')"

# the demos (psf/requests, pallets/click are fetched at their pinned commits)
RUN python scripts/build_demos.py --data-dir /tmp/demo-build --demos-dir /opt/demos \
 && rm -rf /tmp/demo-build

# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# git: fetching visitors' repositories (never run, only parsed)
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 user \
 && mkdir -p /data && chown user:user /data

COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=user:user /opt/hf /opt/hf
COPY --from=build --chown=user:user /opt/tslp /opt/tslp
# demos are written to only as a cache of source files for the code viewer
COPY --from=build --chown=user:user /opt/demos /opt/demos

WORKDIR /app
COPY coderag ./coderag
COPY frontend ./frontend
COPY scripts ./scripts
COPY demos.yaml pyproject.toml ./

ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/tslp \
    CODERAG_DATA_DIR=/data CODERAG_DEMOS_DIR=/opt/demos \
    CODERAG_TRUSTED_PROXY_HOPS=1 FORWARDED_ALLOW_IPS=* \
    PORT=7860

USER user
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/ready' % os.environ.get('PORT', '7860'), timeout=4)"

# one worker: the indexing queue, rate limits and caches live in memory
CMD ["sh", "-c", "exec uvicorn coderag.web.app:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --no-server-header --proxy-headers"]
