"""Redis 客户端（缓存）。

通过 REDIS_URL 配置（默认 redis://127.0.0.1:6379/0）。
用于：案件列表缓存、识别结果缓存等（当前为预留模块，未在业务中强制使用）。
"""
import os

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
_client = None


def get_redis():
    """返回 Redis 客户端（惰性单例）。"""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def cache_get(key: str):
    return get_redis().get(key)


def cache_set(key: str, value: str, ex: int = 300) -> None:
    get_redis().set(key, value, ex=ex)


def cache_delete(key: str) -> None:
    get_redis().delete(key)
