"""认证路由：v1 登录/登出 + AI 文书解析占位接口。"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session

import auth
import error_codes
import models
from database import get_db
from error_codes import BizError
from app.audit import write_audit_log

router = APIRouter()


@router.post("/api/v1/auth/login", tags=["v1认证"], summary="用户登录")
def v1_login(
    username: Optional[str] = Body(default=None, embed=True),
    password: Optional[str] = Body(default=None, embed=True),
    remember: Optional[bool] = Body(default=False, embed=True),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """用户登录：支持用户名或手机号；JWT token（24h / 记住我7天）。"""
    if not username or not password:
        raise BizError(error_codes.MISSING_REQUIRED_FIELD, "请输入用户名和密码")
    user = db.query(models.SysUser).filter(
        (models.SysUser.username == username) | (models.SysUser.phone == username),
        models.SysUser.status == 1,
    ).first()
    if user is None:
        raise BizError(error_codes.AUTH_FAILED, "用户名或密码错误")
    if not auth.verify_password(password, user.password_hash):
        raise BizError(error_codes.AUTH_FAILED, "用户名或密码错误")
    user.last_login_at = datetime.now()
    db.commit()
    # 审计：登录
    write_audit_log(db, "LOGIN", "SYSTEM", target_id=user.id,
                    action_detail={"username": user.username}, request=request,
                    user_id=user.id, user_name=user.username)
    token = auth.create_token(user.id, user.username or user.phone, remember=bool(remember))
    return error_codes.error_response(0, "登录成功", {
        "token": token,
        "user": {"id": user.id, "username": user.username, "phone": user.phone, "real_name": user.real_name, "role": user.role},
        "permissions": auth.get_permissions(user.role),
        "expires_in": (7 * 24 * 3600) if remember else (24 * 3600),
    })


@router.post("/api/v1/auth/logout", tags=["v1认证"], summary="用户登出")
def v1_logout():
    """用户登出。JWT 无状态，前端删除 token 即可完成登出。"""
    return error_codes.error_response(0, "登出成功")


# ---------- AI 文书解析（业务逻辑暂未实现，占位） ----------

_AI_TASKS: dict = {}  # 内存占位：task_id -> 状态；后续替换为持久化任务表


@router.post("/api/v1/ai/parse", tags=["v1AI解析"], summary="触发AI文书解析")
def v1_ai_parse(
    file_id: Optional[int] = Body(default=None, embed=True),
):
    """触发 AI 文书解析。业务逻辑待实现，当前创建占位任务。"""
    # TODO: 接入 AI 解析（LangChain OCR/视觉模型），异步任务 + 持久化
    task_id = uuid.uuid4().hex
    _AI_TASKS[task_id] = {"status": "pending", "file_id": file_id}
    return error_codes.error_response(0, "AI解析任务已创建（业务逻辑待实现）", {"task_id": task_id, "status": "pending"})


@router.get("/api/v1/ai/parse/{task_id}", tags=["v1AI解析"], summary="查询解析任务结果")
def v1_ai_parse_result(task_id: str):
    """查询 AI 解析任务结果。业务逻辑待实现。"""
    if task_id not in _AI_TASKS:
        return error_codes.error_response(error_codes.AI_PARSE_FAILED, "任务不存在")
    return error_codes.error_response(0, "AI解析业务逻辑待实现", {"task_id": task_id, "status": _AI_TASKS[task_id]["status"]})
