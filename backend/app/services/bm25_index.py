"""Per-member BM25 keyword index over report_chunks.chunk_text.

Why an in-process index instead of PostgreSQL full-text search:
- Chinese FTS in PG needs pg_jieba / zhparser, i.e. a custom image rebuild.
- A family has tens of reports -> hundreds of chunks; brute-force BM25 is trivial.
- Migrate to Elasticsearch or pg_jieba once one member exceeds ~50k chunks.

Two safety properties worth keeping when this is modified:
1. The index is built with `WHERE member_id = :member_id`, so other members'
   text never enters this process' memory. Never retrieve globally then filter.
2. jieba/rank_bm25 are optional dependencies. If they are missing, every
   function here degrades to "no keyword hits" instead of raising, so the
   vector-only path keeps working.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

try:  # optional: keyretrieval deps
    import jieba
    from rank_bm25 import BM25Okapi

    _DEPS_AVAILABLE = True
    _DEPS_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover - ImportError or broken numpy
    jieba = None  # type: ignore[assignment]
    BM25Okapi = None  # type: ignore[assignment,misc]
    _DEPS_AVAILABLE = False
    _DEPS_ERROR = str(_exc)


# Rebuild automatically when the cached index is older than this. Explicit
# invalidation on write is the primary mechanism; the TTL is only a safety net.
CACHE_TTL_SEC = 300.0

# Terms jieba would otherwise split into pieces, which silently destroys recall
# for exactly the tokens users search by (test names / drug names).
_DOMAIN_TERMS = (
    "谷丙转氨酶", "谷草转氨酶", "谷氨酰转肽酶", "总胆红素", "直接胆红素",
    "总胆固醇", "甘油三酯", "低密度脂蛋白", "高密度脂蛋白",
    "收缩压", "舒张压", "空腹血糖", "餐后血糖", "糖化血红蛋白",
    "氨氯地平", "缬沙坦", "二甲双胍", "阿司匹林", "他汀",
    "甲状腺结节", "甲状腺功能", "心电图", "尿常规", "血常规", "肝功能", "肾功能",
)

_jieba_ready = False


def deps_available() -> bool:
    """True when BM25 keyword retrieval can actually run."""
    return _DEPS_AVAILABLE


def _ensure_jieba() -> None:
    """Initialise the dictionary once. load time is ~1s, so this is lazy."""
    global _jieba_ready
    if _jieba_ready or not _DEPS_AVAILABLE:
        return
    jieba.initialize()  # type: ignore[union-attr]
    for term in _DOMAIN_TERMS:
        jieba.add_word(term)  # type: ignore[union-attr]
    _jieba_ready = True


def _tokenize(text: str) -> list[str]:
    """Tokenise for BM25: jieba precise mode, lowercased, whitespace dropped."""
    if not _DEPS_AVAILABLE:  # defensive: callers should check deps_available()
        return []
    _ensure_jieba()
    return [t.lower() for t in jieba.lcut(text) if t.strip()]  # type: ignore[union-attr]


@dataclass
class _Index:
    chunks: list[dict]
    bm25: object
    built_at: float


_cache: dict[int, _Index] = {}
_locks: dict[int, asyncio.Lock] = {}


def invalidate_index(member_id: int) -> None:
    """Drop a member's cached index.

    Must be called after ANY write to report_chunks (archival, rebuild), or
    keyword search keeps serving a stale corpus.
    """
    if _cache.pop(member_id, None) is not None:
        logger.info("BM25 index invalidated for member %s", member_id)


def clear_all() -> None:
    """Test helper — drop every cached index."""
    _cache.clear()


def _build_index_sync(chunks: list[dict]) -> _Index | None:
    """CPU-bound; always call through asyncio.to_thread."""
    pairs = [(_tokenize(c["chunk_text"]), c) for c in chunks]
    pairs = [(tokens, c) for tokens, c in pairs if tokens]
    if not pairs:
        return None

    kept = [c for _, c in pairs]
    corpus = [tokens for tokens, _ in pairs]
    return _Index(chunks=kept, bm25=BM25Okapi(corpus), built_at=time.monotonic())  # type: ignore[misc]


async def _load_chunks(db, member_id: int) -> list[dict]:
    """Load this member's chunks only — the permission boundary of this module."""
    stmt = sql_text(
        """
        SELECT rc.id, rc.report_id, rc.chunk_text
        FROM report_chunks rc
        WHERE rc.member_id = :member_id
        """
    )
    rows = await db.execute(stmt, {"member_id": member_id})
    return [
        {"id": r.id, "report_id": r.report_id, "chunk_text": r.chunk_text}
        for r in rows
    ]


async def _get_index(db, member_id: int) -> _Index | None:
    cached = _cache.get(member_id)
    if cached is not None and (time.monotonic() - cached.built_at) < CACHE_TTL_SEC:
        return cached

    lock = _locks.setdefault(member_id, asyncio.Lock())
    async with lock:  # avoid rebuilding the same member's index concurrently
        cached = _cache.get(member_id)
        if cached is not None and (time.monotonic() - cached.built_at) < CACHE_TTL_SEC:
            return cached

        chunks = await _load_chunks(db, member_id)
        chunks = [c for c in chunks if (c.get("chunk_text") or "").strip()]
        if not chunks:
            return None

        # Tokenisation + BM25 construction is CPU-bound and jieba's first call
        # loads a ~50MB dictionary; never run that on the event loop.
        index = await asyncio.to_thread(_build_index_sync, chunks)
        if index is None:
            logger.warning("BM25 index empty for member %s (no tokenisable text)", member_id)
            return None

        _cache[member_id] = index
        logger.info("BM25 index built for member %s: %d chunks", member_id, len(index.chunks))
        return index


async def search_bm25(
    db, member_id: int, query: str, limit: int = 20
) -> list[dict]:
    """Return keyword hits sorted by BM25 score desc.

    Each hit: {"chunk_id", "report_id", "chunk_text", "bm25_score"}.
    Returns [] on any problem (deps missing, empty corpus, no term overlap) so
    the caller can fall back to vector-only retrieval.
    """
    if not _DEPS_AVAILABLE:
        logger.debug("BM25 deps unavailable (%s), skipping keyword retrieval", _DEPS_ERROR)
        return []

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    if not query or not query.strip():
        return []

    index = await _get_index(db, member_id)
    if index is None:
        return []

    try:
        scores = index.bm25.get_scores(_tokenize(query))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — never break retrieval on BM25
        logger.warning("BM25 scoring failed for member %s: %s", member_id, exc)
        return []

    order = sorted(range(len(scores)), key=lambda i: float(scores[i]), reverse=True)[:limit]

    hits: list[dict] = []
    for i in order:
        score = float(scores[i])
        if score <= 0.0:  # no term overlap with this chunk
            continue
        chunk = index.chunks[i]
        hits.append(
            {
                "chunk_id": chunk["id"],
                "report_id": chunk["report_id"],
                "chunk_text": chunk["chunk_text"],
                "bm25_score": score,
            }
        )
    return hits
