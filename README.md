# 案件管理系统

## 功能特性

- 案件增查删改：多条件筛选、分页、排序，Excel 导出
- 用户认证与角色权限：JWT 登录（`admin` / `manager` / `user` 三种角色）
- 文书识别：PDF/图片经本地 MinerU OCR 真实识别，结构化输出（含页码），自动提取案件字段
- 案件文件管理：三类文书（核心法定文书 / 证据材料 / 程序性告知附件）分类存储、树形浏览、在线预览、软删除
- 操作审计：关键操作写入审计日志，可按条件查询
- 部署友好：支持 Docker Compose 一键部署（app + PostgreSQL + Redis + MinerU），配置全部通过环境变量注入

## 环境要求

- Python 3.10 及以上
- PostgreSQL 14 及以上（本地或服务器均可）
- Redis 7（可选，预留缓存模块，未配置不影响运行）
- MinerU OCR 服务（可选，PDF/图片识别需要，默认 `http://localhost:30000`）
- Docker + Docker Compose（可选，容器化一键部署）

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/Olof233/demo.git
cd demo
```

### 2. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows 使用: .venv\Scripts\activate
pip install -r requirements.txt
```

> 依赖统一由 `pyproject.toml` 管理，`requirements.txt` 仅作为入口。也可以直接：
>
> ```bash
> pip install .        # 生产安装
> pip install -e .[dev]  # 开发安装（含 pytest / httpx）
> ```
>
> OCR 增强依赖（pdfplumber / pytesseract / langchain-openai 等）见 `pyproject.toml` 的 `[project.optional-dependencies].ocr`，按需 `pip install -e .[ocr]`。

## 配置 PostgreSQL

### 创建数据库（应用不会自动创建数据库，需先手动创建）

```sql
CREATE DATABASE case_management;
```

> 应用启动时会通过 SQLAlchemy 自动创建 `cases` 表，但数据库本身需要先存在。

### 设置连接环境变量

| 环境变量 | 说明 | 默认值 |
| ---- | ---- | ---- |
| `PG_HOST` | PostgreSQL 主机地址 | `127.0.0.1` |
| `PG_PORT` | PostgreSQL 端口 | `5432` |
| `PG_USER` | 用户名 | `case_app` |
| `PG_PASSWORD` | 密码 | `case_pass_123` |
| `PG_DB` | 数据库名 | `case_management` |
| `DATABASE_URL` | 完整连接串（可选，优先级最高） | 由上述变量自动拼装 |
| `REDIS_URL` | Redis 连接串（可选，预留缓存模块） | `redis://127.0.0.1:6379/0` |
| `SECRET_KEY` | JWT 签名密钥（生产务必修改） | `case-management-secret-key-change-me` |
| `MINERU_API_URL` | MinerU OCR 服务地址 | `http://localhost:30000` |
| `OCR_PARSE_TIMEOUT` | OCR 单批解析超时（秒） | `600` |
| `OCR_BATCH_PAGES` | OCR 每批提交页数 | `10` |
| `OCR_PREPROCESS_DPI` | OCR PDF 渲染 DPI | `300` |
| `OCR_MAX_IMAGE_SIDE` | OCR 图像长边上限（px） | `3500` |
| `OCR_BINARIZE` | OCR 二值化策略（`auto`/`true`/`false`） | `auto` |
| `OCR_TEXT_MIN_CHARS` | PDF 内嵌文本判定阈值（字符） | `30` |

> 项目根目录提供 `.env.example` 模板，可 `cp .env.example .env` 后按需修改；Docker Compose 部署同样读取该文件。

macOS / Linux 示例：

```bash
export PG_HOST=127.0.0.1
export PG_PORT=5432
export PG_USER=case_app
export PG_PASSWORD=你的密码
export PG_DB=case_management
```

Windows（PowerShell）示例：

```powershell
$env:PG_HOST="127.0.0.1"
$env:PG_PORT="5432"
$env:PG_USER="case_app"
$env:PG_PASSWORD="你的密码"
$env:PG_DB="case_management"
```

> 密码包含特殊字符时，连接串中的密码需要 URL 编码。也可以直接用 `DATABASE_URL` 整体覆盖：
>
> ```bash
> export DATABASE_URL="postgresql+psycopg2://user:pass@127.0.0.1:5432/case_management"
> ```

## 启动服务

在项目根目录执行：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

或：

```bash
python main.py   # 默认监听 127.0.0.1:8000
```

启动成功后访问：

- 网页端：http://127.0.0.1:8000
- 登录页：http://127.0.0.1:8000/login
- API 文档（Swagger UI）：http://127.0.0.1:8000/docs

### 默认账号（首次启动自动创建）

| 用户名 | 初始密码 | 角色 |
| ---- | ---- | ---- |
| `admin` | `admin123` | 管理员 |
| `manager` | `manager123` | 业务经理 |
| `user` | `user123` | 普通用户 |

> 默认账号写入 `sys_user` 表，登录返回 JWT（`POST /api/v1/auth/login`）。生产环境建议首次登录后及时修改初始密码并更换 `SECRET_KEY`。

## 项目结构

```
demo/
├── main.py            # 应用入口：FastAPI 实例、启动逻辑、前端页面、挂载路由
├── app/               # 应用包：所有业务代码集中在此
│   ├── database.py    #   PostgreSQL 连接与会话管理（环境变量配置）
│   ├── models.py      #   ORM 模型（LawsuitCase + 字典/文件/任务/审计/时间线表）
│   ├── schemas.py     #   Pydantic 请求/响应模型
│   ├── crud.py        #   数据访问层（增查删改、筛选、排序）
│   ├── auth.py        #   认证工具（密码哈希 / JWT / 角色权限）
│   ├── logging.py     #   统一结构化日志
│   ├── constants.py   #   业务枚举常量
│   ├── error_codes.py #   业务错误码与统一响应
│   ├── excel_export.py#   Excel 导出
│   ├── ocr_service.py #   OCR 识别服务（本地 MinerU）
│   ├── redis_client.py#   Redis 客户端（预留缓存模块）
│   ├── deps.py        #   依赖注入（get_db / get_current_user）
│   ├── utils.py       #   共享工具（案号校验 / 案件编号生成）
│   ├── audit.py       #   审计日志写入
│   ├── files_config.py#   文件类型配置与上传校验规则
│   ├── ocr_tasks.py   #   OCR 后台任务（内存任务表 + 识别执行）
│   └── routers/       #   业务路由：cases / files / recognize / export / auth / audit
├── index.html         # 前端单页（识别上传 + 案件文件管理）
├── static/
│   ├── chart.min.js   # 前端静态资源
│   └── uploads/       # 上传文件存储（按 YYYY/MM 分目录，git 忽略）
└── requirements.txt   # 依赖清单
```

其他根目录文件：

```
├── login.html         # 登录页前端
├── Dockerfile         # 应用镜像构建（python:3.12-slim）
├── docker-compose.yml # Docker Compose 编排（app + postgres + redis + mineru）
├── .env.example       # 环境变量模板
├── pyproject.toml     # Python 依赖与打包配置（依赖统一在此管理）
├── alembic/           # 数据库迁移脚本（Alembic，复用 database.py 的连接配置）
├── scripts/           # 运维脚本（deploy_preflight.py 部署预检 + MinerU 补丁）
├── skills/            # 法律文书解析技能提示词（类型判定 + 分类型字段提取规则）
├── testset/           # 评测集（5 类文书样例 PDF + 人工标注 GT + 识别结果）
├── batch_test.py      # 批量识别测试脚本（并发调用识别接口）
├── batch_eval.py      # 识别结果评测脚本（字段匹配率统计）
├── langchain.py       # LangChain 实验脚本（历史方案参考）
├── DEPLOY.md          # 详细部署指南（macOS / Linux / Windows）
├── 识别功能接入指南.md  # OCR 识别接入与替换协议说明
└── 测试结果总结.md     # 第一周交付评测基线报告
```

## API 接口

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/` | 前端页面 |
| POST | `/api/cases` | 新增案件（案号必填） |
| GET | `/api/cases` | 查询列表（支持多条件筛选、分页、排序） |
| GET | `/api/cases/{id}` | 查询单个案件 |
| PUT | `/api/cases/{id}` | 修改案件（部分更新） |
| DELETE | `/api/cases/{id}` | 删除案件 |
| GET | `/api/export/excel` | 导出全部案件为 Excel |
| POST | `/api/recognize` | PDF/图片 OCR 识别（真实识别，结构化输出含页码） |
| POST | `/api/files/upload` | 上传文件（按类型存储，核心文书识别字段） |
| GET | `/api/cases/{id}/files` | 查看案件所有文件（按类型分组） |
| GET | `/api/files/preview/{type}/{id}` | 预览文件（PDF/图片，内联显示） |
| DELETE | `/api/files/{type}/{id}` | 删除文件（默认软删除，`?hard=true` 物理删除） |
| POST | `/api/cases/{id}/match-files` | 根据识别字段匹配案件文件（预留） |
| GET | `/login` | 登录页面 |
| POST | `/api/v1/auth/login` | 用户登录（返回 JWT） |
| POST | `/api/v1/auth/logout` | 用户登出 |
| POST | `/api/v1/cases` | 创建诉讼案件（v1） |
| POST | `/api/v1/cases/manual` | 手动录入案件（v1） |
| POST | `/api/v1/files/upload` | 上传文件（v1） |
| POST | `/api/v1/ai/parse` | 触发 AI 文书解析（异步任务） |
| GET | `/api/v1/ai/parse/{task_id}` | 查询解析任务结果 |
| GET | `/api/files/{doc_type}/{file_id}/ocr` | 查询文件 OCR 识别状态（pending/running/done/failed） |
| GET | `/api/audit-logs` | 查询操作审计日志 |

## 案件文件管理功能

### PDF/图片识别上传
- 主页面支持拖拽或点击上传 PDF/图片
- 上传后选择文件所属类型：**核心法定文书**（识别字段）/ **证据材料**（仅存储）/ **程序性告知附件**（仅存储）
- 填写上传人，可关联案件（可选）
- 核心法定文书会模拟识别 19 个案件字段，可应用到表单并保存（真实识别逻辑预留，后续接入 OCR）
- 当前已接入真实 OCR（本地 MinerU）：核心法定文书上传后自动触发后台识别，识别状态与结果可通过 `GET /api/files/{id}/ocr` 查询，结果回写案件的 `ocr_text` / `ai_result` 字段

### 案件专属文件表
- 每个案件保存后自动创建专属索引表 `case_{案件ID}`，记录该案件三类文件的索引
- 三类文件全局存储于 `core_documents` / `evidence_materials` / `procedural_notices` 表
- 上传文件写入 `static/uploads/YYYY/MM/` 目录（按日期分片），文件名 UUID 化避免冲突

### 文件管理页面
- 通过案件选择器切换查看不同案件的文件
- 文件以树形目录按类型折叠/展开，展示文件名、大小、上传日期、上传人
- 支持 PDF 内嵌预览与图片预览
- 文件删除支持软删除（标记保留）与物理删除

## Docker Compose 一键部署

项目提供 `Dockerfile` 与 `docker-compose.yml`（app + postgres + redis + mineru 四个服务）：

```bash
# 1. 准备环境变量（务必修改 PG_PASSWORD 与 SECRET_KEY）
cp .env.example .env

# 2. 构建并启动基础服务
docker compose up -d --build

# 3. （可选）启动 MinerU OCR 服务（需先打补丁，见下文）
docker compose --profile ocr up -d

# 4. 验证
docker compose ps                    # 服务均 healthy
curl http://127.0.0.1:18080/         # 前端页面（应用映射到宿主机 18080）
curl http://127.0.0.1:18080/docs     # Swagger
curl http://127.0.0.1:30000/health   # MinerU 健康检查
```

端口映射与数据卷：

| 服务 | 宿主机端口 | 数据卷 | 说明 |
| ---- | ---- | ---- | ---- |
| app | `18080` -> `8000` | `upload_data`（上传文件持久化） | 应用镜像基于 `python:3.12-slim` |
| postgres | `127.0.0.1:15432` -> `5432` | `pg_data` | 仅本机可访问，避免暴露公网 |
| redis | `127.0.0.1:16379` -> `6379` | `redis_data` | 预留缓存，未设密码 |
| mineru（profile `ocr`） | `30000` | `./mineru_data` | CPU pipeline 后端，默认不启动 |

> `docker compose down` 保留数据卷；`docker compose down -v` 会清空数据库与上传文件，慎用。
> 首次部署可运行 `python scripts/deploy_preflight.py` 做端口冲突预检（PowerShell / bash 均兼容）。

## 数据库迁移（Alembic）

项目集成 Alembic（`alembic/` 目录），迁移环境复用 `app/database.py` 的 `DATABASE_URL`（即 `PG_*` / `DATABASE_URL` 环境变量）：

```bash
alembic upgrade head                          # 应用全部迁移
alembic revision --autogenerate -m "说明"     # 依据 ORM 模型生成新迁移
alembic history                               # 查看迁移历史
```

> 应用启动时仍会执行 `create_all` 自动补齐缺失的表；对已有表结构的变更请通过 Alembic 迁移管理。

## OCR 识别服务（MinerU）

- 识别链路：预处理（PDF 渲染 / 图像压缩 / 二值化）-> MinerU 解析 -> 结构化输出（`pages` / `structured_text` / `markdown` / `recognized_fields`），核心逻辑在 `app/ocr_service.py` 与 `app/ocr_tasks.py`
- 本地/容器 CPU 环境使用 **pipeline 后端**（`--backend pipeline --device cpu`）；hybrid-engine 依赖 CUDA GPU，Docker Desktop / 无 GPU 环境不可用
- ⚠️ MinerU 3.4.4 存在已知 bug（`dict() got multiple values for keyword argument 'backend'`），启动容器后必须为容器内 `fast_api.py` 打补丁：
  - 手工方式见 `DEPLOY.md`「常见问题排查」
  - 自动方式：`python scripts/deploy_preflight.py --patch`（幂等，容器重建后需重新执行）
- 识别参数（超时、批页数、DPI、二值化等）见上表 `OCR_*` 环境变量
- 历史方案与替换协议详见 `识别功能接入指南.md`

## 测试与评测

- `testset/`：评测集，含 5 类法律文书（起诉状/仲裁申请书、判决书/裁定书/调解书、应诉通知书/参加诉讼通知书、举证通知书、传票/开庭通知书）各 3 份样例 PDF 及人工标注 GT
- `batch_test.py`：批量识别测试脚本（并发调用识别接口，结果写入 `testset/*/…_results.json`）
- `batch_eval.py`：评测脚本，统计各文书类型的字段匹配率
- `skills/`：法律文书解析提示词（`AGENT.md` 定义类型判定 + 分类型字段提取流程，供 AI 解析方案使用）
- 当前基线：15 份文书、126 个字段，整体字段匹配率 **70.63%**（详见 `测试结果总结.md`）

## 相关文档

| 文档 | 说明 |
| ---- | ---- |
| `DEPLOY.md` | 详细部署指南：macOS 本地开发 / Linux Docker Compose / Windows（Docker Desktop + WSL2），含 19 条常见问题排查 |
| `识别功能接入指南.md` | OCR 识别接入方式、输入输出协议、历史 LangChain 方案背景 |
| `测试结果总结.md` | 第一周交付评测基线报告（各文书类型字段匹配率与问题清单） |
| `.env.example` | 全量环境变量模板（数据库 / Redis / JWT / OCR） |
| `skills/AGENT.md` | 法律文书类型判定与字段提取的提示词总则 |

## 常见问题

**1. 启动报错 `FATAL: database does not exist`**
先执行「创建数据库」一节的建库 SQL，再启动服务。

**2. 报错 `password authentication failed`**
检查 `PG_USER` / `PG_PASSWORD` 是否与 PostgreSQL 实际账号一致。

**3. 连接超时或被拒绝**
确认 PostgreSQL 服务已启动、端口正确；若部署在远程或容器中，需确认其允许该主机访问（listen_addresses 与防火墙设置）。

**4. 中文乱码**
PostgreSQL 默认 UTF8 编码，连接串无需额外参数。确保数据库以 UTF8 创建（见「创建数据库」一节）。

**5. pip 安装依赖缓慢**
可切换国内镜像：
```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**6. Docker Compose 部署后访问不到服务**
Compose 将应用映射到宿主机 `18080` 端口（PostgreSQL `15432`、Redis `16379` 仅绑定本机），请访问 http://127.0.0.1:18080 ；`docker compose ps` 确认各服务为 healthy，异常时用 `docker compose logs -f app` 排查。

**7. OCR 识别失败（提示「OCR识别失败，建议手动录入」）**
确认 MinerU 服务可达（`curl http://<MINERU_API_URL>/health`）；容器部署需先应用 MinerU 3.4.4 补丁（`python scripts/deploy_preflight.py --patch`，详见 `DEPLOY.md`）。

**8. 登录后接口返回 401**
检查请求是否携带 `Authorization: Bearer <token>`；`SECRET_KEY` 变更后已签发的 JWT 会全部失效，需重新登录。

## 部署到服务器

本项目的数据库配置全部通过环境变量注入，服务器上同样适用：

```bash
export PG_HOST=内网或公网PostgreSQL地址
export PG_USER=数据库账号
export PG_PASSWORD=数据库密码
export PG_DB=case_management
uvicorn main:app --host 0.0.0.0 --port 8000
```

生产环境建议使用多个 worker（`--workers 2`）并配合 Nginx 反向代理。

容器化部署（含 PostgreSQL / Redis / MinerU 编排、端口映射与数据卷说明）见上文「Docker Compose 一键部署」；各平台（macOS / Linux / Windows）完整部署步骤与常见问题排查见 [DEPLOY.md](DEPLOY.md)。
