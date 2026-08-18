"""认证工具：密码哈希、JWT token、登录锁定。"""
import hashlib
import hmac
import os
from datetime import datetime, timedelta

import jwt

SECRET_KEY = os.getenv("SECRET_KEY", "case-management-secret-key-change-me")
TOKEN_EXPIRE_HOURS = 24   # token 有效期 24 小时
REMEMBER_DAYS = 7         # 记住我：7 天免登录
MAX_FAILED_ATTEMPTS = 5   # 连续失败次数阈值
LOCK_MINUTES = 5          # 锁定时长（分钟）


def hash_password(password: str) -> str:
    """PBKDF2 加盐哈希，返回 salt$digest。"""
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 100000
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。"""
    try:
        salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), 100000
        ).hex()
        return hmac.compare_digest(calc, digest)
    except Exception:
        return False


def create_token(user_id: int, username: str, remember: bool = False) -> str:
    """生成 JWT token。记住我=7天，否则=24小时。"""
    if remember:
        expire = timedelta(days=REMEMBER_DAYS)
    else:
        expire = timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.utcnow() + expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str):
    """解析 token，失败返回 None。"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def is_locked(locked_until) -> bool:
    """判断账户是否处于锁定状态。"""
    if locked_until:
        try:
            locked_ts = datetime.fromisoformat(locked_until)
            return datetime.now() < locked_ts
        except Exception:
            return False
    return False

# ===== 角色权限矩阵 =====
# 权限标识：
#   upload_parse    上传解析
#   manual_entry    手动录入
#   case_create     案件创建
#   system_settings 系统设置（仅 admin）
#   user_management 用户管理（仅 admin）
#   audit_log       审计日志查看（admin / manager）
ROLE_PERMISSIONS = {
    "admin": ["upload_parse", "manual_entry", "case_create", "system_settings", "user_management", "audit_log"],
    "manager": ["upload_parse", "manual_entry", "case_create", "audit_log"],
    "user": ["upload_parse", "manual_entry", "case_create"],
}


def get_permissions(role: str):
    """根据角色返回权限列表（未知角色返回空）。"""
    return ROLE_PERMISSIONS.get(role, [])
