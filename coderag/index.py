"""Dense vector index: sentence-transformers embeddings in a flat FAISS index."""

import numpy as np


class VectorIndex:

    def __init__(self, model_name="all-MiniLM-L6-v2", model=None):
        """`model`: an already-loaded SentenceTransformer to share between indexes."""
        if model is None:
            # heavy imports are lazy so chunking/graph code works without ML deps
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(model_name)
        self.model = model
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
