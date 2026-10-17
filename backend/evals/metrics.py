"""Retrieval metrics. A ranked list is the doc keys of each hit (a chunk can belong to
several pages); a hit counts if any of its pages is an expected source."""

from collections.abc import Sequence


def first_relevant_rank(ranked: Sequence[Sequence[str]], expected: set[str]) -> int | None:
    """1-based rank of the first hit that contains an expected doc key, or None."""
    for rank, doc_keys in enumerate(ranked, start=1):
        if expected.intersection(doc_keys):
            return rank
    return None


def recall_at(rank: int | None, k: int) -> float:
    return 1.0 if rank is not None and rank <= k else 0.0


def reciprocal_rank(rank: int | None, cutoff: int) -> float:
    return 1.0 / rank if rank is not None and rank <= cutoff else 0.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
