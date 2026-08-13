"""init_schema

Revision ID: 801713bdc6d3
Revises: 
Create Date: 2026-08-13 13:44:32.792127

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '801713bdc6d3'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None




def upgrade() -> None:
    """初始架构：9 张表 + 索引 + CHECK + 触发器 + 状态字典数据（源自 migrations/001_schema.sql）。"""
    op.execute("""
-- ============================================================
-- 案件管理系统数据库重构 —— 001 初始架构（PostgreSQL 15+）
-- 表清单：
--   1. case_status_dict   案件状态字典表
--   2. case_type          业务类型表
--   3. standard_cause     标准案由表
--   4. sys_user           系统用户表
--   5. lawsuit_case       诉讼案件主表
--   6. case_file          案件文件关联表
--   7. ai_parse_task      AI 解析任务表
--   8. audit_log          操作审计日志表
--   9. case_key_date      案件关键日期时间线表
-- ============================================================

-- ============ 1. 案件状态字典表 ============
CREATE TABLE IF NOT EXISTS case_status_dict (
    code        VARCHAR(20)  PRIMARY KEY,
    en_name     VARCHAR(60)  NOT NULL UNIQUE,
    name        VARCHAR(60)  NOT NULL,
    description VARCHAR(200),
    sort_order  SMALLINT     NOT NULL DEFAULT 0
);
COMMENT ON TABLE case_status_dict IS '案件状态字典表';

INSERT INTO case_status_dict (code, en_name, name, description, sort_order) VALUES
('S1','PENDING_FILING','立案待处理','案件刚创建，待确认立案',1),
('S2','FILED','已立案/受理中','法院/仲裁委已立案',2),
('S3','DEFENSE_EVIDENCE','答辩举证阶段','需提交答辩状、举证',3),
('S4','TRIAL','开庭审理阶段','已排期开庭或正在审理',4),
('S5','JUDGMENT_ISSUED','裁判作出阶段','裁判已作出但未生效',5),
('S6','FIRST_INSTANCE_CLOSED','一审结案','一审判决送达，上诉期内',6),
('S7','EFFECTIVE_CLOSED','生效结案','判决已生效，案件终结',7),
('S8','SECOND_DEFENSE_EVIDENCE','二审-答辩举证阶段','二审中的答辩举证',8),
('S9','SECOND_INSTANCE_CLOSED','二审结案','二审裁判作出',9),
('S10','ENFORCEMENT','强制执行阶段','进入执行程序',10),
('S11','ENFORCEMENT_CLOSED','执行结案','执行完毕',11)
ON CONFLICT (code) DO NOTHING;

-- ============ 2. 业务类型表 ============
CREATE TABLE IF NOT EXISTS case_type (
    id          BIGSERIAL PRIMARY KEY,
    type_name   VARCHAR(100) NOT NULL,
    description VARCHAR(200),
    sort_order  INT DEFAULT 0,
    status      SMALLINT DEFAULT 1,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_case_type_status CHECK (status IN (0,1))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_case_type_name_active ON case_type(type_name) WHERE status = 1;
COMMENT ON TABLE case_type IS '业务类型表';

-- ============ 3. 标准案由表 ============
CREATE TABLE IF NOT EXISTS standard_cause (
    id           BIGSERIAL PRIMARY KEY,
    case_type_id BIGINT NOT NULL REFERENCES case_type(id),
    cause_name   VARCHAR(200) NOT NULL,
    cause_code   VARCHAR(50)  NOT NULL,
    sort_order   INT DEFAULT 0,
    status       SMALLINT DEFAULT 1,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_standard_cause_type_name UNIQUE (case_type_id, cause_name),
    CONSTRAINT uq_standard_cause_code UNIQUE (cause_code),
    CONSTRAINT chk_standard_cause_status CHECK (status IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_standard_cause_type_id ON standard_cause(case_type_id);
COMMENT ON TABLE standard_cause IS '标准案由表';

-- ============ 4. 系统用户表 ============
CREATE TABLE IF NOT EXISTS sys_user (
    id            BIGSERIAL PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL,
    phone         VARCHAR(20),
    real_name     VARCHAR(50)  NOT NULL,
    password_hash VARCHAR(200) NOT NULL,
    role          VARCHAR(50)  NOT NULL DEFAULT 'user',
    status        SMALLINT DEFAULT 1,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_sys_user_status CHECK (status IN (0,1)),
    CONSTRAINT chk_sys_user_role CHECK (role IN ('admin','manager','user'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sys_user_username_active ON sys_user(username) WHERE status = 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_sys_user_phone_active ON sys_user(phone) WHERE status = 1 AND phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sys_user_role ON sys_user(role);
COMMENT ON TABLE sys_user IS '系统用户表';

-- ============ 5. 案件主表 ============
CREATE TABLE IF NOT EXISTS lawsuit_case (
    id                       BIGSERIAL PRIMARY KEY,
    case_no                  VARCHAR(50)  NOT NULL,
    court_case_no            VARCHAR(100),
    document_type            VARCHAR(100),
    document_type_confidence DECIMAL(5,2),
    plaintiff                VARCHAR(200),
    defendant                VARCHAR(200),
    involved_parties         JSONB,
    involved_clients         JSONB,
    case_type_id             BIGINT REFERENCES case_type(id),
    case_type_name           VARCHAR(100),
    standard_cause_id        BIGINT REFERENCES standard_cause(id),
    standard_cause_name      VARCHAR(200),
    case_description         TEXT,
    court_name               VARCHAR(200),
    court_code               VARCHAR(50),
    current_status           VARCHAR(20) NOT NULL DEFAULT 'S1' REFERENCES case_status_dict(code),
    status_updated_at        TIMESTAMPTZ,
    assigned_contact         VARCHAR(50),
    assigned_contact_id      BIGINT,
    assign_type              SMALLINT,
    claim_amount             DECIMAL(15,2),
    risk_level               VARCHAR(20),
    urgency_level            VARCHAR(20),
    deadline_date            DATE,
    deadline_type            VARCHAR(50),
    remark                   TEXT,
    source_type              SMALLINT NOT NULL DEFAULT 3,
    source_file_id           BIGINT,
    status_flag              SMALLINT NOT NULL DEFAULT 1,
    created_by               VARCHAR(50),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_case_no_format CHECK (case_no ~ '^CASE-[0-9]{8}-[0-9]{4}$'),
    CONSTRAINT chk_doc_type_confidence CHECK (document_type_confidence BETWEEN 0 AND 100),
    CONSTRAINT chk_source_type CHECK (source_type IN (1,2,3)),
    CONSTRAINT chk_assign_type CHECK (assign_type IN (1,2,3)),
    CONSTRAINT chk_risk_level CHECK (risk_level IN ('high','medium','low')),
    CONSTRAINT chk_urgency_level CHECK (urgency_level IN ('urgent','normal','low')),
    CONSTRAINT chk_status_flag CHECK (status_flag IN (0,1)),
    CONSTRAINT chk_deadline_type CHECK (deadline_type IN
        ('filing','evidence_deadline','trial','judgment','judgment_served','effective','enforcement_apply','enforcement_close'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_case_no_active ON lawsuit_case(case_no) WHERE status_flag = 1;
CREATE INDEX IF NOT EXISTS idx_court_case_no  ON lawsuit_case(court_case_no);
CREATE INDEX IF NOT EXISTS idx_current_status ON lawsuit_case(current_status);
CREATE INDEX IF NOT EXISTS idx_created_at     ON lawsuit_case(created_at);
CREATE INDEX IF NOT EXISTS idx_source_type    ON lawsuit_case(source_type);
COMMENT ON TABLE lawsuit_case IS '诉讼案件主表';

-- ============ 6. 案件文件关联表（case_id 可空，支持先传后挂） ============
CREATE TABLE IF NOT EXISTS case_file (
    id            BIGSERIAL PRIMARY KEY,
    case_id       BIGINT REFERENCES lawsuit_case(id),
    file_name     VARCHAR(500) NOT NULL,
    file_path     VARCHAR(1000) NOT NULL,
    file_size     BIGINT,
    mime_type     VARCHAR(100),
    file_type     VARCHAR(20),
    file_category VARCHAR(50),
    ocr_text      TEXT,
    ai_result     JSONB,
    md5           VARCHAR(64),
    is_deleted    SMALLINT DEFAULT 0,
    upload_by     VARCHAR(50),
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_file_category CHECK (file_category IN ('core_legal','evidence','procedural')),
    CONSTRAINT chk_file_is_deleted CHECK (is_deleted IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_case_file_case_id ON case_file(case_id);
CREATE INDEX IF NOT EXISTS idx_case_file_category ON case_file(file_category);
COMMENT ON TABLE case_file IS '案件文件关联表';

-- ============ 7. AI 解析任务表 ============
CREATE TABLE IF NOT EXISTS ai_parse_task (
    id            BIGSERIAL PRIMARY KEY,
    file_id       BIGINT REFERENCES case_file(id),
    task_type     VARCHAR(50),
    task_status   VARCHAR(20) DEFAULT 'pending',
    input_text    TEXT,
    output_result JSONB,
    confidence    DECIMAL(5,2),
    error_message TEXT,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_task_status CHECK (task_status IN ('pending','processing','completed','failed'))
);
CREATE INDEX IF NOT EXISTS idx_ai_parse_task_file_id ON ai_parse_task(file_id);
CREATE INDEX IF NOT EXISTS idx_ai_parse_task_status ON ai_parse_task(task_status);
COMMENT ON TABLE ai_parse_task IS 'AI 解析任务表';

-- ============ 8. 操作审计日志表 ============
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT REFERENCES sys_user(id),
    user_name     VARCHAR(50),
    action_type   VARCHAR(50) NOT NULL,
    target_type   VARCHAR(50),
    target_id     BIGINT,
    action_detail JSONB,
    ip_address    VARCHAR(50),
    created_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_action_type CHECK (action_type IN ('CREATE','UPDATE','DELETE','LOGIN','EXPORT')),
    CONSTRAINT chk_target_type CHECK (target_type IN ('CASE','FILE','MAPPING','SYSTEM'))
);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action_type ON audit_log(action_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_target_type ON audit_log(target_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
COMMENT ON TABLE audit_log IS '操作审计日志表';

-- ============ 9. 案件关键日期时间线表 ============
CREATE TABLE IF NOT EXISTS case_key_date (
    id             BIGSERIAL PRIMARY KEY,
    case_id        BIGINT NOT NULL REFERENCES lawsuit_case(id),
    date_type      VARCHAR(50)  NOT NULL,
    date_type_name VARCHAR(100) NOT NULL,
    key_date       DATE,
    status_code    VARCHAR(20),
    remark         VARCHAR(200),
    created_at     TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_date_type CHECK (date_type IN
        ('filing','evidence_deadline','trial','judgment','judgment_served','effective','enforcement_apply','enforcement_close'))
);
CREATE INDEX IF NOT EXISTS idx_case_key_date_case_id ON case_key_date(case_id);
CREATE INDEX IF NOT EXISTS idx_case_key_date_status ON case_key_date(status_code);
COMMENT ON TABLE case_key_date IS '案件关键日期时间线表（随状态推进追加）';

-- ============ 统一 updated_at 触发器 ============
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lawsuit_case_updated ON lawsuit_case;
CREATE TRIGGER trg_lawsuit_case_updated
    BEFORE UPDATE ON lawsuit_case
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_sys_user_updated ON sys_user;
CREATE TRIGGER trg_sys_user_updated
    BEFORE UPDATE ON sys_user
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_case_file_updated ON case_file;
CREATE TRIGGER trg_case_file_updated
    BEFORE UPDATE ON case_file
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_case_type_updated ON case_type;
CREATE TRIGGER trg_case_type_updated
    BEFORE UPDATE ON case_type
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_standard_cause_updated ON standard_cause;
CREATE TRIGGER trg_standard_cause_updated
    BEFORE UPDATE ON standard_cause
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


""")


def downgrade() -> None:
    """回滚：删除触发器、函数与全部新表（逆序）。"""
    op.execute("DROP TRIGGER IF EXISTS trg_standard_cause_updated ON standard_cause")
    op.execute("DROP TRIGGER IF EXISTS trg_case_type_updated ON case_type")
    op.execute("DROP TRIGGER IF EXISTS trg_case_file_updated ON case_file")
    op.execute("DROP TRIGGER IF EXISTS trg_sys_user_updated ON sys_user")
    op.execute("DROP TRIGGER IF EXISTS trg_lawsuit_case_updated ON lawsuit_case")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.execute('DROP TABLE IF EXISTS case_key_date CASCADE')
    op.execute('DROP TABLE IF EXISTS audit_log CASCADE')
    op.execute('DROP TABLE IF EXISTS ai_parse_task CASCADE')
    op.execute('DROP TABLE IF EXISTS case_file CASCADE')
    op.execute('DROP TABLE IF EXISTS lawsuit_case CASCADE')
    op.execute('DROP TABLE IF EXISTS sys_user CASCADE')
    op.execute('DROP TABLE IF EXISTS standard_cause CASCADE')
    op.execute('DROP TABLE IF EXISTS case_type CASCADE')
    op.execute('DROP TABLE IF EXISTS case_status_dict CASCADE')
