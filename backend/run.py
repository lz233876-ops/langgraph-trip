"""启动脚本"""

import uvicorn
from uvicorn.config import LOGGING_CONFIG
from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()

    # 自定义 uvicorn 日志配置: 关闭 disable_existing_loggers。
    # 保证 app/core/logging.py 已挂到 root 的控制台+文件日志不会被 uvicorn 的 dictConfig 清掉。
    log_config = dict(LOGGING_CONFIG)
    log_config["disable_existing_loggers"] = False

    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        # 热重载只监视源码目录 app/。
        # 不能监视整个 backend: logs/*.log 持续写入会触发 reload 无限循环;
        # 且 uvicorn 的 reload_excludes 在 reload 模式下不可靠, 缩小监视范围是根本解法。
        reload_dirs=["app"],
        log_level=settings.log_level.lower(),
        log_config=log_config,
    )
