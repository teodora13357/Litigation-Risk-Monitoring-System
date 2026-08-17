import json
import re
import time
from pathlib import Path
import requests

from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain_core.utils.uuid import uuid7

# ============================================================
# 1. 配置
# ============================================================
BASE_URL = "http://192.168.10.250:8000"
PDF_PATH = Path("/Users/olof.chenx2x.net/s2/案件材料扫描件/（2026）粤 0981 民初 4148 号.pdf").resolve()

assert PDF_PATH.exists(), f"文件不存在: {PDF_PATH}"
print(f"待上传文件: {PDF_PATH} ({PDF_PATH.stat().st_size} bytes)")


# ============================================================
# 2. 提交 PDF 解析异步任务
# ============================================================
def submit_parse_task(pdf_path: Path) -> str:
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"{BASE_URL}/tasks",
            files={"files": (pdf_path.name, f, "application/pdf")},
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
                # "start_page_id": 0,
                # "end_page_id": 1,
            },
            timeout=60,
        )

    print(f"HTTP {resp.status_code}")
    task = resp.json()
    print(json.dumps(task, ensure_ascii=False, indent=2))
    task_id = task.get("task_id")
    assert task_id, "未获取到 task_id"
    return task_id


def poll_task(task_id: str, interval: int = 3, timeout: int = 60) -> dict:
    while True:
        r = requests.get(f"{BASE_URL}/tasks/{task_id}", timeout=30)
        st = r.json()
        status = st.get("status")
        print(f"status={status}, error={st.get('error', '无')}", flush=True)
        if status in ("completed", "failed"):
            break
        time.sleep(interval)

    assert status == "completed", f"任务失败: {st.get('error')}"
    print("解析完成")
    return st


def get_parse_result(task_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/tasks/{task_id}/result", timeout=60)
    print(f"HTTP {r.status_code}")
    return r.json()


# ============================================================
# 3. 模型
# ============================================================
model = ChatOpenAI(
    base_url="http://192.168.10.250:8006",
    api_key="None",
    model="Qwen3.6-27B",
    max_tokens=None,
    temperature=0,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)


# ============================================================
# 4. Skill 工具与 Agent
# ============================================================
from deepagents.middleware import SkillsMiddleware
from deepagents.backends.filesystem import FilesystemBackend

SKILLS_ROOT = Path("/Users/olof.chenx2x.net/s4/skills")

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
    return (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")


def _match_skill(name: str):
    name = str(name or "").strip().strip("/").strip(".")
    if (SKILLS_ROOT / name).is_dir():
        return name
    for d in SKILLS_ROOT.iterdir():
        if d.is_dir() and (d.name in name or name in d.name):
            return d.name
    alias = {"传票": "summons-hearing-extract", "开庭通知": "summons-hearing-extract",
             "起诉状": "complaint-arbitration-extract", "仲裁申请书": "complaint-arbitration-extract",
             "举证": "evidence-notice-extract", "应诉": "defense-notice-extract",
             "判决": "judgment-ruling-mediation-extract", "裁定": "judgment-ruling-mediation-extract",
             "调解": "judgment-ruling-mediation-extract", "分类": "document-type-classification"}
    for k, v in alias.items():
        if k in name:
            return v
    return None


@tool
def load_skill(skill_name: str) -> str:
    """读取指定 skill 的完整指令（SKILL.md 全文）。

    Available skills:
    - document-type-classification: 文书类型判定
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


def make_agent():
    """构建带默认 SkillsMiddleware + load_skill 工具的 agent（渐进披露，纯文本 JSON 输出）。"""
    backend = FilesystemBackend(root_dir=str(SKILLS_ROOT))
    middleware = SkillsMiddleware(backend=backend, sources=["."])
    return create_agent(
        model,
        middleware=[middleware],
        tools=[load_skill],
    )


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


def _vote_value(candidates):
    if not candidates:
        return None
    counts = {}
    first = {}
    for x in candidates:
        key = json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)
        if key not in first:
            first[key] = x
        counts[key] = counts.get(key, 0) + 1
    best = max(counts, key=counts.get)
    return first[best]


def majority_vote(results):
    """对多次生成结果逐字段取众数（支持 list/dict 值，平局保留最先出现）。"""
    if not results:
        return {}
    keys = []
    for r in results:
        for k in r:
            if k not in keys:
                keys.append(k)
    merged = {}
    for k in keys:
        candidates = [r.get(k) for r in results if k in r]
        merged[k] = _vote_value(candidates)
    return merged


# ============================================================
# 5. Prompt 模板
# ============================================================
CLASSIFY_PROMPT = """你是一名严谨的法律文书解析专家。
用户输入一份法律文书。请判断其文书类型。

执行流程（必须按顺序）：
1. 首先调用 load_skill 工具，传入 skill 名称 "document-type-classification"，读取完整的类型判定规则。
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


# ============================================================
# 6. 主流程
# ============================================================
def main():
    task_id = submit_parse_task(PDF_PATH)
    poll_task(task_id)
    result = get_parse_result(task_id)

    query1 = str(result.get("results"))
    NUM_VOTES = 20

    agent_executor = make_agent()

    # 第一步：分类
    cls_resp = agent_executor.invoke(
        {"messages": [("system", CLASSIFY_PROMPT), ("user", query1)]},
        config={"configurable": {"thread_id": str(uuid7())}},
    )
    cls = extract_json(cls_resp.get("messages", [])[-1].content)
    doc_type = cls.get("文书类型") or ""
    need_parse = str(cls.get("是否需解析") or "").strip()
    print("分类结果:", json.dumps(cls, ensure_ascii=False))

    # 第二步：按类型加载 skill 提取（生成 NUM_VOTES 次，逐字段取众数）
    skill_name = skill_for_doc_type(doc_type)
    if skill_name is None or not need_parse.lower().startswith(("是", "true", "1")):
        print("无需提取或未知类型:", doc_type, need_parse)
    else:
        results = []
        for _ in range(NUM_VOTES):
            ext_resp = agent_executor.invoke(
                {"messages": [("system", extract_prompt(SKILL_FIELDS[skill_name])), ("user", query1)]},
                config={"configurable": {"thread_id": str(uuid7())}},
            )
            results.append(extract_json(ext_resp.get("messages", [])[-1].content))
        extracted = majority_vote(results)
        extracted.setdefault("文书类型", doc_type)
        print(json.dumps(extracted, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
