"""案件管理系统 —— FastAPI 应用入口。

- 启动：建表 / 默认用户 / 状态字典同步 / 日志配置 / 中间件
- 页面：/（案件管理页）、/login（登录页）
- 业务路由按域拆分到 app/routers/ 下，统一 include 到此应用
"""

import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import app_logging
import auth
import constants
import error_codes
import models
from database import SessionLocal, engine
from app.routers import cases as cases_router
from app.routers import files as files_router
from app.routers import recognize as recognize_router
from app.routers import export as export_router
from app.routers import auth as auth_router
from app.routers import audit as audit_router

app_logging.setup_logging()
logger = app_logging.get_logger("main")

models.Base.metadata.create_all(bind=engine)


def _ensure_default_user():
    """确保默认用户存在：admin / manager / user（各角色一个，写入 sys_user 表）。"""
    db = SessionLocal()
    try:
        defaults = [
            {"username": "admin", "real_name": "系统管理员", "phone": "13800138000", "password": "admin123", "role": "admin"},
            {"username": "manager", "real_name": "业务经理", "phone": "13800138001", "password": "manager123", "role": "manager"},
            {"username": "user", "real_name": "普通用户", "phone": "13800138002", "password": "user123", "role": "user"},
        ]
        for d in defaults:
            user = db.query(models.SysUser).filter(
                models.SysUser.username == d["username"], models.SysUser.status == 1
            ).first()
            if user is None:
                db.add(models.SysUser(
                    username=d["username"],
                    real_name=d["real_name"],
                    phone=d["phone"],
                    password_hash=auth.hash_password(d["password"]),
                    role=d["role"],
                    status=1,
                ))
            else:
                user.role = d["role"]
        db.commit()
        logger.info("默认用户已就绪: admin/manager/user")
    finally:
        db.close()


def _sync_status_dict():
    """按 constants.CASE_STATUS_DICT（唯一数据源）同步 case_status_dict 表。

    - 缺失的字典行插入；已存在的行更新中文名/描述/排序。
    """
    db = SessionLocal()
    try:
        for row in constants.status_dict_rows():
            exist = db.query(models.CaseStatusDict).filter(
                models.CaseStatusDict.code == row["code"]
            ).first()
            if exist is None:
                db.add(models.CaseStatusDict(**row))
            else:
                exist.en_name = row["en_name"]
                exist.name = row["name"]
                exist.description = row["description"]
                exist.sort_order = row["sort_order"]
        db.commit()
        logger.info("案件状态字典同步完成（共 %d 项）", len(constants.CASE_STATUS_DICT))
    finally:
        db.close()


_ensure_default_user()
_sync_status_dict()


app = FastAPI(
    title="案件管理系统",
    description="法律案件管理 — 增查删改 + Excel导出",
    version="1.0.0",
)


@app.exception_handler(error_codes.BizError)
async def biz_error_handler(request: Request, exc: error_codes.BizError):
    return JSONResponse(status_code=exc.http_status, content={
        "code": exc.code,
        "message": exc.message,
        "data": None,
    })


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_ACCESS_LOGGER = app_logging.get_logger("access")


@app.middleware("http")
async def _request_context_middleware(request: Request, call_next):
    """为请求注入 request_id / user_id，并记录访问日志。"""
    rid = app_logging.new_request_id()
    app_logging.request_id_var.set(rid)
    # 尝试从 Authorization 解析用户 ID（仅日志用途，失败不影响请求）
    uid = "-"
    auth_header = (request.headers.get("Authorization") or "")
    if auth_header.startswith("Bearer "):
        payload = auth.decode_token(auth_header[7:])
        if payload:
            try:
                uid = str(int(payload.get("sub", 0) or 0))
            except (TypeError, ValueError):
                uid = "-"
    app_logging.user_id_var.set(uid)
    start = datetime.now()
    try:
        response = await call_next(request)
    except Exception:
        _ACCESS_LOGGER.exception("request failed method=%s path=%s", request.method, request.url.path)
        raise
    cost_ms = int((datetime.now() - start).total_seconds() * 1000)
    _ACCESS_LOGGER.info(
        "method=%s path=%s status=%d cost_ms=%d",
        request.method, request.url.path, response.status_code, cost_ms,
    )
    return response


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ===================== 前端页面 =====================

@app.get("/", response_class=HTMLResponse, tags=["前端页面"], summary="案件管理页面")
def index():
    """返回案件管理前端页面。"""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/login", response_class=HTMLResponse, tags=["认证页面"], summary="登录页")
def login_page():
    login_path = os.path.join(os.path.dirname(__file__), "login.html")
    if not os.path.exists(login_path):
        raise HTTPException(status_code=404, detail="登录页不存在")
    with open(login_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
        })


# ===================== 业务路由 =====================

app.include_router(cases_router.router)
app.include_router(files_router.router)
app.include_router(recognize_router.router)
app.include_router(export_router.router)
app.include_router(auth_router.router)
app.include_router(audit_router.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
