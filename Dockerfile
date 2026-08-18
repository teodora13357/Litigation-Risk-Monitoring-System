# 案件管理系统 —— 应用镜像
# 基础镜像：python 3.12 slim（Debian bookworm）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖：
#   libglib2.0-0  -> opencv-python-headless 运行所需
#   libgomp1      -> numpy / opencv 的 OpenMP 运行库
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖清单，利用构建缓存
COPY pyproject.toml README.md ./

# 复制项目源码（.dockerignore 已排除 .venv/.git/static/uploads 等）
COPY . .

# 安装依赖与打包项目（pyproject.toml 的 dependencies 已含 OCR 增强包）
RUN pip install .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
