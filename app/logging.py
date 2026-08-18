"""统一结构化日志。

- 日志行格式：时间 / 级别 / request_id / user_id / 模块 / 消息
- request_id、user_id 通过 ContextVar 由 main.py 的中间件注入，方便跨请求排查与 grep。
"""

import logging
import sys
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


class _ContextFilter(logging.Filter):
    """为每条日志记录附加 request_id / user_id 字段。"""

    def filter(self, record):
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        return True


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """初始化根日志器（幂等）：统一格式 + 上下文过滤。"""
    root = logging.getLogger()
    if getattr(root, "_app_logging_configured", False):
        return logging.getLogger("app")
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s request_id=%(request_id)s user_id=%(user_id)s "
        "module=%(name)s %(message)s"
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    handler.addFilter(_ContextFilter())
    root.handlers = [handler]
    root.propagate = False
    root._app_logging_configured = True
    return logging.getLogger("app")


def get_logger(name: str = "app") -> logging.Logger:
    """获取带模块名的子 logger。"""
    return logging.getLogger(name)


def new_request_id() -> str:
    """生成请求 ID（用于日志关联）。"""
    return uuid.uuid4().hex[:12]
