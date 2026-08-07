"""pytest 全局配置

重要: 在 import 应用之前设置测试环境变量。
环境变量优先于 .env 文件(pydantic-settings 默认行为),
用假 key 隔离真实的高德/LLM 凭据, 保证测试不发任何真实外部请求。
"""

import os
import sys
from pathlib import Path

# 确保 backend 目录在 sys.path, 允许 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试环境变量 (必须在 import app 之前设置)
os.environ["AMAP_API_KEY"] = "test_amap_key"
os.environ["LLM_API_KEY"] = "test_llm_key"
os.environ["LLM_BASE_URL"] = "http://localhost:9999/v1"  # 无效端点, 防止误发请求
os.environ["LLM_MODEL_ID"] = "test-model"

import pytest
from fastapi.testclient import TestClient

from app.api.main import app


@pytest.fixture(autouse=True, scope="session")
def _isolate_logs():
    """测试期间隔离日志: 移除文件 handler。

    异常测试会故意触发异常(如 /test-uncaught), 全局异常处理器会把完整堆栈写入
    logs/app.log, 导致测试产物污染生产日志。测试只输出到控制台, 不落盘。
    """
    import logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)

    yield


@pytest.fixture(scope="session")
def client() -> TestClient:
    """测试客户端 (整个测试会话复用同一个应用实例)"""
    return TestClient(app)
