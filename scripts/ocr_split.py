"""OCR 解析拼接文书并按文书切分（方案A：逐页 OCR + 页码映射）。

用法:
    python scripts/ocr_split.py <input_path> [--out <输出根目录>]

- input_path: 文件夹（递归收集其下所有 *.pdf）或单个 PDF 路径
- 输出: <out>/<案件名>/<NN>_<文书类型>.md|.pdf
     另附 manifest.json / summary.json
- 需要 LLM 解析的 9 类文书 -> 保存 .md（逐页 OCR 拼接的全文文本）
- 不需要 LLM 解析的类型（证据材料、程序性附件、未知类型）-> 保存 .pdf
  即从原 PDF 按页码范围直接切出的原件（不动 OCR 文本，保留原始版面）

切分方式:
    输入 PDF 可能由多份文书拼接而成。为同时得到"全文文本"和"页码范围"，
    将 PDF 按页拆成单页文件逐页 OCR（每页一次任务），按页序拼接并在页间
    插入哨兵标记 <!--MINERU_PAGE k-->；再调用现有 file-type-classification
    skill（通过 agent 的 load_skill 工具加载），让模型逐处定位每份文书的
    "起始标记"（原文连续片段）并判定"是否需解析"。脚本按标记在拼接文本中
    精确切分：需解析 -> 保存 md 文本；不需解析 -> 由哨兵换算页码范围，
    用 PyMuPDF 从原 PDF 抽出对应页保存为 .pdf。
文本过长时自动分块识别标记，最后统一按原始全文切分，保证切出的内容与原文一致。

断点重试:
    输出目录中已存在该 PDF 的 manifest 且对应输出文件齐全时，直接跳过该 PDF 的 OCR，
    只处理尚未完成/失败的 PDF，便于中断后续跑；旧版（无 source_pdf）多 PDF 案件
    按输出文件名前缀兜底归属，避免已解析的 PDF 重复 OCR。
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pymupdf
import requests
from tqdm import tqdm
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from deepagents.middleware import SkillsMiddleware
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.utils.uuid import uuid7

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loader

# 内网/本机调用不走代理，避免 VPN 影响
os.environ.setdefault("NO_PROXY", "192.168.10.250,127.0.0.1,localhost")
os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])

BASE_URL = "http://192.168.10.250:8000"
LLM_BASE = "http://192.168.10.250:8006"
DEFAULT_OUT_ROOT = Path("/Users/olof.chenx2x.net/s2/split")

SKILLS_ROOT = loader.SKILLS_ROOT

# 单次交给模型识别标记的最大文本长度；超长时按此分块（带重叠）
CHUNK_SIZE = 60000
CHUNK_OVERLAP = 2000

# 逐页 OCR 拼接文本中用于标记页码边界的哨兵（保证不会被 OCR 内容命中）
PAGE_SENTINEL_RE = re.compile(r"<!--MINERU_PAGE (\d+)-->")


# ============================================================
# MinerU OCR（异步任务，方案A：逐页）
# ============================================================
def submit_parse_task(pdf_path: Path) -> str:
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"{BASE_URL}/tasks",
            files={"files": (pdf_path.name, f, "application/pdf")},
            data={
                "lang_list": ["ch"],
                "parse_method": "ocr",
                "backend": "hybrid-engine",
                "effort": "high",
                "formula_enable": "true",
                "table_enable": "true",
                "image_analysis": "true",
                "return_md": "true",
                "response_format_zip": "false",
            },
            timeout=60,
        )
    task = resp.json()
    task_id = task.get("task_id")
    assert task_id, f"未获取到 task_id: {task}"
    return task_id


def poll_tasks(task_ids: list[str]) -> None:
    """轮询一批任务直到全部完成；服务端并发处理，轮询可并行等待。"""
    pending = set(task_ids)
    while pending:
        for tid in list(pending):
            st = requests.get(f"{BASE_URL}/tasks/{tid}", timeout=30).json()
            status = st.get("status")
            if status in ("completed", "failed"):
                assert status == "completed", f"解析任务失败: {st.get('error')}"
                pending.discard(tid)
        if pending:
            tqdm.write(f"  [OCR] 等待剩余 {len(pending)}/{len(task_ids)} 页 ...")
            time.sleep(3)


def get_parse_result(task_id: str) -> dict:
    return requests.get(f"{BASE_URL}/tasks/{task_id}/result", timeout=60).json()


def extract_md(result: dict) -> str:
    results = result.get("results", result)
    if not isinstance(results, dict) or not results:
        return ""
    infos = list(results.values())
    info = infos[0] if isinstance(infos[0], dict) else {"md_content": str(infos[0])}
    return info.get("md_content", "") if isinstance(info, dict) else str(info)


def ocr_pages(pdf_path: Path) -> list[dict]:
    """按页 OCR：拆成单页 PDF 逐页提交，返回 [{"page":1,"md":...}, ...]（page 从 1 起）。"""
    doc = pymupdf.open(pdf_path)
    n = doc.page_count
    tqdm.write(f"  [OCR] {pdf_path.name} 共 {n} 页，逐页解析 ...")
    tmpdir = Path(tempfile.mkdtemp(prefix="ocrpages_"))
    try:
        page_files: list[Path] = []
        for i in range(n):
            single = pymupdf.open()
            single.insert_pdf(doc, from_page=i, to_page=i)
            f = tmpdir / f"page_{i + 1:04d}.pdf"
            single.save(str(f))
            single.close()
            page_files.append(f)

        task_ids = [submit_parse_task(f) for f in page_files]
        poll_tasks(task_ids)

        out: list[dict] = []
        for k, tid in enumerate(task_ids, 1):
            out.append({"page": k, "md": extract_md(get_parse_result(tid))})
        total = sum(len(p["md"]) for p in out)
        tqdm.write(f"  [OCR] 完成，{n} 页，md 共 {total} 字符")
        return out
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 拼接 / 页码映射 / 按页切 PDF
# ============================================================
def build_full_text(pages: list[dict]) -> str:
    """按页序拼接 OCR 文本，页间插入哨兵，得到可反查页码的全文。"""
    parts = [f"<!--MINERU_PAGE {p['page']}-->\n{p['md']}" for p in pages]
    return "\n\n".join(parts)


def page_at(text: str, pos: int) -> int:
    """返回 text 中 pos 位置之前最近哨兵所标注的页码（默认第 1 页）。"""
    if pos < 0:
        pos = 0
    page = 1
    for m in PAGE_SENTINEL_RE.finditer(text, 0, pos + 1):
        page = int(m.group(1))
    return page


def slice_pdf(src: Path, start_page: int, end_page: int, dst: Path) -> None:
    """从原 PDF 抽出 [start_page, end_page]（1-based，闭区间）保存为 dst。"""
    doc = pymupdf.open(src)
    out = pymupdf.open()
    out.insert_pdf(doc, from_page=start_page - 1, to_page=end_page - 1)
    out.save(str(dst))
    doc.close()
    out.close()


# ============================================================
# Agent（复用现有 skill，通过 load_skill 暴露）
# ============================================================
def load_skill(skill_name: str) -> str:
    return loader.load_skill(skill_name)


load_skill.__doc__ = loader.available_skills_docstring()
load_skill = tool(load_skill)


def build_model():
    return ChatOpenAI(
        base_url=LLM_BASE,
        api_key="None",
        model="Qwen3.6-27B",
        max_tokens=None,
        timeout=180,
        temperature=0.1,
        top_p=0.9,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


_AGENT_CACHE: dict[str, object] = {}


def get_agent(system_prompt: str | None = None):
    key = system_prompt or ""
    agent = _AGENT_CACHE.get(key)
    if agent is None:
        backend = FilesystemBackend(root_dir=str(SKILLS_ROOT))
        middleware = SkillsMiddleware(backend=backend, sources=["."])
        agent = create_agent(
            build_model(),
            middleware=[middleware],
            tools=[load_skill],
            system_prompt=system_prompt,
        )
        _AGENT_CACHE[key] = agent
    return agent


SPLIT_PROMPT = """你是一名严谨的法律文书解析专家。
用户输入一份可能由多份文书拼接而成的法律文书全文（逐页 OCR 结果拼接，页间含 <!--MINERU_PAGE k--> 页码标记）。请识别其中的每一份独立文书，并给出每份文书的起始标记与"是否需解析"。

执行流程（必须按顺序）：
1. 首先调用 load_skill 工具，传入 skill 名称 "file-type-classification"，读取完整的类型判定规则。
2. 依据该 SKILL 的完整规则（包括"需完整解析的 9 类文书"、"仅归档存储的材料（证据材料 / 程序性附件）"），在全文逐处定位**每一份**独立文书的起始位置（例如 "传票"、"应诉通知书"、"民事起诉状"、"举证通知书"、"判决书"、"授权委托书"、"送达地址确认书" 等标题或首行）。
3. 每份文书输出：文书类型 + 起始标记 + 是否需解析（依据 file-type-classification 的判定："9 类文书"=是；证据材料 / 程序性附件 / 未知类型=否）。

规则：
- 起始标记必须是原文中连续 10~30 个字符、逐字复制的内容，禁止改写、补全或猜测。
- 标记应尽量取到文书标题/首行等能唯一锚定位置的内容。
- **所有文书都必须识别并输出**：即使某份文书不需要 LLM 解析（如证据材料、程序性附件，file-type-classification 判定"是否需解析=否"），也必须是独立的文档对象，不能并入相邻文书，也不能跳过。
- 若全文只含一份文书，只输出一个文档对象，起始标记取全文开头。
- 文书类型使用 file-type-classification 中的类型名称（9 类名称，或如 "证据材料-合同类"、"程序性附件-授权委托书"）；无法判定类型时用"未分类"。
- 是否需解析只允许取值 "是" 或 "否"。

最终输出为单一 JSON 对象：
{"documents": [{"文书类型": "...", "起始标记": "...", "是否需解析": "是|否"}]}
只输出 JSON，不要输出其他文字或代码块标记。
"""


def extract_json(text: str) -> dict:
    """从模型输出中提取首个合法 JSON 对象（容忍前后夹杂的分析文本）。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip(), flags=re.S)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError(f"响应中未找到 JSON: {text[:200]}")


def split_documents(text: str, max_retry: int = 1) -> list[dict]:
    """识别一段文本中文书的起始标记；非 JSON 时重试一次。"""
    for attempt in range(max_retry + 1):
        try:
            resp = get_agent(SPLIT_PROMPT).invoke(
                {"messages": [("user", text)]},
                config={"configurable": {"thread_id": str(uuid7())}},
            )
            obj = extract_json(resp.get("messages", [])[-1].content)
            docs = obj.get("documents")
            if isinstance(docs, list) and docs:
                return docs
        except Exception as e:
            tqdm.write(f"    [警告] 第 {attempt + 1} 次识别失败: {e}")
    return []


def _norm_marker(doc: dict) -> dict:
    return {
        "起始标记": str(doc.get("起始标记") or "").strip(),
        "文书类型": str(doc.get("文书类型") or "未分类").strip(),
        "是否需解析": str(doc.get("是否需解析") or "").strip(),
    }


def collect_start_markers(text: str) -> list[dict]:
    """返回有序的起始标记列表；文本超长时分块识别。"""
    def _run(chunk: str) -> list[dict]:
        out = []
        for d in split_documents(chunk):
            m = _norm_marker(d)
            if m["起始标记"]:
                out.append(m)
        return out

    if len(text) <= CHUNK_SIZE:
        return _run(text)

    markers: list[dict] = []
    start = 0
    while start < len(text):
        chunk = text[start:start + CHUNK_SIZE]
        markers.extend(_run(chunk))
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return markers


# 仅本地兜底用的判定：agent 未给出"是否需解析"时，按文书类型关键词推断。
_NON_PARSE_MARKERS = ("证据材料", "程序性附件", "证据清单", "未分类", "未知类型")
_PARSE_KEYWORDS = (
    "应诉", "参加诉讼", "起诉状", "仲裁申请", "举证通知",
    "传票", "开庭通知", "改期开庭", "判决书", "裁定书", "调解书",
    "上诉状", "上诉于", "执行通知", "执行结案", "生效证明",
)


def needs_parse(dtype: str) -> bool:
    """按文书类型关键词判断是否需要 LLM 解析（与 file-type-classification 规则一致）。"""
    if any(k in dtype for k in _NON_PARSE_MARKERS):
        return False
    return any(k in dtype for k in _PARSE_KEYWORDS)


def _resolve_needs(needs: str, dtype: str) -> bool:
    if needs in ("是", "Yes", "YES", "yes", "true", "True", "1"):
        return True
    if needs in ("否", "No", "NO", "no", "false", "False", "0"):
        return False
    return needs_parse(dtype)


def _trailing_junk_len(raw: str) -> int:
    """返回 raw 尾部需要剔除的长度：尾部空白 + 完整的页码哨兵标签。"""
    stripped = 0
    while raw:
        if raw[-1].isspace():
            stripped += 1
            raw = raw[:-1]
            continue
        ms = list(PAGE_SENTINEL_RE.finditer(raw))
        if ms and ms[-1].end() == len(raw):
            m = ms[-1]
            stripped += len(m.group(0))
            raw = raw[:m.start()]
            continue
        break
    return stripped


def _slice_segment(text: str, start: int, end: int) -> tuple[str, int, int] | None:
    """按 [start, end) 切段：去掉哨兵与首尾空白得到内容，同时换算页码范围。"""
    raw = text[start:end]
    trail = _trailing_junk_len(raw)
    real_end = end - trail
    if real_end <= start:
        return None
    content = PAGE_SENTINEL_RE.sub("", text[start:real_end]).strip()
    if not content:
        return None
    start_page = page_at(text, start)
    end_page = page_at(text, max(start, real_end - 1))
    return content, start_page, end_page


def cut_by_markers(text: str, markers: list[dict]) -> list[dict]:
    """按起始标记在拼接文本中精确切分，返回 [{文书类型, 内容, 需要解析, 页码范围}]。"""
    if not markers:
        sliced = _slice_segment(text, 0, len(text))
        if sliced is None:
            return []
        content, start_page, end_page = sliced
        return [{
            "文书类型": "未分类",
            "内容": content,
            "需要解析": False,
            "start_page": start_page,
            "end_page": end_page,
        }]

    boundaries: list[tuple[int, str, str]] = []
    pos = 0
    for m in markers:
        marker, dtype, needs = m["起始标记"], m["文书类型"], m["是否需解析"]
        idx = text.find(marker, pos)
        if idx < 0:
            idx = text.find(marker)
        if idx < 0:
            tqdm.write(f"    [警告] 原文中未找到起始标记: {marker!r}")
            continue
        boundaries.append((idx, dtype, needs))
        pos = idx + len(marker)

    if not boundaries:
        sliced = _slice_segment(text, 0, len(text))
        if sliced is None:
            return []
        content, start_page, end_page = sliced
        return [{
            "文书类型": "未分类",
            "内容": content,
            "需要解析": False,
            "start_page": start_page,
            "end_page": end_page,
        }]

    boundaries.sort(key=lambda x: x[0])
    out = []
    for i, (start, dtype, needs) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        sliced = _slice_segment(text, start, end)
        if sliced is None:
            continue
        content, start_page, end_page = sliced
        out.append({
            "文书类型": dtype,
            "内容": content,
            "需要解析": _resolve_needs(needs, dtype),
            "start_page": start_page,
            "end_page": end_page,
        })
    return out


# ============================================================
# 主流程
# ============================================================
def collect_pdfs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() == ".pdf" else []
    return sorted(input_path.rglob("*.pdf"))


def process_pdf(pdf: Path, out_root: Path) -> dict:
    case = pdf.parent.name  # 按 PDF 所在案件文件夹归档：s2/sample/<案件>/x.pdf → s2/split/<案件>/x/…
    tqdm.write(f"\n=== {pdf.relative_to(pdf.parents[1]) if len(pdf.parents) > 1 else pdf.name} ===")
    pages = ocr_pages(pdf)
    if not pages or not any(p["md"] for p in pages):
        tqdm.write("  [跳过] 未解析到内容")
        return {"pdf": str(pdf), "error": "未解析到内容"}

    full_text = build_full_text(pages)
    markers = collect_start_markers(full_text)
    tqdm.write(f"  识别到 {len(markers)} 个起始标记")
    segments = cut_by_markers(full_text, markers)
    tqdm.write(f"  切分为 {len(segments)} 份文书")

    # 全部直接输出到 <案件名> 目录，不新建 <pdf文件名> 子目录；
    # 案件内多个 PDF 时用文件名前缀区分，避免同名冲突。
    pdf_dir = out_root / case
    pdf_dir.mkdir(parents=True, exist_ok=True)
    new_entries = []
    multi = len(list(pdf.parent.glob("*.pdf"))) > 1
    for i, seg in enumerate(segments, 1):
        safe_type = re.sub(r'[\\/:*?"<>|]', "_", seg["文书类型"]) or "未分类"
        base = f"{pdf.stem}_{i:02d}_{safe_type}" if multi else f"{i:02d}_{safe_type}"
        if seg["需要解析"]:
            # 需要 LLM 解析 -> 保存 md 文本（内容已去掉页码哨兵）
            md = re.sub(r"\n{3,}", "\n\n", seg["内容"]).strip()
            fname = base + ".md"
            (pdf_dir / fname).write_text(md, encoding="utf-8")
            meta = {
                "index": i,
                "source_pdf": str(pdf),
                "文书类型": seg["文书类型"],
                "是否需解析": "是",
                "file": fname,
                "format": "md",
                "chars": len(md),
            }
            detail = f"{meta['chars']} 字符"
        else:
            # 不需要 LLM 解析 -> 保存原 PDF 对应页（保留原件版面）
            fname = base + ".pdf"
            start_page, end_page = seg["start_page"], seg["end_page"]
            slice_pdf(pdf, start_page, end_page, pdf_dir / fname)
            meta = {
                "index": i,
                "source_pdf": str(pdf),
                "文书类型": seg["文书类型"],
                "是否需解析": "否",
                "file": fname,
                "format": "pdf",
                "pages": [start_page, end_page],
            }
            detail = f"第 {start_page}-{end_page} 页"
        new_entries.append(meta)
        tqdm.write(f"    -> {fname}（{seg['文书类型']}，{detail}）")

    # 合并而非覆盖：多 PDF 案件里各 PDF 的 manifest 条目都要保留，
    # 断点重试时按 source_pdf（或旧版文件名前缀）归属到具体 PDF。
    manifest = _load_manifest(pdf_dir)
    prefix = f"{pdf.stem}_" if multi else ""
    manifest = [
        e for e in manifest
        if not _belongs_to(e, pdf, multi, prefix)
    ]
    manifest.extend(new_entries)
    manifest.sort(key=lambda e: (str(e.get("source_pdf", "")), e.get("index", 0)))
    _save_manifest(pdf_dir, manifest)
    return {"pdf": str(pdf), "documents": len(segments), "out_dir": str(pdf_dir)}


def _load_manifest(pdf_dir: Path) -> list:
    p = pdf_dir / "manifest.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_manifest(pdf_dir: Path, manifest: list) -> None:
    (pdf_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _belongs_to(entry: object, pdf: Path, multi: bool, prefix: str) -> bool:
    """判断 manifest 条目是否属于当前 PDF（兼容旧版无 source_pdf 的条目）。"""
    if not isinstance(entry, dict):
        return False
    if entry.get("source_pdf") == str(pdf):
        return True
    if entry.get("source_pdf"):
        return False
    # 旧版条目：多 PDF 案件按文件名前缀归属，单 PDF 案件整份 manifest 都属于当前 PDF
    if multi:
        return str(entry.get("file", "")).startswith(prefix)
    return True


def has_parsed_output(pdf: Path, out_root: Path) -> bool:
    """断点重试：案件目录已有该 PDF 的 manifest 且输出文件齐全时返回 True。"""
    pdf_dir = out_root / pdf.parent.name
    manifest = _load_manifest(pdf_dir)
    if not manifest:
        return False
    multi = len(list(pdf.parent.glob("*.pdf"))) > 1
    prefix = f"{pdf.stem}_" if multi else ""
    entries = [
        e for e in manifest if _belongs_to(e, pdf, multi, prefix)
    ]
    if entries:
        return all(
            (pdf_dir / e["file"]).is_file()
            for e in entries if isinstance(e, dict) and e.get("file")
        )
    # 纯旧版 manifest（所有条目都没有 source_pdf）且多 PDF 案件：
    # 旧版每次处理都会覆盖 manifest，只保留最后一份 PDF 的条目，
    # 其他 PDF 的输出文件仍在，按文件名前缀兜底认定已解析。
    if any(isinstance(e, dict) and e.get("source_pdf") for e in manifest):
        return False
    if not multi:
        return True
    try:
        names = os.listdir(pdf_dir)
    except OSError:
        return False
    return any(n.startswith(prefix) for n in names)


def main():
    parser = argparse.ArgumentParser(description="OCR 解析拼接 PDF 并按文书切分（方案A：逐页 OCR + 页码映射）")
    parser.add_argument("input_path", help="文件夹（递归收集 *.pdf）或单个 PDF 路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT), help="输出根目录，默认 s2/split")
    args = parser.parse_args()

    root = Path(args.input_path).resolve()
    assert root.exists(), f"路径不存在: {root}"
    out_root = Path(args.out).resolve()

    pdfs = collect_pdfs(root)
    assert pdfs, f"未找到 PDF: {root}"
    print(f"共 {len(pdfs)} 个 PDF，输出到 {out_root}（按 PDF 所在案件文件夹归档）")

    summary = []
    fails: list[str] = []
    with tqdm(total=len(pdfs), desc="OCR+切分", unit="pdf") as pbar:
        for pdf in pdfs:
            pbar.set_postfix_str(pdf.name)
            if has_parsed_output(pdf, out_root):
                pdf_dir = out_root / pdf.parent.name
                tqdm.write(f"  [跳过] {pdf.name}：已有解析结果，复用 manifest")
                summary.append({"pdf": str(pdf), "skipped": True, "out_dir": str(pdf_dir)})
                pbar.update(1)
                continue
            try:
                result = process_pdf(pdf, out_root)
                summary.append(result)
                if result.get("error"):
                    fails.append(str(pdf))
            except Exception as e:
                tqdm.write(f"  [失败] {pdf.name}: {e}")
                summary.append({"pdf": str(pdf), "error": str(e)})
                fails.append(str(pdf))
            pbar.update(1)
    summary.append({"fails": fails})
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n完成，结果见 {out_root}")


if __name__ == "__main__":
    main()
