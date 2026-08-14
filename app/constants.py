"""案件管理系统 —— 业务枚举常量。

与 migrations/001_schema.sql 中的字典表/CHECK 约束保持一致。
"""

# ===================== 案件状态（S1~S11） =====================
# code -> 中文名（与 case_status_dict 表对应）
CASE_STATUS = {
    "S1": "立案待处理",
    "S2": "已立案/受理中",
    "S3": "答辩举证阶段",
    "S4": "开庭审理阶段",
    "S5": "裁判作出阶段",
    "S6": "一审结案",
    "S7": "生效结案",
    "S8": "二审-答辩举证阶段",
    "S9": "二审结案",
    "S10": "强制执行阶段",
    "S11": "执行结案",
}
# 中文名 -> code（前端提交中文文本时反查）
CASE_STATUS_NAME_TO_CODE = {name: code for code, name in CASE_STATUS.items()}

# ===================== 案件状态字典表数据（case_status_dict） =====================
# 单一数据源：code -> (en_name, name, description, sort_order)
# 与 alembic/versions/801713bdc6d3_init_schema.py 的 INSERT 保持一致；
# 应用启动时按此数据同步 case_status_dict 表（见 main._sync_status_dict）。
CASE_STATUS_DICT = {
    "S1": ("PENDING_FILING", "立案待处理", "案件刚创建，待确认立案", 1),
    "S2": ("FILED", "已立案/受理中", "法院/仲裁委已立案", 2),
    "S3": ("DEFENSE_EVIDENCE", "答辩举证阶段", "需提交答辩状、举证", 3),
    "S4": ("TRIAL", "开庭审理阶段", "已排期开庭或正在审理", 4),
    "S5": ("JUDGMENT_ISSUED", "裁判作出阶段", "裁判已作出但未生效", 5),
    "S6": ("FIRST_INSTANCE_CLOSED", "一审结案", "一审判决送达，上诉期内", 6),
    "S7": ("EFFECTIVE_CLOSED", "生效结案", "判决已生效，案件终结", 7),
    "S8": ("SECOND_DEFENSE_EVIDENCE", "二审-答辩举证阶段", "二审中的答辩举证", 8),
    "S9": ("SECOND_INSTANCE_CLOSED", "二审结案", "二审裁判作出", 9),
    "S10": ("ENFORCEMENT", "强制执行阶段", "进入执行程序", 10),
    "S11": ("ENFORCEMENT_CLOSED", "执行结案", "执行完毕", 11),
}


def status_dict_rows() -> list[dict]:
    """返回 case_status_dict 表的字典行列表（供启动同步/接口使用）。"""
    return [
        {"code": code, "en_name": en_name, "name": name,
         "description": desc, "sort_order": order}
        for code, (en_name, name, desc, order) in CASE_STATUS_DICT.items()
    ]

# ===================== 对接人分配方式（assign_type） =====================
ASSIGN_TYPE = {
    1: "自动匹配",
    2: "手动指定",
    3: "规则匹配",
}

# ===================== 风险等级（risk_level） =====================
RISK_LEVEL = {
    "high": "高风险",
    "medium": "中风险",
    "low": "低风险",
}
RISK_LEVEL_NAME_TO_CODE = {name: code for code, name in RISK_LEVEL.items()}

# ===================== 紧急程度（urgency_level） =====================
URGENCY_LEVEL = {
    "urgent": "紧急",
    "normal": "一般",
    "low": "不急",
}
URGENCY_LEVEL_NAME_TO_CODE = {name: code for code, name in URGENCY_LEVEL.items()}

# ===================== 关键日期类型（date_type / deadline_type） =====================
DATE_TYPE = {
    "filing": "立案日期",
    "evidence_deadline": "举证期限",
    "trial": "开庭日期",
    "judgment": "裁判日期",
    "judgment_served": "判决送达日期",
    "effective": "生效日期",
    "enforcement_apply": "申请执行日期",
    "enforcement_close": "执行结案日期",
}

# ===================== 录入来源（source_type） =====================
SOURCE_TYPE = {
    1: "上传解析",
    2: "企查查",
    3: "手动录入",
}

# ===================== 文件分类（case_file.file_category） =====================
FILE_CATEGORY = {
    "core_legal": "核心法律文书",
    "evidence": "证据材料",
    "procedural": "程序性告知附件",
}

# ===================== AI 任务类型 / 状态（ai_parse_task） =====================
AI_TASK_TYPE = ["doc_type", "field_extract", "client_match", "status_match"]
AI_TASK_STATUS = ["pending", "processing", "completed", "failed"]

# ===================== 审计操作 / 对象类型（audit_log） =====================
AUDIT_ACTION_TYPE = ["CREATE", "UPDATE", "DELETE", "LOGIN", "EXPORT"]
AUDIT_TARGET_TYPE = ["CASE", "FILE", "MAPPING", "SYSTEM"]


def status_to_code(name_or_code: str) -> str:
    """案件状态：支持传中文名或状态码，统一返回状态码（S1~S11）。

    传入已是码则原样返回；传中文名则反查；都不匹配返回 'S1'。
    """
    if name_or_code in CASE_STATUS:
        return name_or_code
    return CASE_STATUS_NAME_TO_CODE.get(name_or_code, "S1")


def status_to_name(code: str) -> str:
    """案件状态码 -> 中文名；未知码返回原值。"""
    return CASE_STATUS.get(code, code)
