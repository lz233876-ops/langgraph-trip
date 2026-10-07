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
import math
import os
import re
from collections import Counter
from http import HTTPStatus
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings as LangChainEmbeddings

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

# ============ 关键词检索 (BM25) ============

# BM25 标准默认参数
_BM25_K1 = 1.5
_BM25_B = 0.75
# RRF 融合常数(标准取值): 融合分 = Σ 1/(_RRF_K + 排名)
_RRF_K = 60
# 融合时每路取多少候选。实测这个值比 _RRF_K 关键得多: 只取 top-k(3) 时两路候选池
# 太浅, RRF 无处发挥, 难组 MRR 0.93; 取到 5 即升到 0.96 (见 eval/exp_fusion.py)。
_FUSE_CANDIDATES = 5

# ASCII 词 或 CJK 字串
_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]+")


def _tokenize(text: str) -> List[str]:
    """中文按字符二元组切分, 英文按词 —— 不引分词器依赖

    中文没有空格, 按空白切词会把整句变成一个 token, BM25 就退化成整句匹配。
    字符二元组("城堡烟花秀" → 城堡/堡烟/烟花/花秀) 对短查询足够, 且零依赖。
    """
    tokens: List[str] = []
    for run in _TOKEN_RUN_RE.findall(text):
        if run.isascii():
            tokens.append(run.lower())
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class _BM25:
    """极简 BM25 (Okapi)

    语料只有几十个片段, 纯 Python 足够快, 不值得为它引 rank_bm25 依赖。
    分数尺度与向量距离不可比, 所以上层用 RRF 按排名融合, 不比较绝对分数。
    """

    def __init__(self, corpus: List[List[str]]):
        self._corpus = corpus
        self._freqs = [Counter(doc) for doc in corpus]
        self._avgdl = (sum(len(d) for d in corpus) / len(corpus)) if corpus else 0.0
        doc_freq: Counter = Counter()
        for doc in corpus:
            doc_freq.update(set(doc))
        n = len(corpus)
        self._idf = {
            term: math.log(1 + (n - cnt + 0.5) / (cnt + 0.5))
            for term, cnt in doc_freq.items()
        }

    def scores(self, query_tokens: List[str]) -> List[float]:
        avgdl = self._avgdl or 1.0
        result = []
        for doc, freq in zip(self._corpus, self._freqs):
            dl = len(doc) or 1
            score = 0.0
            for term in query_tokens:
                tf = freq.get(term)
                if not tf:
                    continue
                score += self._idf.get(term, 0.0) * tf * (_BM25_K1 + 1) / (
                    tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
                )
            result.append(score)
        return result


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
# 模型冷启动生成的外部知识存放目录 (人工维护的是 KNOWLEDGE_DIR 根下的 *.md)。
# 放在子目录便于区分来源、需要时整体丢弃; 索引规则与人工文档完全一致。
EXTERNAL_KNOWLEDGE_DIR = KNOWLEDGE_DIR / "external"

_KNOWLEDGE_COLLECTION = "trip_knowledge"
_HISTORY_COLLECTION = "trip_history"


def iter_knowledge_files() -> List[Path]:
    """枚举全部知识文档 (人工维护的城市文档 + 冷启动生成的外部知识)

    只扫 KNOWLEDGE_DIR 根目录是原行为; 现在额外吸收 external/ 子目录, 让冷启动
    生成的内容无需任何新索引代码就能"转正"进知识库。
    """
    files = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if EXTERNAL_KNOWLEDGE_DIR.is_dir():
        files.extend(sorted(EXTERNAL_KNOWLEDGE_DIR.glob("*.md")))
    return files


class RagService:
    """RAG 检索服务 (单例)"""

    def __init__(self):
        self.settings = get_settings()
        self._embedding = None
        self._knowledge_store = None
        self._history_store = None
        # BM25 索引缓存 {城市: (片段列表, _BM25)}, 惰性构建; 重建知识索引时清空
        self._bm25_cache: Dict[str, Tuple[List[Document], _BM25]] = {}
        # 景点详情本地映射 {城市: {景点名: 详情}}, 不依赖 embedding, 生成后精确回填用
        self._attraction_details: Dict[str, Dict[str, str]] = {}
        # 已收录城市集合缓存 (None = 尚未计算); 重建知识索引时失效
        self._covered_cities_cache: Optional[Set[str]] = None
        self._init()
        self._attraction_details = self._load_attraction_details()

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

    @staticmethod
    def _parse_sections(content: str) -> List[Tuple[str, str, List[str]]]:
        """把知识文档拆成 [(二级标题, 三级标题, 正文行)]

        一段一块、不跨节合并 —— 合并块(如「北京路步行街 + 陈家祠」)会让一个向量
        代表两个景点, 拉低区分度。纯容器型 ## (正文全在 ### 子节里, 如「必去景点」)
        没有直属正文, 直接跳过, 避免产生空块。
        """
        sections: List[Tuple[str, str, List[str]]] = []
        h2: Optional[str] = None
        h3: Optional[str] = None
        lines: List[str] = []

        def flush() -> None:
            if h2 and (lines or h3):
                sections.append((h2, h3, list(lines)))

        for raw in content.splitlines():
            stripped = raw.strip()
            if stripped.startswith("### "):
                flush()
                h3, lines = stripped[4:].strip(), []
            elif stripped.startswith("## "):
                flush()
                h2, h3, lines = stripped[3:].strip(), None, []
            elif stripped.startswith("# "):
                continue  # 文档标题, 丢弃
            elif stripped:
                lines.append(stripped)
        flush()
        return sections

    def _load_knowledge_documents(self) -> List[Document]:
        """读取 data/knowledge/*.md, 按 ## / ### 一段一块

        嵌入文本 = 「城市 + 标题」前缀 + 该段原文。之所以要加前缀: 所有块共用同一套
        模板正文(- 门票：/- 开放时间：/...), 块间余弦高达 0.65, 景点名的区分度被模板
        噪声淹没, 「故宫门票多少钱」都召回不到故宫。前置标题后离线评测 MRR 由 0.81
        升到 0.91 (对比实验见 eval/exp_chunking.py)。
        """
        documents: List[Document] = []
        for md_path in iter_knowledge_files():
            city = _CITY_NAME_MAP.get(md_path.stem, md_path.stem)
            content = md_path.read_text(encoding="utf-8")
            for h2, h3, lines in self._parse_sections(content):
                title = h3 or h2
                header = f"### {h3}" if h3 else f"## {h2}"
                documents.append(
                    Document(
                        page_content=f"{city} {title}\n{header}\n" + "\n".join(lines),
                        metadata={"city": city, "source": md_path.name},
                    )
                )
        return documents

    def _load_attraction_details(self) -> Dict[str, Dict[str, str]]:
        """解析 data/knowledge/*.md, 建立 {城市: {景点名: 详情}} 内存映射

        景点详情按 `### 景点名` 标题切分, 供生成后精确回填使用, 不依赖 embedding
        (未配置 DASHSCOPE_API_KEY 时也能工作)。单个文件解析失败仅告警并跳过。
        """
        details_map: Dict[str, Dict[str, str]] = {}
        for md_path in iter_knowledge_files():
            city = _CITY_NAME_MAP.get(md_path.stem, md_path.stem)
            try:
                content = md_path.read_text(encoding="utf-8")
                details_map[city] = self._parse_attraction_details(content)
            except Exception as e:
                logger.warning(f"⚠️  知识库景点详情解析失败 ({md_path.name}): {e}")
        return details_map

    @staticmethod
    def _parse_attraction_details(content: str) -> Dict[str, str]:
        """从单个 md 文件解析 {景点名: 详情文本}

        规则: `### 景点名` 开启一个景点段落, 遇到下一个 `##`/`###` 标题结束;
        详情保留段落内非空内容行 (去掉标题行本身)。
        """
        details: Dict[str, str] = {}
        current_name: Optional[str] = None
        current_lines: List[str] = []

        def _flush() -> None:
            if current_name and current_lines:
                details[current_name] = "\n".join(current_lines).strip()

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("### "):
                _flush()
                current_name = stripped[4:].strip()
                current_lines = []
            elif stripped.startswith("## "):
                _flush()
                current_name = None
                current_lines = []
            elif current_name is not None and stripped:
                current_lines.append(stripped)
        _flush()
        return details

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

        # 本地景点详情缓存始终刷新 (不依赖 embedding, 无 key 时详情回填仍可用)
        self._attraction_details = self._load_attraction_details()
        # 知识片段变了, BM25 缓存必须一起失效, 否则关键词路会继续用旧语料
        self._bm25_cache.clear()
        # 收录城市可能变化(新增外部知识/文档), 覆盖判定缓存一并失效
        self._covered_cities_cache = None

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

    # ============ 覆盖范围判定 (冷启动兜底用) ============

    def covered_cities(self) -> Set[str]:
        """已收录进知识库的城市集合

        优先从向量库的实际 metadata 统计 —— 这样冷启动生成并落盘的城市, 重建索引后
        会自动算作"已收录", 无需维护任何硬编码名单。
        向量库为空/未启用/查询失败时, 回退到 data/knowledge 的文件名映射,
        保证"启动时索引尚未建好"这一刻的判定也不会把已知城市误判成未知城市。
        """
        if self._covered_cities_cache is not None:
            return self._covered_cities_cache
        cities: Set[str] = set()
        if self.enabled:
            try:
                data = self._knowledge_store.get(include=["metadatas"])
                for meta in data.get("metadatas") or []:
                    city = (meta or {}).get("city")
                    if city:
                        cities.add(str(city).strip())
            except Exception as e:
                logger.warning(f"⚠️  知识库城市统计失败, 回退文件名映射: {e}")
        if not cities:
            cities = {city for city in _CITY_NAME_MAP.values() if city}
        self._covered_cities_cache = cities
        return cities

    def is_city_covered(self, city: str) -> bool:
        """城市是否有知识库支撑 (供"知识库未命中则模型冷启动"判定)

        做了三种等价性兼容: 去空白、去「市」后缀、包含关系
        (兼容前端输入「西安市」而知识库记为「西安」这类写法差异)。
        """
        norm = re.sub(r"\s+", "", (city or ""))
        if not norm:
            return False
        variants = {norm, norm[:-1] if norm.endswith("市") else f"{norm}市"}
        for known in self.covered_cities():
            known_norm = re.sub(r"\s+", "", known)
            if not known_norm:
                continue
            if known_norm in variants:
                return True
            if known_norm in norm or norm in known_norm:
                return True
        return False

    # ============ 检索 ============

    def _city_documents(self, city: str) -> List[Document]:
        """取某城市全部知识片段 (给 BM25 建索引用)

        直接从向量库读, 而不是重读 data/knowledge —— 保证关键词路与向量路永远
        是同一份片段, 不会出现「改了文档但忘了 rebuild」导致两路不一致。
        """
        data = self._knowledge_store.get(where={"city": city})
        return [
            Document(page_content=text, metadata=meta or {})
            for text, meta in zip(data.get("documents") or [], data.get("metadatas") or [])
        ]

    def _keyword_search(self, query: str, city: str, k: int) -> List[Document]:
        """BM25 关键词检索 (限定城市)"""
        cached = self._bm25_cache.get(city)
        if cached is None:
            docs = self._city_documents(city)
            if not docs:
                return []
            cached = (docs, _BM25([_tokenize(d.page_content) for d in docs]))
            self._bm25_cache[city] = cached
        docs, index = cached
        scores = index.scores(_tokenize(query))
        ranked = sorted(range(len(docs)), key=lambda i: -scores[i])
        return [docs[i] for i in ranked[:k] if scores[i] > 0]

    def _fused_search(self, query: str, city: str, k: int) -> List[Document]:
        """向量 + BM25 双路召回, 用 RRF 按排名融合

        两路分数尺度不可比 (L2 距离 vs BM25 分数), 所以不比分数、只比排名, 省掉
        归一化调参。向量负责语义泛化, BM25 负责词面命中 —— 后者是纯词面查询
        ("晚上有城堡烟花秀的地方")唯一能召回目标的那条路。
        """
        depth = max(k, _FUSE_CANDIDATES)  # 每路多取一些候选再融合, 最后仍只返回 k 条
        paths = [
            self._knowledge_store.similarity_search(query, k=depth, filter={"city": city}),
            self._keyword_search(query, city, depth),
        ]
        fused: Dict[str, float] = {}
        by_key: Dict[str, Document] = {}
        for docs in paths:
            for rank, doc in enumerate(docs):
                key = doc.page_content  # 同城市内片段内容唯一, 直接当键
                by_key[key] = doc
                fused[key] = fused.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
        return [by_key[key] for key in sorted(fused, key=lambda d: -fused[d])[:k]]

    def retrieve_documents(self, query: str, city: Optional[str] = None, k: int = 3) -> List[Document]:
        """检索并返回原始 Document (带 metadata.source), 供评测脚本计算召回率

        与 retrieve 的检索逻辑完全一致, 只是不拼字符串、不丢来源信息。
        """
        if not self.enabled:
            return []
        docs: List[Document] = []
        try:
            # 1. 城市知识库: 向量 + BM25 双路融合 (限定城市, 相关性最高)
            if city:
                docs.extend(self._fused_search(query, city, k))
            # 2. 历史行程 (跨城市, 风格参考)
            docs.extend(self._history_store.similarity_search(query, k=2))
        except Exception as e:
            logger.warning(f"⚠️  RAG 检索失败: {e}")
        return docs

    def retrieve(self, query: str, city: Optional[str] = None, k: int = 3) -> List[str]:
        """检索知识库 + 历史行程, 返回匹配文本片段"""
        if not self.enabled:
            return []
        results: List[str] = []
        for doc in self.retrieve_documents(query, city=city, k=k):
            if "record_id" in doc.metadata:
                results.append(f"[历史行程] {doc.page_content}")
            else:
                results.append(f"[知识库-{doc.metadata.get('city')}] {doc.page_content}")
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

    def _lookup_attraction_detail(self, name: str, city: str) -> str:
        """在本地解析的景点详情映射中查找 (精确 → 去括号别名 → 双向子串)

        返回命中的详情文本; 未命中返回空字符串。
        """
        details = self._attraction_details.get(city) or {}
        if not details:
            return ""
        # 1. 精确匹配
        if name in details:
            return details[name]
        # 2. 规范化后匹配: 去括号别名/空白, 兼容 "故宫" vs "故宫博物院（紫禁城）"
        norm = self._normalize_name(name)
        for key, text in details.items():
            key_norm = self._normalize_name(key)
            if not norm or not key_norm:
                continue
            if norm == key_norm or norm in key_norm or key_norm in norm:
                return text
        return ""

    @staticmethod
    def _normalize_name(name: str) -> str:
        """规范化景点名: 去括号内容与空白, 便于别名匹配"""
        name = re.sub(r"[（(].*?[)）]", "", name)  # 去掉全角/半角括号及其内容
        return re.sub(r"\s+", "", name).strip()

    def get_attraction_rag_text(self, name: str, city: str, max_chars: int = 320) -> str:
        """检索知识库中某景点的详细信息 (门票/开放时间/交通/打卡/避坑)

        供行程生成后回填到景点描述, 让知识库内容真正落到前端每个景点上。
        优先走本地精确/模糊匹配 (零 embedding 调用, 快且准, 不依赖 key);
        未命中时退回向量检索兜底 (兼容知识库结构不规范的情况)。
        """
        # 1. 本地精确/模糊匹配 (无 key 时也能回填详情)
        detail = self._lookup_attraction_detail(name, city)
        if detail:
            return detail[:max_chars]

        # 2. 向量检索兜底 (RAG 未启用时跳过)
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
