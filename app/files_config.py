"""文件类型配置与上传校验规则。"""

import os

# 文件类型映射：类型标识 → 中文名 / case_file.file_category / 是否识别
# 兼容新旧命名：core_document↔core_legal，procedural_notice↔procedural
DOC_TYPE_MAP = {
    "core_document": {"label": "核心法定文书", "category": "core_legal", "recognize": True},
    "evidence": {"label": "证据材料", "category": "evidence", "recognize": False},
    "procedural_notice": {"label": "程序性告知附件", "category": "procedural", "recognize": False},
    "core_legal": {"label": "核心法定文书", "category": "core_legal", "recognize": True},
    "procedural": {"label": "程序性告知附件", "category": "procedural", "recognize": False},
}

# case_file.file_category -> 响应分组用的旧 doc_type 键（前端兼容）
CATEGORY_TO_DOCTYPE = {"core_legal": "core_document", "evidence": "evidence", "procedural": "procedural_notice"}

# 上传校验规则
ALLOWED_EXTS = {"pdf", "jpg", "jpeg", "png", "bmp", "tiff"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# 上传目录：项目根目录 static/uploads
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(_PROJECT_ROOT, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
