from typing import List, Optional

from sqlalchemy import case
from sqlalchemy.orm import Session

import models
import schemas


def create_case(db: Session, case: schemas.CaseCreate) -> models.Case:
    db_case = models.Case(**case.model_dump())
    db.add(db_case)
    db.commit()
    db.refresh(db_case)
    return db_case


def get_case(db: Session, case_id: int) -> Optional[models.Case]:
    return db.query(models.Case).filter(models.Case.id == case_id).first()


def get_cases(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    case_number: Optional[str] = None,
    case_type: Optional[str] = None,
    stage: Optional[str] = None,
    is_closed: Optional[bool] = None,
    handler: Optional[str] = None,
    processing_status: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
) -> List[models.Case]:
    query = db.query(models.Case)
    if case_number:
        query = query.filter(models.Case.case_number.contains(case_number))
    if case_type:
        query = query.filter(models.Case.case_type == case_type)
    if stage:
        query = query.filter(models.Case.stage == stage)
    if is_closed is not None:
        query = query.filter(models.Case.is_closed == is_closed)
    if handler:
        query = query.filter(models.Case.handler.contains(handler))
    if processing_status:
        query = query.filter(models.Case.processing_status == processing_status)

    if sort_by in ("delivery_time", "court_time"):
        col = getattr(models.Case, sort_by)
        is_empty = case((col.is_(None) | (col == ""), 1), else_=0)
        query = query.order_by(is_empty)
        if sort_order == "desc":
            query = query.order_by(col.desc())
        else:
            query = query.order_by(col.asc())

    return query.offset(skip).limit(limit).all()


def update_case(
    db: Session, case_id: int, case: schemas.CaseUpdate
) -> Optional[models.Case]:
    db_case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if db_case:
        update_data = case.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_case, field, value)
        db.commit()
        db.refresh(db_case)
    return db_case


def delete_case(db: Session, case_id: int) -> bool:
    db_case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if db_case:
        db.delete(db_case)
        db.commit()
        return True
    return False


def get_all_cases(db: Session) -> List[models.Case]:
    return db.query(models.Case).all()
