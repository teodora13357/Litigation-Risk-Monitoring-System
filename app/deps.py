"""依赖注入：数据库会话 + 当前登录用户（JWT 强制认证）。"""

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.database import get_db  # noqa: F401  —— 直接复用数据库模块的 get_db
from app import auth
from app import models

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> "models.SysUser":
    """解析 JWT 并返回当前启用用户；未认证 / token 无效 / 用户停用 → 401。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录或缺少 token")
    payload = auth.decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    try:
        user_id = int(payload.get("sub", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="token 无效")
    user = db.query(models.SysUser).filter(
        models.SysUser.id == user_id, models.SysUser.status == 1
    ).first()
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在或已停用")
    return user
