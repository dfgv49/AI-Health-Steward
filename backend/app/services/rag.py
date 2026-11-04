"""RAG service — generates and stores report embeddings for semantic retrieval.

Called after report confirmation (archived). Builds a text representation
from the confirmed extraction, generates embedding, and persists to report_chunks.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.health import ReportChunk, ReportRecord
from app.providers.embedding import EmbeddingProvider
from app.services.bm25_index import invalidate_index, search_bm25

logger = logging.getLogger(__name__)

# Must match the pgvector column width declared in models.health.ReportChunk
# (Vector(1024)). A mismatch used to surface as a DataError at flush time.
EMBEDDING_DIM = 1024


def _build_chunk_text(member_id: int, report: ReportRecord, extraction: dict) -> str:
    """Build a text representation of a report for embedding."""
    parts: list[str] = []

    if report.report_type:
        parts.append(f"报告类型: {report.report_type}")
    if report.report_date:
        parts.append(f"报告日期: {report.report_date.strftime('%Y-%m-%d')}")
    if report.patient_name:
        parts.append(f"患者: {report.patient_name}")
    if report.summary:
        parts.append(f"摘要: {report.summary}")

    # Metrics
    metrics = extraction.get("metrics", [])
    if metrics:
        lines = ["指标:"]
        for m in metrics:
            abnormal = " (异常)" if m.get("is_abnormal") else ""
            ref = ""
            if m.get("reference_lower") is not None and m.get("reference_upper") is not None:
                ref = f" 参考范围:{m['reference_lower']}-{m['reference_upper']}"
            lines.append(f"  {m.get('label', m.get('metric_name', ''))}: {m.get('value')}{m.get('unit', '')}{ref}{abnormal}")
        parts.append("\n".join(lines))

    # Lab tests
    lab_tests = extraction.get("lab_tests", [])
    if lab_tests:
        lines = ["检验:"]
        for lt in lab_tests:
            abnormal = " (异常)" if lt.get("is_abnormal") else ""
            ref = ""
            if lt.get("reference_lower") is not None and lt.get("reference_upper") is not None:
                ref = f" 参考范围:{lt['reference_lower']}-{lt['reference_upper']}"
            lines.append(f"  {lt.get('report_name', '')}/{lt.get('test_name', '')}: {lt.get('value')}{lt.get('unit', '')}{ref}{abnormal}")
        parts.append("\n".join(lines))

    # Exam findings
    exams = extraction.get("exam_findings", [])
    if exams:
        lines = ["检查异常:"]
        for ef in exams:
            val = f" {ef.get('value_num', '')}{ef.get('unit', '')}" if ef.get("value_num") is not None else ""
            conclusion = f" 结论:{ef.get('conclusion', '')}" if ef.get("conclusion") else ""
            lines.append(f"  {ef.get('finding_category', '')}: {ef.get('finding_desc', '')}{val}{conclusion}")
        parts.append("\n".join(lines))

    # Diagnoses
    diagnoses = extraction.get("diagnoses", [])
    if diagnoses:
        lines = ["诊断:"]
        for d in diagnoses:
            sev = f" ({d['severity']})" if d.get("severity") else ""
            lines.append(f"  {d.get('disease_name', '')}{sev}")
        parts.append("\n".join(lines))

    # Medications
    meds = extraction.get("medications", [])
    if meds:
        lines = ["用药:"]
        for m in meds:
            lines.append(f"  {m.get('drug_name', '')} {m.get('dosage', '')} {m.get('frequency', '')}")
        parts.append("\n".join(lines))

    return "\n".join(parts)


async def generate_and_store_embedding(
    db: AsyncSession,
    member_id: int,
    report: ReportRecord,
    extraction_data: dict,
) -> Optional[ReportChunk]:
    """Generate embedding for a report and store in report_chunks.

    Returns the ReportChunk if successful, None if embedding not configured or failed.
    """
    provider = EmbeddingProvider()
    if not provider.is_configured:
        logger.info("Embedding model not configured, skipping embedding for report %s", report.id)
        return None

    chunk_text = _build_chunk_text(member_id, report, extraction_data)
    if not chunk_text.strip():
        logger.warning("Empty chunk text for report %s, skipping", report.id)
        return None

    try:
        embedding = await provider.embed(chunk_text)
    except Exception as e:
        logger.error("Embedding generation failed for report %s: %s", report.id, e)
        return None

    # Validate the dimension before touching the DB. report_chunks.embedding is
    # vector(1024); storing a differently-sized vector raises DataError at flush
    # time, which (without a savepoint) poisons the caller's transaction and can
    # roll back an otherwise successful report archival.
    actual_dim = len(embedding) if embedding is not None else 0
    if actual_dim != EMBEDDING_DIM:
        logger.error(
            "Embedding dimension mismatch for report %s: model returned %d, "
            "column expects %d. Skipping embedding storage (semantic search "
            "disabled for this report). Configure EMBEDDING_MODEL to a "
            "%d-dimension model.",
            report.id, actual_dim, EMBEDDING_DIM, EMBEDDING_DIM,
        )
        return None

    # Delete existing chunk for this report (in case of re-archival)
    await db.execute(
        delete(ReportChunk).where(ReportChunk.report_id == report.id)
    )

    chunk = ReportChunk(
        member_id=member_id,
        report_id=report.id,
        chunk_text=chunk_text,
        embedding=embedding,
    )
    db.add(chunk)
    await db.flush()
    # The cached BM25 index for this member no longer reflects report_chunks.
    # Explicit invalidation is required: the 5-minute TTL alone would keep
    # keyword retrieval blind to a freshly archived report.
    invalidate_index(member_id)
    logger.info("Embedding stored for report %s (chunk_id=%s)", report.id, chunk.id)
    return chunk


# ---------------------------------------------------------------------------
# Hybrid retrieval: vector (semantic) + BM25 (keyword) fused with RRF
# ---------------------------------------------------------------------------

# pgvector's <=> is cosine distance on [0, 2]. Map it to a [0, 1]
# "higher is better" score so one threshold works for every recall path.
_VECTOR_MAX_DISTANCE = 2.0


async def _vector_recall(
    db: AsyncSession, member_id: int, query: str, limit: int
) -> list[dict]:
    """pgvector cosine recall. Returns [] when embedding is unavailable."""
    provider = EmbeddingProvider()
    if not provider.is_configured:
        return []

    try:
        query_embedding = await provider.embed(query)
    except Exception as e:
        logger.error("Query embedding failed: %s", e)
        return []

    from sqlalchemy import text as sql_text

    stmt = sql_text("""
        SELECT rc.id AS chunk_id, rc.chunk_text, rc.report_id,
               rc.embedding <=> :query_vec AS distance
        FROM report_chunks rc
        WHERE rc.member_id = :member_id
        ORDER BY rc.embedding <=> :query_vec
        LIMIT :limit
    """)

    result = await db.execute(stmt, {
        "query_vec": str(query_embedding),
        "member_id": member_id,
        "limit": limit,
    })

    return [
        {
            "chunk_id": row.chunk_id,
            "chunk_text": row.chunk_text,
            "report_id": row.report_id,
            "distance": float(row.distance),
            "score": max(0.0, 1.0 - float(row.distance) / _VECTOR_MAX_DISTANCE),
        }
        for row in result
    ]


def _rrf_fuse(vector_hits: list[dict], bm25_hits: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion over two ranked lists.

        score(d) = sum over lists of 1 / (k + rank_in_that_list(d))

    Only ranks are used, never raw scores: cosine distance and BM25 scores live
    on incomparable scales, so a weighted sum needs fragile per-query
    normalisation. RRF sidesteps that entirely.
    """
    fused: dict[int, float] = {}
    payload: dict[int, dict] = {}

    for rank, hit in enumerate(vector_hits, start=1):
        cid = hit["chunk_id"]
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
        payload[cid] = hit

    for rank, hit in enumerate(bm25_hits, start=1):
        cid = hit["chunk_id"]
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
        payload.setdefault(cid, hit)

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    # Theoretical maximum: both lists rank the same document first.
    max_possible = 2.0 / (k + 1)

    return [
        {**payload[cid], "rrf_score": score, "score": min(score / max_possible, 1.0)}
        for cid, score in ordered
    ]


async def search_reports(
    db: AsyncSession,
    member_id: int,
    query: str,
    limit: int = 5,
) -> list[dict]:
    """Hybrid retrieval across a member's archived reports.

    Pipeline: vector recall || BM25 recall -> RRF fusion -> threshold filter.

    Returns list of {chunk_text, report_id, score} with `score` in [0, 1],
    higher = more relevant, sorted desc. An empty list means "no evidence" and
    callers should treat it as a signal to abstain, not to guess.
    """
    cfg = get_settings()

    # `limit` can originate from an LLM tool argument. Bounds-check it: a
    # negative value reached PostgreSQL as `LIMIT -1` and raised
    # InvalidRowCountInLimitClauseError, and a huge value returned the whole table.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 20))

    # ---- 1) Recall on both paths ----
    vector_hits = await _vector_recall(db, member_id, query, cfg.VECTOR_RECALL_N)

    bm25_hits: list[dict] = []
    if cfg.HYBRID_ENABLED:
        bm25_hits = await search_bm25(db, member_id, query, cfg.BM25_RECALL_N)

    if not vector_hits and not bm25_hits:
        logger.info(
            "No recall for member %s (query=%r, hybrid=%s)",
            member_id, query, cfg.HYBRID_ENABLED,
        )
        return []

    # ---- 2) Fuse ----
    if bm25_hits:
        candidates = _rrf_fuse(vector_hits, bm25_hits, k=cfg.RRF_K)
    else:
        # Vector-only: BM25 disabled, deps missing, or no term overlap.
        # `score` is already derived from distance on the same 0-1 scale.
        candidates = list(vector_hits)

    # P2 hook: when a cross-encoder is added, widen the slice to
    # cfg.RERANK_CANDIDATES here and rerank before the threshold step.
    candidates = candidates[:limit]

    # ---- 3) Threshold ----
    # MIN_RELEVANCE defaults to 0 (no filtering) so that enabling hybrid
    # retrieval alone does not change existing behaviour. Tune it with the
    # evaluation set, then turn strictness on deliberately.
    floor = float(cfg.MIN_RELEVANCE)
    if floor > 0.0:
        kept = [c for c in candidates if c.get("score", 0.0) >= floor]
        if not kept:
            logger.info(
                "All %d candidates below MIN_RELEVANCE=%.2f for member %s "
                "(best=%.3f, query=%r)",
                len(candidates), floor, member_id,
                candidates[0].get("score", 0.0) if candidates else 0.0,
                query,
            )
            return []
        candidates = kept

    return [
        {
            "chunk_text": c["chunk_text"],
            "report_id": c["report_id"],
            "score": round(float(c.get("score", 0.0)), 4),
        }
        for c in candidates[:limit]
    ]
