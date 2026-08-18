import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from deepagents.middleware import SkillsMiddleware
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.utils.uuid import uuid7

BASE_URL = "http://192.168.10.250:8000"
SKILLS_ROOT = Path("/Users/olof.chenx2x.net/s4/Litigation-Risk-Monitoring-System/prompts/skills")

# 文书类型关键词 -> 提取 skill 目录名（包含匹配，容忍分类输出的措辞差异）
TYPE_TO_SKILL = [
    ("应诉", "defense-notice-extract"),
    ("参加诉讼", "defense-notice-extract"),
    ("起诉状", "complaint-arbitration-extract"),
    ("仲裁申请", "complaint-arbitration-extract"),
    ("举证", "evidence-notice-extract"),
    ("传票", "summons-hearing-extract"),
    ("开庭通知", "summons-hearing-extract"),
    ("改期开庭", "summons-hearing-extract"),
    ("判决书", "judgment-ruling-mediation-extract"),
    ("裁定书", "judgment-ruling-mediation-extract"),
    ("调解书", "judgment-ruling-mediation-extract"),
]

# 每个 skill 的提取字段（不含 文书类型 和 提取说明）
SKILL_FIELDS = {
    "complaint-arbitration-extract": ["原告/申请人", "被告/被申请人", "涉及主体", "业务类型", "标准案由", "受理法院/仲裁委", "标的额", "诉讼请求金额"],
    "defense-notice-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "业务类型", "标准案由", "受理法院/仲裁委", "立案日期"],
    "evidence-notice-extract": ["案号", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "标准案由", "业务类型", "举证期限"],
    "judgment-ruling-mediation-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "业务类型", "标准案由", "标的额", "赔偿金", "诉讼请求金额", "裁判结果"],
    "summons-hearing-extract": ["案号", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "应到时间", "标准案由", "业务类型", "应到地点"],
}


def skill_for_doc_type(doc_type: str):
    for kw, skill in TYPE_TO_SKILL:
        if kw in doc_type:
            return skill
    return None


def read_skill(skill_name: str) -> str:
    skill_file = SKILLS_ROOT / skill_name / "SKILL.md"
    if not skill_file.exists():
        raise FileNotFoundError(f"skill 不存在: {skill_file}")
    return skill_file.read_text(encoding="utf-8")


def _match_skill(name: str) -> str | None:
    """模糊匹配 skill 目录名，容忍模型传入路径/前缀/别名等脏参数。"""
    name = str(name or "").strip().strip("/").strip(".")
    # 直接目录名匹配
    if (SKILLS_ROOT / name).is_dir():
        return name
    # 去除可能的前缀（如 /.summons-hearing-extract/ -> summons-hearing-extract）
    for d in SKILLS_ROOT.iterdir():
        if d.is_dir() and (d.name in name or name in d.name):
            return d.name
    # 中文别名
    alias = {"传票": "summons-hearing-extract", "开庭通知": "summons-hearing-extract",
             "起诉状": "complaint-arbitration-extract", "仲裁申请书": "complaint-arbitration-extract",
             "举证": "evidence-notice-extract", "应诉": "defense-notice-extract",
             "判决": "judgment-ruling-mediation-extract", "裁定": "judgment-ruling-mediation-extract",
             "调解": "judgment-ruling-mediation-extract", "分类": "file-type-classification"}
    for k, v in alias.items():
        if k in name:
            return v
    return None


@tool
def load_skill(skill_name: str) -> str:
    """读取指定 skill 的完整指令（SKILL.md 全文）。

    Available skills:
    - file-type-classification: 文书类型判定
    - summons-hearing-extract: 传票/开庭通知书/改期开庭通知书字段提取
    - evidence-notice-extract: 举证通知书字段提取
    - complaint-arbitration-extract: 起诉状/仲裁申请书字段提取
    - defense-notice-extract: 应诉通知书/参加诉讼通知书字段提取
    - judgment-ruling-mediation-extract: 判决书/裁定书/调解书字段提取
    """
    match = _match_skill(skill_name)
    if match is None:
        candidates = [d.name for d in SKILLS_ROOT.iterdir() if d.is_dir()]
        return f"未找到 skill '{skill_name}'，可用: {candidates}"
    return read_skill(match)


def build_model():
    return ChatOpenAI(
        base_url="http://192.168.10.250:8006",
        api_key="None",
        model="Qwen3.6-27B",
        max_tokens=4096,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


def make_agent(system_prompt: str | None = None):
    """构建带默认 SkillsMiddleware + load_skill 工具的 agent（渐进披露，纯文本 JSON 输出）。
    通过 system_prompt 传指令，避免额外 system 消息导致服务端模板报错。"""
    model = build_model()
    backend = FilesystemBackend(root_dir=str(SKILLS_ROOT))
    middleware = SkillsMiddleware(backend=backend, sources=["."])
    return create_agent(
        model,
        middleware=[middleware],
        tools=[load_skill],
        system_prompt=system_prompt,
    )


CLASSIFY_PROMPT = """你是一名严谨的法律文书解析专家。
用户输入一份法律文书。请判断其文书类型。

执行流程（必须按顺序）：
1. 首先调用 load_skill 工具，传入 skill 名称 "file-type-classification"，读取完整的类型判定规则。
2. 严格按读取到的 SKILL 判定规则判断文书类型。

判定规则要点：
1. 扫描全文，对 SKILL 中"需完整解析的 9 类文书"逐类型做关键词子串匹配。
2. 只要命中任意 9 类文书的关键词（如"传票"、"开庭通知"、"改期开庭"、"起诉状"、"判决书"、"举证通知"等），"是否需解析"就必须为"是"。
3. "改期开庭通知书"属于类型"开庭传票/通知书"（命中关键词"改期开庭"/"开庭通知"），必须判定"是否需解析"为"是"。
4. 仅当 9 类均未命中、且属于证据材料或程序性附件时，"是否需解析"才为"否"。
5. "判定依据"记录命中的关键词列表。

最终输出为单一 JSON 对象，包含：
   - 文书类型
   - 是否需解析（是/否）
   - 判定依据
只输出 JSON，不要输出其他文字或代码块标记。
"""


def extract_prompt(fields: list[str]) -> str:
    lines = "\n".join(f"   - {f}" for f in fields)
    return f"""你是一名严谨的法律文书解析专家。
用户输入一份法律文书。请严格按照字段提取规则提取。

执行流程（必须按顺序）：
1. 首先调用 load_skill 工具，传入对应文书类型的 skill 名称，读取该 skill 的完整提取规则。
2. 严格按读取到的 SKILL 规则逐字段提取，不得遗漏。

关键要求：
1. 每个字段都必须严格按 SKILL 定义的规则识别，不得遗漏。
2. 无法提取的字段填 null。
3. 提取说明为 JSON 对象，逐字段记录提取依据或无法提取的原因，必须填写。字段名必须是"提取说明"。
4. 字段名必须与下方"字段清单"完全一致（中文），不得翻译或改写。
5. 只输出 JSON，不要输出其他任何文字、解释或代码块标记。

字段清单：
{lines}
"""


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象（可容忍前后夹杂的分析文本）。"""
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


def _ref_fields(skill_name: str) -> list[str]:
    """从 ref.json 读取该 skill 的字段清单（正常分支，去掉提取说明）。"""
    rf = SKILLS_ROOT / skill_name / "ref.json"
    sch = json.loads(rf.read_text(encoding="utf-8"))
    for br in sch.get("oneOf", []):
        props = br.get("properties", {})
        if "错误" in props:
            continue
        return [k for k in props if k != "提取说明"]
    return []


def align_to_ref(obj: dict, skill_name: str) -> dict:
    """把模型输出按 ref.json 规整：只保留清单字段，缺失补 null，返回 {字段: 值}。
    模型输出若已包成 {value: ...} 则取其 value。"""
    fields = _ref_fields(skill_name)
    out = {}
    for f in fields:
        raw = obj.get(f)
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]
        out[f] = raw
    notes = obj.get("提取说明")
    if isinstance(notes, dict):
        out["提取说明"] = notes
    return out


def invoke_classify(classify_agent_, md_content: str) -> dict:
    response = classify_agent_.invoke(
        {"messages": [("user", md_content)]},
        config={"configurable": {"thread_id": str(uuid7())}},
    )
    return extract_json(response.get("messages", [])[-1].content)


def invoke_extract(extract_agent_, md_content: str) -> dict:
    response = extract_agent_.invoke(
        {"messages": [("user", md_content)]},
        config={"configurable": {"thread_id": str(uuid7())}},
    )
    return extract_json(response.get("messages", [])[-1].content)


def _vote_value(candidates: list) -> tuple:
    """对单个字段的多次生成值取众数（支持 list/dict 值）。"""
    if not candidates:
        return None
    counts: dict[str, int] = {}
    first: dict[str, object] = {}
    for c in candidates:
        key = json.dumps(c, ensure_ascii=False, sort_keys=True, default=str)
        if key not in first:
            first[key] = c
        counts[key] = counts.get(key, 0) + 1
    best_key = max(counts, key=counts.get)
    return first[best_key]


def majority_vote(results: list[dict]) -> dict:
    """对多次生成结果逐字段取众数。

    - 字段名取所有结果键的并集
    - 每个字段在多次结果中出现次数最多的值胜出（按 json 序列化比较，支持 list/dict）
    - 平局时保留最先出现的值
    """
    if not results:
        return {}
    keys: list[str] = []
    for r in results:
        for k in r:
            if k not in keys:
                keys.append(k)
    merged = {}
    for k in keys:
        candidates = [r.get(k) for r in results if k in r]
        merged[k] = _vote_value(candidates)
    return merged


def process_document(classify_agent_, md_content: str, num_votes: int = 5) -> dict:
    """先分类，再按类型加载对应 skill 提取（每字段生成 num_votes 次取众数，结果与 ref.json 对齐）。"""
    cls = invoke_classify(classify_agent_, md_content)
    doc_type = cls.get("文书类型") or ""
    need_parse = str(cls.get("是否需解析") or "").strip()
    # 分类模型可能输出 "是, 判定依据：..." 等带附加文字的格式，宽松匹配
    if not doc_type or not need_parse.lower().startswith(("是", "true", "1")):
        return cls
    skill_name = skill_for_doc_type(doc_type)
    if skill_name is None:
        return {"文书类型": doc_type, "提取说明": {"错误": f"无对应提取 skill: {doc_type}"}}
    fields = SKILL_FIELDS[skill_name]
    extract_agent = make_agent(extract_prompt(fields))
    results = [align_to_ref(invoke_extract(extract_agent, md_content), skill_name) for _ in range(num_votes)]
    extracted = majority_vote(results)
    extracted.setdefault("文书类型", doc_type)
    return extracted


def submit_parse_task(folder: Path) -> str:
    pdfs = sorted(folder.glob("*.pdf"))
    assert pdfs, f"文件夹中未找到 PDF 文件: {folder}"

    print(f"提交 {len(pdfs)} 个 PDF: {[p.name for p in pdfs]}")
    files = [("files", (p.name, p.open("rb"), "application/pdf")) for p in pdfs]
    try:
        resp = requests.post(
            f"{BASE_URL}/tasks",
            files=files,
            data={
                "lang_list": ["ch"],
                "parse_method": "auto",
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
    finally:
        for f in files:
            f[1][1].close()

    print(f"HTTP {resp.status_code}")
    task = resp.json()
    task_id = task.get("task_id")
    assert task_id, f"未获取到 task_id: {task}"
    return task_id


def poll_task(task_id: str) -> dict:
    while True:
        r = requests.get(f"{BASE_URL}/tasks/{task_id}", timeout=30)
        st = r.json()
        status = st.get("status")
        if status in ("completed", "failed"):
            break
        time.sleep(3)

    assert status == "completed", f"解析任务失败: {st.get('error')}"
    r = requests.get(f"{BASE_URL}/tasks/{task_id}/result", timeout=60)
    return r.json()


def ocr_parse_folder(folder: Path) -> dict:
    """提交并等待一个文件夹的 OCR 解析，返回解析结果（可并行调用）。"""
    print(f"[OCR] 提交文件夹: {folder.name}", flush=True)
    task_id = submit_parse_task(folder)
    print(f"[OCR] {folder.name} task_id={task_id}，等待解析...", flush=True)
    result = poll_task(task_id)
    print(f"[OCR] {folder.name} 解析完成", flush=True)
    return result


def extract_folder(folder: Path, ocr_result: dict, classify_agent_, llm_pbar: tqdm) -> None:
    """对单个文件夹的 OCR 结果做 LLM 字段提取并保存（串行调用）。"""
    results = ocr_result.get("results", ocr_result)
    assert isinstance(results, dict) and results, f"未解析到任何文件内容: {folder}"

    outputs = {}
    for name, info in results.items():
        md_content = info.get("md_content", "") if isinstance(info, dict) else str(info)
        llm_pbar.set_postfix_str(f"{folder.name}/{name}")
        try:
            extracted = process_document(classify_agent_, md_content)
            outputs[name] = extracted
        except Exception as e:
            print(f"\n[{name}] 提取失败: {e}", flush=True)
            outputs[name] = {"错误": str(e)}
        llm_pbar.update(1)

    out_path = folder / f"{folder.name}_results.json"
    out_path.write_text(json.dumps(outputs, ensure_ascii=False, indent=4), encoding="utf-8")


def main(root_str: str):
    root = Path(root_str).resolve()
    assert root.is_dir(), f"不是有效文件夹: {root}"

    folders = sorted(p for p in root.iterdir() if p.is_dir())
    assert folders, f"根目录下未找到子文件夹: {root}"
    print(f"共发现 {len(folders)} 个子文件夹: {[f.name for f in folders]}")

    # 阶段一：OCR 解析并行提交、并行轮询
    ocr_results = {}
    with tqdm(total=len(folders), desc="OCR 解析", unit="文件夹") as ocr_pbar:
        with ThreadPoolExecutor(max_workers=len(folders)) as executor:
            future_to_folder = {executor.submit(ocr_parse_folder, f): f for f in folders}
            for future in as_completed(future_to_folder):
                folder = future_to_folder[future]
                try:
                    ocr_results[folder.name] = future.result()
                except Exception as e:
                    print(f"\n[OCR] {folder.name} 解析失败: {e}", flush=True)
                    ocr_results[folder.name] = {"错误": str(e)}
                ocr_pbar.set_postfix_str(f"完成 {folder.name}")
                ocr_pbar.update(1)

    # 阶段二：LLM 提取串行（先分类，再按类型提取）
    total_docs = sum(
        len(ocr_results[f.name].get("results", ocr_results[f.name]))
        for f in folders
        if isinstance(ocr_results[f.name], dict)
    )
    classify_agent_ = make_agent(CLASSIFY_PROMPT)
    with tqdm(total=total_docs, desc="LLM 提取", unit="文书") as llm_pbar:
        for folder in folders:
            try:
                extract_folder(folder, ocr_results[folder.name], classify_agent_, llm_pbar)
            except Exception as e:
                print(f"\n[LLM] {folder.name} 提取失败: {e}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法: python {sys.argv[0]} <testset根目录>")
        sys.exit(1)
    main(sys.argv[1])
