"""OCR 异步任务追踪与后台识别执行。

- OCR_TASKS：内存态（file_id -> 状态）。服务重启后任务状态丢失，
  查询接口会回读数据库 case_file.ai_result 兜底。
- run_ocr_background：后台执行 OCR，结果回写 case_file 与 ai_parse_task。
"""

import os
from datetime import datetime

from app import logging as app_logging
from app import models
from app import ocr_service
from app.database import SessionLocal
from app.files_config import UPLOAD_DIR

logger = app_logging.get_logger("ocr_tasks")

OCR_TASKS: dict = {}


def run_ocr_background(
    file_id: int,
    doc_type: str,
    rel_path: str,
    mime_type: str,
    content_type: str,
    task_id: int,
) -> None:
    """后台执行 OCR：结果写入 case_file（ocr_text/ai_result）与 ai_parse_task。"""
    task = OCR_TASKS.get(file_id)
    if task is not None:
        task["status"] = "running"
    db = SessionLocal()
    try:
        at = db.query(models.AiParseTask).filter(models.AiParseTask.id == task_id).first()
        if at is not None:
            at.task_status = "processing"
            at.started_at = datetime.now()
            db.commit()

        local_path = os.path.join(UPLOAD_DIR, rel_path)
        logger.info("OCR 开始后台识别 file_id=%s path=%s", file_id, local_path)
        result = ocr_service.recognize_document(local_path, content_type or mime_type)
        logger.info("OCR 完成 file_id=%s ok=%s pages=%s", file_id, result.get("ok"), result.get("page_count"))

        # 回写 case_file
        cf = db.query(models.CaseFile).filter(models.CaseFile.id == file_id).first()
        if cf is not None:
            cf.ocr_text = (result.get("structured_text") or "")
            cf.ai_result = result

        # 回写 ai_parse_task
        if at is not None:
            at.task_status = "completed" if result.get("ok") else "failed"
            at.completed_at = datetime.now()
            at.output_result = result
            if not result.get("ok"):
                at.error_message = result.get("reason") or result.get("message") or "OCR失败"

        db.commit()

        if task is not None:
            task["status"] = "done" if result.get("ok") else "failed"
            task["result"] = result
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR 后台识别异常 file_id=%s", file_id, exc_info=exc)
        try:
            at = db.query(models.AiParseTask).filter(models.AiParseTask.id == task_id).first()
            if at is not None:
                at.task_status = "failed"
                at.completed_at = datetime.now()
                at.error_message = str(exc)
                db.commit()
        except Exception:
            pass
        if task is not None:
            task["status"] = "failed"
            task["result"] = {
                "ok": False,
                "status": "failed",
                "message": ocr_service.OCR_FAIL_MESSAGE,
                "reason": str(exc),
                "page_count": 0,
                "pages": [],
                "structured_text": "",
                "markdown": "",
                "recognized_fields": {},
            }
    finally:
        db.close()
