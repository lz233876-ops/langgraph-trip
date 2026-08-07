"""系统路由测试: 根路径与健康检查"""


def test_root(client):
    """根路径应返回服务信息"""
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert "name" in data
    assert "docs" in data


def test_health(client):
    """全局健康检查"""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


def test_trip_health(client):
    """旅行规划服务健康检查"""
    resp = client.get("/api/trip/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["framework"] == "langgraph"


def test_map_health(client):
    """地图服务健康检查"""
    resp = client.get("/api/map/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["amap_api_key_configured"] is True


def test_health_response_header(client):
    """每个请求都应带上 X-Process-Time 耗时响应头"""
    resp = client.get("/health")
    assert "X-Process-Time" in resp.headers
