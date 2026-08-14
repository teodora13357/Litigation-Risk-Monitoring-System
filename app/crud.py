from typing import List, Optional

from sqlalchemy.orm import Session

from app import models
from app import schemas


# ============================================================
# 数据库重构：LawsuitCase CRUD（软删除 + 状态映射在调用方处理）
# ============================================================


def create_lawsuit_case(
    db: Session,
    case: "schemas.LawsuitCaseCreate",
    case_no: str,
    created_by: Optional[str] = None,
) -> "models.LawsuitCase":
    data = case.model_dump(exclude={"key_dates"})  # key_dates 单独写入时间线表
    db_case = models.LawsuitCase(**data, case_no=case_no, created_by=created_by)
    db.add(db_case)
    db.commit()
    db.refresh(db_case)
    return db_case


def get_lawsuit_case(db: Session, case_id: int) -> Optional["models.LawsuitCase"]:
    return (
        db.query(models.LawsuitCase)
        .filter(models.LawsuitCase.id == case_id, models.LawsuitCase.status_flag == 1)
        .first()
    )


def get_lawsuit_cases(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    court_case_no: Optional[str] = None,
    plaintiff: Optional[str] = None,
    defendant: Optional[str] = None,
    current_status: Optional[str] = None,
    case_type_id: Optional[int] = None,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
) -> List["models.LawsuitCase"]:
    query = db.query(models.LawsuitCase).filter(models.LawsuitCase.status_flag == 1)
    if court_case_no:
        query = query.filter(models.LawsuitCase.court_case_no.contains(court_case_no))
    if plaintiff:
        query = query.filter(models.LawsuitCase.plaintiff.contains(plaintiff))
    if defendant:
        query = query.filter(models.LawsuitCase.defendant.contains(defendant))
    if current_status:
        query = query.filter(models.LawsuitCase.current_status == current_status)
    if case_type_id:
        query = query.filter(models.LawsuitCase.case_type_id == case_type_id)

    if sort_by in ("created_at", "claim_amount", "deadline_date"):
        col = getattr(models.LawsuitCase, sort_by)
        query = query.order_by(col.desc() if sort_order == "desc" else col.asc())
    else:
        query = query.order_by(models.LawsuitCase.id.desc())

    return query.offset(skip).limit(limit).all()


def update_lawsuit_case(
    db: Session,
    case_id: int,
    case: "schemas.LawsuitCaseUpdate",
) -> Optional["models.LawsuitCase"]:
    db_case = get_lawsuit_case(db, case_id)
    if db_case:
        data = case.model_dump(exclude={"key_dates"}, exclude_unset=True)
        for field, value in data.items():
            setattr(db_case, field, value)
        db.commit()
        db.refresh(db_case)
    return db_case


def delete_lawsuit_case(db: Session, case_id: int) -> bool:
    """软删除：status_flag 置 0。"""
    db_case = get_lawsuit_case(db, case_id)
    if db_case:
        db_case.status_flag = 0
        db.commit()
        return True
    return False


def get_all_lawsuit_cases(db: Session) -> List["models.LawsuitCase"]:
    return (
        db.query(models.LawsuitCase)
        .filter(models.LawsuitCase.status_flag == 1)
        .order_by(models.LawsuitCase.id.desc())
        .all()
    )


def add_case_key_date(
    db: Session,
    case_id: int,
    date_type: str,
    key_date,
    status_code: Optional[str] = None,
    remark: Optional[str] = None,
) -> "models.CaseKeyDate":
    """向案件关键日期时间线追加一条记录（date_type_name 从 constants 取中文名）。"""
    from app.constants import DATE_TYPE

    db_item = models.CaseKeyDate(
        case_id=case_id,
        date_type=date_type,
        date_type_name=DATE_TYPE.get(date_type, date_type),
        key_date=key_date,
        status_code=status_code,
        remark=remark,
    )
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item
