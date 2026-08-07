"""业务异常与全局异常处理器

设计:
- 路由层不再重复写 try/except, 业务出错直接 raise BizException
- 未知异常由 global_exception_handler 统一兜底, 记录完整堆栈并返回统一错误格式
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class BizException(Exception):
    """业务异常: 携带HTTP状态码, 由全局异常处理器统一转响应"""

    def __init__(self, detail: str, status_code: int = 400):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


async def biz_exception_handler(request: Request, exc: BizException):
    """可控业务异常: 记警告日志 + 返回对应状态码"""
    logger.warning(f"业务异常 [{exc.status_code}] {exc.detail} (path={request.url.path})")
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": exc.detail},
    )


async def global_exception_handler(request: Request, exc: Exception):
    """未捕获异常: 记完整堆栈到 error 日志 + 返回 500"""
    logger.exception(f"未捕获异常 (path={request.url.path}): {exc}")
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": f"服务器内部错误: {exc}"},
    )
