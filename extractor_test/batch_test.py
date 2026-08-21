import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_CEILING
from functools import lru_cache
from pathlib import Path

import requests
from tqdm import tqdm
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from deepagents.middleware import SkillsMiddleware
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.utils.uuid import uuid7

# 内网/本机调用不走代理，避免 VPN 影响
os.environ.setdefault("NO_PROXY", "192.168.10.250,127.0.0.1,localhost")
os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])

BASE_URL = "http://192.168.10.250:8000"
SKILLS_ROOT = Path("/Users/olof.chenx2x.net/s4/Litigation-Risk-Monitoring-System/prompts/skills")

# 文书类型关键词 -> 提取 skill 目录名（包含匹配，容忍分类输出的措辞差异）
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

# 每个 skill 的提取字段（不含 文书类型 和 提取说明）
SKILL_FIELDS = {
    "complaint-arbitration-extract": ["原告/申请人", "被告/被申请人", "涉及主体", "标准案由", "业务类型", "受理法院/仲裁委", "标的额", "诉讼请求金额", "赔偿金"],
    "defense-notice-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "标准案由", "业务类型", "受理法院/仲裁委", "立案日期"],
    "evidence-notice-extract": ["案号", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "标准案由", "业务类型", "举证期限"],
    "judgment-ruling-mediation-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "标准案由", "业务类型", "标的额", "赔偿金", "诉讼请求金额", "裁判结果"],
    "summons-hearing-extract": ["案号", "被告/被申请人", "涉及主体", "受理法院/仲裁委", "应到时间", "标准案由", "业务类型", "应到地点"],
    "appeal-extract": ["案号", "原告/申请人", "被告/被申请人", "涉及主体", "标准案由", "业务类型", "受理法院/仲裁委", "标的额", "诉讼请求金额", "上诉请求"],
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
        skill_file = SKILLS_ROOT / name / "SKILL.md"
        if not skill_file.exists():
            raise FileNotFoundError(f"skill 不存在: {skill_file}")
        return skill_file.read_text(encoding="utf-8")
    for d in SKILLS_ROOT.iterdir():
        if d.is_dir() and (d.name in name or name in d.name):
            skill_file = SKILLS_ROOT / d.name / "SKILL.md"
            if not skill_file.exists():
                raise FileNotFoundError(f"skill 不存在: {skill_file}")
            return skill_file.read_text(encoding="utf-8")
    alias = {"传票": "summons-hearing-extract", "开庭通知": "summons-hearing-extract",
             "起诉状": "complaint-arbitration-extract", "仲裁申请书": "complaint-arbitration-extract",
             "举证": "evidence-notice-extract", "应诉": "defense-notice-extract",
             "判决": "judgment-ruling-mediation-extract", "裁定": "judgment-ruling-mediation-extract",
             "调解": "judgment-ruling-mediation-extract", "上诉状": "appeal-extract", "分类": "file-type-classification"}
    for k, v in alias.items():
        if k in name:
            skill_file = SKILLS_ROOT / v / "SKILL.md"
            if not skill_file.exists():
                raise FileNotFoundError(f"skill 不存在: {skill_file}")
            return skill_file.read_text(encoding="utf-8")
    candidates = [d.name for d in SKILLS_ROOT.iterdir() if d.is_dir()]
    return f"未找到 skill '{skill_name}'，可用: {candidates}"


def build_model():
    return ChatOpenAI(
        base_url="http://192.168.10.250:8006",
        api_key="None",
        model="Qwen3.6-27B",
        max_tokens=None,
        temperature=0.1,
        top_p=0.9,
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


@lru_cache(maxsize=16)
def _ref_fields(skill_name: str) -> list[str]:
    """从 ref.json 读取该 skill 的字段清单（正常分支，去掉提取说明）。
    按 skill 名缓存：每个进程只读一次磁盘（align_to_ref 每个 voter/attempt 都会调用）；
    修改 ref.json 后需 `_ref_fields.cache_clear()` 或重启进程。"""
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


# ============================================================
# 溯源：全字段布尔判定（源头=文书原文，输出=字段值）
# ============================================================
NUMBER_FIELDS = ["标的额", "赔偿金", "诉讼请求金额"]
SKIP_PROVENANCE_FIELDS = ["应到地点", "业务类型", "标准案由"]  # 这些字段不做溯源（视为通过）


# 汉字数字/单位映射（含大写），用于把原文汉字金额归一化为数字
_CN_DIGITS = {
    "零": 0, "〇": 0, "○": 0,
    "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7,
    "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS = {
    "十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000,
}
_CN_BIG_UNITS = {"万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000}
_CN_SCALE_CHARS = "十百千万亿拾佰仟萬億"
_CN_FRAC_DIGITS = "零〇○一二两三四五六七八九壹贰叁肆伍陆柒捌玖"


def _cn_amount(s: str):
    """汉字金额串 → 数值（如 二千→2000、贰仟元→2000、两万→20000、一万零三百→10300、十万→100000；
    壹仟贰佰叁拾肆元伍角陆分→1234.56、伍角→0.5、三分→0.03）。
    仅当串含数量级单位（十/百/千/万/亿）或带 元/人民币/整/角/分 标记时才判定为金额，避免误识别日期等；
    解析失败返回 None。"""
    t = (s or "").strip()
    if not t:
        return None
    had_money_marker = False
    if t.startswith("人民币"):
        had_money_marker = True
        t = t[len("人民币"):].strip()
    # 先剥离尾部 分/角/元/人民币/整，并累加 角/分 小数
    frac = 0.0
    changed = True
    while changed and t:
        changed = False
        if t.endswith("分"):
            m = re.search(rf"([{_CN_FRAC_DIGITS}])分$", t)
            if not m:
                return None
            frac += _CN_DIGITS[m.group(1)] * 0.01
            t = t[: m.start()].strip()
            had_money_marker = True
            changed = True
        elif t.endswith("角"):
            m = re.search(rf"([{_CN_FRAC_DIGITS}])角$", t)
            if not m:
                return None
            frac += _CN_DIGITS[m.group(1)] * 0.1
            t = t[: m.start()].strip()
            had_money_marker = True
            changed = True
        else:
            for suf in ("元", "人民币", "整"):
                if t.endswith(suf):
                    t = t[: -len(suf)].strip()
                    had_money_marker = True
                    changed = True
                    break
    if not t:
        return _money_result(0, frac) if frac > 0 else None
    if not any(c in _CN_SCALE_CHARS for c in t) and not had_money_marker:
        return None
    total = 0
    section = 0
    number = 0
    for ch in t:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            if number == 0:
                number = 1  # 十/百/千 前无数时按 1（如 十五、十万）
            section += number * _CN_UNITS[ch]
            number = 0
        elif ch in _CN_BIG_UNITS:
            section = (section + number) * _CN_BIG_UNITS[ch]
            total += section
            section = 0
            number = 0
        else:
            return None
    total += section + number
    if total <= 0 and frac <= 0:
        return None
    return _money_result(total, frac)


def _money_result(total: float, frac: float):
    """整数元部分 + 角/分小数合并为数值；整数金额返回 int，带小数返回 float。"""
    val = total + round(frac, 2)
    return int(val) if val == int(val) else val


# 汉字金额 token 正则（原文扫描用）：中文数字串 + 可选 元/人民币/整 + 可选 角/分 小数
_CN_AMOUNT_RE = re.compile(
    rf"[零〇○一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億]+"
    rf"(?:\s*(?:元|人民币|整))?"
    rf"(?:[{_CN_FRAC_DIGITS}]+\s*角)?"
    rf"(?:[{_CN_FRAC_DIGITS}]+\s*分)?"
)


def _has_money_marker(s: str) -> bool:
    """金额标记（元/人民币/整/角/分）：原文扫描时排除日期、法条、期限等非金额汉字数字。"""
    return any(m in s for m in ("元", "人民币", "整", "角", "分"))


# 阿拉伯金额正则：货币符号/人民币前缀 + 数字 + 单位（带金额标记才算金额，排除年份/案号/日期）
AMOUNT_TOKEN_RE = r"(?:人民币|￥|¥)?\d[\d，,．.]*\s*(?:亿元|万元|元|人民币|￥|¥)"


@lru_cache(maxsize=8)
def _text_number_tokens(text: str) -> tuple:
    """原文中的金额 token 数值列表（含 万元/亿元 换算、汉字金额归一化与角/分小数）。
    只认带金额标记的数字：阿拉伯数字须带单位/货币符号，汉字金额须带 元/人民币/整/角/分，
    排除年份、案号、日期、法条、期限等非金额数字。按原文缓存。"""
    out = []
    for m in re.finditer(AMOUNT_TOKEN_RE, text):
        v = _parse_amount(m.group(0))
        if v is not None:
            out.append(v)
    for m in _CN_AMOUNT_RE.finditer(text):
        tok = m.group(0)
        if not _has_money_marker(tok):
            continue
        v = _parse_amount(tok)
        if v is not None:
            out.append(v)
    return tuple(out)


def _amount_token_match(target, text: str) -> bool:
    """目标金额是否与原文某个金额 token 数值相等（含 万元/亿元 换算、汉字金额归一化与角/分，如 80000 可匹配 "8万元"、2000 可匹配 "二千元"）。"""
    if target is None:
        return False
    return any(parsed == target for parsed in _text_number_tokens(text))


def _parse_amount(s):
    """金额串 → 数值(元)：汉字金额（二千/贰仟/两万/一万零三百）先归一化为数字；
    阿拉伯数字支持 万元×10000、亿元×1亿、去千分位/单位/全角数字；保留小数（整数金额返回 int）。"""
    if s is None:
        return None
    t = str(s).replace("，", ",").replace(" ", "").replace("　", "")
    # 全角数字/字母转半角
    t = "".join(chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" else ch for ch in t)
    # 汉字金额：交给 _cn_amount（内部处理 元/人民币/整 后缀与 万/亿 单位）；解析失败则回落阿拉伯路径
    if re.search(r"[零〇○一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億]", t):
        cn = _cn_amount(t)
        if cn is not None:
            return cn
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
    """字段值归一化：number 字段统一转为数值(元)，复用 _parse_amount；
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
# 数字字段溯源兜底重生成：单值匹配 或 加算(子集和) 复合判定
# ============================================================
SUMMED_FIELDS = ["诉讼请求金额", "标的额", "赔偿金"]   # 可能由原文若干金额加算的字段
# 金额候选锚点触发词：诉讼请求区 + 标的额 + 赔偿金 + 裁判区（与各 skill 字段触发词对齐）
CLAIM_TRIGGERS = ["诉讼请求", "请求判令", "请求支付", "请求赔偿", "诉请", "判令", "裁判",
                  "标的额", "标的金额", "涉案金额", "争议金额",
                  "赔偿金", "赔偿款", "损害赔偿", "赔偿金额"]
AMOUNT_TOL = 0.0051  # 金额舍入容差(元)：仅供 restore_amount_precision 将模型四舍五入的值还原为原文全精度（如 832552.67 → 832552.665）；溯源判定不使用容差
MAX_REGEN = 2      # 每个 voter 内字段溯源不通过的最大重生成次数
_AMOUNT_CTX_BEFORE = 24  # 候选金额上下文：金额前文长度（字符）
_AMOUNT_CTX_AFTER = 10   # 候选金额上下文：金额后文长度（字符）
_NEAREST_TOL_RATIO = 0.2  # 重生成提示“最接近加算值”搜索带宽：相对提取值的比例（0.2 = ±20%）


@lru_cache(maxsize=8)
def _source_amount_values(text: str, window: int = 100) -> tuple:
    """原文金额候选（含来源片段）：各 CLAIM_TRIGGERS 触发词后 window 字符内带金额标记的金额（应加算的部分）。
    触发词覆盖诉讼请求区 / 标的额 / 赔偿金 / 裁判区（与各 skill 字段触发词对齐）；
    返回 ((数值, 原文片段), ...) 按数值去重，每个候选都带金额上下文片段（前 _AMOUNT_CTX_BEFORE / 后 _AMOUNT_CTX_AFTER 字符），
    供重生成提示让模型按多段原文复核。
    仅用于 _regen_hint 展示多段原文上下文；
    溯源判定与金额还原用 _text_number_tokens（全文、保留重复金额，见 provenance_fails / restore_amount_precision）。
    按原文缓存（全文只扫描一次）；
    返回 tuple（不可变，防止缓存结果被调用方修改）。"""
    vals = []
    seen = set()
    for trig in CLAIM_TRIGGERS:
        for m in re.finditer(re.escape(trig), text):
            base = m.end()
            seg = text[base: base + window]
            for am in re.finditer(AMOUNT_TOKEN_RE, seg):
                v = _parse_amount(am.group(0))
                if v is None or v <= 0 or v in seen:
                    continue
                seen.add(v)
                abs_start = base + am.start()
                abs_end = base + am.end()
                ctx = text[max(0, abs_start - _AMOUNT_CTX_BEFORE): abs_end + _AMOUNT_CTX_AFTER]
                vals.append((v, ctx.replace("\n", "⏎").replace("\r", "")))
            for am in _CN_AMOUNT_RE.finditer(seg):
                tok = am.group(0)
                if not _has_money_marker(tok):
                    continue
                v = _parse_amount(tok)
                if v is None or v <= 0 or v in seen:
                    continue
                seen.add(v)
                abs_start = base + am.start()
                abs_end = base + am.end()
                ctx = text[max(0, abs_start - _AMOUNT_CTX_BEFORE): abs_end + _AMOUNT_CTX_AFTER]
                vals.append((v, ctx.replace("\n", "⏎").replace("\r", "")))
    return tuple(vals)


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


DATE_FIELDS = ["应到时间", "立案日期", "举证期限"]  # 日期类字段：溯源额外支持年月日数字匹配


@lru_cache(maxsize=256)
def _digit_groups(s) -> tuple:
    """提取文本中的数字分组（去前导零），如 "2026-06-29 09:00" -> ("2026","6","29","9","0")。
    按输入缓存（日期类字段溯源对全文与字段值重复计算时直接命中）。"""
    return tuple(str(int(m)) for m in re.findall(r"\d+", str(s)))


def _date_digits_match(value, text: str) -> bool:
    """日期类字段溯源：值中年月日三元组须在原文数字分组中连续出现（容忍中文/ISO/零填充格式差异）。"""
    vg = _digit_groups(value)
    tg = _digit_groups(text)
    if len(vg) < 3:
        return False
    triple = tuple(vg[:3])
    return any(tuple(tg[i:i + 3]) == triple for i in range(len(tg) - 2))


# CJK 部首补充块（U+2E80-U+2EFF）部分字符无 NFKC 分解（如 ⺠→民、⻋→车），需手动映射到简体汉字
_CJK_RAD_SUP = {
    0x2EA0: "民", 0x2EC5: "见", 0x2EC9: "贝", 0x2ECB: "车", 0x2ED0: "钅",
    0x2ED1: "长", 0x2ED2: "长", 0x2ED3: "长", 0x2ED4: "门", 0x2ED9: "革",
    0x2EDA: "页", 0x2EDB: "风", 0x2EDC: "飞", 0x2EE0: "饣", 0x2EE2: "马",
    0x2EE5: "鱼", 0x2EE6: "鸟", 0x2EE7: "卤", 0x2EE8: "麦", 0x2EE9: "黄",
    0x2EEC: "齐", 0x2EEE: "齿", 0x2EF0: "龙", 0x2EF2: "龟", 0x2EF3: "龟",
}


# 文本溯源归一化需去除的标点（保留小数点 .，全角 ． 经 NFKC 统一为半角 .）
_PUNCT_RE = re.compile(r"[，。、；：？！（）《》〈〉「」『』“”‘’—…～·〔〕【】｛｝,;:!?()\[\]{}<>\"'`|/\\@#%&*+=^~_-]+")


@lru_cache(maxsize=256)
def _provenance_norm(s) -> str:
    """归一化文本用于溯源比较：NFKC（全角转半角、CJK 兼容字/部首统一到汉字）+ 部首补充块映射
    + 去空白 + 去除标点（保留小数点 .）。
    按输入缓存：全文归一化（约 50KB 的 NFKC + 正则）每份文书只算一次，
    provenance_fails / _text_provenance_ok 重复调用直接命中。"""
    t = unicodedata.normalize("NFKC", str(s))
    t = "".join(_CJK_RAD_SUP.get(ord(c), c) for c in t)
    t = _PUNCT_RE.sub("", t)
    return re.sub(r"\s+", "", t)


def _text_provenance_ok(value, text: str, field: str, nt: str | None = None) -> bool:
    """文本字段溯源布尔判定：列表须全部命中；归一化后是原文子串 → True；
    日期类字段额外支持年月日数字匹配。null/空/无法归一化 → True（跳过）。"""
    if isinstance(value, dict):
        if "value" not in value:
            return True
        value = value["value"]
    if value is None or value == "":
        return True
    if isinstance(value, (list, tuple)):
        return all(_text_provenance_ok(v, text, field, nt) for v in value)
    s = str(value).strip()
    if not s:
        return True
    nv = _provenance_norm(s)
    if not nv:
        return True
    if nt is None:
        nt = _provenance_norm(text)
    if nv in nt:
        return True
    if field in DATE_FIELDS and _date_digits_match(s, text):
        return True
    return False


def provenance_fails(cand: dict, text: str) -> list[str]:
    """全字段溯源复合判定（除 SKIP_PROVENANCE_FIELDS 与 提取说明），返回未通过、需重生成的字段列表：
    - 数字字段：数值与原文某金额 token 精确相等（含 万元/亿元 换算、汉字金额归一化与角/分）；或字段∈SUMMED_FIELDS 且值等于全文金额 token（保留重复金额）的子集和（精确）
    - 文本字段：NFKC+去空白归一化后为原文子串（日期类字段支持年月日数字匹配）
    - 文本字段 null/空/无法归一化跳过（视为通过）；数字字段非空但无法解析视为不通过（触发重生成）"""
    amount_tokens = _text_number_tokens(text)
    nt = _provenance_norm(text)
    fails = []
    for f, v in cand.items():
        if f == "提取说明" or f in SKIP_PROVENANCE_FIELDS:
            continue
        if v is None or v == "":
            continue
        if f in NUMBER_FIELDS:
            if isinstance(v, (int, float)):
                target = v  # 保留小数，不截断
            else:
                parsed = _parse_amount(v)
                if parsed is None:
                    fails.append(f)
                    continue
                target = parsed
            # 1) 单值精确匹配（数值相等，含 万元/亿元 换算）
            if _amount_token_match(target, text):
                continue
            # 2) 加算：子集和（候选=全文金额 token，保留重复金额，按最大小数位缩放为整数后精确比较）
            if f in SUMMED_FIELDS:
                ints, itarget, _, _ = _scale_to_int(amount_tokens, target)
                if _subset_sum_possible(ints, itarget, 0):
                    continue
            fails.append(f)
            continue
        if not _text_provenance_ok(v, text, f, nt):
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


def _nearest_subset_sum(vals, target, max_terms: int = 12):
    """返回与 target 距离最近的不超过 max_terms 项子集和（缩放整数）；超过 ±20% 带宽或无法组成时返回 None。
    二分最小容差 + _subset_sum_value 剪枝回溯，避免全组合枚举。"""
    vals = sorted((v for v in vals if v > 0), reverse=True)
    if not vals or target <= 0:
        return None
    hi = max(1, int(target * _NEAREST_TOL_RATIO))
    if _subset_sum_value(vals, target, hi, max_terms) is None:
        return None
    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if _subset_sum_value(vals, target, mid, max_terms) is not None:
            hi = mid
        else:
            lo = mid + 1
    return _subset_sum_value(vals, target, lo, max_terms)


def restore_amount_precision(value, text: str, field: str):
    """把被模型四舍五入（如 832552.67）的金额字段值还原为原文的全精度值（如 832552.665）；
    候选=全文金额 token（保留重复金额，与 provenance_fails 一致）；无对应原文金额时原样返回。"""
    if value is None:
        return value
    target = value if isinstance(value, (int, float)) else _parse_amount(value)
    if target is None:
        return value
    amount_tokens = _text_number_tokens(text)
    if not amount_tokens:
        return value
    # 1) 直接命中：与原文某金额在舍入容差内 → 用原文全精度
    best = min(amount_tokens, key=lambda c: abs(c - target))
    if abs(best - target) <= AMOUNT_TOL:
        return best
    # 2) 加算命中：某子集和与 target 在容差内 → 用该子集精确和（全精度）
    if field in SUMMED_FIELDS:
        ints, itarget, scale, tol_int = _scale_to_int(amount_tokens, target)
        s = _subset_sum_value(ints, itarget, tol_int)
        if s is not None:
            val = s / scale
            return int(val) if val == int(val) else val
    return value


def _regen_hint(fails: list[str], cand: dict, text: str) -> str:
    """为重生成拼接溯源提示：数字字段给出还原金额，并按多段原文片段让模型复核；
    文本字段提示严格按原文提取。"""
    cands = _source_amount_values(text)
    segs = "；".join(f"[{v}元]「{ctx}」" for v, ctx in cands)
    parts = []
    for f in fails:
        v = cand.get(f)
        if f in NUMBER_FIELDS:
            hint = f"{f} 的提取值 {v} 无法由原文金额验证"
            restored = restore_amount_precision(v, text, f) if v is not None and str(v).strip() else None
            if restored is not None and str(restored) != str(v):
                hint += f"，请按原文还原值 {restored}元 输出"
            if f in SUMMED_FIELDS and v is not None and str(v).strip():
                target = v if isinstance(v, (int, float)) else _parse_amount(v)
                if target is not None:
                    ints, itarget, scale, _ = _scale_to_int(_text_number_tokens(text), target)
                    near = _nearest_subset_sum(ints, itarget)
                    if near is not None and near != itarget:
                        val = near / scale
                        val = int(val) if val == int(val) else val
                        hint += f"；原文金额可加算出的最接近值为 {val}元，请核对是否为正确答案"
            if segs:
                hint += f"（原文金额候选及上下文，请逐段复核后提取：{segs}；如需加算请按各项之和）"
        else:
            hint = f"{f} 的提取值 {v} 未能在原文中找到对应内容，请严格按原文提取：只输出原文中明确出现的值，不得改写、推断或补充"
        parts.append(hint)
    return "；".join(parts)


def _best_source_snippet(value, text: str, field: str, win: int = 16) -> tuple:
    """溯源调试信息：字段值在原文中的 (是否命中, 原文片段)。
    - 优先定位原文中的原始值/归一化值，取 ±win 字符上下文
    - 找不到时返回 (False, 原文片段)"""
    s = str(value)
    nt = _provenance_norm(text)
    nv = _provenance_norm(s)
    if field in NUMBER_FIELDS:
        hit = _amount_token_match(_parse_amount(s) if s else None, text)
    else:
        hit = bool(nv) and nv in nt
    raw_idx = text.find(s) if s else -1
    if raw_idx >= 0:
        snip = text[max(0, raw_idx - win): raw_idx + len(s) + win]
    elif nv and nv in nt:
        i = nt.find(nv)
        snip = nt[max(0, i - win): i + len(nv) + win]
    else:
        i = nt.find(nv[:1]) if nv else -1
        snip = nt[max(0, i - win): i + win * 2] if i >= 0 else nt[:win * 2]
    return hit, snip.replace("\n", "⏎")


def invoke_extract_msgs(extract_agent_, messages: list) -> dict:
    response = extract_agent_.invoke(
        {"messages": messages},
        config={"configurable": {"thread_id": str(uuid7())}},
    )
    return extract_json(response.get("messages", [])[-1].content)


def extract_voter(extract_agent_, voter_idx: int, skill_name: str, text: str) -> dict:
    """单个 voter：提取并做全字段溯源布尔判定（除 SKIP_PROVENANCE_FIELDS），返回溯源合格的结果。
    重生成只重新提取未溯源通过的字段，已通过字段沿用上次结果。"""
    cand, fails = None, []
    for attempt in range(MAX_REGEN + 1):
        if attempt > 0 and fails:
            hint = _regen_hint(fails, cand, text)
            print(f"  → 溯源提示：{hint}", flush=True)
            if all(f in NUMBER_FIELDS for f in fails):
                # 全是数字字段失败：只发备选数字+上下文片段，不再带完整原文
                msgs = [("user", f"溯源提示：{hint}。请重新提取。")]
            else:
                # 含文本字段失败：仍须带完整原文（文本字段提示不含原文片段）
                msgs = [("user", text), ("user", f"溯源提示：{hint}。请重新提取。")]
            agent = make_agent(extract_prompt(fails))   # 只重生成失败字段
        else:
            agent = extract_agent_
            msgs = [("user", text)]
        cand_new = align_to_ref(invoke_extract_msgs(agent, msgs), skill_name)
        if attempt > 0 and fails:
            # 合并：只更新失败字段，保留已通过字段
            for f in fails:
                cand[f] = cand_new[f]
            if "提取说明" in cand_new:
                old_notes = cand.get("提取说明")
                cand["提取说明"] = {**(old_notes if isinstance(old_notes, dict) else {}), **cand_new["提取说明"]}
        else:
            cand = cand_new
        fails = provenance_fails(cand, text)
        if not fails:
            break
        details = []
        for f in fails:
            hit, snip = _best_source_snippet(cand.get(f), text, f)
            details.append(f"{f}={cand.get(f)}（命中 {hit}，原文「{snip}」）")
        print(f"[voter {voter_idx}] 字段溯源不通过 {'; '.join(details)}，重生成 {attempt+1}/{MAX_REGEN+1}", flush=True)
    return cand


def wrap_extracted(extracted: dict, counts: dict, text: str, doc_type: str, total_votes: int) -> dict:
    """最终输出包装：归一化 value（金额字段还原原文全精度）；置信度 = 众数/20 的百分数。"""
    final = {}
    for f, v in extracted.items():
        if f == "提取说明":
            final[f] = v   # 说明字段原样保留，不包属性
            continue
        value = normalize_value(v, f)                       # 归一化 value
        if f in NUMBER_FIELDS:
            value = restore_amount_precision(value, text, f)  # 金额字段还原为原文全精度（防四舍五入）
        final[f] = {
            "value": value,
            "置信度": round(counts.get(f, 0) / total_votes * 100),  # 众数/20 的百分数
        }
    final.setdefault("文书类型", doc_type)
    return final


def majority_vote(results: list[dict]) -> tuple:
    """对多次生成结果逐字段取众数。

    - 字段名取所有结果键的并集
    - 每个字段在多次结果中出现次数最多的值胜出（按 json 序列化比较，支持 list/dict）
    - 平局时保留最先出现的值
    返回 (众数结果 dict, 每字段众数出现次数 dict)。
    """
    if not results:
        return {}, {}
    keys: list[str] = []
    for r in results:
        for k in r:
            if k not in keys:
                keys.append(k)
    merged = {}
    counts = {}
    for k in keys:
        cc: dict[str, int] = {}
        first: dict[str, object] = {}
        for c in [r.get(k) for r in results]:
            key = json.dumps(c, ensure_ascii=False, sort_keys=True, default=str)
            if key not in first:
                first[key] = c
            cc[key] = cc.get(key, 0) + 1
        best_key = max(cc, key=cc.get)
        merged[k], counts[k] = first[best_key], cc[best_key]
    return merged, counts


NUM_VOTES = 20  # 与 parse_doc.py 对齐：每篇文书投票数


def process_document(classify_agent_, md_content: str, num_votes: int = NUM_VOTES) -> dict:
    """先分类，再按类型加载对应 skill 提取（每 voter 字段溯源布尔判定兜底重生成，投票后每字段包装 value/置信度）。"""
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
    results = [extract_voter(extract_agent, v, skill_name, md_content) for v in range(num_votes)]
    extracted, counts = majority_vote(results)
    return wrap_extracted(extracted, counts, md_content, doc_type, num_votes)


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
