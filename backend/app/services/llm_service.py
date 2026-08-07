"""LLM服务模块 (LangChain ChatOpenAI)"""

import logging

from langchain_openai import ChatOpenAI
from ..config import get_settings

logger = logging.getLogger(__name__)

# 全局LLM实例
_llm_instance = None

# LLM调用失败自动重试次数 (网络抖动/限流时自动重试)
_LLM_MAX_RETRIES = 2


def get_llm() -> ChatOpenAI:
    """
    获取LLM实例(单例模式)

    使用 langchain-openai 的 ChatOpenAI, 兼容任意 OpenAI 格式的 API 端点
    (OpenAI / DeepSeek / Moonshot / 通义 / 智谱等)。
    换模型只需修改 .env 中的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID。

    Returns:
        ChatOpenAI实例
    """
    global _llm_instance

    if _llm_instance is None:
        settings = get_settings()

        _llm_instance = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key or None,
            base_url=settings.llm_base_url or None,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            max_retries=_LLM_MAX_RETRIES,  # 网络抖动/限流时自动重试
        )

        logger.info(
            f"LLM服务初始化成功 | model={settings.llm_model}, "
            f"base_url={settings.llm_base_url or 'https://api.openai.com/v1 (官方默认)'}"
        )

    return _llm_instance


def reset_llm():
    """重置LLM实例(用于测试或重新配置)"""
    global _llm_instance
    _llm_instance = None
