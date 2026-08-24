import json
import os
import re
import sys
import time
from decimal import Decimal, ROUND_CEILING
from functools import lru_cache
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from provenance_utils import DATE_FIELDS, _date_digits_match, _digit_groups, _provenance_norm, _text_provenance_ok

# 内网/本机调用不走代理，避免 VPN 影响
os.environ.setdefault("NO_PROXY", "192.168.10.250,127.0.0.1,localhost")
os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])

from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain_core.utils.uuid import uuid7
from langgraph.graph import StateGraph, START, END

# ============================================================
# 1. 配置
# ============================================================
BASE_URL = "http://192.168.10.250:8000"
PDF_PATH = Path(__file__).resolve().parents[1] / "testset" / "起诉状_仲裁申请书" / "孙丽红诉王佳鑫的起诉状.pdf"


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
                "parse_method": "ocr",
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

import sys

# 统一 skill loader（字段清单单一事实来源）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import loader

SKILLS_ROOT = loader.SKILLS_ROOT
TYPE_TO_SKILL = loader.TYPE_TO_SKILL


def skill_for_doc_type(doc_type: str):
    return loader.skill_for_doc_type(doc_type)


def load_skill(skill_name: str) -> str:
    return loader.load_skill(skill_name)


load_skill.__doc__ = loader.available_skills_docstring()
load_skill = tool(load_skill)


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


_AGENT_CACHE: dict[str, object] = {}


def get_agent(system_prompt: str | None = None):
    """按 system_prompt 缓存 agent，避免每个 voter/每次重生成重复构建 create_agent。"""
    key = system_prompt or ""
    agent = _AGENT_CACHE.get(key)
    if agent is None:
        agent = make_agent(system_prompt)
        _AGENT_CACHE[key] = agent
    return agent


NUM_VOTES = 20  # 与 batch_test.py 对齐：每篇文书投票数


def _ref_fields(skill_name: str) -> list[str]:
    return loader.skill_fields(skill_name)


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
# 5. 溯源：全字段布尔判定（源头=文书原文，输出=字段值）
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
# 6. 数字字段溯源兜底重生成：单值匹配 或 加算(子集和) 复合判定
# ============================================================
SUMMED_FIELDS = ["诉讼请求金额", "标的额", "赔偿金"]   # 可能由原文若干金额加算的字段
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


class State(TypedDict):
    doc: str
    runnable: bool
    cls_result: dict
    need_parse: bool
    doc_type: str
    skill_name: str
    voter: int
    attempt: int
    cand: dict
    fail_fields: list
    responses: Annotated[list, add]
    done: bool
    final: dict


def invoke_classify(classify_agent_, md_content: str) -> dict:
    response = classify_agent_.invoke(
        {"messages": [("user", md_content)]},
        config={"configurable": {"thread_id": str(uuid7())}},
    )
    return extract_json(response.get("messages", [])[-1].content)


def invoke_extract_msgs(extract_agent_, messages: list) -> dict:
    response = extract_agent_.invoke(
        {"messages": messages},
        config={"configurable": {"thread_id": str(uuid7())}},
    )
    content = response.get("messages", [])[-1].content
    try:
        return extract_json(content)
    except ValueError as e:
        # 模型偶发输出纯分析文本（非 JSON），此时按空对象继续，避免整篇文书失败；
        # 空对象会被 align_to_ref 补成全 null，并让溯源跳过 null 字段。
        print(f"  [警告] 提取响应未包含 JSON，按空对象处理: {e}", flush=True)
        return {}


def classify_node(state: State) -> dict:
    """判型节点：调用 file-type-classification skill，输出文书类型与是否需解析。"""
    doc = state["doc"]
    try:
        cls = invoke_classify(get_agent(CLASSIFY_PROMPT), doc)
    except Exception as e:
        return {"runnable": False, "final": {"错误": f"分类失败: {e}"}}
    need_parse = str(cls.get("是否需解析") or "").strip().lower().startswith(("是", "true", "1"))
    return {
        "runnable": True,
        "cls_result": cls,
        "doc_type": cls.get("文书类型") or "",
        "need_parse": need_parse,
    }


def parse_check_node(state: State) -> dict:
    """解析校验节点：根据判型结果映射提取 skill；无需解析/无匹配 skill 时直接产出 final。"""
    if not state.get("need_parse"):
        return {"runnable": False, "final": state.get("cls_result") or {}}
    skill_name = skill_for_doc_type(state["doc_type"])
    if skill_name is None:
        return {
            "runnable": False,
            "final": {"文书类型": state["doc_type"], "提取说明": {"错误": f"无对应提取 skill: {state['doc_type']}"}},
        }
    return {"runnable": True, "skill_name": skill_name}


def extract_node(state: State) -> dict:
    """单个 voter 提取节点：与 batch_test.extract_node 等价的状态机版本。
    每 voter 最多 MAX_REGEN+1 次尝试；失败字段单独存 fail_fields，重生成只提取失败字段并合并；
    溯源通过或次数耗尽后把最终候选 append 进 responses，并推进 voter。"""
    text = state["doc"]
    skill_name = state["skill_name"]
    voter = state["voter"]
    attempt = state["attempt"]
    cand = dict(state.get("cand") or {})
    fails = list(state.get("fail_fields") or [])

    if attempt > 0 and fails:
        hint = _regen_hint(fails, cand, text)
        print(f"  → 溯源提示：{hint}", flush=True)
        if all(f in NUMBER_FIELDS for f in fails):
            # 全是数字字段失败：只发备选数字+上下文片段，不再带完整原文
            msgs = [("user", f"溯源提示：{hint}。请重新提取。")]
        else:
            # 含文本字段失败：仍须带完整原文（文本字段提示不含原文片段）
            msgs = [("user", text), ("user", f"溯源提示：{hint}。请重新提取。")]
        fields = fails          # 只重生成失败字段
    else:
        msgs = [("user", text)]
        fields = loader.skill_fields(skill_name)

    cand_new = align_to_ref(invoke_extract_msgs(get_agent(extract_prompt(fields)), msgs), skill_name)
    if attempt > 0 and fails:
        # 合并：只更新失败字段，保留已通过字段
        for f in fails:
            cand[f] = cand_new[f]
        if "提取说明" in cand_new:
            old_notes = cand.get("提取说明")
            cand["提取说明"] = {**(old_notes if isinstance(old_notes, dict) else {}), **cand_new["提取说明"]}
    else:
        cand = cand_new

    new_fails = provenance_fails(cand, text)
    if new_fails:
        print(f"[voter {voter}] 字段溯源不通过 {new_fails}，重生成 {attempt+1}/{MAX_REGEN+1}", flush=True)
        if attempt < MAX_REGEN:
            # 还有重生成次数：保存候选与失败字段，回到本节点
            return {"cand": cand, "fail_fields": new_fails, "attempt": attempt + 1}

    # 溯源通过或次数耗尽：本 voter 收尾，结果入 responses
    next_voter = voter + 1
    return {
        "voter": next_voter,
        "attempt": 0,
        "cand": {},
        "fail_fields": [],
        "responses": [cand],
        "done": next_voter >= NUM_VOTES,
    }


def collect_node(state: State) -> dict:
    """收集节点：多数投票并包装最终输出。"""
    extracted, counts = majority_vote(state["responses"])
    return {"final": wrap_extracted(extracted, counts, state["doc"], state["doc_type"], NUM_VOTES)}


def route_after_classify(state: State) -> str:
    return "parse_check" if state.get("runnable") else "end"


def route_after_parse(state: State) -> str:
    return "extract" if state.get("runnable") else "end"


def route_after_extract(state: State) -> str:
    return "collect" if state.get("done") else "extract"


_pipeline_graph = None


def build_graph():
    """构建并缓存 langgraph 判型→解析校验→多 voter 提取→投票收尾流水线。"""
    global _pipeline_graph
    if _pipeline_graph is not None:
        return _pipeline_graph
    graph = StateGraph(State)
    graph.add_node("classify", classify_node)
    graph.add_node("parse_check", parse_check_node)
    graph.add_node("extract", extract_node)
    graph.add_node("collect", collect_node)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {"parse_check": "parse_check", "end": END},
    )
    graph.add_conditional_edges(
        "parse_check",
        route_after_parse,
        {"extract": "extract", "end": END},
    )
    graph.add_conditional_edges(
        "extract",
        route_after_extract,
        {"collect": "collect", "extract": "extract"},
    )
    graph.add_edge("collect", END)
    _pipeline_graph = graph.compile()
    return _pipeline_graph


def process_document(md_content: str) -> dict:
    """通过 langgraph 流水线处理单篇文书：判型→解析校验→多 voter 提取→投票收尾。"""
    state = build_graph().invoke({
        "doc": md_content,
        "runnable": False,
        "cls_result": {},
        "need_parse": False,
        "doc_type": "",
        "skill_name": "",
        "voter": 0,
        "attempt": 0,
        "cand": {},
        "fail_fields": [],
        "responses": [],
        "done": False,
        "final": {},
    })
    return state.get("final") or {}


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
2. 只要命中任意 9 类文书的关键词（如"传票"、"开庭通知"、"改期开庭"、"起诉状"、"判决书"、"举证通知"、"应诉通知"、"参加诉讼通知"等），"是否需解析"就必须为"是"。
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
def main(pdf_path: Path = PDF_PATH):
    """单文件解析接口：OCR 解析 → 走与 batch_test 相同的 langgraph 流水线 → 打印结果。"""
    pdf_path = pdf_path.resolve()
    assert pdf_path.exists(), f"文件不存在: {pdf_path}"
    print(f"待上传文件: {pdf_path} ({pdf_path.stat().st_size} bytes)")

    task_id = submit_parse_task(pdf_path)
    poll_task(task_id)
    result = get_parse_result(task_id)

    results = result.get("results", result)
    if not isinstance(results, dict) or not results:
        print("未解析到任何文件内容")
        return

    for name, info in results.items():
        md_content = info.get("md_content", "") if isinstance(info, dict) else str(info)
        print(f"解析文书: {name}（{len(md_content)} 字符）")
        final = process_document(md_content)
        print(json.dumps(final, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    pdf_arg = sys.argv[1] if len(sys.argv) > 1 else PDF_PATH
    main(Path(pdf_arg))
