"""Remote backend, fallback chain and the structured ask/stream API, without network."""

import json

import pytest

from coderag import RAGCodebaseAgent, get_config
from coderag.context import parse_citations
from coderag.llm import (FallbackChain, LLMError, LLMRateLimited, LLMUnavailable, RemoteLLM,
                         build_messages)
from conftest import FakeIndex, FakeLLM

REMOTE = get_config("v7", llm_backend="remote", llm_provider="groq", llm_model="m",
                    reasoning_effort="low")


def sse(*pieces):
    """A fake transport streaming `pieces` as OpenAI-style SSE lines."""
    def transport(url, headers, payload, timeout):
        transport.calls.append((url, headers, payload))
        yield ": keep-alive comment"
        for p in pieces:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": p}}]})
            yield ""
        yield "data: [DONE]"
    transport.calls = []
    return transport


def failing(error, after=()):
    def transport(url, headers, payload, timeout):
        for p in after:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": p}}]})
        raise error
    return transport


def test_remote_streams_and_builds_request():
    transport = sse("Hello", " world")
    llm = RemoteLLM(REMOTE, api_key="secret", transport=transport)
    assert list(llm.stream("ctx", "q?")) == ["Hello", " world"]

    url, headers, payload = transport.calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    assert payload["model"] == "m" and payload["stream"] is True
    assert payload["reasoning_effort"] == "low"
    assert "secret" not in repr(llm)


def test_remote_requires_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMUnavailable, match="GROQ_API_KEY"):
        RemoteLLM(REMOTE)


def test_remote_error_event_raises():
    def transport(url, headers, payload, timeout):
        yield 'data: {"error": {"message": "model not found"}}'
    with pytest.raises(LLMError, match="model not found"):
        list(RemoteLLM(REMOTE, api_key="k", transport=transport).stream("c", "q"))


def test_cited_prompt_messages():
    cited = REMOTE.with_overrides(prompt_style="cited")
    system, user = build_messages(cited, "CTX", "Q?")
    assert "[S<n>]" in system["content"] and "guess" in system["content"]
    assert user["content"].startswith("Sources:") and user["content"].endswith("Question: Q?")
    assert build_messages(REMOTE, "CTX", "Q?")[1]["content"].startswith("Code context:")


def test_fallback_skips_unavailable_backends():
    first = RemoteLLM(REMOTE, api_key="k", transport=failing(LLMRateLimited("slow down", 3)))
    second = RemoteLLM(REMOTE.with_overrides(llm_model="backup"), api_key="k",
                       transport=sse("ok"))
    chain = FallbackChain([first, second])
    assert chain.generate("c", "q") == "ok"
    assert chain.name == "groq/backup"


def test_fallback_never_switches_mid_answer():
    first = RemoteLLM(REMOTE, api_key="k",
                      transport=failing(LLMUnavailable("dropped"), after=["partial "]))
    chain = FallbackChain([first, RemoteLLM(REMOTE, api_key="k", transport=sse("other"))])
    with pytest.raises(LLMUnavailable):
        list(chain.stream("c", "q"))


def test_fallback_does_not_hide_bad_key():
    first = RemoteLLM(REMOTE, api_key="k", transport=failing(LLMError("API key rejected")))
    chain = FallbackChain([first, RemoteLLM(REMOTE, api_key="k", transport=sse("x"))])
    with pytest.raises(LLMError, match="rejected"):
        list(chain.stream("c", "q"))


def test_parse_citations():
    assert parse_citations("A [S2] and [S1, S3]; bad [S9] [S2]", 4) == ([1, 2, 3], [9])
    assert parse_citations(None, 3) == ([], [])
    # gpt-oss style: full-width brackets and zero-width spaces
    assert parse_citations("uses it【S7】 and [​S3] and [​S1,​S2]", 8) == (
        [1, 2, 3, 7], [])


# ── agent.stream / ask ──────────────────────────────────────────────────────
def agent(repo, llm=None, **overrides):
    return RAGCodebaseAgent(repo, get_config("v7", **overrides), index=FakeIndex(),
                            llm=llm, load_llm=False, verbose=False)


def test_stream_emits_sources_first_then_tokens(zoo_repo):
    events = list(agent(zoo_repo, FakeLLM("Zoo calls Bat [S1]")).stream("zoo?"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "sources" and kinds[-1] == "done"
    assert set(kinds[1:-1]) == {"token"}
    first = events[0]["sources"][0]
    assert first["id"] == 1 and first["file"] == "test_inheritance.py" and first["start_line"] > 0
    assert events[-1]["cited"] == [1]


def test_ask_returns_structured_result(zoo_repo):
    result = agent(zoo_repo, FakeLLM("See [S1] and [S2]; not [S99]")).ask(
        "zoo?", prompt_style="cited")
    assert result.answer == "See [S1] and [S2]; not [S99]"
    assert result.cited == [1, 2] and result.invalid_citations == [99]
    assert result.backend == "fake" and result.error is None
    assert result.context.startswith("[S1] test_inheritance.py:L")
    assert "llm_ms" in result.timings and "retrieve_ms" in result.timings


def test_ask_without_llm_degrades_to_sources(zoo_repo):
    result = agent(zoo_repo).ask("zoo?")
    assert result.answer is None and result.error["code"] == "no_llm"
    assert result.sources


def test_ask_reports_rate_limit(zoo_repo):
    llm = RemoteLLM(REMOTE, api_key="k", transport=failing(LLMRateLimited("slow", 7)))
    result = agent(zoo_repo, llm).ask("zoo?")
    assert result.answer is None
    assert result.error == {"type": "error", "code": "rate_limited", "message": "slow",
                            "retry_after": 7}
