# 部署指南（DEPLOY.md）

> 本项目主要在 **macOS** 上开发，本文档同时覆盖：
> - **macOS 本地开发部署**（推荐日常开发）
> - **Linux 服务器 Docker Compose 部署**（推荐生产）
>
> 所有 macOS 特有事项均已标注 ⚠️，请在 macOS 上部署时特别留意。

---

## 一、环境依赖清单

### 1.1 应用运行依赖

| 依赖 | 版本要求 | 说明 |
| ---- | ---- | ---- |
| Python | 3.10+（推荐 3.12） | ⚠️ macOS 推荐用 Homebrew 安装，避免系统自带 Python 的权限问题 |
| PostgreSQL | 14+ | 应用不会自动创建数据库，需先建库（见第二章） |
| Redis | 7.x | 预留缓存模块，**可选**（未配置时应用可正常运行） |
| 阿里云 OSS | — | **可选**；未配置时文件回退本地存储 `static/uploads/` |

### 1.2 Python 依赖（由 `pyproject.toml` 管理）

- 基础：`fastapi` `uvicorn` `sqlalchemy` `psycopg2-binary` `pydantic` `python-multipart` `pyjwt` `openpyxl` `oss2` `redis` `pypdf`
- OCR 增强：`requests` `pillow` `opencv-python-headless` `numpy` `pymupdf`

安装方式（本地开发）：

```bash
pip install .
# 或仅安装依赖：
pip install fastapi uvicorn sqlalchemy psycopg2-binary pydantic python-multipart pyjwt openpyxl oss2 redis pypdf requests pillow opencv-python-headless numpy pymupdf
```

### 1.3 OCR 识别依赖（MinerU）

- 需要本地部署 **MinerU HTTP API 服务**（默认端口 `30000`，可用环境变量 `MINERU_API_URL` 覆盖）
- 本机 CPU 环境下使用 **pipeline 后端**（`--backend pipeline --device cpu`）
- 容器必须应用 MinerU 3.4.4 的 `fast_api.py` 补丁（否则解析必失败，见第五章）

### 1.4 部署工具

| 工具 | 用途 | 安装 |
| ---- | ---- | ---- |
| Docker + Docker Compose | 服务器一键部署 | Linux: `apt install docker-compose-plugin`；⚠️ macOS: 安装 Docker Desktop |
| Homebrew | ⚠️ macOS 本地装 PostgreSQL / Python / Redis | `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` |

### 1.5 ⚠️ macOS 特有说明

1. **Docker Desktop 容器内没有 GPU / MPS**：Docker Desktop 通过 Linux VM 运行容器，Apple Silicon 的 MPS 无法直通容器。因此容器内 MinerU **只能使用 pipeline 后端（CPU）**，hybrid-engine（依赖 vLLM/CUDA）无法运行。
2. **本地直跑可用 MPS**：若在 macOS 原生环境跑 MinerU，PyTorch 会自动选择 MPS（约 2~5 倍于 CPU 的速度）。
3. **`timeout` 命令默认不存在**（GNU coreutils 才有）。需要超时控制时请用 `gtimeout`（`brew install coreutils`）或改用 Python。
4. **端口冲突**：macOS 上常见 5432 / 8000 / 30000 被本机已运行的服务占用（见第五章排查）。
5. 若在 Apple Silicon 上安装 `pymupdf`/`opencv` 失败，请确认 Python 为 Homebrew 的 3.10+（`arch -arm64` 或 `arch -x86_64` 视情况，避免 Rosetta 混用）。

---

## 二、数据库初始化步骤

### 2.1 Docker Compose 方式（自动初始化）

`docker-compose.yml` 通过 `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` 自动创建数据库与用户，**无需手动建库**。应用启动时会用 SQLAlchemy `create_all` 自动建表。

### 2.2 ⚠️ macOS 本地方式（手动建库）

```bash
# 1. 启动 PostgreSQL（Homebrew）
brew install postgresql@16
brew services start postgresql@16

# 2. 创建用户与数据库
psql postgres -c "CREATE USER case_app WITH PASSWORD 'case_pass_123';"
psql postgres -c "CREATE DATABASE case_management OWNER case_app ENCODING 'UTF8';"

# 3. 验证连接
PGPASSWORD=case_pass_123 psql -h 127.0.0.1 -U case_app -d case_management -c "SELECT version();"
```

### 2.3 建表说明

应用启动时自动创建以下表（无需手动执行建表 SQL）：
- `cases`（案件主表）
- `core_documents` / `evidence_materials` / `procedural_notices`（三类文件表）
- `users`（用户表，启动时自动写入默认账号 `admin` / `manager` / `user`，初始密码 `admin123` / `manager123` / `user123`）
- 每个案件首次关联文件时自动创建 `case_{案件ID}` 索引表

⚠️ 默认账号初始密码建议首次登录后修改（当前无改密接口，需通过 SQL 或后续功能）。

---

## 三、环境变量配置说明

全部通过环境变量注入（`.env` / compose `environment` / `export`）。示例见 `.env.example`。

### 3.1 数据库

| 变量 | 默认值 | 说明 |
| ---- | ---- | ---- |
| `PG_HOST` | `127.0.0.1` | PostgreSQL 主机（compose 内为 `postgres`） |
| `PG_PORT` | `5432` | 端口 |
| `PG_USER` | `case_app` | 用户名 |
| `PG_PASSWORD` | `case_pass_123` | 密码（生产务必修改） |
| `PG_DB` | `case_management` | 数据库名 |
| `DATABASE_URL` | 由上述拼接 | 完整连接串，优先级最高 |

> 密码含特殊字符时需 URL 编码，或直接用 `DATABASE_URL` 整体覆盖。

### 3.2 Redis / 认证 / 存储

| 变量 | 默认值 | 说明 |
| ---- | ---- | ---- |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接串（compose 内为 `redis://redis:6379/0`） |
| `SECRET_KEY` | `case-management-secret-key-change-me` | JWT 签名密钥（**生产务必修改**） |
| `OSS_ENDPOINT` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_BUCKET` | 空 | 阿里云 OSS 配置；四项全填才启用 OSS，否则本地存储 |

### 3.3 OCR（MinerU）

| 变量 | 默认值 | 说明 |
| ---- | ---- | ---- |
| `MINERU_API_URL` | `http://localhost:30000` | MinerU 服务地址（compose 内为 `http://mineru:30000`） |
| `OCR_PARSE_TIMEOUT` | `600` | 单批解析超时（秒） |
| `OCR_BATCH_PAGES` | `10` | 每批提交页数 |
| `OCR_PREPROCESS_DPI` | `300` | PDF 渲染 DPI |
| `OCR_MAX_IMAGE_SIDE` | `3500` | 图像长边上限（px） |
| `OCR_BINARIZE` | `auto` | 二值化策略 `auto`/`true`/`false` |
| `OCR_TEXT_MIN_CHARS` | `30` | PDF 内嵌文本判定阈值（字符） |

---

## 四、首次部署完整步骤

### 方案 A：Docker Compose（推荐，服务器 / 生产）

```bash
# 1. 克隆项目
git clone <仓库地址> && cd demo_olof

# 2. 准备 .env（务必修改 PG_PASSWORD 与 SECRET_KEY）
cp .env.example .env
# vim .env

# 3. 构建并启动（app + postgres + redis）
docker compose up -d --build

# 4. （可选）启动 MinerU OCR 服务
#    注意：先按第五章应用 MinerU 补丁，否则 OCR 会失败
docker compose --profile ocr up -d

# 5. 验证
docker compose ps                     # 三个服务均 healthy
curl http://127.0.0.1:8000/           # 前端页面
curl http://127.0.0.1:8000/docs       # Swagger
docker compose --profile ocr ps       # MinerU 服务
curl http://127.0.0.1:30000/health    # MinerU 健康检查

# 6. 查看日志 / 停止
docker compose logs -f app
docker compose down                   # 停止（数据卷保留）
docker compose down -v                # 停止并删除数据卷（慎用）
```

> 首次构建需拉取镜像并安装 Python 依赖，耗时数分钟属正常。

### 方案 B：⚠️ macOS 本地开发

```bash
# 1. 安装基础软件（Homebrew）
brew install python@3.12 postgresql@16
brew services start postgresql@16
# Redis 可选：brew install redis && brew services start redis

# 2. 初始化数据库（见第二章 2.2）

# 3. 创建虚拟环境并安装依赖
python3.12 -m venv .venv
source .venv/bin/activate
pip install .

# 4. 配置环境变量（本地直连）
export PG_HOST=127.0.0.1
export PG_USER=case_app
export PG_PASSWORD=case_pass_123
export PG_DB=case_management
export REDIS_URL="redis://127.0.0.1:6379/0"
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export MINERU_API_URL="http://localhost:30000"

# 5. 启动 MinerU OCR 服务（Docker）
#    详见第五章「MinerU 报 backend 错误」：需要先给容器内 fast_api.py 打补丁
docker run -d --name mineru-ocr \
  -p 30000:30000 \
  -v "$HOME/mineru_data/input:/app/input" \
  -v "$HOME/mineru_data/output:/app/output" \
  mineru:latest \
  mineru-api --host 0.0.0.0 --port 30000 --backend pipeline --device cpu

# 6. 启动应用
uvicorn main:app --host 127.0.0.1 --port 8000
# 访问 http://127.0.0.1:8000
```

---

## 五、常见问题排查

### 5.1 数据库类

**1. 启动报错 `FATAL: database does not exist`**
先执行第二章的建库 SQL，再启动应用。Compose 方式确认 `POSTGRES_DB` 与 `PG_DB` 一致。

**2. `password authentication failed`**
检查 `PG_USER` / `PG_PASSWORD` 与实际数据库账号一致；Compose 方式注意 `.env` 中 `POSTGRES_PASSWORD` 与 `PG_PASSWORD` 都要改。

**3. 连接超时 / 拒绝连接**
- 本地：确认 `brew services list` 中 postgresql 已启动；确认 `PG_HOST=127.0.0.1` 而非 `postgres`
- Compose：`docker compose logs postgres` 查看启动日志；确认应用容器 `depends_on` 已生效

**4. 端口 5432 被占用（⚠️ macOS 常见）**
```bash
lsof -iTCP:5432 -sTCP:LISTEN
```
若本机已有其它 PostgreSQL 实例，请改用别的端口（如 5433）并同步 `.env` 的 `PG_PORT`。

### 5.2 OCR / MinerU 类

**5. MinerU 报错 `dict() got multiple values for keyword argument 'backend'`（必现）**
这是 **MinerU 3.4.4 已知 bug**：通过 CLI 启动参数传入的 `backend`/`device` 会进入 `model_config`，在 `run_parse_job` 中与请求参数冲突。

修复方法（容器内打补丁）：
```bash
# 进入容器
docker exec -it mineru-dev bash
# 或用容器名: docker exec -it case-mgmt-mineru bash

# 备份并给 fast_api.py 打补丁（过滤与请求参数重叠的 config 键）
cd /usr/local/lib/python3.12/dist-packages/mineru/cli
cp fast_api.py fast_api.py.bak
python3 - <<'PY'
import pathlib
p = pathlib.Path('/usr/local/lib/python3.12/dist-packages/mineru/cli/fast_api.py')
s = p.read_text()
marker = '    parse_kwargs = dict(\n'
ins = ("    _reserved = {'backend','device','output_dir','pdf_file_names','pdf_bytes_list',"
       "'p_lang_list','parse_method','effort','formula_enable','table_enable','image_analysis',"
       "'server_url','f_draw_layout_bbox','f_draw_span_bbox','f_dump_md','f_dump_middle_json',"
       "'f_dump_model_output','f_dump_orig_pdf','f_dump_content_list','start_page_id','end_page_id',"
       "'client_side_output_generation'}\n"
       "    config = {k: v for k, v in config.items() if k not in _reserved}\n\n")
if '_reserved' not in s:
    p.write_text(s.replace(marker, ins + marker, 1))
    print('patched')
else:
    print('already patched')
PY

# 语法检查并重启
python3 -m py_compile fast_api.py && exit
docker restart mineru-dev    # 或 docker compose --profile ocr restart mineru
```

> ⚠️ 补丁只写入运行中的容器，容器重建后需重新应用。也可在 `docker run` 后用上述命令执行一次。

**6. OCR 报 `Device string must not be empty` / `AsyncEngineArgs got an unexpected keyword argument 'device'`**
hybrid-engine 依赖 vLLM（需要 CUDA GPU）。在 ⚠️ macOS Docker Desktop（无 GPU）或纯 CPU 环境下不可用。
解决：`ocr_service.py` 已默认使用 **pipeline 后端**，请勿将请求或 MinerU 启动参数改为 hybrid-engine。

**7. OCR 识别文字乱码（如 `l. j j llun` 之类）**
多为提交的图像格式问题。**必须使用 PNG 无损格式**——`ocr_service.py` 已内置该处理。若自建脚本，请勿用 JPEG 提交页图像。

**8. 上传后返回 `OCR识别失败，建议手动录入`**
- 检查 `MINERU_API_URL` 是否可访问：`curl http://<MINERU_API_URL>/health`
- 检查文件是否为受支持的 PDF/图片；损坏文件会在上传校验阶段被拦截
- 查看后端日志：`docker compose logs -f app` 或本地终端中的 `[OCR]` 日志

### 5.3 ⚠️ macOS 特有

**9. Docker Desktop 资源不足导致容器启动慢 / OOM**
Docker Desktop → Settings → Resources 调高内存（建议 ≥ 8GB，MinerU 模型加载需要）。当前项目开发机配置：10 CPU / 7.75GB。

**10. 容器内 `python` 命令不存在**
MinerU 容器基于 vllm-openai 镜像，默认无 `python` 软链，请用 `python3` 或 `/usr/bin/python3`。

**11. `timeout: command not found`**
macOS 默认无 GNU `timeout`。请用 `gtimeout`（`brew install coreutils`）或 Python 脚本实现超时。

**12. `fitz`/`pymupdf` 导入告警**
新版 PyMuPDF 弃用 `import fitz`。`ocr_service.py` 已优先 `import pymupdf`，并兼容旧包名。若自写脚本请同样处理。

**13. 端口 8000 被占用（常见于本机已有其它 Web 服务）**
```bash
lsof -iTCP:8000 -sTCP:LISTEN
```
可改用 `uvicorn main:app --port 8001` 或 `docker compose` 中修改映射端口。

**14. Apple Silicon 安装 `pymupdf` / `opencv` 失败**
确认使用 Homebrew Python（`which python3`），避免系统自带 Python（`/usr/bin/python3`）的权限与依赖问题；必要时 `brew reinstall python@3.12`。

### 5.4 其它

**15. 上传文件后无法预览 / 文件丢失**
- 本地存储模式：确认 `static/uploads/` 目录存在且有写权限
- OSS 模式：检查四个 `OSS_*` 变量是否全部正确配置；预览时会从 OSS 回填本地缓存
- Compose 部署：确认 `upload_data` 卷已挂载（`docker compose down -v` 会清空上传文件，慎用）

**16. 前端识别结果显示"提取 0 个字段"**
OCR 成功但正则未提取到字段属正常（启发式提取，找不到为 null）。可查看 `ocr_result.structured_text` 确认识别文本本身是否正确；若文本为空，按第 7、8 条排查。

**17. pip 安装慢 / 失败**
```bash
pip install . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**18. Docker Hub 拉取镜像超时（`context deadline exceeded` / `failed to resolve source metadata`）**
常见于网络无法直连 `registry-1.docker.io`。解决：配置 Docker 镜像加速器。

⚠️ macOS（Docker Desktop）：
1. Docker Desktop → Settings → Docker Engine
2. 在 JSON 中加入 `registry-mirrors`，例如：
```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com"
  ]
}
```
3. Apply & Restart 后重新 `docker compose build`

Linux（Docker Engine）：
```bash
# 编辑 /etc/docker/daemon.json，加入上述 registry-mirrors 后：
sudo systemctl restart docker
```

> 也可先手动 `docker pull python:3.12-slim` 验证加速器是否生效，再继续构建。

**19. Docker 构建时 apt-get 下载 Debian 软件包缓慢**
构建容器镜像时，`Dockerfile` 的 `apt-get update` 访问 `deb.debian.org` 可能很慢（网络受限时尤其明显）。可临时改用国内 Debian 镜像源：

```dockerfile
# 在 Dockerfile 的 apt-get update 前追加（示例为清华源）：
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true
```

---

> 本指南未尽事项，请结合 `README.md`、`识别功能接入指南.md` 与后端日志排查。


