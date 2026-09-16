"""token 用量提取测试: 不调用真实 LLM, 仅验证 usage 字段解析"""

from langchain_core.messages import AIMessage

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import TokenUsage


def test_extract_usage_metadata():
    """新版 langchain: AIMessage.usage_metadata 直接携带 input/output/total tokens"""
    msg = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    )
    usage = MultiAgentTripPlanner._extract_token_usage(msg)
    assert usage == TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30)


def test_extract_usage_legacy_prompt_tokens():
    """旧版 OpenAI 风格: response_metadata.token_usage 使用 prompt/completion_tokens"""
    msg = AIMessage(
        content="x",
        response_metadata={
            "token_usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
        },
    )
    usage = MultiAgentTripPlanner._extract_token_usage(msg)
    assert usage == TokenUsage(input_tokens=5, output_tokens=7, total_tokens=12)


def test_extract_usage_missing():
    """端点未返回 usage 时各字段兜底为 0"""
    msg = AIMessage(content="x")
    usage = MultiAgentTripPlanner._extract_token_usage(msg)
    assert usage == TokenUsage()
