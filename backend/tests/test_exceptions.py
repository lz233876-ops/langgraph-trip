"""全局异常处理器测试"""

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.core.exceptions import BizException


@pytest.fixture
def no_raise_client():
    """模拟真实服务器行为: 未捕获异常不抛给客户端, 由全局处理器返回500"""
    return TestClient(app, raise_server_exceptions=False)


def test_biz_exception_returns_status_code(no_raise_client):
    """BizException 应返回对应状态码与统一错误格式"""

    @no_raise_client.app.get("/test-biz")
    def test_biz():
        raise BizException("景点不存在", status_code=404)

    resp = no_raise_client.get("/test-biz")

    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False
    assert data["message"] == "景点不存在"


def test_uncaught_exception_returns_500(no_raise_client):
    """未捕获异常应由全局处理器返回 500 统一格式"""

    @no_raise_client.app.get("/test-uncaught")
    def test_uncaught():
        raise ValueError("模拟未捕获异常")

    resp = no_raise_client.get("/test-uncaught")

    assert resp.status_code == 500
    data = resp.json()
    assert data["success"] is False
    assert "服务器内部错误" in data["message"]


def test_not_found_returns_404(no_raise_client):
    """不存在的路由应返回 FastAPI 默认 404, 不被全局异常处理器误伤"""
    resp = no_raise_client.get("/api/not-exist-path")
    assert resp.status_code == 404
