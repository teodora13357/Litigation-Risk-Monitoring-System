"""skills 目录统一加载器：字段清单单一事实来源 + skill 解析/别名/黑名单。

设计约定：
- ``SKILLS_ROOT`` 通过 ``Path(__file__).parents[1] / "prompts" / "skills"`` 自定位，不依赖 cwd。
- 字段清单优先读各 ``SKILL.md`` frontmatter 的 ``fields``；无 ``fields`` 时回退
  ``ref.json`` 的 ``properties`` 键（并去掉「提取说明」）。
- ``load_skill`` 不缓存（改 prompt 即时生效）；``skill_fields`` 按 skill 名缓存，
  修改 frontmatter/ref.json 后需 ``skill_fields.cache_clear()`` 或重启进程。
- ``archived-field-extraction`` 在黑名单中，不参与解析/模糊匹配。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "prompts" / "skills"

# 文书类型关键词 -> 提取 skill 目录名（子串匹配，容忍分类输出的措辞差异）
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

# 中文别名/简称 -> skill 目录名
SKILL_ALIASES = {
    "传票": "summons-hearing-extract",
    "开庭通知": "summons-hearing-extract",
    "起诉状": "complaint-arbitration-extract",
    "仲裁申请书": "complaint-arbitration-extract",
    "举证": "evidence-notice-extract",
    "应诉": "defense-notice-extract",
    "判决": "judgment-ruling-mediation-extract",
    "裁定": "judgment-ruling-mediation-extract",
    "调解": "judgment-ruling-mediation-extract",
    "上诉状": "appeal-extract",
    "分类": "file-type-classification",
}

ARCHIVED_SKILLS = {"archived-field-extraction"}


def parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter（仅覆盖 name/description/fields）。

    为保持零额外依赖，此处用受限解析器而非 PyYAML；frontmatter 值统一加双引号即可。
    """
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    block = parts[1]
    data: dict = {}
    fields: list[str] = []
    in_fields = False
    for raw in block.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("fields:"):
            in_fields = True
            rest = stripped[len("fields:"):].strip()
            if rest:
                fields.append(_unquote(rest))
            continue
        if in_fields:
            if stripped.startswith("-"):
                fields.append(_unquote(stripped[1:].strip()))
                continue
            in_fields = False
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            data[key.strip()] = _unquote(value.strip())
    if fields:
        data["fields"] = fields
    return data


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _skill_dirs() -> list[Path]:
    return sorted(
        d
        for d in SKILLS_ROOT.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists() and d.name not in ARCHIVED_SKILLS
    )


def list_skills() -> list[str]:
    """列出可用 skill 目录名（排除归档黑名单）。"""
    return [d.name for d in _skill_dirs()]


def resolve_skill(name: str) -> str | None:
    """把用户/模型给出的名称解析为 skill 目录名；找不到返回 None。"""
    name = str(name or "").strip().strip("/").strip(".")
    if not name:
        return None
    if name not in ARCHIVED_SKILLS and (SKILLS_ROOT / name / "SKILL.md").exists():
        return name
    for d in _skill_dirs():
        if d.name in name or name in d.name:
            return d.name
    for alias, skill in SKILL_ALIASES.items():
        if alias in name and (SKILLS_ROOT / skill / "SKILL.md").exists():
            return skill
    return None


def load_skill(name: str) -> str:
    """返回 skill 的 SKILL.md 全文（不缓存，热更新即时生效）。找不到时返回可用列表提示。"""
    resolved = resolve_skill(name)
    if resolved is None:
        return f"未找到 skill '{name}'，可用: {list_skills()}"
    skill_file = SKILLS_ROOT / resolved / "SKILL.md"
    if not skill_file.exists():
        return f"未找到 skill '{name}'，可用: {list_skills()}"
    return skill_file.read_text(encoding="utf-8")


def skill_for_doc_type(doc_type: str) -> str | None:
    """按文书类型关键词子串匹配，返回提取 skill 目录名。"""
    for keyword, skill in TYPE_TO_SKILL:
        if keyword in doc_type:
            return skill
    return None


@lru_cache(maxsize=32)
def skill_fields(name: str) -> list[str]:
    """字段清单单一事实来源：frontmatter ``fields``，无则回退 ``ref.json`` 的 properties 键。"""
    resolved = resolve_skill(name)
    if not resolved:
        return []
    skill_file = SKILLS_ROOT / resolved / "SKILL.md"
    frontmatter = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    fields = frontmatter.get("fields")
    if fields:
        return list(fields)
    ref_file = SKILLS_ROOT / resolved / "ref.json"
    if ref_file.exists():
        schema = json.loads(ref_file.read_text(encoding="utf-8"))
        for branch in schema.get("oneOf", []):
            props = branch.get("properties", {})
            if "错误" in props:
                continue
            return [key for key in props if key != "提取说明"]
    return []


def available_skills_docstring() -> str:
    """根据各 skill frontmatter 自动生成「Available skills」列表。"""
    lines = ["读取指定 skill 的完整指令（SKILL.md 全文）。", "", "Available skills:"]
    for name in list_skills():
        skill_file = SKILLS_ROOT / name / "SKILL.md"
        frontmatter = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        description = frontmatter.get("description", name)
        lines.append(f"    - {name}: {description}")
    return "\n".join(lines)
