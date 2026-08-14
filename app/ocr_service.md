# OCR 提取模块说明

> 模块文件：`app/ocr_service.py`
>
> 职责：PDF / 图片的统一 OCR 识别与结构化输出，为前端表单回填提供识别数据。

## 一、功能概述

- **统一识别入口**：`recognize_document(file_path, mime_type)` 同时支持 PDF 与常见图片（jpg / jpeg / png / bmp / tiff）
- **OCR 预处理**：灰度 → 限幅 → 降噪 → 倾斜校正（纠偏）→ 对比度增强 → 二值化（可选）
- **结构化输出**：调用本地 MinerU 服务，输出带页码标记的 `pages` / `structured_text` / `markdown`
- **关键字段提取**：规划接入 AI 模型识别（当前为占位，见第四章）

## 二、快速开始

```bash
pip install requests pillow opencv-python-headless numpy pymupdf
```

```python
from app.ocr_service import recognize_document

result = recognize_document("/path/to/起诉状.pdf", "application/pdf")
# result = {
#     "ok": True,
#     "message": "识别成功，共 N 页",
#     "page_count": N,
#     "pages": [...],            # [{page, blocks, markdown}, ...]
#     "structured_text": "...",  # 带【第 N 页】标记的纯文本
#     "markdown": "...",         # 带页码标题的 Markdown
#     "recognized_fields": {...}  # 见第四章
# }
```

## 三、处理流程

1. **格式与文件校验**：扩展名白名单 + 文件存在性检查
2. **预处理**：
   - PDF：区分文字型 PDF 与扫描件，分别走「逐页拆分」或「渲染（DPI）+ 图像预处理」路线
   - 图片：直接进入图像预处理（降噪 / 纠偏 / 二值化等）
3. **MinerU 分批提交**：按 `OCR_BATCH_PAGES` 分批调用本地 MinerU 服务
4. **结果组装**：按页重组为带「【第 N 页】」标记的结构化文本与 Markdown
5. **关键字段提取**：待接入 AI 模型识别（见第四章）

## 四、关键字段提取（AI 模型识别 · 待接入）

> 当前模块仅输出 OCR 结构化文本；**关键字段（案号 / 当事人 / 案由 / 金额 / 日期等）计划通过 AI 模型识别**，正则匹配逻辑暂不纳入。

**目标输出**：与 `LawsuitCase` 表单字段对齐的字典，键名约定见 `ocr_service.FIELD_NAMES`：

```python
FIELD_NAMES = [
    "court_case_no", "document_type", "plaintiff", "defendant",
    "involved_parties", "involved_clients", "case_type_name",
    "standard_cause_name", "court_name", "current_status",
    "assigned_contact", "case_description", "claim_amount",
    "deadline_date", "remark",
]
```

**建议接入方式**：

- 输入：OCR 的 `structured_text` / `markdown`（或页面图像）
- 模型：视觉语言模型（如 LangChain + GPT-4o / Qwen-VL 等）按固定 JSON Schema 输出
- 校验：输出键与 `FIELD_NAMES` 精确对齐，缺失键补 `null`
- 失败兜底：模型不可用时返回空 `{}`，前端提示手动录入

## 五、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| ---- | ---- | ---- |
| `MINERU_API_URL` | `http://localhost:30000` | MinerU 服务地址 |
| `OCR_PARSE_TIMEOUT` | `600` | 单批解析超时（秒） |
| `OCR_BATCH_PAGES` | `10` | 每批提交页数 |
| `OCR_PREPROCESS_DPI` | `300` | PDF 渲染 DPI |
| `OCR_MAX_IMAGE_SIDE` | `3500` | 图像长边上限（px） |
| `OCR_BINARIZE` | `auto` | 二值化策略 auto / true / false |
| `OCR_TEXT_MIN_CHARS` | `30` | PDF 内嵌文本判定阈值（字符） |

## 六、失败处理

- 识别失败统一返回 `ok=False`，`message="OCR识别失败，建议手动录入"`，附 `reason` 便于排查
- 不影响上传 / 列表 / 预览等主流程

## 七、依赖

- 必需：`requests`、`pillow`
- 增强：`opencv-python-headless`、`numpy`（降噪 / 纠偏 / CLAHE / Otsu）
- PDF 渲染：`pymupdf`
