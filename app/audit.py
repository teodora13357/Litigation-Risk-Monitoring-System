"""审计日志写入（失败不阻断业务）。"""

from fastapi import Request
from sqlalchemy.orm import Session

from app import logging as app_logging
from app import models

logger = app_logging.get_logger("audit")


def write_audit_log(
    db: Session,
    action_type: str,
    target_type: str,
    target_id: int = None,
    action_detail: dict = None,
    request: Request = None,
    user_id: int = None,
    user_name: str = None,
) -> None:
    """写入一条操作审计日志（审计失败不阻断业务）。"""
    try:
        ip = request.client.host if (request and request.client) else None
        log = models.AuditLog(
            user_id=user_id,
            user_name=user_name,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            action_detail=action_detail,
            ip_address=ip,
        )
        db.add(log)
        db.commit()
    except Exception:  # noqa: BLE001 —— 审计日志失败不影响主流程
        db.rollback()
        logger.warning("审计日志写入失败 action_type=%s target_type=%s", action_type, target_type)
