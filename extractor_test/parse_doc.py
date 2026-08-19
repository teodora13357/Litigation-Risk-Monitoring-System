import json
import re
import time
from decimal import Decimal, ROUND_CEILING
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
PDF_PATH = Path("/Users/olof.chenx2x.net/s4/（2026）粤 0981 民初 4148 号.pdf").resolve()

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
    temperature=0.1,
    top_p=0.9,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)


# ============================================================
# 4. Skill 工具与 Agent
# ============================================================
from deepagents.middleware import SkillsMiddleware
from deepagents.backends.filesystem import FilesystemBackend

SKILLS_ROOT = Path("/Users/olof.chenx2x.net/s4/Litigation-Risk-Monitoring-System/prompts/skills")

TYPE_TO_SKILL = [
    ("应诉", "defense-notice-extract"),
    ("参加诉讼", "defense-notice-extract"),
    ("起诉状", "complaint-arbitration-extract"),
    ("仲裁申请", "complaint-arbitration-extract"),
    ("上诉状", "appeal-extract"),
    ("举证", "evidence-notice-extract"),
    ("传票", "summons-hearing-extract"),
    ("开庭通知", "summons-hearing-extract"),
    ("改期开庭", "summons-hearing-extract"),
    ("判决书", "judgment-ruling-mediation-extract"),
    ("裁定书", "judgment-ruling-mediation-extract"),
    ("调解书", "judgment-ruling-mediation-extract"),
]

SKILL_FIELDS = {
    "complaint-arbitration-extract": ["原告/申请人", "被告/被申请人", "涉及主体", "业务类型", "标准案由", "受理法院/仲裁委", "标的额", "诉讼请求金额", "赔偿金"],
    "defense-notice-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "业务类型", "标准案由", "受理法院/仲裁委", "立案日期"],
    "evidence-notice-extract": ["案号", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "标准案由", "业务类型", "举证期限"],
    "judgment-ruling-mediation-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "业务类型", "标准案由", "标的额", "赔偿金", "诉讼请求金额", "裁判结果"],
    "summons-hearing-extract": ["案号", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "应到时间", "标准案由", "业务类型", "应到地点"],
    "appeal-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "业务类型", "标准案由", "受理法院/仲裁委", "标的额", "诉讼请求金额", "上诉请求"],
}


def skill_for_doc_type(doc_type: str):
    for kw, skill in TYPE_TO_SKILL:
        if kw in doc_type:
            return skill
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
    - appeal-extract: 上诉状字段提取
    """
    name = str(skill_name or "").strip().strip("/").strip(".")
    if (SKILLS_ROOT / name).is_dir():
        return (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    for d in SKILLS_ROOT.iterdir():
        if d.is_dir() and (d.name in name or name in d.name):
            return (SKILLS_ROOT / d.name / "SKILL.md").read_text(encoding="utf-8")
    alias = {"传票": "summons-hearing-extract", "开庭通知": "summons-hearing-extract",
             "起诉状": "complaint-arbitration-extract", "仲裁申请书": "complaint-arbitration-extract",
             "举证": "evidence-notice-extract", "应诉": "defense-notice-extract",
             "判决": "judgment-ruling-mediation-extract", "裁定": "judgment-ruling-mediation-extract",
             "调解": "judgment-ruling-mediation-extract", "上诉状": "appeal-extract", "分类": "file-type-classification"}
    for k, v in alias.items():
        if k in name:
            return (SKILLS_ROOT / v / "SKILL.md").read_text(encoding="utf-8")
    candidates = [d.name for d in SKILLS_ROOT.iterdir() if d.is_dir()]
    return f"未找到 skill '{skill_name}'，可用: {candidates}"


def make_agent(system_prompt: str | None = None):
    """构建带默认 SkillsMiddleware + load_skill 工具的 agent（渐进披露，纯文本 JSON 输出）。
    通过 system_prompt 传指令，避免额外 system 消息导致服务端模板报错。"""
    backend = FilesystemBackend(root_dir=str(SKILLS_ROOT))
    middleware = SkillsMiddleware(backend=backend, sources=["."])
    return create_agent(
        model,
        middleware=[middleware],
        tools=[load_skill],
        system_prompt=system_prompt,
    )


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


def majority_vote(results):
    """对多次生成结果逐字段取众数（支持 list/dict 值，平局保留最先出现）。
    返回 (众数结果 dict, 每字段众数出现次数 dict)。"""
    if not results:
        return {}, {}
    keys = []
    for r in results:
        for k in r:
            if k not in keys:
                keys.append(k)
    merged = {}
    counts = {}
    for k in keys:
        cc = {}
        first = {}
        for x in [r.get(k) for r in results]:
            key = json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)
            if key not in first:
                first[key] = x
            cc[key] = cc.get(key, 0) + 1
        best = max(cc, key=cc.get)
        merged[k], counts[k] = first[best], cc[best]
    return merged, counts


# ============================================================
# 5. 溯源：全字段编辑距离（源头=文书原文，输出=字段值）
# ============================================================
NUMBER_FIELDS = ["标的额", "赔偿金", "诉讼请求金额"]
SKIP_PROVENANCE_FIELDS = ["应到地点", "业务类型"]  # 这些字段不做溯源（编辑距离固定为 None）


def _normalize_number(s: str) -> str:
    """归一化数字写法：去空白、千分位逗号/顿号、人民币/元等后缀、全角转半角。"""
    if s is None:
        return ""
    t = str(s)
    t = t.replace("，", "").replace(",", "").replace(" ", "").replace("　", "")
    # 全角数字/字母转半角
    t = "".join(chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" else ch for ch in t)
    # 去掉 元/人民币/万元/亿 等单位词（匹配数字主体）
    for unit in ["万元", "亿元", "元", "人民币", "￥", "¥", "约", "共", "整"]:
        t = t.replace(unit, "")
    return t.strip()


def _best_token_dist(value: str, text: str) -> int | None:
    """value(归一化数字串) 与 text 中各数字 token 的最小编辑距离；无 token 返回 None。"""
    def _lev(a: str, b: str) -> int:
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    v = _normalize_number(value)
    if not v:
        return None
    try:
        v_num = float(v)
    except ValueError:
        v_num = None
    best = None
    # 数字 token：允许千分位逗号、小数、前后可带 元/人民币 等单位
    for m in re.finditer(r"[\d，,．.][\d，,．.]*(?:\s*(?:万元|亿元|元|人民币|￥|¥))?", text):
        raw = m.group(0)
        # 带 万元/亿元 等单位的 token 先按数值比较（如 "8万元" 可匹配 80000）
        if v_num is not None:
            p = _parse_amount(raw)
            if p is not None and p == v_num:
                return 0
        cand = _normalize_number(raw)
        if not cand:
            continue
        d = _lev(v, cand)
        if best is None or d < best:
            best = d
            if best == 0:
                return 0
    return best


def _min_substring_edit_dist(value: str, text: str) -> int:
    """value 与 text 任意连续子串的最小编辑距离（子串首尾可自由裁剪）。
    先做逐字命中快速路径，未命中再跑 O(len(value)*len(text)) 的 DP。"""
    if not value:
        return 0
    if not text:
        return len(value)
    if value in text:
        return 0
    # dp 第 0 行全 0：子串可从任意位置开始，前置字符免费跳过
    prev = [0] * (len(text) + 1)
    for ca in value:
        cur = [prev[0] + 1]
        for j, cb in enumerate(text, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return min(prev)


def _edit_dist_for(value, text: str) -> int | None:
    """单个字段值在原文(源头)中的最小编辑距离；null/空值 → None（不做溯源）。
    - 数字：与原文数字 token 比较（归一化后）
    - 列表：取各元素编辑距离的最大值（最差项）
    - 文本：与原文任意连续子串的最小编辑距离"""
    if isinstance(value, dict):
        if "value" not in value:
            return None  # 非 {value:...} 结构（如提取说明），不做溯源
        value = value["value"]
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        ds = [_edit_dist_for(v, text) for v in value]
        ds = [d for d in ds if d is not None]
        return max(ds) if ds else 0
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        value = str(int(value)) if value.is_integer() else str(value)  # 保留小数
    s = str(value).strip()
    if not s:
        return None
    if s in text:  # 原文逐字包含 → 编辑距离 0
        return 0
    if s.replace(".", "", 1).isdigit():  # 数字字段：与原文数字 token 比较
        d = _best_token_dist(s, text)
        if d is not None:
            return d
    return _min_substring_edit_dist(s, text)


def field_provenance(extracted: dict, text: str) -> dict:
    """全字段溯源：返回 {字段: 编辑距离}。编辑距离 = 字段输出值与其在原文(源头)中最佳匹配的编辑距离。
    SKIP_PROVENANCE_FIELDS 中的字段不做溯源。"""
    out = {}
    for f, v in extracted.items():
        if f in SKIP_PROVENANCE_FIELDS:
            out[f] = None  # 不做溯源
            continue
        out[f] = _edit_dist_for(v, text)
    return out


def _parse_amount(s):
    """金额串 → 数值(元)：万元×10000、亿元×1亿、去千分位/单位/全角数字；保留小数（整数金额返回 int）。"""
    if s is None:
        return None
    t = str(s).replace("，", ",").replace(" ", "").replace("　", "")
    # 全角数字/字母转半角
    t = "".join(chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" else ch for ch in t)
    mult = 1
    if t.endswith("亿元"):
        mult = 100000000
    elif t.endswith("万元"):
        mult = 10000
    for unit in ["亿元", "万元", "元", "人民币", "￥", "¥", "约", "共", "整"]:
        t = t.replace(unit, "")
    t = t.replace(",", "")
    try:
        val = float(t) * mult
    except (TypeError, ValueError):
        return None
    return int(val) if val == int(val) else val


def normalize_value(value, field: str):
    """字段值归一化（在溯源之后执行）：number 字段统一转为数值(元)，复用 _parse_amount；
    非 number 字段原样返回。"""
    if field not in NUMBER_FIELDS:
        return value
    if isinstance(value, dict):
        if "value" in value:
            return normalize_value(value["value"], field)
        return value
    if isinstance(value, (list, tuple)):
        return [normalize_value(v, field) for v in value]
    if value is None:
        return None
    if not str(value).strip():
        return value
    parsed = _parse_amount(value)
    return parsed if parsed is not None else value


# ============================================================
# 6. 数字字段溯源兜底重生成：单值匹配 或 加算(子集和) 复合判定
# ============================================================
SUMMED_FIELDS = ["诉讼请求金额", "标的额", "赔偿金"]   # 可能由原文若干金额加算的字段
CLAIM_TRIGGERS = ["诉讼请求", "请求判令", "请求支付", "请求赔偿", "诉请", "判令"]
NUM_TOL = 0        # 单值编辑距离容差
AMOUNT_TOL = 0.0051  # 金额舍入容差(元)：仅供 restore_amount_precision 将模型四舍五入的值还原为原文全精度（如 832552.67 → 832552.665）；溯源判定不使用容差
MAX_REGEN = 2      # 每个 voter 内数字字段溯源不通过的最大重生成次数
AMOUNT_TOKEN_RE = r"\d[\d，,．.]*\s*(?:亿元|万元|元|人民币|￥|¥)"  # 带单位才算金额，排除年份/案号


def _source_amount_values(text: str, window: int = 100) -> list[int]:
    """原文诉讼请求区金额候选：各触发词后 window 字符内带单位的金额数值列表（应加算的部分）。"""
    vals = []
    for trig in CLAIM_TRIGGERS:
        for m in re.finditer(re.escape(trig), text):
            seg = text[m.end(): m.end() + window]
            for am in re.finditer(AMOUNT_TOKEN_RE, seg):
                v = _parse_amount(am.group(0))
                if v is not None and v > 0:
                    vals.append(v)
    return vals


def _scale_to_int(vals, target):
    """按候选与目标的最大小数位数求缩放，把元金额转成整数（保留全部小数）。
    用 Decimal 精确缩放，不做四舍五入。"""
    maxdp = 0
    for x in list(vals) + [target]:
        s = f"{Decimal(str(x)):.10f}".rstrip("0")
        if "." in s:
            maxdp = max(maxdp, len(s.split(".")[1]))
    scale = 10 ** maxdp

    def _to_int(x):
        return int(Decimal(str(x)) * scale)  # scale 覆盖全部小数位，int 截断即精确

    # 仅当原文金额带小数时才允许舍入容差（整数金额无舍入问题）
    tol_int = 0 if maxdp == 0 else int((Decimal(str(AMOUNT_TOL)) * scale).to_integral_value(rounding=ROUND_CEILING))
    return [_to_int(x) for x in vals], _to_int(target), scale, tol_int


def _subset_sum_possible(vals, target, tol, max_terms: int = 12) -> bool:
    """vals 中是否存在不超过 max_terms 项之和与 target 相差 <= tol（正数，降序+前缀和剪枝回溯）。"""
    if target <= 0:
        return False
    hi = target + tol
    vals = sorted((v for v in vals if 0 < v <= hi), reverse=True)
    n = len(vals)
    prefix = [0] * (n + 1)
    for i in range(n):
        prefix[i + 1] = prefix[i] + vals[i]
    if prefix[n] < target - tol:
        return False

    def dfs(idx, remaining, terms):
        if remaining <= tol:
            return True
        if terms >= max_terms or idx >= n:
            return False
        if vals[idx] > remaining:
            return dfs(idx + 1, remaining, terms)
        if prefix[n] - prefix[idx] < remaining - tol:
            return False
        if dfs(idx + 1, remaining - vals[idx], terms + 1):
            return True
        return dfs(idx + 1, remaining, terms)

    return dfs(0, target, 0)


def number_provenance_fails(cand: dict, text: str) -> list[str]:
    """数字字段溯源复合判定，返回未通过的字段列表。
    通过条件：单值编辑距离 <= NUM_TOL；或字段∈SUMMED_FIELDS 且值等于原文加算金额候选的子集和（精确）。
    金额被模型四舍五入导致不通过时，最终输出由 restore_amount_precision 还原为原文全精度。
    null/空/无法解析的值跳过（视为通过）。"""
    claim_vals = _source_amount_values(text)
    fails = []
    for f in NUMBER_FIELDS:
        v = cand.get(f)
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            target = v  # 保留小数，不截断
        else:
            parsed = _parse_amount(v)
            if parsed is None:
                continue
            target = parsed
        # 1) 单值编辑距离
        d = _best_token_dist(str(target), text)
        if d is not None and d <= NUM_TOL:
            continue
        # 2) 加算：子集和（按最大小数位缩放为整数，保留全部小数精确比较）
        if f in SUMMED_FIELDS:
            ints, itarget, _, _ = _scale_to_int(claim_vals, target)
            if _subset_sum_possible(ints, itarget, 0):
                continue
        fails.append(f)
    return fails


def _subset_sum_value(vals, target, tol, max_terms: int = 12):
    """存在子集和与 target 相差 <= tol 时，返回该精确子集和（缩放整数）；否则 None。"""
    if target <= 0:
        return None
    lo, hi = target - tol, target + tol
    cand = sorted((v for v in vals if 0 < v <= hi), reverse=True)
    n = len(cand)
    prefix = [0] * (n + 1)
    for i in range(n):
        prefix[i + 1] = prefix[i] + cand[i]
    if prefix[n] < lo:
        return None
    best = [None]

    def dfs(idx, s, terms):
        if best[0] is not None:
            return
        if lo <= s <= hi:
            best[0] = s
            return
        if terms >= max_terms or idx >= n:
            return
        if prefix[n] - prefix[idx] < lo - s:
            return
        if s + cand[idx] <= hi:
            dfs(idx + 1, s + cand[idx], terms + 1)
            if best[0] is not None:
                return
        dfs(idx + 1, s, terms)

    dfs(0, 0, 0)
    return best[0]


def restore_amount_precision(value, text: str, field: str):
    """把被模型四舍五入（如 832552.67）的金额字段值还原为原文的全精度值（如 832552.665）；
    无对应原文金额时原样返回。"""
    if value is None:
        return value
    target = value if isinstance(value, (int, float)) else _parse_amount(value)
    if target is None:
        return value
    claim_vals = _source_amount_values(text)
    if not claim_vals:
        return value
    # 1) 直接命中：与原文某金额在舍入容差内 → 用原文全精度
    best = min(claim_vals, key=lambda c: abs(c - target))
    if abs(best - target) <= AMOUNT_TOL:
        return best
    # 2) 加算命中：某子集和与 target 在容差内 → 用该子集精确和（全精度）
    if field in SUMMED_FIELDS:
        ints, itarget, scale, tol_int = _scale_to_int(claim_vals, target)
        s = _subset_sum_value(ints, itarget, tol_int)
        if s is not None:
            val = s / scale
            return int(val) if val == int(val) else val
    return value


def _regen_hint(fails: list[str], cand: dict, text: str) -> str:
    """为重生成拼接溯源提示：列出未通过字段的提取值、restore_amount_precision 还原出的最接近原文金额，
    以及原文诉讼请求区金额候选，供模型参考后重新提取。"""
    claim_vals = "、".join(f"{x}元" for x in sorted(set(_source_amount_values(text)))[:10])
    parts = []
    for f in fails:
        v = cand.get(f)
        hint = f"{f} 的提取值 {v} 无法由原文金额验证"
        restored = restore_amount_precision(v, text, f) if v is not None and str(v).strip() else None
        if restored is not None and str(restored) != str(v):
            hint += f"，请按原文还原值 {restored}元 输出"
        if claim_vals:
            hint += f"（原文诉讼请求区金额候选：{claim_vals}，如需加算请按各项之和）"
        parts.append(hint)
    return "；".join(parts)


def extract_voter(skill_name: str, text: str) -> dict:
    """单个 voter：提取并做数字溯源兜底重生成，返回溯源合格的结果。"""
    cand, fails = None, []
    for attempt in range(MAX_REGEN + 1):
        extract_agent = make_agent(extract_prompt(SKILL_FIELDS[skill_name]))
        msgs = [("user", text)]
        if attempt > 0 and fails:
            msgs.append(("user", f"溯源提示：{_regen_hint(fails, cand, text)}。请重新提取。"))
        ext_resp = extract_agent.invoke(
            {"messages": msgs},
            config={"configurable": {"thread_id": str(uuid7())}},
        )
        cand = align_to_ref(extract_json(ext_resp["messages"][-1].content), skill_name)
        fails = number_provenance_fails(cand, text)
        if not fails:
            break
        print(f"[voter] 数字字段溯源不通过 {str({f: cand.get(f) for f in fails})}，重生成 {attempt+1}/{MAX_REGEN+1}")
    return cand


def wrap_extracted(extracted: dict, counts: dict, text: str, doc_type: str, total_votes: int) -> dict:
    """最终输出包装：先用原始输出值溯源（编辑距离），再归一化 value；置信度 = 众数/20 的百分数。"""
    provenance = field_provenance(extracted, text)
    final = {}
    for f, v in extracted.items():
        if f == "提取说明":
            final[f] = v   # 说明字段原样保留，不包属性
            continue
        value = normalize_value(v, f)                       # 溯源之后再归一化 value
        if f in NUMBER_FIELDS:
            value = restore_amount_precision(value, text, f)  # 金额字段还原为原文全精度（防四舍五入）
        final[f] = {
            "value": value,
            "编辑距离": _edit_dist_for(value, text) if f in NUMBER_FIELDS else provenance.get(f),
            "置信度": round(counts.get(f, 0) / total_votes * 100),  # 众数/20 的百分数
        }
    final.setdefault("文书类型", doc_type)
    return final


# ============================================================
# 7. Prompt 模板
# ============================================================
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


# ============================================================
# 8. 主流程
# ============================================================
def main():
    task_id = submit_parse_task(PDF_PATH)
    poll_task(task_id)
    result = get_parse_result(task_id)

    query1 = str(result.get("results"))
    NUM_VOTES = 20

    classify_agent = make_agent(CLASSIFY_PROMPT)

    # 第一步：分类（system_prompt 已内置，messages 里不要再传 system，否则服务端报错）
    cls_resp = classify_agent.invoke(
        {"messages": [("user", query1)]},
        config={"configurable": {"thread_id": str(uuid7())}},
    )
    cls_msg = cls_resp["messages"][-1]
    cls = extract_json(cls_msg.content)
    doc_type = cls.get("文书类型") or ""
    need_parse = str(cls.get("是否需解析") or "").strip()
    print("分类结果:", json.dumps(cls, ensure_ascii=False))

    # 第二步：按类型加载 skill 提取（每 voter 数字溯源兜底重生成，投票后每字段包装 value/编辑距离/置信度）
    skill_name = skill_for_doc_type(doc_type)
    if skill_name is None or not need_parse.lower().startswith(("是", "true", "1")):
        print("无需提取或未知类型:", doc_type, need_parse)
    else:
        results = []
        for _ in range(NUM_VOTES):
            results.append(extract_voter(skill_name, query1))
        extracted, counts = majority_vote(results)
        final = wrap_extracted(extracted, counts, query1, doc_type, NUM_VOTES)
        print(json.dumps(final, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
