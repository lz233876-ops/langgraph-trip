"""外部知识冷启动服务 + 知识库覆盖判定 测试

全部离线: LLM 与高德均用桩替换, 不发任何真实请求。
覆盖重点不是"文案对不对", 而是几条会出错的边界:
  1. 已收录城市绝不能触发冷启动 (否则模型内容会盖掉人工维护的资料);
  2. 模型编造的景点必须被高德校验挡在候选池之外;
  3. 生成内容要能落盘并被 RagService 当知识库吸收 (冷启动一次即"转正");
  4. 冷启动的 token 用量必须并入总用量, 不能漏报成本。
"""

import sys
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.schemas import Location, POIInfo, TripRequest
from app.services import external_knowledge_service as eks
from app.services.external_knowledge_service import (
    ColdStartUsage,
    ExternalKnowledge,
    ExternalKnowledgeService,
)

# ============ 测试用假数据 ============

GENERATED_MD = """## 城市概览

一座有历史的城市。

- 出行方式：地铁+公交
- 消费水平：人均 50 元
- 最佳季节：春秋

## 必去景点

### 真实景点甲
- 门票：免费
- 开放时间：以官方为准
- 建议游玩：2 小时

### 编造景点乙
- 门票：100 元
- 开放时间：08:00-18:00

### 这一个标题明显是一整句话所以不该被当成景点名去校验
- 门票：0 元

## 美食推荐

- 当地小吃：好吃
"""


class _FakeResponse:
    """模拟 AIMessage"""

    def __init__(self, content: str, usage: dict = None):
        self.content = content
        self.usage_metadata = usage or {}
        self.response_metadata = {}


class _FakeChain(RunnableLambda):
    """模拟 LLM: 必须是 LangChain Runnable, 否则 prompt | llm 的管道拼不起来"""

    def __init__(self, content: str = GENERATED_MD, usage: dict = None):
        super().__init__(self._respond)
        self.content = content
        self.usage = usage or {"input_tokens": 120, "output_tokens": 340, "total_tokens": 460}
        self.calls = 0

    def _respond(self, variables):
        self.calls += 1
        return _FakeResponse(self.content, self.usage)


class _FakeAmap:
    """模拟高德: 只"收录"真实景点甲"""

    def __init__(self, known=None):
        self.known = known if known is not None else {"真实景点甲"}
        self.queries = []

    def search_poi(self, keywords, city, citylimit=True):
        self.queries.append(keywords)
        if keywords in self.known:
            return [POIInfo(
                id="p1", name=keywords, type="风景名胜", address=f"{city}某区",
                location=Location(longitude=108.9, latitude=34.2),
            )]
        return []


class _FakeRag:
    """模拟 RagService 的覆盖判定"""

    def __init__(self, covered=("北京", "上海"), fail=False):
        self.covered = set(covered)
        self.fail = fail

    def is_city_covered(self, city):
        if self.fail:
            raise RuntimeError("chroma boom")
        return city in self.covered


@pytest.fixture
def tmp_external(monkeypatch, tmp_path):
    """把落盘目录指向临时目录, 避免污染真实知识库"""
    target = tmp_path / "external"
    monkeypatch.setattr(eks, "EXTERNAL_DIR", target)
    return target


@pytest.fixture
def service(tmp_external):
    """桩替换后的冷启动服务 (rag 覆盖判定可控)"""
    return ExternalKnowledgeService(rag_service=_FakeRag())


def _request(city="西安", **kw):
    base = dict(
        city=city, start_date="2025-06-01", end_date="2025-06-03", travel_days=3,
        transportation="公共交通", accommodation="经济型酒店",
    )
    base.update(kw)
    return TripRequest(**base)


# ============ 内容清洗与解析 ============

def test_clean_markdown_strips_fence_and_promotes_h1():
    """代码围栏要去掉, 模型用 # 写的城市名要降级成 ## (否则被解析器丢弃)"""
    raw = "```markdown\n# 西安\n\n## 城市概览\n\n内容\n```"
    cleaned = ExternalKnowledgeService._clean_markdown(raw)
    assert "```" not in cleaned
    assert cleaned.startswith("## 西安")
    assert "## 城市概览" in cleaned


def test_clean_markdown_rejects_content_without_h2():
    """没有 ## 标题的内容解析不出片段, 必须判定为无效而不是硬塞进 prompt"""
    assert ExternalKnowledgeService._clean_markdown("就是一段话, 没有标题") == ""
    assert ExternalKnowledgeService._clean_markdown("") == ""


def test_extract_attraction_names_filters_noise():
    """只取结构规整的 ### 标题, 句子型标题与括号注释要过滤掉"""
    names = ExternalKnowledgeService.extract_attraction_names(GENERATED_MD, limit=10)
    assert "真实景点甲" in names
    assert "编造景点乙" in names
    # 超长句子型标题被丢弃
    assert all(len(n) <= 24 for n in names)
    assert not any("一整句话" in n for n in names)


def test_extract_attraction_names_respects_limit():
    """景点数必须封顶, 否则每个都要一次高德请求, 会吃掉配额"""
    md = "\n".join(f"### 景点{i}\n- 门票：免费" for i in range(20))
    assert len(ExternalKnowledgeService.extract_attraction_names(md, limit=4)) == 4


# ============ 覆盖判定: 防止覆盖人工知识库 ============

def test_covered_city_never_triggers_generation(service, monkeypatch):
    """已收录城市必须直接短路, 连 LLM 都不能碰"""
    chain = _FakeChain()
    monkeypatch.setattr(eks, "get_llm", lambda: chain)

    result = service.get_external_knowledge("北京", _request("北京"))

    assert result.available is False
    assert result.source == "none"
    assert chain.calls == 0, "已收录城市不得触发冷启动生成"


def test_coverage_judgement_failure_is_conservative(monkeypatch, tmp_external):
    """覆盖判定本身报错时, 宁可退回无增强, 也不能生成内容去盖掉知识库"""
    service = ExternalKnowledgeService(rag_service=_FakeRag(fail=True))
    chain = _FakeChain()
    monkeypatch.setattr(eks, "get_llm", lambda: chain)

    assert service.is_city_covered("西安") is True
    assert service.get_external_knowledge("西安", _request()).available is False
    assert chain.calls == 0


def test_unknown_city_triggers_generation_and_caches(service, monkeypatch):
    """未收录城市: 生成 → 落盘; 第二次请求命中缓存不再调 LLM"""
    chain = _FakeChain()
    monkeypatch.setattr(eks, "get_llm", lambda: chain)

    first = service.get_external_knowledge("西安", _request())
    assert first.available is True
    assert first.source == "model_generated"
    assert first.cached is False
    assert chain.calls == 1

    second = service.get_external_knowledge("西安", _request())
    assert second.available is True
    assert second.cached is True, "第二次必须命中磁盘缓存"
    assert chain.calls == 1, "命中缓存时不得再次调用 LLM"
    assert second.markdown == first.markdown


def test_cache_file_is_written_where_rag_can_index_it(service, tmp_external, monkeypatch):
    """落盘位置必须能被 RagService 的扫描吸收, 否则冷启动城市无法"转正""" ""
    monkeypatch.setattr(eks, "get_llm", lambda: _FakeChain())
    service.get_external_knowledge("西安", _request())
    path = tmp_external / "西安.md"
    assert path.exists()
    assert "## 城市概览" in path.read_text(encoding="utf-8")


def test_generation_failure_degrades_silently(service, monkeypatch):
    """LLM 抛异常时只降级, 不能让整个行程规划挂掉"""
    def _boom():
        raise RuntimeError("llm down")

    monkeypatch.setattr(eks, "get_llm", _boom)
    result = service.get_external_knowledge("西安", _request())
    assert result.available is False
    assert result.markdown == ""


def test_failed_city_is_not_retried_within_process(service, monkeypatch):
    """同进程内失败的城市不重复烧钱 (失败也要记忆)"""
    chain = _FakeChain(content="无标题内容, 解析不出片段")
    monkeypatch.setattr(eks, "get_llm", lambda: chain)

    assert service.get_external_knowledge("西安", _request()).available is False
    assert service.get_external_knowledge("西安", _request()).available is False
    assert chain.calls == 1


def test_usage_is_extracted(service, monkeypatch):
    """token 用量要提取出来, 供上层并入总成本"""
    monkeypatch.setattr(eks, "get_llm", lambda: _FakeChain())
    result = service.get_external_knowledge("西安", _request())
    assert result.usage.total_tokens == 460
    assert result.usage.calls == 1


# ============ 高德校验: 防幻觉的关键 ============

def test_collect_validated_pois_drops_hallucinated_names(service, monkeypatch):
    """模型编造的景点搜不到 → 丢弃; 真实景点保留且坐标来自高德"""
    monkeypatch.setattr(eks, "get_llm", lambda: _FakeChain())
    knowledge = service.get_external_knowledge("西安", _request())
    amap = _FakeAmap(known={"真实景点甲"})

    import time as _time
    pois = service.collect_validated_pois(knowledge, "西安", amap, _time.monotonic() + 5)

    assert [p.name for p in pois] == ["真实景点甲"]
    assert pois[0].location.longitude == 108.9, "坐标必须来自高德, 不是模型编的"


def test_collect_validated_pois_stops_at_deadline(service, monkeypatch):
    """时间预算耗尽即停止校验, 不拖慢用户主请求"""
    monkeypatch.setattr(eks, "get_llm", lambda: _FakeChain())
    knowledge = service.get_external_knowledge("西安", _request())
    amap = _FakeAmap()

    import time as _time
    pois = service.collect_validated_pois(knowledge, "西安", amap, _time.monotonic() - 1)
    assert pois == []
    assert amap.queries == [], "已超时就不该再发高德请求"


def test_collect_validated_pois_tolerates_amap_errors(service, monkeypatch):
    """单个景点查询失败不能中断整轮校验"""
    class _FlakyAmap(_FakeAmap):
        def search_poi(self, keywords, city, citylimit=True):
            if keywords == "编造景点乙":
                raise RuntimeError("amap 500")
            return super().search_poi(keywords, city, citylimit)

    monkeypatch.setattr(eks, "get_llm", lambda: _FakeChain())
    knowledge = service.get_external_knowledge("西安", _request())

    import time as _time
    pois = service.collect_validated_pois(
        knowledge, "西安", _FlakyAmap(), _time.monotonic() + 5
    )
    assert [p.name for p in pois] == ["真实景点甲"]


def test_amap_qps_limit_is_retried_not_dropped(service, monkeypatch):
    """QPS 超限是暂时性的, 重试后要把真实景点留下, 不能当"不存在"丢掉"""
    class _QpsAmap(_FakeAmap):
        def __init__(self):
            super().__init__()
            self.failures = 0

        def search_poi(self, keywords, city, citylimit=True):
            if self.failures < 1:
                self.failures += 1
                raise RuntimeError("高德API错误: CUQPS_HAS_EXCEEDED_THE_LIMIT")
            return super().search_poi(keywords, city, citylimit)

    monkeypatch.setattr(eks, "get_llm", lambda: _FakeChain())
    monkeypatch.setattr(eks, "_AMAP_QPS_BACKOFF_S", 0)  # 不真的等待
    knowledge = service.get_external_knowledge("西安", _request())

    import time as _time
    pois = service.collect_validated_pois(
        knowledge, "西安", _QpsAmap(), _time.monotonic() + 5
    )
    assert "真实景点甲" in [p.name for p in pois]


def test_amap_qps_gives_up_after_retries(service, monkeypatch):
    """持续限流时最终放弃, 但不能把景点误判为"确认不存在""" ""
    class _AlwaysQps(_FakeAmap):
        def search_poi(self, keywords, city, citylimit=True):
            raise RuntimeError("高德API错误: CUQPS_HAS_EXCEEDED_THE_LIMIT")

    monkeypatch.setattr(eks, "get_llm", lambda: _FakeChain())
    monkeypatch.setattr(eks, "_AMAP_QPS_BACKOFF_S", 0)
    knowledge = service.get_external_knowledge("西安", _request())

    import time as _time
    pois = service.collect_validated_pois(
        knowledge, "西安", _AlwaysQps(), _time.monotonic() + 5
    )
    assert pois == []


# ============ 注入文本与用户提示 ============

def test_build_context_marks_unverified_source():
    """注入 prompt 时必须声明来源与不可信, 否则下游会把生成内容当权威数据"""
    knowledge = ExternalKnowledge(
        available=True, city="西安", markdown="## 城市概览\n内容", source="model_generated"
    )
    context = ExternalKnowledgeService().build_context(knowledge)
    assert "暂无本地知识库" in context
    assert "未经核实" in context
    assert "## 城市概览" in context


def test_build_context_empty_when_unavailable():
    assert ExternalKnowledgeService().build_context(ExternalKnowledge()) == ""


def test_build_context_truncates():
    """生成内容超长时要截断, 不能把主 prompt 撑爆"""
    knowledge = ExternalKnowledge(
        available=True, city="西安", markdown="x" * 9000, source="model_generated"
    )
    context = ExternalKnowledgeService().build_context(knowledge, max_chars=100)
    assert len(context) < 400


def test_build_notice_wording():
    """提示文案要区分"模型生成"与"完全没有知识"; 知识库来源不提示"""
    assert "模型自身知识生成" in ExternalKnowledgeService.build_notice("model_generated", "西安")
    assert "未成功获取" in ExternalKnowledgeService.build_notice("none", "西安")
    assert ExternalKnowledgeService.build_notice("knowledge_base", "北京") == ""


def test_cold_start_usage_merge():
    a = ColdStartUsage(input_tokens=1, output_tokens=2, total_tokens=3, calls=1, duration_ms=10)
    b = ColdStartUsage(input_tokens=10, output_tokens=20, total_tokens=30, calls=1, duration_ms=100)
    merged = a.merge(b)
    assert merged == ColdStartUsage(input_tokens=11, output_tokens=22, total_tokens=33, calls=2, duration_ms=110)
