"""日志配置模块: 控制台 + 文件落盘 + 大小轮转 + 级别分离

- 控制台: 开发时实时查看
- logs/app.log: 全量日志(INFO+), 单文件最大5MB, 保留5个备份自动滚动
- logs/error.log: 仅 ERROR+ 日志, 排查问题只看这个文件
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from ..config import get_settings

# backend/logs 目录
_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs",
)

# 日志格式: 时间 级别 模块名: 内容
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# 单文件大小上限(5MB)与备份数量
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_configured = False


def setup_logging() -> None:
    """初始化日志(幂等): 给 root logger 挂控制台/文件 handler

    幂等设计保证在 reload 模式下, uvicorn 子进程重新 import 应用时不会重复挂 handler。
    """
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(_FORMAT)

    handlers: list = [logging.StreamHandler()]  # 控制台

    # 文件落盘
    os.makedirs(_LOG_DIR, exist_ok=True)
    app_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, "app.log"),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setFormatter(formatter)
    handlers.append(app_handler)

    # 错误单独一份, 排查问题只看这个文件
    error_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, "error.log"),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    handlers.append(error_handler)

    # 挂到 root logger, 所有模块的 logger 自动继承输出
    root = logging.getLogger()
    root.setLevel(level)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)

    _configured = True
    logging.getLogger(__name__).info(f"日志已初始化: 级别={settings.log_level}, 文件目录={_LOG_DIR}")

    # 过滤第三方噪音日志: uvicorn reload 的 watchfiles 会持续刷 "1 change detected"(INFO),
    # 若任其 propagate 到 root, 会连同文件 handler 一起污染 app.log。降低级别后不再进应用日志。
    for noisy_name in ("watchfiles", "watchfiles.main", "uvicorn.access"):
        logging.getLogger(noisy_name).setLevel(logging.WARNING)
