"""阿里云 OSS 存储客户端。

通过环境变量配置（与 docker-compose 的 .env 对应）：
    OSS_ENDPOINT / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET

存储后端开关 STORAGE_BACKEND：
    local（默认）：文件存本地 static/uploads
    oss：文件上传到阿里云 OSS

用法示例：
    from oss_client import is_oss_configured, upload_object, get_object_url, delete_object
"""
import os

import oss2

ENDPOINT = os.getenv("OSS_ENDPOINT", "")
ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "")
ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "")
BUCKET_NAME = os.getenv("OSS_BUCKET", "")

_BUCKET = None


def is_oss_configured() -> bool:
    """是否已配置 OSS（未配置时应用回退本地存储）。"""
    return bool(ENDPOINT and ACCESS_KEY_ID and ACCESS_KEY_SECRET and BUCKET_NAME)


def get_oss_bucket():
    """初始化并返回 OSS Bucket 客户端（惰性单例）。"""
    global _BUCKET
    if _BUCKET is None:
        auth = oss2.Auth(ACCESS_KEY_ID, ACCESS_KEY_SECRET)
        _BUCKET = oss2.Bucket(auth, ENDPOINT, BUCKET_NAME)
    return _BUCKET


def upload_object(key: str, file_path: str) -> str:
    """上传本地文件到 OSS，返回 ETag（用于数据完整性校验）。"""
    result = get_oss_bucket().put_object_from_file(key, file_path)
    return result.etag


def get_object_url(key: str, expires: int = 3600) -> str:
    """生成 OSS 对象临时访问 URL（用于预览/下载）。"""
    return get_oss_bucket().sign_url("GET", key, expires)


def delete_object(key: str) -> None:
    """删除 OSS 对象。"""
    get_oss_bucket().delete_object(key)


def download_object(key: str, file_path: str) -> None:
    """从 OSS 下载对象到本地文件（用于本地缓存回填）。"""
    get_oss_bucket().get_object_to_file(key, file_path)
