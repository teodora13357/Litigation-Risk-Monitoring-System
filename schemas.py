from typing import Optional, List
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


# ============================================================
# 数据库重构：LawsuitCase（诉讼案件主表）请求/响应模型
# ============================================================


class KeyDateInput(BaseModel):
    """案件关键日期时间线输入（单条）"""
    date_type: str
    key_date: date
    status_code: Optional[str] = None
    remark: Optional[str] = None


class LawsuitCaseBase(BaseModel):
    court_case_no: Optional[str] = None           # 案号
    document_type: Optional[str] = None           # 文书类型
    plaintiff: Optional[str] = None               # 原告（申请人）
    defendant: Optional[str] = None               # 被告（被申请人）
    involved_parties: Optional[List[dict]] = None # 涉及主体 JSONB
    involved_clients: Optional[List[dict]] = None # 涉及客户 JSONB
    case_type_id: Optional[int] = None            # 业务类型ID
    case_type_name: Optional[str] = None          # 业务类型名称（快照）
    standard_cause_id: Optional[int] = None       # 标准案由ID
    standard_cause_name: Optional[str] = None     # 标准案由名称（快照）
    case_description: Optional[str] = None        # 案由描述
    court_name: Optional[str] = None              # 受理法院/仲裁委
    current_status: Optional[str] = "S1"          # 案件状态：接受状态码(S1)或中文名，后端统一映射为码
    assigned_contact: Optional[str] = None        # 对接人
    assign_type: Optional[int] = None             # 分配方式：1自动匹配/2手动指定/3规则匹配
    claim_amount: Optional[float] = None          # 涉案金额
    risk_level: Optional[str] = None              # 风险等级 high/medium/low（可传中文）
    urgency_level: Optional[str] = None           # 紧急程度 urgent/normal/low（可传中文）
    deadline_date: Optional[date] = None          # 当前期限快照日期
    deadline_type: Optional[str] = None           # 当前期限快照类型
    remark: Optional[str] = None                  # 备注
    key_dates: Optional[List[KeyDateInput]] = None  # 关键日期时间线（随状态追加）


class LawsuitCaseCreate(LawsuitCaseBase):
    court_case_no: str  # 案号必填


class LawsuitCaseUpdate(LawsuitCaseBase):
    pass


class LawsuitCaseResponse(LawsuitCaseBase):
    id: int
    case_no: str                        # 系统生成编号
    current_status: str                 # 状态码
    current_status_name: Optional[str] = None  # 状态中文名
    source_type: Optional[int] = None
    source_file_id: Optional[int] = None
    status_flag: Optional[int] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
