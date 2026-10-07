"""外部知识冷启动服务: 知识库未收录的城市, 直接调用模型取知识并落地

背景
----
预设知识库 (data/knowledge/*.md) 只收录了少数城市。检索这类城市时, RAG 三处
调用(get_knowledge_attractions / build_rag_context / get_attraction_rag_text)
全部静默返回空 —— 行程照样能生成, 但门票/开放时间/避坑这些"当地细节"全靠模型
临场编。本服务把这条静默降级路径改成显式兜底:

    知识库命中 → 走原 RAG 链路 (本服务不参与, 零额外开销)
    知识库未命中 → 模型冷启动生成该城市攻略 → 高德逐条校验 → 落盘 → 注入 prompt

为什么不直接联网搜
------------------
本项目 LLM 走 OpenAI 兼容端点, 不带宽带联网能力; 引入搜索工具需新增依赖与
key。而**幻觉可以用现成的验证器消掉**: 高德 POI 搜索是事实性数据源,
模型给出的景点名拿去高德搜, 搜不到的丢弃、搜到的直接用高德的真实坐标。
假景点进不了行程, 坐标也全都不是模型编的。代价是门票价格/开放时间这类文本
细节无法验证 —— 所以强制显著标注来源, 见 knowledge_source / notice。

落盘为什么放 knowledge/external/
--------------------------------
生成结果按知识库同款 Markdown 落盘到子目录, 即可被 RagService 的
_load_knowledge_documents / _parse_attraction_details 原样吸收 —— **零额外索引
代码**, 冷启动一次之后该城市就"转正"进知识库。放在 external/ 子目录是为了和
人工维护的城市文档区分开: 想丢弃机器生成内容时整个目录删掉即可。

降级保证: 生成/校验/落盘任一步失败都只返回 available=False, 调用方退回原行为
(纯高德 POI + 模型自身知识), 绝不阻断主流程。
"""

import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple

from langchain_core.prompts import ChatPromptTemplate

from ..config import get_settings
from ..models.schemas import POIInfo, TripRequest
from .llm_service import get_llm

logger = logging.getLogger(__name__)

# 生成内容落盘目录 (RagService 会把它当知识库一起索引)
EXTERNAL_DIR = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "external"

# 冷启动整体时间预算(秒): 超时即放弃本次冷启动, 不拖慢用户主请求。
# 生成只发生一次(之后命中磁盘缓存), 所以这里宁可放弃也不让用户等太久。
_COLD_START_BUDGET_S = 45.0
# 单次 LLM 调用超时(秒), 比全局 llm_timeout 短, 避免冷启动吃满主流程时间
_LLM_TIMEOUT_S = 30
# 注入 prompt 的生成内容上限(字符), 防止把主 prompt 撑爆
_MAX_INJECT_CHARS = 2400
# 高德校验景点数的上限 (每个都要一次 POI 搜索, 控制配额消耗)
_MAX_VALIDATE_NAMES = 6
# 高德 QPS 超限时的重试次数与退避基数(秒): 免费 key 并发稍高就会限流,
# 不退避重试会把真实景点当成"不存在"丢掉
_AMAP_QPS_RETRIES = 2
_AMAP_QPS_BACKOFF_S = 0.8
# 景点名长度/格式约束: 过滤掉模型输出的句子型"景点"
_MAX_NAME_CHARS = 24

# Markdown 围栏 (模型常把内容包在 ```markdown 里)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
# 结构标记行: ## 二级标题 / ### 景点名
_H2_RE = re.compile(r"^##\s+(.+)$")
_H3_RE = re.compile(r"^###\s+(.+)$")
# 景点详情行前缀, 用于判断校验后的段落是否有真实内容
_DETAIL_PREFIXES = ("-", "*", "门票", "开放", "建议", "地址", "交通", "打卡", "避坑")

GENERATOR_SYSTEM_PROMPT = """你是资深中文旅行攻略编辑。用户要去的城市不在系统的本地知识库里, 请凭你的知识补一份该城市的旅游攻略, 供行程规划参考。

**输出格式 (严格遵守, 只输出 Markdown, 不要任何解释性开场白或结束语):**
## 城市概览
一段 2-4 句话的介绍, 再用 3 条 `- ` 列表给出: 出行方式 / 消费水平 / 最佳季节。

## 必去景点
### 景点名称
- 门票：价格或"免费"
- 开放时间：时间或"以官方为准"
- 建议游玩：N 小时
- 地址：所在区/路
- 交通：到达方式
- 打卡点：值得看的点
- 避坑：需要注意的事

## 美食推荐
2-4 条 `- ` 列表, 每条写"菜品/小吃名：一句话说明"。

## 实用提示
2-4 条 `- ` 列表, 写交通卡/预约习惯/天气穿着等实用信息。

**硬性要求:**
1. 必须给出 4-6 个景点, 每个都要有 `### 景点名` 标题, 且景点名必须是真实存在、广为人知的名称(2-10 个字, 不要写句子)。
2. 景点名不要带修饰语、不要带括号注释、不要写英文别名。
3. 只写你有把握的内容。门票、开放时间这类可能变动的数字, 不确定就写"以官方为准", 禁止编造精确数字。
4. 地址只写到区/路这一级, 具体门牌号与交通线路拿不准就留空或写"建议导航确认", 不要编造。
"""

GENERATOR_USER_PROMPT = """请为「{city}」生成旅游攻略。
用户行程背景: {days}天行程, 交通方式 {transportation}, 住宿偏好 {accommodation}, 旅行偏好 {preferences}。
请按 system 中的格式输出该城市的攻略 Markdown。"""


@dataclass
class ColdStartUsage:
    """冷启动自身的 token 用量 (需并入总用量, 否则会漏报成本)"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    duration_ms: int = 0

    def merge(self, other: "ColdStartUsage") -> "ColdStartUsage":
        """合并另一次冷启动用量"""
        return ColdStartUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            calls=self.calls + other.calls,
            duration_ms=self.duration_ms + other.duration_ms,
        )


@dataclass
class ExternalKnowledge:
    """一次冷启动的结果"""

    available: bool = False                 # 是否拿到了可注入的生成内容
    city: str = ""
    markdown: str = ""                      # 生成的攻略全文
    source: str = "none"                    # knowledge_base | model_generated | none
    cached: bool = False                    # 是否命中磁盘缓存(未调用 LLM)
    validated_pois: List[POIInfo] = field(default_factory=list)  # 高德校验通过的景点
    usage: ColdStartUsage = field(default_factory=ColdStartUsage)


class ExternalKnowledgeService:
    """知识库未命中时的模型冷启动兜底 (单例)"""

    def __init__(self, rag_service=None):
        """初始化

        Args:
            rag_service: RagService 实例 (用于判定知识库覆盖与复用其 Markdown 解析)。
                         不传则惰性获取全局单例, 便于测试注入假实现。
        """
        self._rag_service = rag_service
        # 落盘并发保护: FastAPI 同步端点在线程池执行, 同城市并发请求可能同时生成
        self._lock = threading.Lock()
        # 本次进程内已尝试过冷启动的城市, 避免同一城市反复触发 LLM 调用
        self._attempted: Set[str] = set()

    # ============ 依赖与缓存路径 ============

    @property
    def rag_service(self):
        """惰性获取 RagService (避免模块级循环导入)"""
        if self._rag_service is None:
            from .rag_service import get_rag_service

            self._rag_service = get_rag_service()
        return self._rag_service

    @staticmethod
    def _cache_path(city: str) -> Optional[Path]:
        """城市 → 磁盘缓存文件路径; 城市名不可用于文件名时返回 None"""
        name = re.sub(r"[\\/:*?\"<>|\s]+", "", city or "").strip()
        if not name:
            return None
        return EXTERNAL_DIR / f"{name}.md"

    def is_city_covered(self, city: str) -> bool:
        """该城市是否已有知识库支撑 (含已落盘的外部知识)

        判定失败(如 Chroma 异常)时保守返回 True —— 宁可退回原有"无知识增强"的
        行为, 也不要在已有权威资料的场合生成模型内容去覆盖它。
        """
        try:
            return bool(self.rag_service.is_city_covered(city))
        except Exception as e:
            logger.warning(f"⚠️  知识库覆盖判定失败, 保守跳过冷启动: {e}")
            return True

    def _read_cache(self, city: str) -> Optional[str]:
        """读磁盘缓存, 未命中/读取失败返回 None"""
        path = self._cache_path(city)
        if not path or not path.exists():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
            return content or None
        except Exception as e:
            logger.warning(f"⚠️  外部知识缓存读取失败 ({path.name}): {e}")
            return None

    @staticmethod
    def _write_cache(city: str, content: str) -> bool:
        """原子写入磁盘缓存 (先写临时文件再替换, 避免并发读到半截文件)"""
        path = ExternalKnowledgeService._cache_path(city)
        if not path:
            return False
        try:
            EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(content, encoding="utf-8")
            shutil.move(str(tmp), str(path))
            return True
        except Exception as e:
            logger.warning(f"⚠️  外部知识缓存写入失败 ({path.name}): {e}")
            return False

    # ============ 主入口 ============

    def get_external_knowledge(self, city: str, request: Optional[TripRequest] = None) -> ExternalKnowledge:
        """获取(必要时生成)某城市的外部知识

        Args:
            city: 城市名
            request: 原始行程请求 (用于让生成内容贴合偏好); 可为 None

        Returns:
            ExternalKnowledge; 任一环节失败都返回 available=False
        """
        city = (city or "").strip()
        if not city:
            return ExternalKnowledge(city=city)

        # 0. 该城市本来就有知识库, 只是索引尚未就绪(启动未建/建失败) → 绝不能生成,
        #    否则会用模型内容覆盖人工维护的权威资料。
        if self.is_city_covered(city):
            return ExternalKnowledge(city=city)

        # 1. 磁盘缓存: 之前生成过 → 直接用, 不再调 LLM (冷启动只有第一次付费)
        cached_md = self._read_cache(city)
        if cached_md:
            logger.info(f"📚 外部知识命中缓存: {city} (未调用 LLM)")
            return ExternalKnowledge(
                available=True, city=city, markdown=cached_md,
                source="model_generated", cached=True,
            )

        # 2. 本次进程已尝试过且失败 → 不重复烧钱
        if city in self._attempted:
            return ExternalKnowledge(city=city)

        # 3. 生成 (加锁: 同城市并发请求只让一个真正调用 LLM)
        with self._lock:
            cached_md = self._read_cache(city)  # 等锁期间可能已被别的线程写好
            if cached_md:
                return ExternalKnowledge(
                    available=True, city=city, markdown=cached_md,
                    source="model_generated", cached=True,
                )
            if city in self._attempted:
                return ExternalKnowledge(city=city)
            self._attempted.add(city)

        return self._generate(city, request)

    def _generate(self, city: str, request: Optional[TripRequest]) -> ExternalKnowledge:
        """调用模型生成攻略并落盘 (不发高德请求; 景点校验由调用方按需触发)"""
        started = time.monotonic()
        logger.info(f"🌐 知识库未收录 {city}, 触发模型冷启动生成...")
        try:
            chain = ChatPromptTemplate.from_messages([
                ("system", GENERATOR_SYSTEM_PROMPT),
                ("human", GENERATOR_USER_PROMPT),
            ]) | get_llm().with_config(timeout=_LLM_TIMEOUT_S)
            response = chain.invoke(self._prompt_vars(city, request))
        except Exception as e:
            logger.warning(f"⚠️  {city} 冷启动生成失败(退回纯高德检索): {e}")
            return ExternalKnowledge(city=city)

        duration_ms = int((time.monotonic() - started) * 1000)
        content = response.content if hasattr(response, "content") else str(response)
        markdown = self._clean_markdown(content)
        if not markdown:
            logger.warning(f"⚠️  {city} 冷启动返回空内容, 本次跳过")
            return ExternalKnowledge(city=city)

        # 落盘供 RagService 后续索引 (失败不影响本次注入)
        if self._write_cache(city, markdown):
            logger.info(f"💾 {city} 生成内容已落盘: {self._cache_path(city)}")

        usage = self._extract_usage(response)
        usage.duration_ms = duration_ms
        usage.calls = 1
        logger.info(
            f"🌐 {city} 冷启动完成: {len(markdown)} 字符 | tokens={usage.total_tokens} | {duration_ms}ms"
        )
        return ExternalKnowledge(
            available=True, city=city, markdown=markdown,
            source="model_generated", cached=False, usage=usage,
        )

    @staticmethod
    def _extract_usage(response) -> ColdStartUsage:
        """从 LLM 响应中提取 token 用量 (兼容新旧 langchain-openai 字段命名)"""
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            meta = getattr(response, "response_metadata", None) or {}
            usage = meta.get("token_usage") or {}
        return ColdStartUsage(
            input_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0,
            output_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
        )

    @staticmethod
    def _prompt_vars(city: str, request: Optional[TripRequest]) -> dict:
        """构造生成 prompt 的变量 (无请求时给中性背景)"""
        if request is None:
            return {
                "city": city, "days": "若干", "transportation": "不限",
                "accommodation": "不限", "preferences": "无特别偏好",
            }
        return {
            "city": city,
            "days": request.travel_days,
            "transportation": request.transportation or "不限",
            "accommodation": request.accommodation or "不限",
            "preferences": "、".join(request.preferences) if request.preferences else "无特别偏好",
        }

    # ============ 内容处理 ============

    @staticmethod
    def _clean_markdown(content: str) -> str:
        """清理模型输出: 去掉代码围栏与包裹引号, 统一标题层级

        :return: 规范化后的 Markdown; 没有有效标题时返回空串(视为生成失败)
        """
        text = _FENCE_RE.sub("", (content or "").strip()).strip()
        # 有的模型会把整段内容用引号包起来
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
            text = text[1:-1].strip()
        if not text:
            return ""

        lines: List[str] = []
        for raw in text.splitlines():
            line = raw.rstrip()
            # 模型可能用 # 一级标题写城市名, 降级成 ## (知识库解析器会丢弃 # 行)
            if line.startswith("# ") and not line.startswith("## "):
                line = "#" + line
            lines.append(line)
        cleaned = "\n".join(lines).strip()

        # 至少要有一个 ## 标题, 否则解析不出任何片段, 视为无效
        if not any(_H2_RE.match(ln.strip()) for ln in cleaned.splitlines()):
            return ""
        return cleaned

    @staticmethod
    def extract_attraction_names(markdown: str, limit: int = _MAX_VALIDATE_NAMES) -> List[str]:
        """从生成内容里抽出候选景点名 (`### 标题`)

        只取结构规整、长度合理的标题, 过滤掉模型偶尔输出的句子型标题。
        """
        names: List[str] = []
        for raw in (markdown or "").splitlines():
            m = _H3_RE.match(raw.strip())
            if not m:
                continue
            name = m.group(1).strip().strip("*`# ").strip()
            if not name or len(name) > _MAX_NAME_CHARS:
                continue
            # 去掉常见后缀噪音, 保留核心景点名
            name = re.sub(r"[（(].*?[)）]", "", name).strip()
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                break
        return names

    def collect_validated_pois(
        self, knowledge: ExternalKnowledge, city: str, amap_service, deadline: float
    ) -> List[POIInfo]:
        """把生成内容里的景点名拿到高德逐条校验, 只返回真实存在的 POI

        这是防幻觉的关键一步: 模型编造的景点在高德搜不到 → 直接丢弃, 不会进
        候选池; 命中的 POI 带高德真实坐标, 行程里的位置数据全部来自高德。

        Args:
            knowledge: 冷启动结果
            city: 城市名 (限定搜索范围)
            amap_service: AmapService 实例
            deadline: time.monotonic() 时间戳, 超时即停止校验(剩余景点放弃)

        Returns:
            校验通过的 POIInfo 列表 (按生成顺序)
        """
        pois: List[POIInfo] = []
        if not knowledge.available or amap_service is None:
            return pois
        for name in self.extract_attraction_names(knowledge.markdown):
            if time.monotonic() > deadline:
                logger.info(f"⏱️  {city} 景点校验超时, 已校验 {len(pois)} 个后停止")
                break
            found = self._search_with_retry(amap_service, name, city)
            if found is None:
                continue  # "不确定"(调用失败/限流) 与 "确认不存在"(空列表) 区别对待
            if not found:
                logger.info(f"   ✗ 模型提到的「{name}」高德未收录, 已丢弃")
                continue
            pois.append(found[0])
            logger.info(f"   ✓ 冷启动景点通过高德校验: {name}")
        knowledge.validated_pois = pois
        return pois

    @staticmethod
    def _search_with_retry(amap_service, name: str, city: str):
        """高德 POI 搜索, 遇 QPS 超限短暂退避重试

        没有重试时, 一次 QPS 超限会把这个景点静默丢掉 —— 而它可能完全真实,
        只是撞上了并发配额(实测免费 key 常见)。退避重试尽量把真实景点留下。

        Returns:
            命中列表 / 空列表(确认不存在) / None(调用失败, 无法判断)
        """
        last_error = None
        for attempt in range(_AMAP_QPS_RETRIES + 1):
            try:
                return amap_service.search_poi(name, city)
            except Exception as e:
                last_error = e
                if "CUQPS_HAS_EXCEEDED_THE_LIMIT" not in str(e):
                    logger.warning(f"   ⚠️ 高德校验「{name}」失败: {e}")
                    return None
                if attempt < _AMAP_QPS_RETRIES:
                    time.sleep(_AMAP_QPS_BACKOFF_S * (attempt + 1))
        logger.warning(f"   ⚠️ 高德校验「{name}」连续 QPS 超限, 本次放弃: {last_error}")
        return None

    # ============ 注入与提示文案 ============

    def build_context(self, knowledge: ExternalKnowledge, max_chars: int = _MAX_INJECT_CHARS) -> str:
        """把生成内容包装成可注入 prompt 的上下文块 (带来源与可信度声明)"""
        if not knowledge.available or not knowledge.markdown:
            return ""
        body = knowledge.markdown[:max_chars]
        origin = "本地缓存" if knowledge.cached else "本次由模型临时生成"
        return (
            f"## 该城市暂无本地知识库, 以下内容来自模型自身知识 ({origin}, 未经核实):\n"
            f"⚠️ 这些内容是参考, 不是权威数据。门票/开放时间/交通等可能过时或不准确, "
            f"请勿编造精确数字, 拿不准时在描述中注明「以官方为准」。\n\n"
            f"{body}"
        )

    @staticmethod
    def build_notice(source: str, city: str) -> str:
        """生成给用户看的数据来源提示 (前端横幅文案)"""
        if source == "model_generated":
            return (
                f"「{city}」暂未收录本地知识库, 以上攻略信息由模型自身知识生成 (未经核实), "
                f"门票价格、开放时间等可能已变动, 出行前请以官方渠道为准。"
            )
        if source == "none":
            return (
                f"「{city}」暂未收录本地知识库, 且模型补充知识未成功获取, "
                f"行程主要基于高德地图实时检索结果, 当地细节请自行核实。"
            )
        return ""


# 全局单例
_external_knowledge_service: Optional[ExternalKnowledgeService] = None


def get_external_knowledge_service(rag_service=None) -> ExternalKnowledgeService:
    """获取外部知识冷启动服务实例(单例模式)"""
    global _external_knowledge_service
    if _external_knowledge_service is None:
        _external_knowledge_service = ExternalKnowledgeService(rag_service=rag_service)
    return _external_knowledge_service
