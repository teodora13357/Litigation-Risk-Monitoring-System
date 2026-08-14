"""数据导出路由：案件 Excel 导出。"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import crud
from app import excel_export
from app import models
from app.database import get_db
from app.audit import write_audit_log
from app.deps import get_current_user

router = APIRouter()


@router.get("/api/export/excel", tags=["数据导出"], summary="导出Excel")
def export_excel(user: models.SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """导出所有案件数据为 Excel 文件（.xlsx），含全部字段中文表头。"""
    cases = crud.get_all_lawsuit_cases(db)
    filepath = excel_export.export_to_excel(cases, "cases_export.xlsx")
    write_audit_log(db, "EXPORT", "CASE", target_id=None,
                    action_detail={"count": len(cases)},
                    user_id=user.id, user_name=user.username)
    return FileResponse(
        path=filepath,
        filename="案件数据导出.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
