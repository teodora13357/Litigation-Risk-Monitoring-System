import json
import re
import time
import unicodedata
from decimal import Decimal, ROUND_CEILING
from functools import lru_cache
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


def _cn_amount(s: str):
    """汉字金额串 → 数值（如 二千→2000、贰仟元→2000、两万→20000、一万零三百→10300、十万→100000）。
    仅当串含数量级单位（十/百/千/万/亿）或带 元/人民币/整 后缀时才判定为金额，避免误识别日期等；
    解析失败返回 None。"""
    t = (s or "").strip()
    if not t:
        return None
    had_unit_suffix = False
    if t.startswith("人民币"):
        t = t[len("人民币"):].strip()
    changed = True
    while changed and t:
        changed = False
        for suf in ["元", "人民币", "整"]:
            if t.endswith(suf):
                had_unit_suffix = True
                t = t[: -len(suf)].strip()
                changed = True
    if not t:
        return None
    if not any(c in _CN_SCALE_CHARS for c in t) and not had_unit_suffix:
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
    return total if total > 0 else None


# 汉字金额 token 正则（原文扫描用）：中文数字串 + 可选 元/人民币/整 后缀
_CN_AMOUNT_RE = re.compile(r"[零〇○一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億]+(?:\s*(?:元|人民币|整))?")


@lru_cache(maxsize=8)
def _text_number_tokens(text: str) -> tuple:
    """原文中的金额 token 数值列表（含 万元/亿元 等单位换算 + 汉字金额归一化），按原文缓存供数字字段溯源复用。
    全文只扫描一次。"""
    out = []
    for m in re.finditer(r"[\d，,．.][\d，,．.]*(?:\s*(?:万元|亿元|元|人民币|￥|¥))?", text):
        v = _parse_amount(m.group(0))
        if v is not None:
            out.append(v)
    for m in _CN_AMOUNT_RE.finditer(text):
        v = _parse_amount(m.group(0))
        if v is not None:
            out.append(v)
    return tuple(out)


def _amount_token_match(target, text: str) -> bool:
    """目标金额是否与原文某个数字 token 数值相等（含 万元/亿元 换算、汉字金额归一化，如 80000 可匹配 "8万元"、2000 可匹配 "二千"）。"""
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
AMOUNT_TOKEN_RE = r"\d[\d，,．.]*\s*(?:亿元|万元|元|人民币|￥|¥)"  # 带单位才算金额，排除年份/案号


@lru_cache(maxsize=8)
def _source_amount_values(text: str, window: int = 100) -> tuple:
    """原文金额候选（含来源片段）：各 CLAIM_TRIGGERS 触发词后 window 字符内带单位的金额（应加算的部分）。
    触发词覆盖诉讼请求区 / 标的额 / 赔偿金 / 裁判区（与各 skill 字段触发词对齐）；
    返回 ((数值, 原文片段), ...) 按数值去重，片段供重生成提示让模型按多段原文复核。
    按原文缓存（provenance_fails/_regen_hint/restore_amount_precision 反复调用，全文只扫描一次）；
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
                ctx = text[max(0, abs_start - 45): abs_end + 15]
                vals.append((v, ctx.replace("\n", "⏎").replace("\r", "")))
            for am in _CN_AMOUNT_RE.finditer(seg):
                v = _parse_amount(am.group(0))
                if v is None or v <= 0 or v in seen:
                    continue
                seen.add(v)
                abs_start = base + am.start()
                abs_end = base + am.end()
                ctx = text[max(0, abs_start - 45): abs_end + 15]
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
    - 数字字段：数值与原文某数字 token 精确相等（含 万元/亿元 换算）；或字段∈SUMMED_FIELDS 且值等于原文加算金额候选的子集和（精确）
    - 文本字段：NFKC+去空白归一化后为原文子串（日期类字段支持年月日数字匹配）
    - null/空/无法解析的值跳过（视为通过）"""
    claim_vals = [v for v, _ in _source_amount_values(text)]
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
                    continue
                target = parsed
            # 1) 单值精确匹配（数值相等，含 万元/亿元 换算）
            if _amount_token_match(target, text):
                continue
            # 2) 加算：子集和（按最大小数位缩放为整数，保留全部小数精确比较）
            if f in SUMMED_FIELDS:
                ints, itarget, _, _ = _scale_to_int(claim_vals, target)
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


def restore_amount_precision(value, text: str, field: str):
    """把被模型四舍五入（如 832552.67）的金额字段值还原为原文的全精度值（如 832552.665）；
    无对应原文金额时原样返回。"""
    if value is None:
        return value
    target = value if isinstance(value, (int, float)) else _parse_amount(value)
    if target is None:
        return value
    claim_vals = [v for v, _ in _source_amount_values(text)]
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
    """为重生成拼接溯源提示：数字字段给出还原金额，并按多段原文片段让模型复核；
    文本字段提示严格按原文提取。"""
    cands = _source_amount_values(text)
    segs = "；".join(f"[{v}元]「{ctx}」" for v, ctx in cands[:5])
    parts = []
    for f in fails:
        v = cand.get(f)
        if f in NUMBER_FIELDS:
            hint = f"{f} 的提取值 {v} 无法由原文金额验证"
            restored = restore_amount_precision(v, text, f) if v is not None and str(v).strip() else None
            if restored is not None and str(restored) != str(v):
                hint += f"，请按原文还原值 {restored}元 输出"
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


def extract_voter(skill_name: str, text: str) -> dict:
    """单个 voter：提取并做全字段溯源布尔判定（除 SKIP_PROVENANCE_FIELDS），返回溯源合格的结果。"""
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
        fails = provenance_fails(cand, text)
        if not fails:
            break
        details = []
        for f in fails:
            hit, snip = _best_source_snippet(cand.get(f), text, f)
            details.append(f"{f}={cand.get(f)}（命中 {hit}，原文「{snip}」）")
        print(f"[voter] 字段溯源不通过 {'; '.join(details)}，重生成 {attempt+1}/{MAX_REGEN+1}")
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

    # 第二步：按类型加载 skill 提取（每 voter 字段溯源布尔判定兜底重生成，投票后每字段包装 value/置信度）
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
