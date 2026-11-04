"""query_reports tool — semantic search across archived reports via RAG."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tools.base import HealthTool
from app.services.rag import search_reports

logger = logging.getLogger(__name__)


class QueryReportsTool(HealthTool):
    """Search archived health reports by semantic similarity."""

    name: str = "query_reports"
    description: str = (
        "搜索家庭成员已入档的健康报告（体检报告、检验报告、检查报告等）。"
        "采用语义+关键词混合检索查找相关的报告片段，返回内容与相关度分数。"
        "适用于用户询问之前的检查结果、报告详情等场景。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询，如 '肝功能检查结果' 或 '血脂情况'",
            },
            "limit": {
                "type": "integer",
                "description": "返回最多N条相关结果，默认5",
            },
        },
        "required": ["query"],
    }

    async def execute(
        self, db: AsyncSession, member_id: int, **kwargs: Any
    ) -> dict[str, Any]:
        query: str = kwargs.get("query", "")
        limit: int = kwargs.get("limit", 5)

        if not query:
            return {"error": "查询内容不能为空"}

        results = await search_reports(db, member_id, query, limit=limit)

        if not results:
            return {
                "results": [],
                "count": 0,
                # Explicit signal so the model abstains instead of guessing.
                # Retrieval always used to return top-k, so a "no results" case
                # was indistinguishable from "weak results" for the model.
                "insufficient_evidence": True,
                "message": (
                    "档案中没有足够相关的资料。请如实告知用户未找到相关内容，"
                    "不要推测或编造任何数值。"
                ),
            }

        return {
            "results": [
                {
                    "report_id": r["report_id"],
                    "content": r["chunk_text"],
                    # `score` is already normalised to [0, 1] by search_reports
                    # (RRF-fused, or distance-derived on the vector-only path).
                    "relevance": round(float(r.get("score", 0.0)), 3),
                }
                for r in results
            ],
            "count": len(results),
        }
