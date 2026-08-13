from sqlalchemy import Column, Integer, String, Text, Numeric, SmallInteger, Date, DateTime, Index
from sqlalchemy import func, ForeignKey

from database import Base


# ============================================================
# 数据库重构：新模型（诉讼案件全生命周期）
# 与 migrations/001_schema.sql 保持一致；旧模型（Case/三文件表/User）
# 在文件/OCR/认证代码迁移完成前保留，属过渡期。
# ============================================================

from sqlalchemy.dialects.postgresql import BIGINT, JSONB  # noqa: E402


class CaseStatusDict(Base):
    """案件状态字典表"""
    __tablename__ = "case_status_dict"

    code = Column(String(20), primary_key=True, comment="状态码 S1~S11")
    en_name = Column(String(60), nullable=False, unique=True, comment="英文名")
    name = Column(String(60), nullable=False, comment="中文名")
    description = Column(String(200), comment="描述")
    sort_order = Column(SmallInteger, nullable=False, default=0, comment="排序")


class CaseType(Base):
    """业务类型表"""
    __tablename__ = "case_type"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    type_name = Column(String(100), nullable=False, comment="类型名称（劳动仲裁/民事诉讼等）")
    description = Column(String(200), comment="描述")
    sort_order = Column(Integer, default=0, comment="排序")
    status = Column(SmallInteger, default=1, comment="1启用/0停用")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class StandardCause(Base):
    """标准案由表"""
    __tablename__ = "standard_cause"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    case_type_id = Column(BIGINT, ForeignKey("case_type.id"), nullable=False, comment="业务类型ID")
    cause_name = Column(String(200), nullable=False, comment="案由名称")
    cause_code = Column(String(50), nullable=False, comment="案由编码")
    sort_order = Column(Integer, default=0, comment="排序")
    status = Column(SmallInteger, default=1, comment="1启用/0停用")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SysUser(Base):
    """系统用户表"""
    __tablename__ = "sys_user"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    username = Column(String(50), nullable=False, comment="用户名")
    phone = Column(String(20), comment="手机号（可登录）")
    real_name = Column(String(50), nullable=False, comment="真实姓名")
    password_hash = Column(String(200), nullable=False, comment="密码哈希")
    role = Column(String(50), nullable=False, default="user", comment="admin/manager/user")
    status = Column(SmallInteger, default=1, comment="1启用/0停用")
    last_login_at = Column(DateTime(timezone=True), comment="最后登录时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LawsuitCase(Base):
    """诉讼案件主表"""
    __tablename__ = "lawsuit_case"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    case_no = Column(String(50), nullable=False, comment="案件编号（系统生成）")
    court_case_no = Column(String(100), comment="法院案号")
    document_type = Column(String(100), comment="文书类型")
    document_type_confidence = Column(Numeric(5, 2), comment="文书类型置信度(0-100)")
    plaintiff = Column(String(200), comment="原告（申请人）快照")
    defendant = Column(String(200), comment="被告（被申请人）快照")
    involved_parties = Column(JSONB, comment="涉及主体明细(JSONB)")
    involved_clients = Column(JSONB, comment="涉及客户明细(JSONB)")
    case_type_id = Column(BIGINT, ForeignKey("case_type.id"), comment="业务类型ID")
    case_type_name = Column(String(100), comment="业务类型名称快照")
    standard_cause_id = Column(BIGINT, ForeignKey("standard_cause.id"), comment="标准案由ID")
    standard_cause_name = Column(String(200), comment="标准案由名称快照")
    case_description = Column(Text, comment="案由描述")
    court_name = Column(String(200), comment="受理法院/仲裁委")
    court_code = Column(String(50), comment="法院代码")
    current_status = Column(String(20), ForeignKey("case_status_dict.code"),
                           nullable=False, default="S1", comment="案件状态码 S1~S11")
    status_updated_at = Column(DateTime(timezone=True), comment="状态更新时间")
    assigned_contact = Column(String(50), comment="对接人")
    assigned_contact_id = Column(BIGINT, comment="对接人ID")
    assign_type = Column(SmallInteger, comment="对接人分配方式：1自动匹配/2手动指定/3规则匹配")
    claim_amount = Column(Numeric(15, 2), comment="涉案金额")
    risk_level = Column(String(20), comment="风险等级 high/medium/low")
    urgency_level = Column(String(20), comment="紧急程度 urgent/normal/low")
    deadline_date = Column(Date, comment="当前期限快照日期")
    deadline_type = Column(String(50), comment="当前期限快照类型")
    remark = Column(Text, comment="备注")
    source_type = Column(SmallInteger, nullable=False, default=3, comment="1上传解析/2企查查/3手动录入")
    source_file_id = Column(BIGINT, comment="来源文件ID")
    status_flag = Column(SmallInteger, nullable=False, default=1, comment="1正常/0删除")
    created_by = Column(String(50), comment="创建人")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("uq_case_no_active", "case_no", unique=True, postgresql_where=status_flag == 1),
        Index("idx_court_case_no", "court_case_no"),
        Index("idx_current_status", "current_status"),
        Index("idx_created_at", "created_at"),
        Index("idx_source_type", "source_type"),
    )

    @property
    def current_status_name(self) -> str:
        """案件状态中文名（供响应展示，与 constants.CASE_STATUS 映射）。"""
        from constants import status_to_name
        return status_to_name(self.current_status)


class CaseFile(Base):
    """案件文件关联表"""
    __tablename__ = "case_file"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    case_id = Column(BIGINT, ForeignKey("lawsuit_case.id"), comment="关联案件ID（可空，支持先传后挂）")
    file_name = Column(String(500), nullable=False, comment="文件名")
    file_path = Column(String(1000), nullable=False, comment="文件存储路径")
    file_size = Column(BIGINT, comment="文件大小(字节)")
    mime_type = Column(String(100), comment="MIME类型")
    file_type = Column(String(20), comment="文件类型")
    file_category = Column(String(50), comment="core_legal/evidence/procedural")
    ocr_text = Column(Text, comment="OCR 原始文本")
    ai_result = Column(JSONB, comment="AI 结构化结果")
    md5 = Column(String(64), comment="文件MD5")
    is_deleted = Column(SmallInteger, default=0, comment="0正常/1删除")
    upload_by = Column(String(50), comment="上传人")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_case_file_case_id", "case_id"),
        Index("idx_case_file_category", "file_category"),
    )


class AiParseTask(Base):
    """AI 解析任务表"""
    __tablename__ = "ai_parse_task"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    file_id = Column(BIGINT, ForeignKey("case_file.id"), comment="关联文件ID")
    task_type = Column(String(50), comment="doc_type/field_extract/client_match/status_match")
    task_status = Column(String(20), default="pending", comment="pending/processing/completed/failed")
    input_text = Column(Text, comment="输入文本")
    output_result = Column(JSONB, comment="输出结果")
    confidence = Column(Numeric(5, 2), comment="置信度(0-100)")
    error_message = Column(Text, comment="错误信息")
    started_at = Column(DateTime(timezone=True), comment="开始时间")
    completed_at = Column(DateTime(timezone=True), comment="完成时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """操作审计日志表"""
    __tablename__ = "audit_log"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    user_id = Column(BIGINT, ForeignKey("sys_user.id"), comment="操作用户ID")
    user_name = Column(String(50), comment="操作用户名")
    action_type = Column(String(50), nullable=False, comment="CREATE/UPDATE/DELETE/LOGIN/EXPORT")
    target_type = Column(String(50), comment="CASE/FILE/MAPPING/SYSTEM")
    target_id = Column(BIGINT, comment="操作对象ID")
    action_detail = Column(JSONB, comment="操作详情（变更前后值）")
    ip_address = Column(String(50), comment="IP地址")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CaseKeyDate(Base):
    """案件关键日期时间线表"""
    __tablename__ = "case_key_date"

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    case_id = Column(BIGINT, ForeignKey("lawsuit_case.id"), nullable=False, comment="案件ID")
    date_type = Column(String(50), nullable=False, comment="日期类型（枚举）")
    date_type_name = Column(String(100), nullable=False, comment="日期类型中文名")
    key_date = Column(Date, comment="关键日期")
    status_code = Column(String(20), comment="发生时点对应的案件状态码")
    remark = Column(String(200), comment="备注")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_case_key_date_case_id", "case_id"),
        Index("idx_case_key_date_status", "status_code"),
    )


