"""知识库覆盖判定 + 规划器冷启动接线 集成测试

重点验证三件事:
  1. 覆盖判定用的是向量库真实收录城市 (而非硬编码名单), 且兼容"西安市/西安"写法差异;
  2. 已收录城市照旧走原 RAG 链路, 不会被冷启动改写;
  3. 未收录城市: 生成内容进 prompt、校验过的景点进候选池、用量并入总量。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import trip_planner_agent as tpa
from app.models.schemas import Location, POIInfo, TokenUsage, TripRequest
from app.services import external_knowledge_service as eks
from app.services.external_knowledge_service import (
    ColdStartUsage,
    ExternalKnowledge,
    ExternalKnowledgeService,
)
from app.services.rag_service import RagService, iter_knowledge_files

# ============ 覆盖判定 ============


class _FakeStore:
    """模拟 Chroma: 只返回 metadata, 支持按城市过滤"""

    def __init__(self, cities):
        self.cities = list(cities)

    def get(self, where=None, include=None):
        cities = self.cities
        if where:
            wanted = where.get("city")
            cities = [c for c in cities if c == wanted]
        return {
            "documents": [f"{c} 的片段" for c in cities],
            "metadatas": [{"city": c, "source": f"{c}.md"} for c in cities],
        }


def _rag_with(cities, monkeypatch):
    """构造只带覆盖判定所需状态的最小 RagService (跳过真实 Chroma 初始化)"""
    rag = RagService.__new__(RagService)
    rag.settings = None
    rag._embedding = object()          # 让 enabled 为 True (假装已启用)
    rag._knowledge_store = _FakeStore(cities)
    rag._history_store = None
    rag._bm25_cache = {}
    rag._attraction_details = {}
    rag._covered_cities_cache = None
    return rag


def test_covered_cities_comes_from_vector_store(monkeypatch):
    """收录城市应取自向量库实际数据, 这样外部知识入库后自动算作已收录"""
    rag = _rag_with(["北京", "西安"], monkeypatch)
    assert rag.covered_cities() == {"北京", "西安"}


def test_is_city_covered_handles_suffix_and_whitespace(monkeypatch):
    """前端可能输入"西安市"/带空格, 必须与知识库里的"西安"视为同一城市"""
    rag = _rag_with(["西安"], monkeypatch)
    assert rag.is_city_covered("西安") is True
    assert rag.is_city_covered("西安市") is True
    assert rag.is_city_covered(" 西安 ") is True
    assert rag.is_city_covered("成都") is False


def test_covered_cities_falls_back_to_filenames_when_index_empty(monkeypatch):
    """索引还没建好时, 也不能把已知城市误判成未知城市(否则会生成内容覆盖知识库)"""
    rag = _rag_with([], monkeypatch)
    assert "北京" in rag.covered_cities()
    assert rag.is_city_covered("北京") is True
    assert rag.is_city_covered("拉萨") is False


def test_coverage_cache_invalidated_on_rebuild(monkeypatch):
    """重建索引后覆盖判定必须重算, 否则新入库的城市仍被当成未收录"""
    rag = _rag_with(["北京"], monkeypatch)
    assert rag.covered_cities() == {"北京"}
    rag._knowledge_store = _FakeStore(["北京", "西安"])
    rag._covered_cities_cache = None  # build_knowledge_index 会做这件事
    assert rag.covered_cities() == {"北京", "西安"}


def test_iter_knowledge_files_includes_external_dir(tmp_path, monkeypatch):
    """external/ 子目录必须被知识文档扫描吸收 (冷启动城市转正的唯一机制)"""
    knowledge = tmp_path / "knowledge"
    external = knowledge / "external"
    external.mkdir(parents=True)
    (knowledge / "beijing.md").write_text("## 概览\n内容", encoding="utf-8")
    (external / "xian.md").write_text("## 概览\n内容", encoding="utf-8")

    monkeypatch.setattr("app.services.rag_service.KNOWLEDGE_DIR", knowledge)
    monkeypatch.setattr("app.services.rag_service.EXTERNAL_KNOWLEDGE_DIR", external)

    names = {p.name for p in iter_knowledge_files()}
    assert names == {"beijing.md", "xian.md"}


# ============ 规划器接线 ============


class _StubExternalService:
    """桩: 记录被问过哪些城市, 返回预设冷启动结果"""

    def __init__(self, covered=(), knowledge=None, usage=None):
        self.covered = set(covered)
        self.knowledge = knowledge
        self.usage = usage or ColdStartUsage()
        self.asked = []

    def is_city_covered(self, city):
        return city in self.covered

    def get_external_knowledge(self, city, request=None):
        self.asked.append(city)
        if self.knowledge is None:
            return ExternalKnowledge(city=city)
        self.knowledge.usage = self.usage
        return self.knowledge

    def collect_validated_pois(self, knowledge, city, amap_service, deadline):
        return list(knowledge.validated_pois)

    def build_context(self, knowledge, max_chars=2400):
        # 注意: 真实实现是实例方法, 桩这里是普通函数 —— 转发时必须显式传参
        return ExternalKnowledgeService().build_context(knowledge, max_chars)

    @staticmethod
    def build_notice(source, city):
        return ExternalKnowledgeService.build_notice(source, city)


class _StubAmap:
    def __init__(self, results=None):
        self.results = results or {}

    def search_poi(self, keywords, city, citylimit=True):
        return self.results.get(keywords, [])


@pytest.fixture
def planner(monkeypatch):
    """只注入依赖的规划器 (不触发真实 LLM/高德初始化)"""
    p = tpa.MultiAgentTripPlanner.__new__(tpa.MultiAgentTripPlanner)
    p.amap_service = _StubAmap()
    return p


def _poi(name, city="西安"):
    return POIInfo(
        id="x1", name=name, type="风景名胜", address=f"{city}某区",
        location=Location(longitude=108.9, latitude=34.2),
    )


def _request(city="西安"):
    return TripRequest(
        city=city, start_date="2025-06-01", end_date="2025-06-03", travel_days=3,
        transportation="公共交通", accommodation="经济型酒店",
    )


def _patch_service(monkeypatch, stub):
    monkeypatch.setattr(eks, "get_external_knowledge_service", lambda *a, **k: stub)
    return stub


def test_cold_start_marks_unknown_city(planner, monkeypatch):
    """未收录城市: 应产出 model_generated 并把校验过的景点并入候选池"""
    knowledge = ExternalKnowledge(
        available=True, city="西安", markdown="## 必去景点\n### 真实景点甲\n- 门票：免费",
        source="model_generated",
    )
    knowledge.validated_pois = [_poi("真实景点甲")]
    _patch_service(monkeypatch, _StubExternalService(
        covered=("北京",), knowledge=knowledge,
        usage=ColdStartUsage(input_tokens=10, output_tokens=20, total_tokens=30, calls=1, duration_ms=500),
    ))

    state = planner._cold_start_knowledge(_request(), [])

    assert state["state"]["knowledge_source"] == "model_generated"
    assert [p.name for p in state["validated_pois"]] == ["真实景点甲"]
    assert state["state"]["cold_start_usage"].total_tokens == 30
    assert state["state"]["cold_start_calls"] == 1
    assert state["state"]["external_knowledge"] is knowledge


def test_cold_start_never_runs_for_covered_city(planner, monkeypatch):
    """已收录城市必须短路, 连生成都不能触发"""
    stub = _patch_service(monkeypatch, _StubExternalService(covered=("西安",)))
    state = planner._cold_start_knowledge(_request("西安"), [])
    assert state["state"]["knowledge_source"] == "knowledge_base"
    assert state["validated_pois"] == []
    assert stub.asked == [], "已收录城市不得请求外部知识"


def test_cold_start_degrades_when_generation_unavailable(planner, monkeypatch):
    """生成失败时只标注 none, 不影响主流程继续用高德结果"""
    _patch_service(monkeypatch, _StubExternalService(covered=()))
    state = planner._cold_start_knowledge(_request(), [])
    assert state["state"]["knowledge_source"] == "none"
    assert state["validated_pois"] == []


def test_cold_start_skips_duplicate_attractions(planner, monkeypatch):
    """冷启动景点与高德已搜到的重复时要去重, 否则同一天会出现两个同名景点"""
    knowledge = ExternalKnowledge(
        available=True, city="西安", markdown="## 景点\n### 真实景点甲\n- 门票：免费",
        source="model_generated",
    )
    knowledge.validated_pois = [_poi("真实景点甲")]
    _patch_service(monkeypatch, _StubExternalService(covered=(), knowledge=knowledge))

    state = planner._cold_start_knowledge(_request(), [_poi("真实景点甲")])
    assert state["validated_pois"] == []


def test_external_context_injected_into_prompt(planner, monkeypatch):
    """生成内容要真的进 prompt, 否则冷启动等于白做"""
    knowledge = ExternalKnowledge(
        available=True, city="西安", markdown="## 必去景点\n### 真实景点甲\n- 门票：免费",
        source="model_generated", cached=True,
    )
    _patch_service(monkeypatch, _StubExternalService())
    # 传入桩 rag_service 以避免触发全局单例(单例初始化会去连真实 Chroma)
    service = eks.get_external_knowledge_service(rag_service=object())

    context = service.build_context(knowledge)
    assert "## 必去景点" in context
    assert "本地缓存" in context, "命中缓存时来源说明要如实反映"
    assert planner._external_context({}) == ""


def test_notice_only_for_non_knowledge_base(planner, monkeypatch):
    """只有非知识库来源才提示用户核实"""
    _patch_service(monkeypatch, _StubExternalService())
    assert planner._build_knowledge_notice("knowledge_base", "北京") == ""
    assert "模型自身知识生成" in planner._build_knowledge_notice("model_generated", "西安")
    assert "未成功获取" in planner._build_knowledge_notice("none", "西安")


def test_usage_sums_cold_start_and_generation():
    """冷启动用量必须并入总量, 否则成本统计漏报"""
    gen = TokenUsage(input_tokens=100, output_tokens=200, total_tokens=300)
    cold = TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30)
    merged = TokenUsage(
        input_tokens=gen.input_tokens + cold.input_tokens,
        output_tokens=gen.output_tokens + cold.output_tokens,
        total_tokens=gen.total_tokens + cold.total_tokens,
    )
    assert merged.total_tokens == 330
    assert merged.input_tokens == 110


# ============ 接口契约: 新字段必须出现在 HTTP 响应里 ============

def test_route_exposes_knowledge_source_and_notice(client, monkeypatch):
    """知识来源与提示文案必须真的序列化到 /api/trip/plan 的响应 JSON 中

    前端横幅完全依赖这两个字段; 如果模型层加了字段却没进响应, 前端会静默不显示,
    而用户就会把模型生成的门票/时间当成事实 —— 这是本功能最不能接受的失败方式。
    """
    from unittest.mock import Mock

    from app.models.schemas import (
        Attraction, Budget, DayPlan, Location, TripPlan, TripUsage,
    )

    plan = TripPlan(
        city="洛阳", start_date="2026-08-01", end_date="2026-08-02",
        days=[DayPlan(
            date="2026-08-01", day_index=0, description="第1天", transportation="公共交通",
            accommodation="经济型酒店",
            attractions=[Attraction(
                name="龙门石窟", address="洛阳市洛龙区",
                location=Location(longitude=112.477, latitude=34.558),
                visit_duration=180, description="世界文化遗产",
            )],
            meals=[],
        )],
        overall_suggestions="建议早去", budget=Budget(total=100),
        knowledge_source="model_generated", notice="「洛阳」暂未收录本地知识库",
    )
    fake_agent = Mock()
    fake_agent.plan_trip.return_value = (plan, TripUsage())
    monkeypatch.setattr("app.api.routes.trip.get_trip_planner_agent", lambda: fake_agent)

    resp = client.post("/api/trip/plan", json={
        "city": "洛阳", "start_date": "2026-08-01", "end_date": "2026-08-02",
        "travel_days": 2, "transportation": "公共交通", "accommodation": "经济型酒店",
        "preferences": [], "free_text_input": "",
    })

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["knowledge_source"] == "model_generated"
    assert "暂未收录本地知识库" in data["notice"]
