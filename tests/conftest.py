import os
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_REPO = os.path.join(ROOT, "sample_repo")


class FakeIndex:
    """Stands in for the FAISS index: returns a fixed ranking."""

    def __init__(self, ranking=None):
        self.ranking = ranking

    def build(self, texts):
        self.texts = texts

    def search(self, query, k):
        ranking = self.ranking if self.ranking is not None else range(len(self.texts))
        return list(ranking)[:k]


class FakeLLM:
    def __init__(self):
        self.calls = []

    def generate(self, context, question):
        self.calls.append((context, question))
        return f"answer to {question}"


@pytest.fixture
def sample_repo():
    return SAMPLE_REPO


@pytest.fixture
def zoo_repo(tmp_path):
    """Just the original toy inheritance file."""
    shutil.copy(os.path.join(SAMPLE_REPO, "test_inheritance.py"), tmp_path)
    return str(tmp_path)
