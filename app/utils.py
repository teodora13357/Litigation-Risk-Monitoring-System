"""共享工具函数：案号校验 / 案件编号生成。"""

import random
import re
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

import error_codes
from error_codes import BizError
from models import LawsuitCase

_CASE_NUMBER_RE = re.compile(r"^\(\d{4}\)[^()（）\s]+民(初|终)\d{4}号$")


def validate_case_number(case_number: str) -> None:
    """校验案号格式：(YYYY)法院代码民初/终XXXX号"""
    if _CASE_NUMBER_RE.match(case_number) is None:
        raise BizError(error_codes.MISSING_REQUIRED_FIELD,
                       "案号格式不正确，格式：(YYYY)法院代码民初/终XXXX号")


def generate_case_no(db: Session, max_retry: int = 5) -> str:
    """生成案件编号：CASE-{YYYYMMDD}-{4位随机}（方案 C）。

    并发安全（随机不依赖计数），撞唯一索引时重试。
    """
    for _ in range(max_retry):
        today = datetime.now().strftime("%Y%m%d")
        suffix = f"{random.randint(0, 9999):04d}"
        case_no = f"CASE-{today}-{suffix}"
        exists = db.query(LawsuitCase).filter(LawsuitCase.case_no == case_no).first()
        if exists is None:
            return case_no
    raise HTTPException(status_code=500, detail="案件编号生成失败，请重试")

