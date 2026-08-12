import os
import re
import tempfile
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, File, Body, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import Optional

from database import engine, get_db, SessionLocal
import models
import schemas
import crud
import excel_export
import oss_client
import error_codes
from error_codes import BizError
import auth
import ocr_service

models.Base.metadata.create_all(bind=engine)

_CASE_NUMBER_RE = re.compile(r"^\(\d{4}\)[^()（）\s]+民(初|终)\d{4}号$")


def _validate_case_number(case_number: str) -> None:
    """校验案号格式：(YYYY)法院代码民初/终XXXX号"""
    if _CASE_NUMBER_RE.match(case_number) is None:
        raise BizError(error_codes.MISSING_REQUIRED_FIELD, "案号格式不正确，格式：(YYYY)法院代码民初/终XXXX号")


def _generate_case_code(db) -> str:
    """生成案件编号：CASE-{YYYY}{MMDD}-{4位序号}（当天序号递增）"""
    today = datetime.now()
    prefix = "CASE-" + today.strftime("%Y%m%d") + "-"
    count = db.query(models.Case).filter(models.Case.case_code.like(prefix + "%")).count()
    return f"{prefix}{count + 1:04d}"

def _ensure_default_user():
    """确保默认用户存在：admin / manager / user（各角色一个）。"""
    db = SessionLocal()
    try:
        defaults = [
            {"username": "admin", "phone": "13800138000", "password": "admin123", "role": "admin"},
            {"username": "manager", "phone": "13800138001", "password": "manager123", "role": "manager"},
            {"username": "user", "phone": "13800138002", "password": "user123", "role": "user"},
        ]
        for d in defaults:
            user = db.query(models.User).filter(models.User.username == d["username"]).first()
            if user is None:
                db.add(models.User(
                    username=d["username"],
                    phone=d["phone"],
                    password_hash=auth.hash_password(d["password"]),
                    failed_attempts=0,
                    role=d["role"],
                    created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ))
            else:
                user.role = d["role"]
        db.commit()
        print("默认用户已就绪: admin/manager/user")
    finally:
        db.close()



_ensure_default_user()


app = FastAPI(
    title="案件管理系统",
    description="法律案件管理 — 增查删改 + Excel导出",
    version="1.0.0",
)



@app.exception_handler(error_codes.BizError)
async def biz_error_handler(request: Request, exc: error_codes.BizError):
    return JSONResponse(status_code=exc.http_status, content={
        "code": exc.code,
        "message": exc.message,
        "data": None,
    })
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ===================== 前端页面 =====================

@app.get("/", response_class=HTMLResponse, tags=["前端页面"], summary="案件管理页面")
def index():
    """返回案件管理前端页面。"""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/login", response_class=HTMLResponse, tags=["认证页面"], summary="登录页")
def login_page():
    login_path = os.path.join(os.path.dirname(__file__), "login.html")
    if not os.path.exists(login_path):
        raise HTTPException(status_code=404, detail="登录页不存在")
    with open(login_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
        })

# ===================== 增 =====================

@app.post("/api/cases", response_model=schemas.CaseResponse, tags=["案件管理"], summary="新增案件")
def create_case(case: schemas.CaseCreate, db: Session = Depends(get_db)):
    """新增一条案件记录，案号为必填，其余字段可选。"""
    # 校验必填字段
    if case.case_number is None or case.case_number.strip() == "":
        raise BizError(error_codes.MISSING_REQUIRED_FIELD, "案号不能为空")
    # 校验案号格式：(YYYY)法院代码民初/终XXXX号
    _validate_case_number(case.case_number)
    # 校验案号唯一
    exists = db.query(models.Case).filter(models.Case.case_number == case.case_number).first()
    if exists is not None:
        raise BizError(error_codes.CASE_NUMBER_EXISTS, "案号已存在")
    new_case = crud.create_case(db, case)
    # 自动生成案件编号 CASE-{YYYYMMDD}-{4位序号}
    new_case.case_code = _generate_case_code(db)
    db.commit()
    db.refresh(new_case)
    # 为该案件自动创建专属文件索引表 case_{id}
    get_case_table(new_case.id)
    return new_case


# ===================== 查 =====================

@app.get("/api/cases", response_model=list[schemas.CaseResponse], tags=["案件管理"], summary="查询案件列表")
def list_cases(
    skip: int = 0,
    limit: int = Query(default=100, le=500),
    case_number: Optional[str] = None,
    case_type: Optional[str] = None,
    stage: Optional[str] = None,
    is_closed: Optional[bool] = None,
    handler: Optional[str] = None,
    processing_status: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
    db: Session = Depends(get_db),
):
    """查询案件列表，支持按案号、案件类型、阶段、是否结案、处理人、处理情况筛选；支持按时间排序（空值排最后）。"""
    return crud.get_cases(
        db,
        skip=skip,
        limit=limit,
        case_number=case_number,
        case_type=case_type,
        stage=stage,
        is_closed=is_closed,
        handler=handler,
        processing_status=processing_status,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@app.get("/api/cases/{case_id}", response_model=schemas.CaseResponse, tags=["案件管理"], summary="查询单个案件")
def get_case(case_id: int, db: Session = Depends(get_db)):
    """根据 ID 查询单个案件详情。"""
    db_case = crud.get_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    return db_case


# ===================== 改 =====================

@app.put("/api/cases/{case_id}", response_model=schemas.CaseResponse, tags=["案件管理"], summary="修改案件")
def update_case(case_id: int, case: schemas.CaseUpdate, db: Session = Depends(get_db)):
    """修改案件信息，支持部分更新 — 只需传入要修改的字段，每个字段都可单独修改。"""
    db_case = crud.update_case(db, case_id, case)
    if db_case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    return db_case


# ===================== 删 =====================

@app.delete("/api/cases/{case_id}", tags=["案件管理"], summary="删除案件")
def delete_case(case_id: int, db: Session = Depends(get_db)):
    """根据 ID 删除一条案件记录。"""
    success = crud.delete_case(db, case_id)
    if not success:
        raise HTTPException(status_code=404, detail="案件不存在")
    return {"message": "案件已删除", "case_id": case_id}


# ===================== 文件识别（演示模式，后续接入真实 OCR） =====================

# 模拟识别提取结果：用于前端演示“识别→填表”流程，后续接入真实识别后替换
_SAMPLE_RECOGNIZED = {
    "case_number": "(2026)沪0101民初952号",
    "case_type": "民事",
    "involved_parties": "某某科技有限公司",
    "delivery_time": "2026-08-10",
    "court_time": "2026-09-15 09:30",
    "court_location": "上海市徐汇区人民法院",
    "contact_phone": "13800138000",
    "plaintiff": "张三",
    "id_number": "310101199001011234",
    "defendant": "李四",
    "service_client": "某某银行",
    "stage": "一审",
    "client_contact": "王经理",
    "processing_status": "处理中",
    "handler": "张律师",
    "defense_method": "书面答辩",
    "is_closed": False,
    "judgment_result": "",
    "compensation_amount": 12000.00,
}


@app.post("/api/recognize", tags=["文件识别"], summary="PDF/图片 OCR 识别（结构化输出，含页码）")
async def recognize_file(file: UploadFile = File(...)):
    """上传 PDF / 图片，返回 OCR 结构化结果（含页码标记）。

    - OCR 前预处理：降噪 / 纠偏 / 二值化 / 对比度增强
    - 结构化输出：pages / structured_text / markdown（每页带页码标记）
    - 识别失败时 message 为 "OCR识别失败，建议手动录入"
    """
    filename = file.filename or "未命名文件"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "unknown"
    content = await file.read()

    suffix = "." + ext if ext in ocr_service.ALLOWED_EXTS else ""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        ocr_result = ocr_service.recognize_document(tmp_path, file.content_type or "")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return {
        "file_name": filename,
        "file_type": ext,
        "ocr_result": ocr_result,
        "recognized_fields": ocr_result.get("recognized_fields", {}),
    }

# ===================== 案件文件管理（上传/列表/预览/匹配预留） =====================

import json
import uuid
from datetime import datetime
from fastapi import Form
from sqlalchemy import MetaData, Table, Column as SAColumn, Integer as SAInteger, String as SAString, insert, select, delete

# 文件类型映射：类型标识 → 中文名 / 对应模型 / 是否识别
DOC_TYPE_MAP = {
    "core_document": {"label": "核心法定文书", "model": models.CoreDocument, "recognize": True},
    "evidence": {"label": "证据材料", "model": models.EvidenceMaterial, "recognize": False},
    "procedural_notice": {"label": "程序性告知附件", "model": models.ProceduralNotice, "recognize": False},
}

# 上传校验规则
ALLOWED_EXTS = {"pdf", "jpg", "jpeg", "png", "bmp", "tiff"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)

# OCR 异步任务追踪（内存态：file_id -> 状态）。服务重启后任务状态丢失，
# 查询接口会回读数据库 extracted_fields 兜底。
_OCR_TASKS: dict = {}


def _run_ocr_background(
    file_id: int,
    doc_type: str,
    rel_path: str,
    mime_type: str,
    content_type: str,
    use_oss: bool,
) -> None:
    """后台执行 OCR 并回写 core_documents.extracted_fields（同步接口的异步版）。"""
    task = _OCR_TASKS.get(file_id)
    if task is not None:
        task["status"] = "running"
    local_path = os.path.join(_UPLOAD_DIR, rel_path)
    tmp_download = None
    try:
        # OSS 模式下本地文件已清理，先回填到临时文件再识别
        if use_oss and not os.path.exists(local_path):
            tmp_download = local_path + ".ocr_tmp"
            oss_client.download_object(rel_path, tmp_download)
            target = tmp_download
        else:
            target = local_path
        print(f"[OCR] 开始后台识别 file_id={file_id} path={target}")
        result = ocr_service.recognize_document(target, content_type or mime_type)
        print(f"[OCR] 完成 file_id={file_id} ok={result.get('ok')} pages={result.get('page_count')}")

        # 回写数据库（后台线程需独立会话）
        db = SessionLocal()
        try:
            cfg = DOC_TYPE_MAP[doc_type]
            record = db.query(cfg["model"]).filter(cfg["model"].id == file_id).first()
            if record is not None:
                record.extracted_fields = json.dumps(result, ensure_ascii=False)
                db.commit()
        finally:
            db.close()

        if task is not None:
            task["status"] = "done" if result.get("ok") else "failed"
            task["result"] = result
    except Exception as exc:  # noqa: BLE001
        print(f"[OCR] 后台识别异常 file_id={file_id}: {exc}")
        import traceback
        traceback.print_exc()
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
        if tmp_download and os.path.exists(tmp_download):
            try:
                os.remove(tmp_download)
            except OSError:
                pass

_CASE_TABLE_CACHE = {}


def get_case_table(case_id: int):
    """获取（必要时自动创建）某案件专属的文件索引表 case_{case_id}。"""
    name = f"case_{int(case_id)}"
    if name in _CASE_TABLE_CACHE:
        return _CASE_TABLE_CACHE[name]
    metadata = MetaData()
    t = Table(
        name, metadata,
        SAColumn("id", SAInteger, primary_key=True, autoincrement=True),
        SAColumn("doc_type", SAString(50), comment="文件类型"),
        SAColumn("file_id", SAInteger, comment="文件在类型表中的ID"),
        SAColumn("file_name", SAString(255), comment="文件名"),
        SAColumn("created_at", SAString(50), comment="加入时间"),
    )
    t.create(bind=engine, checkfirst=True)
    _CASE_TABLE_CACHE[name] = t
    return t


@app.post("/api/files/upload", tags=["案件文件"], summary="上传文件（按类型存储并写入案件索引表）")
async def upload_file(
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    case_id: Optional[int] = Form(None),
    uploader: str = Form(""),
    background_tasks: BackgroundTasks = BackgroundTasks(),
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
    import hashlib
    file_md5 = hashlib.md5(content).hexdigest()

    now = datetime.now()
    rel_dir = os.path.join(now.strftime("%Y"), now.strftime("%m"))
    subdir = os.path.join(_UPLOAD_DIR, rel_dir)
    os.makedirs(subdir, exist_ok=True)
    stored_name = uuid.uuid4().hex + "." + ext
    rel_path = os.path.join(rel_dir, stored_name)
    file_path = os.path.join(_UPLOAD_DIR, rel_path)

    oss_etag = ""
    use_oss = oss_client.is_oss_configured()
    recognized = None
    ocr_result = None
    try:
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

        # 上传至 OSS（长期归档，本地文件转为预览缓存）
        if use_oss:
            oss_etag = oss_client.upload_object(rel_path, file_path)
    finally:
        # OSS 模式下清理本地临时文件（预览时按需从 OSS 拉取回填缓存）
        if use_oss and os.path.exists(file_path):
            os.remove(file_path)

    record = cfg["model"](
        file_name=filename,
        file_size=len(content),
        file_path=rel_path,
        mime_type=file.content_type or "application/octet-stream",
        uploader=uploader or "未署名",
        uploaded_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        case_id=case_id,
        etag=oss_etag or None,
        md5=file_md5,
    )
    # 保存 OCR 结构化结果（含页码标记）到 extracted_fields
    if ocr_result is not None:
        record.extracted_fields = json.dumps(ocr_result, ensure_ascii=False)
    db.add(record)
    db.commit()
    db.refresh(record)

    # 写入案件专属索引表
    if case_id is not None and crud.get_case(db, case_id) is not None:
        t = get_case_table(case_id)
        with engine.begin() as conn:
            conn.execute(insert(t).values(
                doc_type=doc_type,
                file_id=record.id,
                file_name=filename,
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ))

    # 上传成功后自动触发 OCR（异步后台执行，完成后回写 extracted_fields）
    recognized = None
    ocr_result = None
    if cfg["recognize"]:
        _OCR_TASKS[record.id] = {
            "status": "pending",
            "file_id": record.id,
            "doc_type": doc_type,
            "result": None,
        }
        background_tasks.add_task(
            _run_ocr_background,
            record.id,
            doc_type,
            rel_path,
            record.mime_type,
            file.content_type or "",
            use_oss,
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


@app.get("/api/files/{doc_type}/{file_id}/ocr", tags=["案件文件"], summary="查询文件 OCR 识别状态")
def get_ocr_status(doc_type: str, file_id: int, db: Session = Depends(get_db)):
    """查询某文件的 OCR 识别状态。

    返回 status：pending（后台处理中）/ running / done / failed / not_found。
    服务重启后内存任务丢失时，回读数据库 extracted_fields 兜底。
    """
    if doc_type not in DOC_TYPE_MAP:
        raise HTTPException(status_code=400, detail="未知的文件类型: " + doc_type)

    task = _OCR_TASKS.get(file_id)
    if task is not None:
        return {
            "file_id": file_id,
            "doc_type": doc_type,
            "status": task["status"],
            "ocr_result": task["result"],
        }

    # 兜底：从数据库读取已落库的识别结果
    cfg = DOC_TYPE_MAP[doc_type]
    row = db.query(cfg["model"]).filter(cfg["model"].id == file_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row.extracted_fields:
        try:
            result = json.loads(row.extracted_fields)
            status = "done" if result.get("ok") else "failed"
            return {"file_id": file_id, "doc_type": doc_type, "status": status, "ocr_result": result}
        except Exception:
            pass
    return {"file_id": file_id, "doc_type": doc_type, "status": "not_found", "ocr_result": None}


@app.get("/api/cases/{case_id}/files", tags=["案件文件"], summary="查看案件所有文件（按类型分组）")
def list_case_files(case_id: int, db: Session = Depends(get_db)):
    """从案件专属索引表读取该案件三类文件，并补充文件详情。"""
    if crud.get_case(db, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    t = get_case_table(case_id)
    with engine.connect() as conn:
        rows = conn.execute(select(t)).fetchall()
    grouped = {"core_document": [], "evidence": [], "procedural_notice": []}
    for row in rows:
        doc_type = row.doc_type
        if doc_type not in grouped:
            continue
        cfg = DOC_TYPE_MAP[doc_type]
        detail = db.query(cfg["model"]).filter(cfg["model"].id == row.file_id, cfg["model"].is_deleted == False).first()
        if detail is None:
            continue
        grouped[doc_type].append({
            "id": row.file_id,
            "file_name": row.file_name,
            "file_size": detail.file_size if detail else 0,
            "file_path": detail.file_path if detail else "",
            "mime_type": detail.mime_type if detail else "",
            "uploader": detail.uploader if detail else "",
            "uploaded_at": detail.uploaded_at if detail else "",
            "added_at": row.created_at,
        })
    return {"case_id": case_id, "files": grouped}


@app.get("/api/files/preview/{doc_type}/{file_id}", tags=["案件文件"], summary="预览文件")
def preview_file(doc_type: str, file_id: int, db: Session = Depends(get_db)):
    """返回文件内容（PDF / 图片）用于预览。"""
    if doc_type not in DOC_TYPE_MAP:
        raise HTTPException(status_code=400, detail="未知的文件类型: " + doc_type)
    cfg = DOC_TYPE_MAP[doc_type]
    row = db.query(cfg["model"]).filter(cfg["model"].id == file_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row.is_deleted:
        raise HTTPException(status_code=404, detail="文件已删除")
# 1. 本地缓存优先（预览快，OSS 故障时可离线读缓存）
    local_path = os.path.join(_UPLOAD_DIR, row.file_path)
    if os.path.exists(local_path):
        return FileResponse(path=local_path, media_type=row.mime_type, headers={"Content-Disposition": "inline"})
    # 2. 本地无缓存 → 从 OSS 拉取回填
    if oss_client.is_oss_configured():
        try:
            oss_client.download_object(row.file_path, local_path)
        except Exception:
            raise HTTPException(status_code=404, detail="文件已丢失")
        return FileResponse(path=local_path, media_type=row.mime_type, headers={"Content-Disposition": "inline"})
    raise HTTPException(status_code=404, detail="文件已丢失")


@app.delete("/api/files/{doc_type}/{file_id}", tags=["案件文件"], summary="删除文件（软删除/物理删除）")
def delete_file(
    doc_type: str,
    file_id: int,
    hard: bool = Query(False),
    db: Session = Depends(get_db),
):
    """删除文件。

    - 软删除（默认，hard=false）：标记 is_deleted，文件保留在磁盘，列表不再显示、预览不可访问
    - 物理删除（hard=true）：删除磁盘文件 + 数据库记录 + 案件索引
    """
    if doc_type not in DOC_TYPE_MAP:
        raise HTTPException(status_code=400, detail="未知的文件类型: " + doc_type)
    cfg = DOC_TYPE_MAP[doc_type]
    row = db.query(cfg["model"]).filter(cfg["model"].id == file_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 从案件专属索引表移除该文件索引
    if row.case_id is not None:
        t = get_case_table(row.case_id)
        with engine.begin() as conn:
            conn.execute(delete(t).where(t.c.doc_type == doc_type, t.c.file_id == file_id))

    if hard:
        if oss_client.is_oss_configured():
            oss_client.delete_object(row.file_path)
        path = os.path.join(_UPLOAD_DIR, row.file_path)
        if os.path.exists(path):
            os.remove(path)
        db.delete(row)
        db.commit()
        return {"message": "文件已物理删除", "file_id": file_id, "doc_type": doc_type, "hard": True}
    else:
        row.is_deleted = True
        db.commit()
        return {"message": "文件已软删除（标记保留，可在数据库中恢复）", "file_id": file_id, "doc_type": doc_type, "hard": False}

@app.post("/api/cases/{case_id}/match-files", tags=["案件文件"], summary="根据识别字段匹配案件文件（预留）")
def match_files_to_case(case_id: int, db: Session = Depends(get_db)):
    """预留接口：根据核心法定文书识别字段自动匹配该案件三类文件并写入索引。

    当前业务逻辑未实现，仅返回占位结果；后续在此补充自动匹配逻辑。
    """
    if crud.get_case(db, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    # TODO: 实现自动匹配（根据识别出的 case_number 等字段关联案件与文件）
    return {
        "message": "自动匹配接口已预留（业务逻辑待实现）",
        "case_id": case_id,
        "matched": {"core_document": [], "evidence": [], "procedural_notice": []},
    }

# ===================== Excel 导出 =====================

@app.get("/api/export/excel", tags=["数据导出"], summary="导出Excel")
def export_excel(db: Session = Depends(get_db)):
    """导出所有案件数据为 Excel 文件（.xlsx），含全部字段中文表头。"""
    cases = crud.get_all_cases(db)
    filepath = excel_export.export_to_excel(cases, "cases_export.xlsx")
    return FileResponse(
        path=filepath,
        filename="案件数据导出.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )




# ===================== API v1（版本化接口） =====================

# ---------- 认证（业务逻辑暂未实现，占位） ----------


@app.post("/api/v1/auth/login", tags=["v1认证"], summary="用户登录")
def v1_login(
    username: Optional[str] = Body(default=None, embed=True),
    password: Optional[str] = Body(default=None, embed=True),
    remember: Optional[bool] = Body(default=False, embed=True),
    db: Session = Depends(get_db),
):
    """用户登录：支持用户名或手机号；连续5次失败锁定5分钟；JWT token（24h / 记住我7天）。"""
    if not username or not password:
        raise BizError(error_codes.MISSING_REQUIRED_FIELD, "请输入用户名和密码")
    user = db.query(models.User).filter(
        (models.User.username == username) | (models.User.phone == username)
    ).first()
    if user is None:
        raise BizError(error_codes.AUTH_FAILED, "用户名或密码错误")
    if auth.is_locked(user.locked_until):
        raise BizError(error_codes.ACCOUNT_LOCKED, "帐户已锁定，请5分钟后再试", http_status=403)
    if not auth.verify_password(password, user.password_hash):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= auth.MAX_FAILED_ATTEMPTS:
            user.locked_until = (datetime.now() + timedelta(minutes=auth.LOCK_MINUTES)).isoformat()
            db.commit()
            raise BizError(error_codes.ACCOUNT_LOCKED, "连续失败5次，账户已锁定5分钟", http_status=403)
        db.commit()
        raise BizError(error_codes.AUTH_FAILED, "用户名或密码错误")
    user.failed_attempts = 0
    user.locked_until = None
    db.commit()
    token = auth.create_token(user.id, user.username or user.phone, remember=bool(remember))
    return error_codes.error_response(0, "登录成功", {
        "token": token,
        "user": {"id": user.id, "username": user.username, "phone": user.phone, "role": user.role},
        "permissions": auth.get_permissions(user.role),
        "expires_in": (7 * 24 * 3600) if remember else (24 * 3600),
    })

@app.post("/api/v1/auth/logout", tags=["v1认证"], summary="用户登出")
def v1_logout():
    """用户登出。JWT 无状态，前端删除 token 即可完成登出。"""
    return error_codes.error_response(0, "登出成功")

# ---------- AI 文书解析（业务逻辑暂未实现，占位） ----------

_AI_TASKS: dict = {}  # 内存占位：task_id -> 状态；后续替换为持久化任务表


@app.post("/api/v1/ai/parse", tags=["v1AI解析"], summary="触发AI文书解析")
def v1_ai_parse(
    file_id: Optional[int] = Body(default=None, embed=True),
):
    """触发 AI 文书解析。业务逻辑待实现，当前创建占位任务。"""
    # TODO: 接入 AI 解析（LangChain OCR/视觉模型），异步任务 + 持久化
    task_id = uuid.uuid4().hex
    _AI_TASKS[task_id] = {"status": "pending", "file_id": file_id}
    return error_codes.error_response(0, "AI解析任务已创建（业务逻辑待实现）", {"task_id": task_id, "status": "pending"})


@app.get("/api/v1/ai/parse/{task_id}", tags=["v1AI解析"], summary="查询解析任务结果")
def v1_ai_parse_result(task_id: str):
    """查询 AI 解析任务结果。业务逻辑待实现。"""
    if task_id not in _AI_TASKS:
        return error_codes.error_response(error_codes.AI_PARSE_FAILED, "任务不存在")
    return error_codes.error_response(0, "AI解析业务逻辑待实现", {"task_id": task_id, "status": _AI_TASKS[task_id]["status"]})


# ---------- 案件（复用现有业务逻辑） ----------


@app.post("/api/v1/cases", response_model=schemas.CaseResponse, tags=["v1案件"], summary="创建诉讼案件")
def v1_create_case(case: schemas.CaseCreate, db: Session = Depends(get_db)):
    """创建诉讼案件（v1 接口，复用现有逻辑）。"""
    return create_case(case, db)


@app.post("/api/v1/cases/manual", response_model=schemas.CaseResponse, tags=["v1案件"], summary="手动录入案件")
def v1_create_case_manual(case: schemas.CaseCreate, db: Session = Depends(get_db)):
    """手动录入案件（v1 接口，复用现有逻辑）。"""
    return create_case(case, db)


# ---------- 文件上传（复用现有业务逻辑） ----------


@app.post("/api/v1/files/upload", tags=["v1文件"], summary="上传文件")
async def v1_upload_file(
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    case_id: Optional[int] = Form(None),
    uploader: str = Form(""),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    """上传文件（v1 接口，复用现有逻辑）。"""
    return await upload_file(
        file=file,
        doc_type=doc_type,
        case_id=case_id,
        uploader=uploader,
        background_tasks=background_tasks,
        db=db,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
