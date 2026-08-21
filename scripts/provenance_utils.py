"""溯源（provenance）共享归一化逻辑，供 parse_doc / batch_test / batch_eval 复用。
位于 scripts/（可复用逻辑统一存放处，同 loader.py）。

- 文本归一化：NFKC（全角转半角、CJK 兼容字/部首统一到汉字）+ 部首补充块映射
  + 去空白 + 去除标点（保留小数点 `.`，全角 `．` 经 NFKC 统一为半角 `.`）。
- 日期类字段：额外支持年月日数字分组匹配（容忍中文/ISO/零填充格式差异）。
"""
import re
import unicodedata
from functools import lru_cache


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
_PUNCT_RE = re.compile(r"[，。、；：？！（）《》〈〉「」『』“”‘’‐‑‒–—―−…～·〔〕【】｛｝,;:!?()\[\]{}<>\"'`|/\\@#%&*+=^~_-]+")


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
