"""文件识别路由：PDF/图片 OCR（真实识别，见 ocr_service.py）。"""

import os
import tempfile

from fastapi import APIRouter, Depends, File, UploadFile

import models
import ocr_service
from app.deps import get_current_user

router = APIRouter()


@router.post("/api/recognize", tags=["文件识别"], summary="PDF/图片 OCR 识别（结构化输出，含页码）")
async def recognize_file(file: UploadFile = File(...),
                         user: models.SysUser = Depends(get_current_user)):
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
