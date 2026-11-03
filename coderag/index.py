"""Dense vector index: sentence-transformers embeddings in a flat FAISS index."""

import os

import numpy as np

INDEX_FILE = "faiss.index"


def _load_model(model_name):
    # heavy imports are lazy so chunking/graph code works without ML deps
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name)


class VectorIndex:

    def __init__(self, model_name="all-MiniLM-L6-v2", model=None):
        """`model`: an already-loaded SentenceTransformer to share between indexes."""
        self.model_name = model_name
        self.model = model if model is not None else _load_model(model_name)
        self.index = None

    def build(self, texts, show_progress=False):
        import faiss

        embeddings = self.model.encode(texts, show_progress_bar=show_progress)
        self.index = faiss.IndexFlatL2(embeddings.shape[1])
        self.index.add(np.asarray(embeddings, dtype="float32"))

    def search(self, query, k):
        """Return the indices of the `k` nearest texts, closest first."""
        k = min(k, self.index.ntotal)
        q_emb = self.model.encode([query])
        _, indices = self.index.search(np.asarray(q_emb, dtype="float32"), k)
        return [int(i) for i in indices[0] if i >= 0]

    @property
    def size(self):
        return self.index.ntotal

    def save(self, directory):
        import faiss

        # serialize to bytes ourselves: faiss.write_index can't open non-ASCII paths on Windows
        with open(os.path.join(directory, INDEX_FILE), "wb") as f:
            f.write(faiss.serialize_index(self.index).tobytes())

    @classmethod
    def load(cls, directory, model_name, model=None):
        import faiss

        with open(os.path.join(directory, INDEX_FILE), "rb") as f:
            data = np.frombuffer(f.read(), dtype="uint8")
        vi = cls(model_name, model)
        vi.index = faiss.deserialize_index(data)
        return vi
