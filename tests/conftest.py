import os
import shutil
import zlib

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_REPO = os.path.join(ROOT, "sample_repo")


class HashEncoder:
    """Deterministic bag-of-words embedding: no model download, real FAISS search."""

    def __init__(self):
        self.encoded = 0

    def encode(self, texts, show_progress_bar=False):
        self.encoded += len(texts)
        out = np.zeros((len(texts), 64), dtype="float32")
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, zlib.crc32(word.encode()) % 64] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


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
    name = "fake"

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def stream(self, context, question, config=None):
        self.calls.append((context, question))
        text = self.reply or f"answer to {question}"
        yield from (text[i:i + 4] for i in range(0, len(text), 4))    # several tokens

    def generate(self, context, question, config=None):
        return "".join(self.stream(context, question, config))


@pytest.fixture
def sample_repo():
    return SAMPLE_REPO


@pytest.fixture
def zoo_repo(tmp_path):
    """Just the original toy inheritance file."""
    shutil.copy(os.path.join(SAMPLE_REPO, "test_inheritance.py"), tmp_path)
    return str(tmp_path)
