"""FastAPI主应用"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from ..config import get_settings, validate_config, print_config
from ..core.logging import setup_logging
from ..core.exceptions import BizException, biz_exception_handler, global_exception_handler
from .routes import trip, poi, map as map_routes

# 初始化日志(幂等): 控制台 + 文件落盘。
# 必须在 uvicorn 重配日志之前执行; reload 模式下子进程重新 import 本模块时也会执行, 保证任意模式日志可用。
setup_logging()

# 获取配置
settings = get_settings()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 (替代已弃用的 @app.on_event)

    startup: 打印横幅、打印配置并验证必要配置项
    shutdown: 打印关闭信息 (可在此释放资源, 如数据库连接池)
    """
    print("\n" + "=" * 60)
    print(f"🚀 {settings.app_name} v{settings.app_version}")
    print("=" * 60)

    # 打印配置信息
    print_config()

    # 验证配置
    try:
        validate_config()
        print("\n✅ 配置验证通过")
    except ValueError as e:
        print(f"\n❌ 配置验证失败:\n{e}")
        print("\n请检查.env文件并确保所有必要的配置项都已设置")
        raise

    print("\n" + "=" * 60)
    print(f"📚 API文档: http://localhost:{settings.port}/docs")
    print(f"📖 ReDoc文档: http://localhost:{settings.port}/redoc")
    print("=" * 60 + "\n")

    yield  # 应用运行期间挂起

    print("\n" + "=" * 60)
    print("👋 应用正在关闭...")
    print("=" * 60 + "\n")


# 创建FastAPI应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于LangChain + FastAPI的智能旅行规划助手API",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 全局异常处理器: 业务异常(BizException)返回对应状态码, 其余异常统一500并记录完整堆栈
app.add_exception_handler(BizException, biz_exception_handler)
app.add_exception_handler(Exception, global_exception_handler)

# 可观测性: Prometheus 指标端点 /metrics (供监控系统抓取, 对接 Grafana 看板)
# 指标覆盖: 请求速率/延迟/错误率/HTTP状态码分布等, 按路由与方法打标签
instrumentator = Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """记录每个请求的耗时, 输出到日志与响应头 X-Process-Time"""
    start_time = time.perf_counter()

    # 释放请求进入后续业务逻辑(路由、Agent规划等)
    response = await call_next(request)

    process_time = time.perf_counter() - start_time
    logger.info(f"请求耗时 | 路径: {request.url.path} | 耗时: {process_time:.2f}s")
    response.headers["X-Process-Time"] = f"{process_time:.2f}s"
    return response


# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(trip.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc",
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
