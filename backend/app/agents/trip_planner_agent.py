"""基于 LangGraph 的多智能体旅行规划系统"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import TypedDict, List
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from ..services.llm_service import get_llm
from ..services.amap_service import get_amap_service
from ..models.schemas import (
    TripRequest,
    TripPlan,
    DayPlan,
    Attraction,
    Meal,
    Location,
    Hotel,
    Budget,
    WeatherInfo,
    POIInfo,
)

logger = logging.getLogger(__name__)

# ============ 行程规划提示词 ============

PLANNER_SYSTEM_PROMPT = """你是专业的行程规划专家。根据用户提供的景点、天气和酒店信息, 生成详细的旅行计划。

**输出要求:**
必须只输出一个 JSON 对象, 不要输出任何其他文字, JSON 结构严格如下:
{{
  "city": "城市名称",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "days": [
    {{
      "date": "YYYY-MM-DD",
      "day_index": 0,
      "description": "第1天行程概述",
      "transportation": "交通方式",
      "accommodation": "住宿类型",
      "hotel": {{
        "name": "酒店名称",
        "address": "酒店地址",
        "location": {{"longitude": 116.397128, "latitude": 39.916527}},
        "price_range": "300-500元",
        "rating": "4.5",
        "distance": "距离景点2公里",
        "type": "经济型酒店",
        "estimated_cost": 400
      }},
      "attractions": [
        {{
          "name": "景点名称",
          "address": "详细地址",
          "location": {{"longitude": 116.397128, "latitude": 39.916527}},
          "visit_duration": 120,
          "description": "景点详细描述",
          "category": "景点类别",
          "ticket_price": 60
        }}
      ],
      "meals": [
        {{"type": "breakfast", "name": "早餐推荐", "description": "早餐描述", "estimated_cost": 30}},
        {{"type": "lunch", "name": "午餐推荐", "description": "午餐描述", "estimated_cost": 50}},
        {{"type": "dinner", "name": "晚餐推荐", "description": "晚餐描述", "estimated_cost": 80}}
      ]
    }}
  ],
  "weather_info": [
    {{
      "date": "YYYY-MM-DD",
      "day_weather": "晴",
      "night_weather": "多云",
      "day_temp": 25,
      "night_temp": 15,
      "wind_direction": "南风",
      "wind_power": "1-3级"
    }}
  ],
  "overall_suggestions": "总体建议",
  "budget": {{
    "total_attractions": 180,
    "total_hotels": 1200,
    "total_meals": 480,
    "total_transportation": 200,
    "total": 2060
  }}
}}

**规则:**
1. 每天安排2-3个景点, 考虑景点之间的距离和游览时间
2. 每天必须包含早中晚三餐(breakfast/lunch/dinner)
3. 每天推荐一个具体的酒店(从提供的酒店信息中选择)
4. weather_info 中按日期填入对应天气; 某天没有天气数据时, 字段留空
5. 景点的经纬度坐标必须使用提供的真实坐标
6. 所有费用字段填写合理估算值, budget 为各项费用汇总
"""


class GraphState(TypedDict, total=False):
    """LangGraph 工作流状态"""
    request: TripRequest               # 用户旅行请求
    attraction_pois: List[POIInfo]     # 景点搜索结果
    weather_info: List[WeatherInfo]    # 天气信息
    hotel_pois: List[POIInfo]          # 酒店搜索结果
    trip_plan: TripPlan                # 最终行程计划
    error: bool                        # 是否出错(用于条件路由)


class MultiAgentTripPlanner:
    """基于 LangGraph 的多智能体旅行规划系统

    工作流: 搜索景点 → 查询天气 → 搜索酒店 → LLM生成行程 → (LLM失败)备用计划
    数据获取节点直接调用高德服务(不走LLM), 仅行程规划调用LLM, 高效且省成本。
    """

    def __init__(self):
        """初始化多智能体系统"""
        logger.info("🔄 开始初始化多智能体旅行规划系统...")
        self.llm = get_llm()
        self.amap_service = get_amap_service()
        self.graph = self._build_graph()
        logger.info("✅ 多智能体系统初始化成功")

    # ============ LangGraph 节点 ============

    def _search_attractions(self, state: GraphState) -> dict:
        """节点1: 搜索景点 (服务直调, 不走LLM)"""
        request = state["request"]
        logger.info("📍 步骤1: 搜索景点...")
        try:
            keywords = request.preferences[0] if request.preferences else "景点"
            pois = self.amap_service.search_poi(keywords, request.city)
            logger.info(f"   找到 {len(pois)} 个景点")
            return {"attraction_pois": pois}
        except Exception as e:
            logger.warning(f"   ⚠️ 景点搜索失败: {e}")
            return {"attraction_pois": []}

    def _get_weather(self, state: GraphState) -> dict:
        """节点2: 查询天气 (服务直调, 不走LLM)"""
        request = state["request"]
        logger.info("🌤️  步骤2: 查询天气...")
        try:
            weather = self.amap_service.get_weather(request.city)
            logger.info(f"   获取 {len(weather)} 天天气数据")
            return {"weather_info": weather}
        except Exception as e:
            logger.warning(f"   ⚠️ 天气查询失败: {e}")
            return {"weather_info": []}

    def _search_hotels(self, state: GraphState) -> dict:
        """节点3: 搜索酒店 (服务直调, 不走LLM)"""
        request = state["request"]
        logger.info("🏨 步骤3: 搜索酒店...")
        try:
            hotels = self.amap_service.search_poi(request.accommodation, request.city)
            logger.info(f"   找到 {len(hotels)} 个酒店")
            return {"hotel_pois": hotels}
        except Exception as e:
            logger.warning(f"   ⚠️ 酒店搜索失败: {e}")
            return {"hotel_pois": []}

    def _generate_trip_plan(self, state: GraphState) -> dict:
        """节点4: LLM 生成行程计划

        采用「文本生成 + JSON提取 + Pydantic校验」的通用方案, 兼容任何模型
        (包括不支持 function calling / json_schema 的 thinking 模型)。
        解析失败时携带错误信息自纠错重试一次, 仍失败则走备用计划。
        """
        request = state["request"]
        logger.info("📋 步骤4: LLM 生成行程计划...")
        try:
            planner_query = self._build_planner_query(request, state)
            prompt_template = ChatPromptTemplate.from_messages([
                ("system", PLANNER_SYSTEM_PROMPT),
                ("human", "{query}"),
            ])
            chain = prompt_template | self.llm 

            for attempt in range(2):
                response = chain.invoke({"query": planner_query})
                content = response.content if hasattr(response, "content") else str(response)
                try:
                    trip_plan = self._parse_json_response(content)
                    logger.info("   ✅ 行程计划生成成功")
                    return {"trip_plan": trip_plan, "error": False}
                except Exception as e:
                    logger.warning(f"   ⚠️ 第{attempt + 1}次解析失败: {str(e)[:100]}")
                    # 自纠错: 把校验错误反馈给LLM, 要求重新生成
                    planner_query = (
                        f"你上一次输出的 JSON 不符合结构要求, 错误信息: {e}\n"
                        f"你上一次的输出是: {content[:2000]}\n"
                        f"请严格按照 system 中定义的 JSON 结构重新输出完整 JSON。\n\n"
                        f"原始需求:\n{planner_query}"
                    )

            raise ValueError("两次尝试均未能生成合法行程计划")
        except Exception as e:
            logger.warning(f"   ⚠️ LLM 生成行程失败: {e}")
            return {"error": True}

    def _fallback_plan(self, state: GraphState) -> dict:
        """节点5: 备用计划 (LLM失败时兜底)"""
        logger.info("   🛟 使用备用计划")
        return {"trip_plan": self._create_fallback_plan(state["request"]), "error": False}

    def _should_fallback(self, state: GraphState) -> str:
        """条件路由: LLM生成失败则走备用计划, 否则结束"""
        return "fallback_plan" if state.get("error") else "end"

    # ============ 图构建 ============

    def _build_graph(self):
        """构建 LangGraph 工作流"""
        # 1. 实例化图，指定全局数据结构
        graph = StateGraph(GraphState)
        # 2. 注册所有节点 (把工人拉进厂)
        graph.add_node("search_attractions", self._search_attractions)
        graph.add_node("get_weather", self._get_weather)
        graph.add_node("search_hotels", self._search_hotels)
        graph.add_node("generate_trip_plan", self._generate_trip_plan)
        graph.add_node("fallback_plan", self._fallback_plan)
        
        # 3. 铺设传送带 (普通边: 顺次执行)
        graph.add_edge(START, "search_attractions")
        graph.add_edge("search_attractions", "get_weather")
        graph.add_edge("get_weather", "search_hotels")
        graph.add_edge("search_hotels", "generate_trip_plan")
        # 4. 铺设智能分拣闸门 (条件边: 失败走兜底，成功则结束)
        graph.add_conditional_edges(
            "generate_trip_plan",
            self._should_fallback,
            {"fallback_plan": "fallback_plan", "end": END},
        )
        graph.add_edge("fallback_plan", END)
        return graph.compile()

    # ============ 对外接口 ============

    def plan_trip(self, request: TripRequest) -> TripPlan:
        """使用 LangGraph 工作流生成旅行计划

        Args:
            request: 旅行请求

        Returns:
            旅行计划
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 开始 LangGraph 工作流规划旅行...")
        logger.info(f"目的地: {request.city} | 日期: {request.start_date} 至 {request.end_date} | {request.travel_days}天")
        logger.info(f"偏好: {', '.join(request.preferences) if request.preferences else '无'}")
        logger.info(f"{'='*60}\n")

        result = self.graph.invoke({"request": request})
        trip_plan = result["trip_plan"]

        # 天气: 用高德真实天气覆盖LLM生成的天气。
        # LLM 常因日期不足而把天气字段输出 null/0, 导致前端温度全显示0;
        # 数据节点已拿到高德真实天气(含温度), 直接回填即可, 也符合"服务直调"架构。
        real_weather = result.get("weather_info") or []
        if real_weather:
            trip_plan.weather_info = real_weather

        # 兜底: 若LLM未返回预算, 前端预算页会异常, 这里自动补齐
        trip_plan = self._ensure_budget(trip_plan, request)

        logger.info(f"\n{'='*60}")
        logger.info(f"✅ 旅行计划生成完成! 天数: {len(trip_plan.days)}")
        logger.info(f"{'='*60}\n")
        return trip_plan

    def get_agent_info(self) -> dict:
        """Agent 信息 (供健康检查使用)"""
        return {
            "name": "LangGraph 多智能体旅行规划系统",
            "framework": "langgraph",
            "nodes": ["search_attractions", "get_weather", "search_hotels", "generate_trip_plan", "fallback_plan"],
        }

    # ============ 内部工具方法 ============

    @staticmethod
    def _parse_json_response(content: str) -> TripPlan:
        """从LLM响应中提取JSON并用Pydantic校验

        Args:
            content: LLM原始输出

        Returns:
            校验通过的 TripPlan

        Raises:
            ValueError: JSON提取失败或结构校验失败
        """
        # 1. 提取代码块中的JSON (支持 ```json 包裹)
        if "```" in content:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
            if match:
                content = match.group(1)

        # 2. 截取首尾花括号之间的内容
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("LLM响应中未找到JSON对象")

        # 3. 解析JSON并通过Pydantic校验
        data = json.loads(content[start:end + 1])
        return TripPlan.model_validate(data)

    def _build_planner_query(self, request: TripRequest, state: GraphState) -> str:
        """构建行程规划 prompt (将结构化数据转为文本供LLM参考)"""
        attraction_text = self._pois_to_text(state.get("attraction_pois", []))
        hotel_text = self._pois_to_text(state.get("hotel_pois", []))
        weather_text = self._weather_to_text(state.get("weather_info", []))

        query = f"""请为以下旅行需求生成{request.city}的{request.travel_days}天行程计划:

**基本信息:**
- 城市: {request.city}
- 日期: {request.start_date} 至 {request.end_date}
- 天数: {request.travel_days}天
- 交通方式: {request.transportation}
- 住宿偏好: {request.accommodation}
- 旅行偏好: {', '.join(request.preferences) if request.preferences else '无'}

**可选景点:**
{attraction_text or '无'}

**天气信息:**
{weather_text or '无'}

**可选酒店:**
{hotel_text or '无'}
"""
        if request.free_text_input:
            query += f"\n**额外要求:** {request.free_text_input}\n"

        query += "\n请严格按照 system 中定义的 JSON 结构输出完整 JSON。"
        return query

    @staticmethod
    def _pois_to_text(pois: List[POIInfo]) -> str:
        """POI列表转为可读文本"""
        lines = []
        for i, poi in enumerate(pois, 1):
            coord = f"{poi.location.longitude},{poi.location.latitude}" if poi.location else ""
            lines.append(f"{i}. {poi.name} | 地址: {poi.address} | 坐标: {coord}")
        return "\n".join(lines)

    @staticmethod
    def _weather_to_text(weather_list: List[WeatherInfo]) -> str:
        """天气信息列表转为可读文本"""
        lines = []
        for w in weather_list:
            lines.append(
                f"{w.date}: 白天{w.day_weather} {w.day_temp}°C / 夜间{w.night_weather} {w.night_temp}°C, 风向{w.wind_direction} {w.wind_power}"
            )
        return "\n".join(lines)

    def _ensure_budget(self, trip_plan: TripPlan, request: TripRequest) -> TripPlan:
        """若行程计划缺少预算, 按实际费用自动计算补齐"""
        if trip_plan.budget is not None:
            return trip_plan

        total_attractions = sum(a.ticket_price for day in trip_plan.days for a in day.attractions)
        total_meals = sum(m.estimated_cost for day in trip_plan.days for m in day.meals)
        total_hotels = sum(day.hotel.estimated_cost for day in trip_plan.days if day.hotel and day.hotel.estimated_cost)
        total_transportation = 50 * request.travel_days

        total_attractions = total_attractions or 200
        total_meals = total_meals or 150 * request.travel_days
        total_hotels = total_hotels or 400 * request.travel_days

        trip_plan.budget = Budget(
            total_attractions=total_attractions,
            total_hotels=total_hotels,
            total_meals=total_meals,
            total_transportation=total_transportation,
            total=total_attractions + total_hotels + total_meals + total_transportation,
        )
        return trip_plan

    def _create_fallback_plan(self, request: TripRequest) -> TripPlan:
        """创建备用计划(当Agent失败时)"""
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")

        days = []
        for i in range(request.travel_days):
            current_date = start_date + timedelta(days=i)
            days.append(DayPlan(
                date=current_date.strftime("%Y-%m-%d"),
                day_index=i,
                description=f"第{i+1}天行程",
                transportation=request.transportation,
                accommodation=request.accommodation,
                attractions=[
                    Attraction(
                        name=f"{request.city}景点{j+1}",
                        address=f"{request.city}市",
                        location=Location(longitude=116.4 + i * 0.01 + j * 0.005, latitude=39.9 + i * 0.01 + j * 0.005),
                        visit_duration=120,
                        description=f"这是{request.city}的著名景点",
                        category="景点",
                    )
                    for j in range(2)
                ],
                meals=[
                    Meal(type="breakfast", name=f"第{i+1}天早餐", description="当地特色早餐", estimated_cost=30),
                    Meal(type="lunch", name=f"第{i+1}天午餐", description="午餐推荐", estimated_cost=50),
                    Meal(type="dinner", name=f"第{i+1}天晚餐", description="晚餐推荐", estimated_cost=80),
                ],
            ))

        total_attractions = sum(attr.ticket_price for day in days for attr in day.attractions) or 200
        total_meals = sum(meal.estimated_cost for day in days for meal in day.meals) or 150 * request.travel_days
        total_hotels = 400 * request.travel_days
        total_transportation = 50 * request.travel_days

        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days,
            weather_info=[],
            overall_suggestions=f"这是为您规划的{request.city}{request.travel_days}日游行程,建议提前查看各景点的开放时间。",
            budget=Budget(
                total_attractions=total_attractions,
                total_hotels=total_hotels,
                total_meals=total_meals,
                total_transportation=total_transportation,
                total=total_attractions + total_hotels + total_meals + total_transportation,
            ),
        )


# 全局多智能体系统实例
_multi_agent_planner = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """获取多智能体旅行规划系统实例(单例模式)"""
    global _multi_agent_planner

    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()

    return _multi_agent_planner
