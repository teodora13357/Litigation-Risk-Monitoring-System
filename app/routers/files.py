"""案件文件路由：上传 / 列表 / 预览 / 删除 / OCR 状态 / 自动匹配（预留）。"""

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import crud
from app import error_codes
from app import models
from app.database import get_db
from app.error_codes import BizError
from app.audit import write_audit_log
from app.deps import get_current_user
from app.files_config import DOC_TYPE_MAP, CATEGORY_TO_DOCTYPE, ALLOWED_EXTS, MAX_FILE_SIZE, UPLOAD_DIR
from app.ocr_tasks import OCR_TASKS, run_ocr_background

router = APIRouter()


@router.post("/api/files/upload", tags=["案件文件"], summary="上传文件（按类型存储）")
async def upload_file(
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    case_id: Optional[int] = Form(None),
    uploader: str = Form(""),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    request: Request = None,
    user: models.SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传 PDF / 图片，按所属类型存储并告知处理结果。

    - core_document（核心法定文书）：保存 + 后台异步触发真实 OCR（完成后回写 extracted_fields）
    - evidence（证据材料）/ procedural_notice（程序性告知附件）：仅保存
    若指定 case_id，则向该案件专属表写入文件索引。
    """
    if doc_type not in DOC_TYPE_MAP:
        raise HTTPException(status_code=400, detail="未知的文件类型: " + doc_type)
    cfg = DOC_TYPE_MAP[doc_type]
    filename = file.filename or "未命名文件"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # 校验 1：格式白名单
    if ext not in ALLOWED_EXTS:
        raise BizError(error_codes.FILE_FORMAT_NOT_SUPPORTED, "不支持的文件格式，请上传PDF或图片")

    content = await file.read()

    # 校验 2：单文件大小 <= 50MB
    if len(content) > MAX_FILE_SIZE:
        raise BizError(error_codes.FILE_SIZE_EXCEEDED, "文件大小超出限制（最大50MB）")

    # 文件 MD5（完整性校验 / 审计）
    file_md5 = hashlib.md5(content).hexdigest()

    now = datetime.now()
    rel_dir = os.path.join(now.strftime("%Y"), now.strftime("%m"))
    subdir = os.path.join(UPLOAD_DIR, rel_dir)
    os.makedirs(subdir, exist_ok=True)
    stored_name = uuid.uuid4().hex + "." + ext
    rel_path = os.path.join(rel_dir, stored_name)
    file_path = os.path.join(UPLOAD_DIR, rel_path)

    # 本地存储：文件直接落盘 static/uploads/YYYY/MM/
    with open(file_path, "wb") as f:
        f.write(content)

    # 校验 3/4：PDF 加密/损坏检测；图片损坏检测
    if ext == "pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(file_path)
            if reader.is_encrypted:
                raise BizError(error_codes.FILE_ENCRYPTED_OR_CORRUPTED, "PDF文件已加密，请解除保护后重试")
            _ = len(reader.pages)
        except HTTPException:
            raise
        except Exception:
            raise BizError(error_codes.FILE_ENCRYPTED_OR_CORRUPTED, "文件已损坏，请检查后重新上传")
    else:
        from PIL import Image
        try:
            img = Image.open(file_path)
            img.verify()
        except Exception:
            raise BizError(error_codes.FILE_ENCRYPTED_OR_CORRUPTED, "文件已损坏，请检查后重新上传")

    record = models.CaseFile(
        file_name=filename,
        file_size=len(content),
        file_path=rel_path,
        mime_type=file.content_type or "application/octet-stream",
        file_type=ext,
        file_category=cfg["category"],
        case_id=case_id,
        md5=file_md5,
        upload_by=uploader or "未署名",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # 上传成功后自动触发 OCR（异步后台执行，结果回写 case_file 与 ai_parse_task）
    recognized = None
    ocr_result = None
    if cfg["recognize"]:
        at = models.AiParseTask(file_id=record.id, task_type="field_extract", task_status="pending")
        db.add(at)
        db.commit()
        db.refresh(at)

        OCR_TASKS[record.id] = {
            "status": "pending",
            "file_id": record.id,
            "doc_type": doc_type,
            "result": None,
        }
        background_tasks.add_task(
            run_ocr_background,
            record.id,
            doc_type,
            rel_path,
            record.mime_type,
            file.content_type or "",
            at.id,
        )
        ocr_result = {
            "status": "pending",
            "ok": False,
            "message": "OCR 识别已提交，后台处理中，请稍后查询",
            "page_count": 0,
            "pages": [],
            "structured_text": "",
            "markdown": "",
            "recognized_fields": {},
        }

    # 审计：上传文件
    write_audit_log(db, "CREATE", "FILE", target_id=record.id,
                    action_detail={"file_name": filename, "doc_type": doc_type, "file_category": cfg["category"]},
                    request=request, user_id=user.id, user_name=user.username)

    return {
        "message": "上传成功，已保存为" + cfg["label"],
        "file_id": record.id,
        "doc_type": doc_type,
        "doc_type_label": cfg["label"],
        "case_id": case_id,
        "file_name": filename,
        "file_size": len(content),
        "recognized_fields": recognized,
        "ocr_result": ocr_result,
    }

@router.get("/api/files/{doc_type}/{file_id}/ocr", tags=["案件文件"], summary="查询文件 OCR 识别状态")
def get_ocr_status(doc_type: str, file_id: int,
                   user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """查询某文件的 OCR 识别状态。

    返回 status：pending（后台处理中）/ running / done / failed / not_found。
    服务重启后内存任务丢失时，回读数据库 extracted_fields 兜底。
    """
    if doc_type not in DOC_TYPE_MAP:
        raise HTTPException(status_code=400, detail="未知的文件类型: " + doc_type)

    task = OCR_TASKS.get(file_id)
    if task is not None:
        return {
            "file_id": file_id,
            "doc_type": doc_type,
            "status": task["status"],
            "ocr_result": task["result"],
        }

    # 兜底 1：从 case_file.ai_result 读取已落库的识别结果
    cf = db.query(models.CaseFile).filter(models.CaseFile.id == file_id).first()
    if cf is not None and cf.ai_result:
        result = cf.ai_result if isinstance(cf.ai_result, dict) else json.loads(cf.ai_result)
        status = "done" if result.get("ok") else "failed"
        return {"file_id": file_id, "doc_type": doc_type, "status": status, "ocr_result": result}

    # 兜底 2：从 ai_parse_task 读取最新任务状态
    at = (
        db.query(models.AiParseTask)
        .filter(models.AiParseTask.file_id == file_id)
        .order_by(models.AiParseTask.id.desc())
        .first()
    )
    if at is not None and at.task_status in ("completed", "failed"):
        result = at.output_result or {
            "ok": at.task_status == "completed",
            "message": "OCR识别失败，建议手动录入" if at.task_status == "failed" else "识别成功",
            "reason": at.error_message or "",
        }
        return {"file_id": file_id, "doc_type": doc_type, "status": at.task_status, "ocr_result": result}
    if at is not None:
        return {"file_id": file_id, "doc_type": doc_type, "status": at.task_status, "ocr_result": None}

    return {"file_id": file_id, "doc_type": doc_type, "status": "not_found", "ocr_result": None}


@router.get("/api/cases/{case_id}/files", tags=["案件文件"], summary="查看案件所有文件（按类型分组）")
def list_case_files(case_id: int, user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """查询 case_file 表中该案件的文件，按 file_category 分组（响应键保持旧 doc_type 兼容前端）。"""
    if crud.get_lawsuit_case(db, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")

    files = (
        db.query(models.CaseFile)
        .filter(models.CaseFile.case_id == case_id, models.CaseFile.is_deleted == 0)
        .order_by(models.CaseFile.id.desc())
        .all()
    )
    grouped = {"core_document": [], "evidence": [], "procedural_notice": []}
    for f in files:
        key = CATEGORY_TO_DOCTYPE.get(f.file_category, f.file_category)
        grouped.setdefault(key, []).append({
            "id": f.id,
            "file_name": f.file_name,
            "file_size": f.file_size or 0,
            "file_path": f.file_path or "",
            "mime_type": f.mime_type or "",
            "uploader": f.upload_by or "",
            "uploaded_at": f.created_at.strftime("%Y-%m-%d %H:%M:%S") if f.created_at else "",
        })
    return {"case_id": case_id, "files": grouped}


@router.get("/api/files/preview/{doc_type}/{file_id}", tags=["案件文件"], summary="预览文件")
def preview_file(doc_type: str, file_id: int,
                 user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """返回文件内容（PDF / 图片）用于预览。"""
    if doc_type not in DOC_TYPE_MAP:
        raise HTTPException(status_code=400, detail="未知的文件类型: " + doc_type)
    row = db.query(models.CaseFile).filter(models.CaseFile.id == file_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row.is_deleted:
        raise HTTPException(status_code=404, detail="文件已删除")
    # 本地存储模式：直接返回本地文件；文件丢失则 404
    local_path = os.path.join(UPLOAD_DIR, row.file_path)
    if not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="文件已丢失")
    return FileResponse(path=local_path, media_type=row.mime_type, headers={"Content-Disposition": "inline"})


@router.delete("/api/files/{doc_type}/{file_id}", tags=["案件文件"], summary="删除文件（软删除/物理删除）")
def delete_file(
    doc_type: str,
    file_id: int,
    hard: bool = Query(False),
    request: Request = None,
    user: models.SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除文件。

    - 软删除（默认，hard=false）：标记 is_deleted，文件保留在磁盘，列表不再显示、预览不可访问
    - 物理删除（hard=true）：删除磁盘文件 + 数据库记录 + 案件索引
    """
    if doc_type not in DOC_TYPE_MAP:
        raise HTTPException(status_code=400, detail="未知的文件类型: " + doc_type)
    row = db.query(models.CaseFile).filter(models.CaseFile.id == file_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    uid = user.id
    uname = user.username
    if hard:
        path = os.path.join(UPLOAD_DIR, row.file_path)
        if os.path.exists(path):
            os.remove(path)
        db.delete(row)
        db.commit()
        write_audit_log(db, "DELETE", "FILE", target_id=file_id,
                        action_detail={"file_name": row.file_name, "hard": True},
                        request=request, user_id=uid, user_name=uname)
        return {"message": "文件已物理删除", "file_id": file_id, "doc_type": doc_type, "hard": True}
    else:
        row.is_deleted = 1
        db.commit()
        write_audit_log(db, "DELETE", "FILE", target_id=file_id,
                        action_detail={"file_name": row.file_name, "hard": False},
                        request=request, user_id=uid, user_name=uname)
        return {"message": "文件已软删除（标记保留，可在数据库中恢复）", "file_id": file_id, "doc_type": doc_type, "hard": False}


@router.post("/api/cases/{case_id}/match-files", tags=["案件文件"], summary="根据识别字段匹配案件文件（预留）")
def match_files_to_case(case_id: int, user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """预留接口：根据核心法定文书识别字段自动匹配该案件三类文件并写入索引。

    当前业务逻辑未实现，仅返回占位结果；后续在此补充自动匹配逻辑。
    """
    if crud.get_lawsuit_case(db, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    # TODO: 实现自动匹配（根据识别出的 case_number 等字段关联案件与文件）
    return {
        "message": "自动匹配接口已预留（业务逻辑待实现）",
        "case_id": case_id,
        "matched": {"core_document": [], "evidence": [], "procedural_notice": []},
    }


# ===================== API v1（版本化文件接口，复用现有逻辑） =====================

@router.post("/api/v1/files/upload", tags=["v1文件"], summary="上传文件")
async def v1_upload_file(
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    case_id: Optional[int] = Form(None),
    uploader: str = Form(""),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    user: models.SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传文件（v1 接口，复用现有逻辑）。"""
    return await upload_file(
        file=file,
        doc_type=doc_type,
        case_id=case_id,
        uploader=uploader,
        background_tasks=background_tasks,
        request=None,
        user=user,
        db=db,
    )
