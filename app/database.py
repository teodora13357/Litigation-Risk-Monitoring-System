import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# ===================== PostgreSQL 数据库配置（环境变量优先） =====================
DB_HOST = os.getenv("PG_HOST", "127.0.0.1")
DB_PORT = os.getenv("PG_PORT", "5432")
DB_USER = os.getenv("PG_USER", "case_app")
DB_PASSWORD = os.getenv("PG_PASSWORD", "case_pass_123")
DB_NAME = os.getenv("PG_DB", "case_management")

# 完整连接串可整体覆盖（例如用于测试或特殊配置）
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # 请求前 ping，避免数据库重启后连接失效
    pool_recycle=3600,    # 1 小时回收连接，防止连接被服务端断掉
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
