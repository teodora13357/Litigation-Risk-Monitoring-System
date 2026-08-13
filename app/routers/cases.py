"""案件管理路由：增查删改 + v1 版本化案件接口。"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

import constants
import crud
import error_codes
import models
import schemas
from database import get_db
from error_codes import BizError
from app.audit import write_audit_log
from app.deps import get_current_user
from app.utils import validate_case_number, generate_case_no

router = APIRouter()


@router.post("/api/cases", response_model=schemas.LawsuitCaseResponse, tags=["案件管理"], summary="新增案件")
def create_case(case: schemas.LawsuitCaseCreate, request: Request,
                user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """新增案件：案号必填；状态支持中文/状态码；自动生成 case_no；关键日期写入时间线表。"""
    if case.court_case_no is None or case.court_case_no.strip() == "":
        raise BizError(error_codes.MISSING_REQUIRED_FIELD, "案号不能为空")
    validate_case_number(case.court_case_no)
    # 案号唯一校验（仅未删除记录）
    exists = db.query(models.LawsuitCase).filter(
        models.LawsuitCase.court_case_no == case.court_case_no,
        models.LawsuitCase.status_flag == 1,
    ).first()
    if exists is not None:
        raise BizError(error_codes.CASE_NUMBER_EXISTS, "案号已存在")

    # 生成唯一案件编号（方案 C）
    case_no = generate_case_no(db)

    # 状态：中文名 -> 状态码
    data = case.model_dump(exclude={"key_dates", "current_status"})
    status_code = constants.status_to_code(case.current_status or "S1")
    data["current_status"] = status_code
    # 风险/紧急：中文 -> 码
    if data.get("risk_level"):
        data["risk_level"] = constants.RISK_LEVEL_NAME_TO_CODE.get(data["risk_level"], data["risk_level"])
    if data.get("urgency_level"):
        data["urgency_level"] = constants.URGENCY_LEVEL_NAME_TO_CODE.get(data["urgency_level"], data["urgency_level"])

    db_case = models.LawsuitCase(**data, case_no=case_no)
    db.add(db_case)
    db.commit()
    db.refresh(db_case)

    # 关键日期时间线
    for kd in (case.key_dates or []):
        crud.add_case_key_date(
            db, db_case.id, kd.date_type, kd.key_date,
            status_code=kd.status_code or status_code,
            remark=kd.remark,
        )
    # 审计：创建案件
    write_audit_log(db, "CREATE", "CASE", target_id=db_case.id,
                    action_detail={"case_no": db_case.case_no, "court_case_no": db_case.court_case_no},
                    request=request, user_id=user.id, user_name=user.username)
    return db_case


@router.get("/api/cases", response_model=list[schemas.LawsuitCaseResponse], tags=["案件管理"], summary="查询案件列表")
def list_cases(
    skip: int = 0,
    limit: int = Query(default=100, le=500),
    court_case_no: Optional[str] = None,
    plaintiff: Optional[str] = None,
    defendant: Optional[str] = None,
    current_status: Optional[str] = None,
    case_type_id: Optional[int] = None,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
    user: models.SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查询案件列表，支持按案号/原告/被告/状态/业务类型筛选（状态支持中文或码）。"""
    if current_status:
        current_status = constants.status_to_code(current_status)
    return crud.get_lawsuit_cases(
        db,
        skip=skip,
        limit=limit,
        court_case_no=court_case_no,
        plaintiff=plaintiff,
        defendant=defendant,
        current_status=current_status,
        case_type_id=case_type_id,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get("/api/cases/{case_id}", response_model=schemas.LawsuitCaseResponse, tags=["案件管理"], summary="查询单个案件")
def get_case(case_id: int, user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """根据 ID 查询单个案件详情。"""
    db_case = crud.get_lawsuit_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    return db_case


@router.put("/api/cases/{case_id}", response_model=schemas.LawsuitCaseResponse, tags=["案件管理"], summary="修改案件")
def update_case(case_id: int, case: schemas.LawsuitCaseUpdate, request: Request,
                user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """修改案件信息，支持部分更新；状态支持中文/码，状态变更时写入时间线并更新时间戳。"""
    db_case = crud.get_lawsuit_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="案件不存在")

    data = case.model_dump(exclude={"key_dates"}, exclude_unset=True)
    if "current_status" in data and data["current_status"]:
        data["current_status"] = constants.status_to_code(data["current_status"])
        data["status_updated_at"] = datetime.now()
    if data.get("risk_level"):
        data["risk_level"] = constants.RISK_LEVEL_NAME_TO_CODE.get(data["risk_level"], data["risk_level"])
    if data.get("urgency_level"):
        data["urgency_level"] = constants.URGENCY_LEVEL_NAME_TO_CODE.get(data["urgency_level"], data["urgency_level"])

    for field, value in data.items():
        setattr(db_case, field, value)
    db.commit()
    db.refresh(db_case)

    # 关键日期时间线
    for kd in (case.key_dates or []):
        crud.add_case_key_date(
            db, case_id, kd.date_type, kd.key_date,
            status_code=kd.status_code or db_case.current_status,
            remark=kd.remark,
        )
    # 审计：修改案件
    write_audit_log(db, "UPDATE", "CASE", target_id=case_id,
                    action_detail={"changes": data}, request=request, user_id=user.id, user_name=user.username)
    return db_case


@router.delete("/api/cases/{case_id}", tags=["案件管理"], summary="删除案件")
def delete_case(case_id: int, request: Request,
                user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """根据 ID 软删除案件（status_flag 置 0）。"""
    success = crud.delete_lawsuit_case(db, case_id)
    if not success:
        raise HTTPException(status_code=404, detail="案件不存在")
    write_audit_log(db, "DELETE", "CASE", target_id=case_id,
                    request=request, user_id=user.id, user_name=user.username)
    return {"message": "案件已删除", "case_id": case_id}


# ===================== API v1（版本化案件接口，复用现有逻辑） =====================

@router.post("/api/v1/cases", response_model=schemas.LawsuitCaseResponse, tags=["v1案件"], summary="创建诉讼案件")
def v1_create_case(case: schemas.LawsuitCaseCreate, request: Request,
                   user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """创建诉讼案件（v1 接口，复用现有逻辑）。"""
    return create_case(case, request, user, db)


@router.post("/api/v1/cases/manual", response_model=schemas.LawsuitCaseResponse, tags=["v1案件"], summary="手动录入案件")
def v1_create_case_manual(case: schemas.LawsuitCaseCreate, request: Request,
                          user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动录入案件（v1 接口，复用现有逻辑）。"""
    return create_case(case, request, user, db)
