import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from coderag import RAGCodebaseAgent, get_config  # noqa: E402
from coderag.llm import LLMRateLimited  # noqa: E402
from coderag.web.app import create_app  # noqa: E402
from coderag.web.limits import RateLimiter, client_ip  # noqa: E402
from coderag.web.registry import Demo  # noqa: E402
from coderag.web.settings import Settings  # noqa: E402
from conftest import FakeIndex, FakeLLM  # noqa: E402


class Factory:
    """Records which LLM choice each request asked for; never sees the network."""

    def __init__(self, llm=None):
        self.llm = llm or FakeLLM("DataProcessor.transform calls it [S1]")
        self.choices = []
        self.server_available = True

    def __call__(self, choice):
        self.choices.append(choice)
        return None if choice.provider == "none" else self.llm


def make_client(sample_repo, factory=None, **settings):
    agent = RAGCodebaseAgent(sample_repo, get_config("v7"), index=FakeIndex(),
                             load_llm=False, verbose=False)
    demo = Demo(id="sample", name="Sample", url="https://github.com/o/r", sha="a" * 40,
                root="src", questions=["q?"], agents={"v7": agent})
    factory = factory or Factory()
    app = create_app(Settings(**settings), demos={"sample": demo}, llm_factory=factory,
                     limiter=RateLimiter())
    return TestClient(app), factory


def sse_events(response):
    events = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
def client(sample_repo):
    return make_client(sample_repo)


def test_health_and_demos(client):
    c, _ = client
    assert c.get("/api/health").json()["demos"] == ["sample"]
    [demo] = c.get("/api/demos").json()
    assert demo["id"] == "sample" and demo["versions"] == ["v7"]
    assert c.get("/api/repos/sample").json()["questions"] == ["q?"]


def test_unknown_repo_and_version(client):
    c, _ = client
    assert c.get("/api/repos/nope").status_code == 404
    r = c.post("/api/repos/sample/retrieve", json={"question": "x", "version": "v1"})
    assert r.status_code == 400 and "available: v7" in r.json()["error"]


def test_validation(client):
    c, _ = client
    assert c.post("/api/repos/sample/ask", json={"question": ""}).status_code == 422
    assert c.post("/api/repos/sample/ask", json={"question": "x" * 501}).status_code == 422
    assert c.post("/api/repos/sample/ask",
                  json={"question": "x", "top_k": 99}).status_code == 422


def test_retrieve_returns_linked_sources(client):
    c, _ = client
    body = c.post("/api/repos/sample/retrieve", json={"question": "normalize names"}).json()
    s = body["sources"][0]
    assert body["version"] == "v7" and "context" not in body
    assert s["url"] == (f"https://github.com/o/r/blob/{'a' * 40}/src/{s['file']}"
                        f"#L{s['start_line']}-L{s['end_line']}")
    with_ctx = c.post("/api/repos/sample/retrieve",
                      json={"question": "x", "include_context": True}).json()
    assert with_ctx["context"].startswith("# ")


def test_ask_streams_sources_tokens_done(client):
    c, _ = client
    r = c.post("/api/repos/sample/ask", json={"question": "Who calls normalize_name?"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r)
    kinds = [k for k, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and set(kinds[1:-1]) == {"token"}
    answer = "".join(d["text"] for k, d in events if k == "token")
    assert answer == "DataProcessor.transform calls it [S1]"
    assert events[-1][1]["cited"] == [1]
    assert events[0][1]["sources"][0]["chunk_id"] >= 0


def test_sources_match_graph_nodes(client):
    c, _ = client
    graph = c.get("/api/repos/sample/graph").json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert {l["type"] for l in graph["links"]} >= {"calls", "inherits", "member"}
    sources = c.post("/api/repos/sample/retrieve", json={"question": "bat"}).json()["sources"]
    for s in sources:
        assert nodes[s["chunk_id"]]["qname"] == s["qname"]


def test_no_llm_still_returns_sources(sample_repo):
    c, _ = make_client(sample_repo)
    events = sse_events(c.post("/api/repos/sample/ask",
                               json={"question": "x", "llm": {"provider": "none"}}))
    assert [k for k, _ in events] == ["sources", "error"]
    assert events[1][1]["code"] == "no_llm"


def test_provider_rate_limit_becomes_error_event(sample_repo):
    class Busy:
        name = "busy"

        def stream(self, context, question, config=None):
            raise LLMRateLimited("slow down", 5)
            yield

    c, _ = make_client(sample_repo, Factory(Busy()))
    events = sse_events(c.post("/api/repos/sample/ask", json={"question": "x"}))
    assert [k for k, _ in events] == ["sources", "error"]
    assert events[1][1]["code"] == "rate_limited"


def test_per_ip_rate_limit(sample_repo):
    c, _ = make_client(sample_repo, ask_per_ip_per_hour=2, answer_cache_size=0)
    for _ in range(2):
        assert c.post("/api/repos/sample/ask", json={"question": "x"}).status_code == 200
    r = c.post("/api/repos/sample/ask", json={"question": "x"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # retrieval has its own budget
    assert c.post("/api/repos/sample/retrieve", json={"question": "x"}).status_code == 200


def test_server_daily_cap_degrades_to_sources(sample_repo):
    c, _ = make_client(sample_repo, server_daily_cap=1, answer_cache_size=0)
    assert sse_events(c.post("/api/repos/sample/ask", json={"question": "a"}))[-1][0] == "done"
    events = sse_events(c.post("/api/repos/sample/ask", json={"question": "b"}))
    assert [k for k, _ in events] == ["sources", "error"]
    assert events[1][1]["code"] == "server_quota"


def test_own_key_bypasses_server_cap_and_is_never_echoed(sample_repo):
    c, factory = make_client(sample_repo, server_daily_cap=0, answer_cache_size=0)
    secret = "gsk_" + "s3cr3t" * 8
    r = c.post("/api/repos/sample/ask",
               json={"question": "x", "llm": {"provider": "groq", "api_key": secret}})
    assert sse_events(r)[-1][0] == "done"
    assert factory.choices[-1].api_key.get_secret_value() == secret
    assert secret not in repr(factory.choices[-1]) and secret not in r.text


def test_cache_replays_without_calling_llm(sample_repo):
    c, factory = make_client(sample_repo)
    first = sse_events(c.post("/api/repos/sample/ask", json={"question": "Who calls X?"}))
    second = sse_events(c.post("/api/repos/sample/ask", json={"question": "  who CALLS x? "}))
    assert len(factory.llm.calls) == 1
    assert second[-1][1]["cached"] is True
    assert "".join(d["text"] for k, d in second if k == "token") == \
        "".join(d["text"] for k, d in first if k == "token")
    # different settings are a different answer
    c.post("/api/repos/sample/ask", json={"question": "Who calls X?", "max_hops": 0})
    assert len(factory.llm.calls) == 2


def test_client_ip_trusts_only_proxy_hops():
    class Req:
        def __init__(self, xff):
            self.headers = {"x-forwarded-for": xff}
            self.client = type("C", (), {"host": "10.0.0.1"})

    forged = Req("6.6.6.6, 203.0.113.9")       # client sent 6.6.6.6, proxy appended the real one
    assert client_ip(forged, 1) == "203.0.113.9"
    assert client_ip(forged, 0) == "10.0.0.1"
    assert client_ip(Req(""), 1) == "10.0.0.1"


def test_settings_from_env():
    s = Settings.from_env({"CODERAG_ASK_PER_IP_PER_HOUR": "5",
                           "CODERAG_SERVER_LLM_MODELS": "a, b",
                           "CODERAG_DATA_DIR": "/srv"})
    assert s.ask_per_ip_per_hour == 5 and s.server_llm_models == ("a", "b")
    assert s.data_dir == "/srv"
