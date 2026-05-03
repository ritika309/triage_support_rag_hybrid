"""Hybrid retriever: BM25 + dense embeddings + Reciprocal Rank Fusion.

Index is built once from `corpus.load_chunks()` and persisted to .cache/.
Queries are filtered by `company` metadata before scoring.
"""
from __future__ import annotations

import json
import pickle
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from .config import SETTINGS
from .corpus import Chunk, load_chunks

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RRF_K = 60   # standard RRF constant


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class Hit:
    chunk: Chunk
    score: float          # fused RRF score (higher is better)
    bm25_rank: int        # 1-indexed; 0 if absent
    dense_rank: int       # 1-indexed; 0 if absent


class Index:
    def __init__(
        self,
        chunks: list[Chunk],
        bm25: BM25Okapi,
        dense: np.ndarray,
        embed_model_name: str,
    ) -> None:
        self.chunks = chunks
        self.bm25 = bm25
        self.dense = dense                       # (N, D), L2-normalized
        self.embed_model_name = embed_model_name
        # company -> list[int] of indices
        self._by_company: dict[str, list[int]] = {}
        for i, c in enumerate(chunks):
            self._by_company.setdefault(c.company, []).append(i)
        self._encoder = None  # lazy

    def _encode_query(self, query: str) -> np.ndarray:
        if self._encoder is None:
            from sentence_transformers import SentenceTransformer
            self._encoder = SentenceTransformer(self.embed_model_name)
        v = self._encoder.encode(
            [query], normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(v, dtype=np.float32)[0]

    def _company_filter(self, company: str | None) -> np.ndarray:
        if company and company in self._by_company:
            return np.asarray(self._by_company[company], dtype=np.int64)
        return np.arange(len(self.chunks), dtype=np.int64)

    def search(
        self, query: str, company: str | None, top_k: int = 8
    ) -> list[Hit]:
        idx = self._company_filter(company)
        if idx.size == 0:
            return []

        # BM25
        q_tokens = _tokenize(query)
        bm25_scores_full = np.asarray(self.bm25.get_scores(q_tokens))
        bm25_scores = bm25_scores_full[idx]
        bm25_top = idx[np.argsort(-bm25_scores)[: min(20, idx.size)]]

        # Dense
        qv = self._encode_query(query)
        sub = self.dense[idx]
        dense_scores = sub @ qv
        dense_top = idx[np.argsort(-dense_scores)[: min(20, idx.size)]]

        # Reciprocal Rank Fusion
        rrf: dict[int, float] = {}
        bm25_rank: dict[int, int] = {}
        dense_rank: dict[int, int] = {}
        for rank, i in enumerate(bm25_top, start=1):
            rrf[int(i)] = rrf.get(int(i), 0.0) + 1.0 / (_RRF_K + rank)
            bm25_rank[int(i)] = rank
        for rank, i in enumerate(dense_top, start=1):
            rrf[int(i)] = rrf.get(int(i), 0.0) + 1.0 / (_RRF_K + rank)
            dense_rank[int(i)] = rank

        fused = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [
            Hit(
                chunk=self.chunks[i],
                score=score,
                bm25_rank=bm25_rank.get(i, 0),
                dense_rank=dense_rank.get(i, 0),
            )
            for i, score in fused
        ]


_INDEX: Index | None = None


def _cache_paths() -> tuple[Path, Path, Path]:
    base = SETTINGS.cache_dir
    return (
        base / "chunks.jsonl",
        base / "bm25.pkl",
        base / "dense.npy",
    )


def _texts_signature(texts: list[str]) -> str:
    import hashlib
    h = hashlib.sha1()
    for t in texts:
        h.update(t.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


def build_index(verbose: bool = True, force_reencode: bool = False) -> Index:
    global _INDEX
    chunks_path, bm25_path, dense_path = _cache_paths()
    sig_path = SETTINGS.cache_dir / "texts.sig"

    if verbose:
        print("[index] loading + chunking corpus ...")
    chunks = load_chunks()
    if verbose:
        from .corpus import stats
        print(f"[index] {stats(chunks)}")

    texts = [c.text for c in chunks]
    new_sig = _texts_signature(texts)
    can_reuse = (
        not force_reencode
        and dense_path.exists()
        and bm25_path.exists()
        and sig_path.exists()
        and sig_path.read_text().strip() == new_sig
    )

    encoder = None
    if can_reuse:
        if verbose:
            print("[index] chunk texts unchanged -> reusing cached BM25 + dense vectors")
        with bm25_path.open("rb") as f:
            bm25 = pickle.load(f)
        dense = np.load(dense_path).astype(np.float32)
    else:
        if verbose:
            print("[index] tokenizing for BM25 ...")
        tokenized = [_tokenize(t) for t in texts]
        bm25 = BM25Okapi(tokenized)

        if verbose:
            print(f"[index] encoding dense vectors with {SETTINGS.embed_model}"
                  f" (first run downloads ~130MB) ...")
        from sentence_transformers import SentenceTransformer
        encoder = SentenceTransformer(SETTINGS.embed_model)
        dense = encoder.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=verbose,
            batch_size=64,
            convert_to_numpy=True,
        ).astype(np.float32)

    # persist
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    with chunks_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c.__dict__, ensure_ascii=False) + "\n")
    if not can_reuse:
        with bm25_path.open("wb") as f:
            pickle.dump(bm25, f)
        np.save(dense_path, dense)
        sig_path.write_text(new_sig)

    if verbose:
        print(f"[index] persisted to {SETTINGS.cache_dir}")

    _INDEX = Index(chunks, bm25, dense, SETTINGS.embed_model)
    if encoder is not None:
        _INDEX._encoder = encoder
    return _INDEX


def load_index() -> Index:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    chunks_path, bm25_path, dense_path = _cache_paths()
    if not (chunks_path.exists() and bm25_path.exists() and dense_path.exists()):
        return build_index()
    chunks: list[Chunk] = []
    with chunks_path.open("r", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            chunks.append(Chunk(**d))
    with bm25_path.open("rb") as f:
        bm25 = pickle.load(f)
    dense = np.load(dense_path).astype(np.float32)
    _INDEX = Index(chunks, bm25, dense, SETTINGS.embed_model)
    return _INDEX


def retrieve(query: str, company: str | None, top_k: int | None = None) -> list[Hit]:
    idx = load_index()
    return idx.search(query, company, top_k or SETTINGS.top_k)


if __name__ == "__main__":
    build_index()
