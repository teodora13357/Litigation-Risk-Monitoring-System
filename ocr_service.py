"""可复用的 OCR 服务模块 —— 接入本地 MinerU 解析服务。

功能：
1. PDF / 图片的统一 OCR 识别入口（上传成功后自动触发）
2. OCR 前预处理：灰度 → 限幅 → 降噪 → 倾斜校正(纠偏) → 对比度增强 → 二值化
3. 结构化输出（含页码标记）：调用 MinerU 的 return_middle_json 按页重组
4. 失败时返回统一提示："OCR识别失败，建议手动录入"
5. 附带轻量关键字段提取（案号/案由/原告/被告/金额等），供前端表单回填

依赖：
- 必需: requests, Pillow
- 增强: opencv-python-headless, numpy（降噪/纠偏/CLAHE/Otsu）
- PDF 渲染: pymupdf

用法：
    from ocr_service import recognize_document
    result = recognize_document("/path/to/起诉状.pdf", "application/pdf")
    # result: {"ok": bool, "message": str, "page_count": int,
    #          "pages": [...], "structured_text": str, "markdown": str,
    #          "recognized_fields": {...}}
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger("ocr_service")

# ===================== 配置（可用环境变量覆盖） =====================

MINERU_API_URL = os.getenv("MINERU_API_URL", "http://localhost:30000")
OCR_PARSE_TIMEOUT = int(os.getenv("OCR_PARSE_TIMEOUT", "600"))   # 单批解析超时(秒)
OCR_BATCH_PAGES = int(os.getenv("OCR_BATCH_PAGES", "10"))        # 每批提交的页数
OCR_PREPROCESS_DPI = int(os.getenv("OCR_PREPROCESS_DPI", "300"))  # PDF 渲染 DPI
OCR_MAX_IMAGE_SIDE = int(os.getenv("OCR_MAX_IMAGE_SIDE", "3500"))  # 图像长边上限(px)
OCR_BINARIZE = os.getenv("OCR_BINARIZE", "auto").strip().lower()  # auto/true/false
OCR_TEXT_MIN_CHARS = int(os.getenv("OCR_TEXT_MIN_CHARS", "30"))   # PDF 内嵌文本判定阈值(字符)

OCR_FAIL_MESSAGE = "OCR识别失败，建议手动录入"
ALLOWED_EXTS = {"pdf", "jpg", "jpeg", "png", "bmp", "tiff"}

# 与新的 LawsuitCase 手动录入字段对齐（recognized_fields 键名校验用）
FIELD_NAMES = [
    "court_case_no", "document_type", "plaintiff", "defendant",
    "involved_parties", "involved_clients", "case_type_name",
    "standard_cause_name", "court_name", "current_status",
    "assigned_contact", "case_description", "claim_amount",
    "deadline_date", "remark",
]

# ===================== 图像预处理 =====================


def _load_cv2():
    """惰性导入 cv2 / numpy；失败返回 (None, None) 以走 PIL 降级。"""
    try:
        import cv2
        import numpy as np
        return cv2, np
    except Exception:
        return None, None


def _detect_skew_angle(gray, cv2, np) -> float:
    """估算整页文本的倾斜角（度）。

    原理：对文本区域做膨胀 → 找连通域外接最小矩形(minAreaRect)，
    统计接近水平的矩形角度分布，取直方图峰值作为整体倾斜角。
    """
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (12, 4))
    dilated = cv2.dilate(thresh, kernel, iterations=2)
    contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    angles = []
    for cnt in contours:
        if cv2.contourArea(cnt) < 300:
            continue
        rect = cv2.minAreaRect(cnt)
        angle = rect[2]  # 范围 [-90, 0)
        if angle < -45:
            angle = 90 + angle
        if abs(angle) < 20:  # 只统计接近水平的矩形
            angles.append(angle)
    if not angles:
        return 0.0

    hist, edges = np.histogram(angles, bins=41, range=(-20, 20))
    peak = int(np.argmax(hist))
    return float((edges[peak] + edges[peak + 1]) / 2)


def _rotate_image(gray, angle: float, cv2):
    """绕中心旋转指定角度，空白处填充白色，保持原尺寸。"""
    h, w = gray.shape[:2]
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        gray, matrix, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def preprocess_image(image: Image.Image) -> Image.Image:
    """对单页图像做 OCR 前预处理：灰度→限幅→降噪→纠偏→对比度→二值化。

    - 优先使用 OpenCV（双边滤波、minAreaRect 纠偏、CLAHE、Otsu）
    - OpenCV 不可用时降级为 PIL（中值滤波、自动对比度、固定阈值二值化）
    """
    cv2, np = _load_cv2()
    img = image.convert("L")

    # 1) 限幅：超长边等比缩小，控制内存与耗时
    max_side = OCR_MAX_IMAGE_SIDE
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    if cv2 is not None and np is not None:
        arr = np.asarray(img, dtype=np.uint8)

        # 2) 降噪：双边滤波（保边降噪）
        arr = cv2.bilateralFilter(arr, 9, 75, 75)

        # 3) 倾斜校正（纠偏）
        angle = _detect_skew_angle(arr, cv2, np)
        if abs(angle) >= 0.3:
            logger.info("检测到倾斜角 %.2f°，执行纠偏", angle)
            arr = _rotate_image(arr, angle, cv2)

        # 4) 对比度增强：CLAHE
        arr = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(arr)

        # 5) 二值化（自适应）：疑似扫描件/低对比度才做，干净文档跳过
        if _should_binarize(arr):
            _, arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return Image.fromarray(arr)

    # ---- PIL 降级路径 ----
    img = img.filter(ImageFilter.MedianFilter(3))   # 降噪
    img = ImageOps.autocontrast(img)                # 对比度增强
    img = img.point(lambda p: 255 if p >= 140 else 0)  # 简单二值化
    return img


def _should_binarize(arr) -> bool:
    """根据 OCR_BINARIZE 与图像对比度决定是否执行二值化。

    - true:  强制二值化
    - false: 永不二值化
    - auto:  仅当图像整体对比度较低（疑似扫描件/照片）时才二值化，
             干净的数字化文档（高对比度）保留灰度细节，避免破坏抗锯齿文字。
    """
    mode = OCR_BINARIZE
    if mode == "true":
        return True
    if mode == "false":
        return False
    return float(arr.std()) < 60


# ===================== 文档 → 页图像 =====================


def _pdf_to_images(file_path: str) -> list:
    """将 PDF 逐页渲染为灰度 PIL 图像列表（DPI 由 OCR_PREPROCESS_DPI 控制）。"""
    try:
        import pymupdf  # PyMuPDF
    except ImportError:
        try:
            import fitz as pymupdf  # 旧包名兼容
        except ImportError as exc:
            raise RuntimeError("缺少 PyMuPDF 依赖，无法渲染 PDF 页面，请执行 pip install pymupdf") from exc

    doc = pymupdf.open(file_path)
    images = []
    try:
        zoom = OCR_PREPROCESS_DPI / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            images.append(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    finally:
        doc.close()
    return images


def _image_to_images(file_path: str) -> list:
    """加载单张图片为页图像（统一转 RGB）。"""
    with Image.open(file_path) as im:
        return [im.convert("RGB")]


def _pdf_has_text(file_path: str, min_chars: Optional[int] = None) -> bool:
    """用 pypdf 探测 PDF 是否含内嵌文本层（文字型 PDF）。"""
    min_chars = OCR_TEXT_MIN_CHARS if min_chars is None else min_chars
    try:
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        total = sum(len((p.extract_text() or "")) for p in reader.pages)
        return total >= min_chars
    except Exception:
        return False


def _split_pdf_to_pages(file_path: str) -> list:
    """将文字型 PDF 无损拆分为逐页 PDF，返回 [(page_no, bytes)]。"""
    try:
        import pymupdf  # PyMuPDF
    except ImportError:
        try:
            import fitz as pymupdf  # 旧包名兼容
        except ImportError as exc:
            raise RuntimeError("缺少 PyMuPDF 依赖，无法拆分 PDF 页面，请执行 pip install pymupdf") from exc

    doc = pymupdf.open(file_path)
    pages = []
    try:
        for i in range(len(doc)):
            single = pymupdf.open()
            single.insert_pdf(doc, from_page=i, to_page=i)
            buf = io.BytesIO()
            single.save(buf, garbage=3, deflate=True)
            single.close()
            pages.append((i + 1, buf.getvalue()))
    finally:
        doc.close()
    return pages


def preprocess_document(file_path: str, mime_type: str = "") -> tuple:
    """统一入口：返回 (mode, payload)。

    - ("pdf_pages", [(page_no, bytes), ...])  文字型 PDF → 逐页 PDF（无损文本）
    - ("images", [PIL.Image, ...])            扫描件 / 图片 → 预处理后的页图像
    """
    ext = Path(file_path).suffix.lower().lstrip(".")
    is_pdf = ext == "pdf" or "pdf" in (mime_type or "").lower()

    if is_pdf:
        if _pdf_has_text(file_path):
            pages = _split_pdf_to_pages(file_path)
            if pages:
                return "pdf_pages", pages
        # 扫描件或拆分失败：渲染 + 预处理
        images = [preprocess_image(p) for p in _pdf_to_images(file_path)]
    else:
        images = [preprocess_image(p) for p in _image_to_images(file_path)]
    if not images:
        raise ValueError("未能从文件中解析出任何页面")
    return "images", images


# ===================== MinerU 调用 =====================


def _parse_files_to_pages(page_files: list) -> list:
    """上传一组页文件（page_1.*, page_2.* ...）到 MinerU 同步解析。

    返回按页序排列的每页结果（每项含 md_content / middle_json）。
    注意：页图像必须用 PNG 无损格式 —— 实测 JPEG 压缩伪影会严重损害 OCR 质量。
    """
    files = []
    for i, (buf, ext, mime) in enumerate(page_files, start=1):
        files.append(("files", (f"page_{i:03d}.{ext}", buf, mime)))

    data = {
        "lang_list": ["ch"],
        "backend": "pipeline",
        "parse_method": "auto",
        "formula_enable": "true",
        "table_enable": "true",
        "return_md": "true",
        "return_middle_json": "true",
    }
    resp = requests.post(
        f"{MINERU_API_URL}/file_parse", files=files, data=data, timeout=OCR_PARSE_TIMEOUT
    )
    if resp.status_code != 200:
        raise RuntimeError(f"MinerU 返回 HTTP {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if payload.get("status") != "completed":
        raise RuntimeError(f"MinerU 任务未完成: {payload.get('error')}")
    results = payload.get("results") or {}

    # 按 page_NNN 顺序取回，避免 dict 顺序问题
    ordered = []
    for i in range(1, len(page_files) + 1):
        ordered.append(results.get(f"page_{i:03d}") or {})
    return ordered


def _call_mineru_pdf_pages(pages: list) -> list:
    """提交逐页 PDF（文字型 PDF 路径）。pages: [(page_no, bytes)]。"""
    return _parse_files_to_pages([(buf, "pdf", "application/pdf") for _, buf in pages])


def _call_mineru_images(page_images: list) -> list:
    """提交预处理后的页图像（扫描件 / 图片路径）。"""
    items = []
    for img in page_images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        items.append((buf, "png", "image/png"))
    return _parse_files_to_pages(items)


# ===================== 结构化输出（含页码标记） =====================


def _block_to_text(block: dict) -> str:
    """从 middle_json 的 para_block 提取文本（lines -> spans -> content）。"""
    parts = []
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            text = span.get("content")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _md_to_plain_text(md: str) -> str:
    """将页级 Markdown（含 HTML 表格）转成可读纯文本。"""
    if not md:
        return ""
    text = md
    # HTML 表格 → 表格线换行、单元格分隔
    text = re.sub(r"<tr[^>]*>", "\n", text)
    text = re.sub(r"</tr>", "\n", text)
    text = re.sub(r"<t[dh][^>]*>", " | ", text)
    text = re.sub(r"</t[dh]>", "", text)
    text = re.sub(r"<img[^>]*>", "[图片]", text)
    text = re.sub(r"<[^>]+>", "", text)
    # Markdown 图片 / 链接
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[图片]", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # 标题符号
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    # 合并多余空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _extract_blocks(page_result: dict, page_md: str = "") -> list:
    """从单个 MinerU 页结果提取结构化块列表（方案 A：基于 middle_json 按页重组）。

    文本块直接取 para_blocks 内容；表格/图片块仅标记类型。
    若整页无块内容（如表单型页面），降级为整段 md 纯文本块。
    """
    blocks = []
    mid = page_result.get("middle_json")
    if isinstance(mid, str):
        try:
            mid = json.loads(mid)
        except Exception:
            mid = None
    if isinstance(mid, dict):
        pdf_info = mid.get("pdf_info") or []
        for page in pdf_info:
            for block in page.get("para_blocks") or []:
                content = _block_to_text(block)
                if content.strip():
                    blocks.append({"type": block.get("type") or "text", "content": content.strip()})
    if not blocks and page_md.strip():
        blocks.append({"type": "text", "content": _md_to_plain_text(page_md)})
    return blocks


def _page_to_text(page_no: int, page_md: str) -> str:
    """将单页 Markdown 转成纯文本并加页码标记（表格内容保留）。"""
    body = _md_to_plain_text(page_md)
    return f"【第 {page_no} 页】\n{body}"


def _assemble_pages(page_results: list) -> dict:
    """将按页顺序的 MinerU 结果组装为带页码标记的结构化输出。"""
    pages = []
    md_parts = []
    text_parts = []
    for idx, pr in enumerate(page_results, start=1):
        page_md = (pr.get("md_content") or "").strip()
        blocks = _extract_blocks(pr, page_md)
        pages.append({"page": idx, "blocks": blocks, "markdown": page_md})
        md_parts.append(f"## ===== 第 {idx} 页 =====\n\n{page_md}")
        text_parts.append(_page_to_text(idx, page_md))
    return {
        "page_count": len(pages),
        "pages": pages,
        "structured_text": "\n\n".join(text_parts),
        "markdown": "\n\n".join(md_parts),
    }


# ===================== 关键字段启发式提取（供表单回填） =====================


def _first_match(text: str, patterns: list) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            value = (m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)) or ""
            value = value.strip().strip("：:，,。;；")
            return value if value else None
    return None


def _extract_key_fields(text: str) -> dict:
    """从 OCR 结构化文本中轻量正则提取字段（对齐新 LawsuitCase 字段名，找不到为 None）。"""
    fields = {k: None for k in FIELD_NAMES}
    # 表格转纯文本后，标签与值之间可能有 " | " 分隔符
    sep = r"[|：:\s]*"

    # 案号
    fields["court_case_no"] = _first_match(text, [
        r"([（(]?\d{4}[）)]?[^（）()\s]{2,20}民(?:初|终)\d+号)",
    ])
    # 文书类型：从文档标题/关键词识别
    fields["document_type"] = _first_match(text, [
        r"(民事起诉状|行政起诉状|刑事自诉状|仲裁申请书|答辩状|应诉通知书|答辩通知书|举证通知书|开庭传票|传\s*票|开庭通知书|判决书|裁决书|裁定书|调解书|生效证明|上诉状|二审判决书|二审裁定书|执行通知书|执行结案通知)",
    ])
    # 原告（申请人）
    fields["plaintiff"] = _first_match(text, [
        r"原告[（(]?自然人[）)]?" + sep + r"姓名" + sep + r"([^\s|，,。；;]{1,12}?)(?:性别|出生日期|□|$)",
        r"原告" + sep + r"([^\s|，,。；;]{1,12}?)(?:性别|出生日期|□|被告|$)",
    ])
    # 被告（被申请人）
    fields["defendant"] = _first_match(text, [
        r"被告[一二三四五六七八九十]?[（(]?自然人[）)]?" + sep + r"姓名" + sep + r"([^\s|，,。；;]{1,12}?)(?:性别|出生日期|□|$)",
        r"被告[一二三四五六七八九十]?" + sep + r"([^\s|，,。；;]{1,12}?)(?:性别|出生日期|□|第三人|$)",
    ])
    # 涉及主体（传票类文书提取被传唤人）
    fields["involved_parties"] = _first_match(text, [
        r"被传唤人" + sep + r"([^\s\n，,。；;|]{2,40})",
    ])
    # 业务类型：从文书关键词推断（尽力而为）
    fields["case_type_name"] = _first_match(text, [
        r"(劳动仲裁|民事诉讼|行政诉讼|强制执行|商事仲裁)",
    ])
    # 标准案由
    fields["standard_cause_name"] = _first_match(text, [
        r"案由" + sep + r"([^\s\n，,。；;|]{2,30})",
        r"([\u4e00-\u9fa5]{2,12}纠纷)",
    ])
    # 受理法院/仲裁委
    fields["court_name"] = _first_match(text, [
        r"([\u4e00-\u9fa5]{2,25}(?:人民法院|中级人民法院|基层人民法院|仲裁委员会))",
    ])
    # 涉案金额
    fields["claim_amount"] = _first_match(text, [
        r"标的总额" + sep + r"([\d,，]+\.?\d*)\s*元",
        r"总计" + sep + r"([\d,，]+\.?\d*)\s*元",
        r"合计" + sep + r"([\d,，]+\.?\d*)\s*元",
    ])
    # 关键日期（开庭/应到时间）
    fields["deadline_date"] = _first_match(text, [
        r"开庭时间" + sep + r"(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?\s*\d{0,2}:?\d{0,2})",
        r"应到时间" + sep + r"(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}:\d{2})",
    ])
    return fields



# ===================== 主入口 =====================


def _fail(reason: str = "") -> dict:
    """统一失败返回。"""
    return {
        "ok": False,
        "message": OCR_FAIL_MESSAGE,
        "reason": reason or "未知错误",
        "page_count": 0,
        "pages": [],
        "structured_text": "",
        "markdown": "",
        "recognized_fields": {},
    }


def recognize_document(file_path: str, mime_type: str = "") -> dict:
    """PDF / 图片 OCR 识别（含预处理），返回结构化结果（含页码标记）。

    返回值：
        ok                 识别是否成功
        message            结果说明（失败时为 "OCR识别失败，建议手动录入"）
        reason             失败原因（仅失败时有值，便于排查）
        page_count         总页数
        pages              [{page, blocks, markdown}, ...]
        structured_text    纯文本（每页带 【第 N 页】 标记）
        markdown           带页码标题的完整 Markdown
        recognized_fields  关键字段（供前端表单回填，失败时为 {}）
    """
    try:
        file_path = str(file_path)
        if not os.path.exists(file_path):
            return _fail(f"文件不存在: {file_path}")

        ext = Path(file_path).suffix.lower().lstrip(".")
        if ext not in ALLOWED_EXTS:
            return _fail(f"不支持的文件格式: .{ext}")

        logger.info("OCR 开始: %s (%s)", file_path, mime_type or "unknown")
        mode, payload = preprocess_document(file_path, mime_type)
        logger.info("预处理完成，mode=%s，共 %d 页", mode, len(payload))

        # 分批提交 MinerU，避免单次请求过大
        page_results: list = []
        for i in range(0, len(payload), OCR_BATCH_PAGES):
            batch = payload[i:i + OCR_BATCH_PAGES]
            if mode == "pdf_pages":
                page_results.extend(_call_mineru_pdf_pages(batch))
            else:
                page_results.extend(_call_mineru_images(batch))

        assembled = _assemble_pages(page_results)
        recognized = _extract_key_fields(assembled["structured_text"])
        assembled["ok"] = True
        assembled["message"] = f"识别成功，共 {assembled['page_count']} 页"
        assembled["recognized_fields"] = recognized
        assembled["reason"] = ""
        return assembled
    except Exception as exc:  # noqa: BLE001 —— 识别失败不向上抛，统一返回友好提示
        logger.exception("OCR 识别失败: %s", file_path)
        return _fail(str(exc))





