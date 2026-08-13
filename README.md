# 案件管理系统

## 环境要求

- Python 3.10 及以上
- PostgreSQL 14 及以上（本地或服务器均可）

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
- API 文档（Swagger UI）：http://127.0.0.1:8000/docs

## 项目结构

```
demo/
├── main.py            # 应用入口：FastAPI 实例、启动逻辑、前端页面、挂载路由
├── app/               # 应用包：按业务域拆分
│   ├── deps.py        #   依赖注入（get_db / get_current_user）
│   ├── utils.py       #   共享工具（案号校验 / 案件编号生成）
│   ├── audit.py       #   审计日志写入
│   ├── files_config.py#   文件类型配置与上传校验规则
│   ├── ocr_tasks.py   #   OCR 后台任务（内存任务表 + 识别执行）
│   └── routers/       #   业务路由：cases / files / recognize / export / auth / audit
├── database.py        # PostgreSQL 连接与会话管理（环境变量配置）
├── models.py          # ORM 模型（cases 表 + 三类文件表）
├── schemas.py         # Pydantic 请求/响应模型
├── crud.py            # 数据访问层（增查删改、筛选、排序）
├── excel_export.py    # Excel 导出
├── index.html         # 前端单页（识别上传 + 案件文件管理）
├── static/
│   ├── chart.min.js   # 前端静态资源
│   └── uploads/       # 上传文件存储（按 YYYY/MM 分目录，git 忽略）
└── requirements.txt   # 依赖清单
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

## 案件文件管理功能

### PDF/图片识别上传
- 主页面支持拖拽或点击上传 PDF/图片
- 上传后选择文件所属类型：**核心法定文书**（识别字段）/ **证据材料**（仅存储）/ **程序性告知附件**（仅存储）
- 填写上传人，可关联案件（可选）
- 核心法定文书会模拟识别 19 个案件字段，可应用到表单并保存（真实识别逻辑预留，后续接入 OCR）

### 案件专属文件表
- 每个案件保存后自动创建专属索引表 `case_{案件ID}`，记录该案件三类文件的索引
- 三类文件全局存储于 `core_documents` / `evidence_materials` / `procedural_notices` 表
- 上传文件写入 `static/uploads/YYYY/MM/` 目录（按日期分片），文件名 UUID 化避免冲突

### 文件管理页面
- 通过案件选择器切换查看不同案件的文件
- 文件以树形目录按类型折叠/展开，展示文件名、大小、上传日期、上传人
- 支持 PDF 内嵌预览与图片预览
- 文件删除支持软删除（标记保留）与物理删除

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
