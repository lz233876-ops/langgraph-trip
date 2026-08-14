"""RAG 知识库服务: 城市旅游知识文档 + 历史行程 的向量化存储与检索

数据源:
1. backend/data/knowledge/*.md — 预设城市旅游知识库 (启动时自动索引, 可手动重建)
2. 历史行程记录 (TripRecord)   — 每次生成行程后增量入库

嵌入模型: 千问 text-embedding-v4 (阿里云百炼 DashScope)
向量库:   ChromaDB (持久化到 backend/data/chroma)

降级策略: 未配置 DASHSCOPE_API_KEY 或初始化失败时, RAG 整体禁用,
          所有检索返回空, 不影响旅行规划主流程。
"""

import logging
import os
import re
from http import HTTPStatus
from pathlib import Path
from typing import List, Optional

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings as LangChainEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..config import get_settings
from ..models.schemas import TripPlan, TripRequest

logger = logging.getLogger(__name__)

# 千问 text-embedding-v4 单次调用批量上限
_EMBED_BATCH_SIZE = 10

# 知识库文件名(英文) → 城市中文名 (与前端请求的城市保持一致, 用于检索过滤)
_CITY_NAME_MAP = {
    "shenzhen": "深圳",
    "beijing": "北京",
    "shanghai": "上海",
    "guangzhou": "广州",
}


class _DashScopeEmbeddings(LangChainEmbeddings):
    """千问 text-embedding 嵌入模型 (直接封装 dashscope SDK)

    不依赖 langchain-dashscope 薄封装, 减少一层依赖;
    实现 LangChain Embeddings 接口 (embed_documents/embed_query), 供 ChromaDB 使用。
    """

    def __init__(self, api_key: str, model: str = "text-embedding-v4"):
        self.api_key = api_key
        self.model = model

    def _call(self, texts: List[str], text_type: str) -> List[List[float]]:
        try:
            from dashscope import TextEmbedding
        except ImportError as e:
            raise RuntimeError("dashscope SDK 未安装, 请执行: pip install dashscope") from e
        rsp = TextEmbedding.call(
            model=self.model,
            input=texts,
            text_type=text_type,
            api_key=self.api_key,
        )
        if rsp.status_code != HTTPStatus.OK:
            raise ValueError(f"DashScope embedding 调用失败: {rsp.code} {rsp.message}")
        return [item["embedding"] for item in rsp.output["embeddings"]]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # 千问 text-embedding-v4 单次调用最多 10 条, 超出需分批
        cleaned = [t.replace("\n", " ") for t in texts]
        results: List[List[float]] = []
        for i in range(0, len(cleaned), _EMBED_BATCH_SIZE):
            results.extend(self._call(cleaned[i:i + _EMBED_BATCH_SIZE], text_type="document"))
        return results

    def embed_query(self, text: str) -> List[float]:
        return self._call([text.replace("\n", " ")], text_type="query")[0]

# backend/data/knowledge 与 backend/data/chroma
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
CHROMA_DIR = DATA_DIR / "chroma"

_KNOWLEDGE_COLLECTION = "trip_knowledge"
_HISTORY_COLLECTION = "trip_history"


class RagService:
    """RAG 检索服务 (单例)"""

    def __init__(self):
        self.settings = get_settings()
        self._embedding = None
        self._knowledge_store = None
        self._history_store = None
        self._text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=300,
            chunk_overlap=50,
            separators=["\n## ", "\n### ", "\n- ", "\n", "。", "；", " "],
        )
        self._init()

    # ============ 初始化 ============

    def _init(self) -> None:
        """初始化嵌入模型与向量库 (失败则降级禁用)"""
        if not self.settings.dashscope_api_key:
            logger.warning(
                "⚠️  DASHSCOPE_API_KEY 未配置, RAG 知识库功能已禁用 (不影响旅行规划主流程)"
            )
            return
        try:
            from langchain_chroma import Chroma

            os.makedirs(CHROMA_DIR, exist_ok=True)
            self._embedding = _DashScopeEmbeddings(
                api_key=self.settings.dashscope_api_key,
                model=self.settings.embedding_model,
            )
            self._knowledge_store = Chroma(
                collection_name=_KNOWLEDGE_COLLECTION,
                embedding_function=self._embedding,
                persist_directory=str(CHROMA_DIR),
            )
            self._history_store = Chroma(
                collection_name=_HISTORY_COLLECTION,
                embedding_function=self._embedding,
                persist_directory=str(CHROMA_DIR),
            )
            logger.info(
                f"✅ RAG 知识库初始化成功 (嵌入: text-embedding-v4 | 向量库: ChromaDB@{CHROMA_DIR})"
            )
        except Exception as e:
            logger.warning(f"⚠️  RAG 初始化失败, 已降级禁用: {e}")
            self._embedding = None
            self._knowledge_store = None
            self._history_store = None

    @property
    def enabled(self) -> bool:
        """RAG 是否可用"""
        return self._embedding is not None

    def _new_store(self, collection_name: str):
        """(重建用) 创建新的 Chroma 实例"""
        from langchain_chroma import Chroma

        return Chroma(
            collection_name=collection_name,
            embedding_function=self._embedding,
            persist_directory=str(CHROMA_DIR),
        )

    # ============ 知识文档索引 ============

    def _load_knowledge_documents(self) -> List[Document]:
        """读取 data/knowledge/*.md 并按段落切块"""
        documents: List[Document] = []
        for md_path in sorted(KNOWLEDGE_DIR.glob("*.md")):
            city = _CITY_NAME_MAP.get(md_path.stem, md_path.stem)
            content = md_path.read_text(encoding="utf-8")
            for chunk in self._text_splitter.split_text(content):
                documents.append(
                    Document(
                        page_content=chunk,
                        metadata={"city": city, "source": md_path.name},
                    )
                )
        return documents

    def ensure_knowledge_index(self) -> bool:
        """确保知识索引存在 (空库时自动构建, 幂等)"""
        if not self.enabled:
            return False
        try:
            if self._knowledge_store.similarity_search("测试", k=1):
                return True  # 已有数据, 无需重建
        except Exception:
            pass
        return self.build_knowledge_index()["success"]

    def build_knowledge_index(self) -> dict:
        """重建知识索引 (清空旧数据后重新索引)"""

         # ① RAG 没启用 → 直接报告"未启用"
        if not self.enabled:
            return {"success": False, "message": "RAG 未启用 (缺少 DASHSCOPE_API_KEY)", "chunks": 0}
        #  ② 读取 knowledge/*.md 并切块
        documents = self._load_knowledge_documents()
        # ③ 目录为空 → 报告"知识目录为空"
        if not documents:
            return {"success": True, "message": "知识目录为空", "chunks": 0}
        try:
            # ④ 清空旧索引
            self._knowledge_store.delete_collection()
             # ⑤ 新建实例连接空集合
            self._knowledge_store = self._new_store(_KNOWLEDGE_COLLECTION)
             # ⑥ 全部向量化并写入
            self._knowledge_store.add_documents(documents)
            logger.info(f"📚 知识索引重建完成: {len(documents)} 个文本块")
            return {"success": True, "message": f"已索引 {len(documents)} 个文本块", "chunks": len(documents)}
        except Exception as e:
            # ⑦ 任一步失败 → 记录错误并返回失败信息
            logger.error(f"❌ 知识索引构建失败: {e}")
            return {"success": False, "message": str(e), "chunks": 0}

    # ============ 历史行程入库 ============

    def add_history_plan(self, record_id: int, request: TripRequest, trip_plan: TripPlan) -> bool:
        """把一份行程计划写入历史向量库 (增量)"""
        if not self.enabled:
            return False
        try:
            text = self._plan_to_text(request, trip_plan)
            self._history_store.add_documents(
                [
                    Document(
                        page_content=text,
                        metadata={"record_id": record_id, "city": request.city},
                    )
                ]
            )
            logger.info(f"🧠 历史行程已写入 RAG 向量库: record_id={record_id}")
            return True
        except Exception as e:
            logger.warning(f"⚠️  历史行程入库失败: {e}")
            return False

    @staticmethod
    def _plan_to_text(request: TripRequest, trip_plan: TripPlan) -> str:
        """把行程计划转为可检索的摘要文本"""
        lines = [
            f"{request.city} {request.travel_days}天旅行计划",
            f"日期: {request.start_date} 至 {request.end_date}",
            f"交通: {request.transportation}, 住宿: {request.accommodation}",
            f"偏好: {','.join(request.preferences) if request.preferences else '无'}",
        ]
        for day in trip_plan.days:
            attractions = "、".join(a.name for a in day.attractions)
            lines.append(f"第{day.day_index + 1}天: {attractions}")
        if trip_plan.budget:
            lines.append(f"总预算: {trip_plan.budget.total}元")
        return "\n".join(lines)

    # ============ 检索 ============

    def retrieve(self, query: str, city: Optional[str] = None, k: int = 3) -> List[str]:
        """检索知识库 + 历史行程, 返回匹配文本片段"""
        if not self.enabled:
            return []
        results: List[str] = []
        try:
            # 1. 城市知识库 (限定城市, 相关性最高)
            if city:
                docs = self._knowledge_store.similarity_search(
                    query, k=k, filter={"city": city}
                )
                results.extend(f"[知识库-{doc.metadata.get('city')}] {doc.page_content}" for doc in docs)
            # 2. 历史行程 (跨城市, 风格参考)
            docs = self._history_store.similarity_search(query, k=2)
            results.extend(f"[历史行程] {doc.page_content}" for doc in docs)
        except Exception as e:
            logger.warning(f"⚠️  RAG 检索失败: {e}")
        return results

    def build_rag_context(self, request: TripRequest, top_k: int = 3) -> str:
        """为规划请求构建 RAG 上下文文本 (注入 LLM prompt 用)"""
        if not self.enabled:
            return ""
        query = (
            f"{request.city} {request.travel_days}天旅行 "
            f"{','.join(request.preferences) if request.preferences else ''} "
            f"{request.free_text_input or ''}"
        )
        chunks = self.retrieve(query, city=request.city, k=top_k)
        if not chunks:
            return ""
        header = "## 检索到的相关知识 (供你参考, 让行程更真实/贴合当地实际):"
        return header + "\n" + "\n\n".join(f"- {c}" for c in chunks)

    def get_knowledge_attractions(self, city: str, max_names: int = 5) -> List[str]:
        """从知识库提取该城市知名景点名 (供补充进"可选景点"列表, 让LLM能真实采用)

        知识库景点带门票/交通/避坑信息, 但本身无坐标;
        返回景点名后由调用方用高德按名搜索补上真实坐标, 即可进入行程候选。
        """
        if not self.enabled:
            return []
        try:
            docs = self._knowledge_store.similarity_search(
                f"{city} 必去景点 门票 交通 打卡",
                k=3,
                filter={"city": city},
            )
            names: List[str] = []
            for doc in docs:
                for m in re.finditer(r"^###\s+(.+)$", doc.page_content, re.M):
                    name = m.group(1).strip()
                    if name and name not in names:
                        names.append(name)
            return names[:max_names]
        except Exception as e:
            logger.warning(f"⚠️  知识库景点提取失败: {e}")
            return []

    def get_attraction_rag_text(self, name: str, city: str, max_chars: int = 320) -> str:
        """检索知识库中某景点的详细信息 (门票/开放时间/交通/打卡/避坑)

        供行程生成后回填到景点描述, 让知识库内容真正落到前端每个景点上。
        只取最相关的一段, 避免把其他景点的内容拼进来。
        """
        if not self.enabled:
            return ""
        try:
            docs = self._knowledge_store.similarity_search(
                f"{city} {name} 门票 开放时间 交通 避坑 打卡",
                k=1,
                filter={"city": city},
            )
            if not docs:
                return ""
            lines = []
            for line in docs[0].page_content.splitlines():
                line = line.strip()
                if not line or line.startswith("##") or line.startswith("###"):
                    continue  # 跳过标题行
                lines.append(line)
            text = "\n".join(lines).strip()
            return text[:max_chars]
        except Exception as e:
            logger.warning(f"⚠️  知识库景点详情检索失败: {e}")
            return ""


# 全局单例
_rag_service: Optional[RagService] = None


def get_rag_service() -> RagService:
    """获取 RAG 服务实例 (单例模式)"""
    global _rag_service
    if _rag_service is None:
        _rag_service = RagService()
    return _rag_service
