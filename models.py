from sqlalchemy import Column, Integer, String, Float, Boolean, Text

from database import Base


class Case(Base):
    __tablename__ = "cases"

    id = Column(Integer, primary_key=True, index=True)
    delivery_time = Column(String(50), comment="送达时间")
    case_number = Column(String(100), index=True, comment="案号")
    case_type = Column(String(50), comment="案件类型")
    involved_parties = Column(String(200), comment="涉及主体")
    court_time = Column(String(50), comment="开庭时间")
    court_location = Column(String(200), comment="开庭地点")
    contact_phone = Column(String(50), comment="联系电话")
    plaintiff = Column(String(100), comment="原告")
    id_number = Column(String(50), comment="身份证号")
    defendant = Column(String(100), comment="被告")
    service_client = Column(String(100), comment="服务客户")
    stage = Column(String(50), comment="阶段")
    client_contact = Column(String(100), comment="客户对接人")
    processing_status = Column(String(50), comment="处理情况")
    handler = Column(String(100), comment="处理人")
    defense_method = Column(String(100), comment="答辩方式")
    is_closed = Column(Boolean, default=False, comment="是否结案")
    judgment_result = Column(String(500), comment="判决结果")
    compensation_amount = Column(Float, default=0, comment="赔偿金额")

# ===== 案件表单新增字段（手动录入） =====
    case_code = Column(String(30), comment="案件编号(自动生成)")
    document_type = Column(String(50), comment="文书类型")
    business_type = Column(String(50), comment="业务类型")
    standard_cause = Column(String(100), comment="标准案由")
    accept_court = Column(String(200), comment="受理法院/仲裁委")
    case_status = Column(String(50), comment="案件状态")
    cause_description = Column(Text, comment="案由描述")
    key_dates = Column(Text, comment="关键日期(JSON)")
    remark = Column(Text, comment="备注")

# ===================== 案件文件模型（三类文件表，全局存储） =====================


class BaseFile(Base):
    """三类文件表的公共字段（抽象基类，不建表）"""
    __abstract__ = True

    id = Column(Integer, primary_key=True, index=True)
    file_name = Column(String(255), comment="文件名")
    file_size = Column(Integer, default=0, comment="文件大小(字节)")
    file_path = Column(String(500), comment="文件存储路径")
    mime_type = Column(String(100), comment="MIME类型")
    uploader = Column(String(100), default="", comment="上传人")
    uploaded_at = Column(String(50), comment="上传日期")
    case_id = Column(Integer, index=True, comment="关联案件ID")
    is_deleted = Column(Boolean, default=False, comment="软删除标记")
    etag = Column(String(255), comment="OSS ETag")
    md5 = Column(String(64), comment="文件MD5")


class CoreDocument(BaseFile):
    """核心法定文书：需识别提取字段（识别逻辑暂未实现，接口保留）"""
    __tablename__ = "core_documents"

    extracted_fields = Column(Text, comment="提取的字段(JSON)")


class EvidenceMaterial(BaseFile):
    """证据材料：仅存储"""
    __tablename__ = "evidence_materials"


class ProceduralNotice(BaseFile):
    """程序性告知附件：仅存储"""
    __tablename__ = "procedural_notices"


# ===================== 用户模型（认证） =====================


class User(Base):
    """系统用户：支持用户名或手机号登录，含失败锁定信息。"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, comment="用户名")
    phone = Column(String(20), unique=True, index=True, comment="手机号")
    password_hash = Column(String(200), comment="密码哈希(PBKDF2)")
    failed_attempts = Column(Integer, default=0, comment="连续失败次数")
    locked_until = Column(String(50), nullable=True, comment="锁定截止时间")
    created_at = Column(String(50), comment="创建时间")
    role = Column(String(20), default="user", comment="角色: admin/manager/user")
