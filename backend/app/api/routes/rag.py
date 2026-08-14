"""RAG 知识库 API"""

import logging

from fastapi import APIRouter

from ...services.rag_service import get_rag_service

router = APIRouter(prefix="/rag", tags=["RAG知识库"])

logger = logging.getLogger(__name__)


@router.get("/status", summary="RAG 知识库状态")
def rag_status():
    """查看 RAG 是否启用、知识索引情况"""
    rag = get_rag_service()
    return {
        "success": True,
        "enabled": rag.enabled,
        "embedding_model": "text-embedding-v4" if rag.enabled else None,
        "message": (
            "RAG 运行中"
            if rag.enabled
            else "RAG 未启用 (缺少 DASHSCOPE_API_KEY, 不影响旅行规划主流程)"
        ),
    }


@router.post("/rebuild", summary="重建知识索引")
def rebuild_knowledge():
    """清空并重新索引 data/knowledge/*.md 知识文档"""
    rag = get_rag_service()
    return rag.build_knowledge_index()
