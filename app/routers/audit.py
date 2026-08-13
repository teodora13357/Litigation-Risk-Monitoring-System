"""审计日志路由。"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

import models
from database import get_db
from app.deps import get_current_user

router = APIRouter()


@router.get("/api/audit-logs", tags=["审计日志"], summary="查询操作审计日志")
def list_audit_logs(
    skip: int = 0,
    limit: int = Query(default=100, le=500),
    action_type: Optional[str] = None,
    target_type: Optional[str] = None,
    user_name: Optional[str] = None,
    user: models.SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查询操作审计日志，支持按操作类型/对象类型/用户名筛选。"""
    query = db.query(models.AuditLog)
    if action_type:
        query = query.filter(models.AuditLog.action_type == action_type)
    if target_type:
        query = query.filter(models.AuditLog.target_type == target_type)
    if user_name:
        query = query.filter(models.AuditLog.user_name.contains(user_name))

    logs = query.order_by(models.AuditLog.id.desc()).offset(skip).limit(limit).all()
    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "user_name": log.user_name,
            "action_type": log.action_type,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "action_detail": log.action_detail,
            "ip_address": log.ip_address,
            "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else None,
        }
        for log in logs
    ]
