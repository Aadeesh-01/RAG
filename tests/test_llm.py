from coderag.context import asks_for_callers
from coderag.llm import TRUNCATION_MARK, fit_context


class CharTokenizer:
    """One token per character - enough to test budgeting without a model."""

    def __call__(self, text, add_special_tokens=True):
        return {"input_ids": list(text)}

    def decode(self, ids):
        return "".join(ids)


def build(ctx):
    return f"SYSTEM\n{ctx}\nQuestion: why?"


def test_fit_context_keeps_short_context():
    assert fit_context(CharTokenizer(), build, "short", max_tokens=1000) == "short"


def test_fit_context_trims_context_not_question():
    context = "x" * 500
    fitted = fit_context(CharTokenizer(), build, context, max_tokens=100)
    prompt = build(fitted)
    assert len(prompt) <= 100
    assert prompt.endswith("Question: why?")
    assert fitted.endswith(TRUNCATION_MARK)
    assert fitted.startswith("x")


def test_caller_intent():
    for q in ["Which functions call get_netrc_auth?", "Who calls resolve_redirects?",
              "Who uses SQLiteStore?", "Which code uses super_len to size a body?",
              "What calls create_bat?", "callers of foo", "Where is parse_args used?",
              "Is this function called by the CLI?"]:
        assert asks_for_callers(q), q
    for q in ["What happens under the hood when you call requests.get?",
              "How does Command.main run the command?", "Which code reads a CSV file?",
              "How is the digest authentication header computed?"]:
        assert not asks_for_callers(q), q
