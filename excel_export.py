from typing import List

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from models import Case

FIELD_MAP = {
    "id": "ID",
    "delivery_time": "送达时间",
    "case_number": "案号",
    "case_type": "案件类型",
    "involved_parties": "涉及主体",
    "court_time": "开庭时间",
    "court_location": "开庭地点",
    "contact_phone": "联系电话",
    "plaintiff": "原告",
    "id_number": "身份证号",
    "defendant": "被告",
    "service_client": "服务客户",
    "stage": "阶段",
    "client_contact": "客户对接人",
    "processing_status": "处理情况",
    "handler": "处理人",
    "defense_method": "答辩方式",
    "is_closed": "是否结案",
    "judgment_result": "判决结果",
    "compensation_amount": "赔偿金额",
}


def export_to_excel(cases: List[Case], filepath: str = "cases_export.xlsx") -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "案件数据"

    keys = list(FIELD_MAP.keys())
    headers = list(FIELD_MAP.values())

    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    even_fill = PatternFill(start_color="D9E2F3", end_color="D9E2F3", fill_type="solid")

    for row_idx, case in enumerate(cases, 2):
        for col_idx, key in enumerate(keys, 1):
            value = getattr(case, key, None)
            if key == "is_closed" and value is not None:
                value = "是" if value else "否"
            if key == "compensation_amount" and value is not None:
                value = float(value)
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = thin_border
            cell.alignment = Alignment(horizontal="left", vertical="center")
            if row_idx % 2 == 0:
                cell.fill = even_fill

    for col_idx, header in enumerate(headers, 1):
        max_length = len(str(header))
        for row_idx in range(2, len(cases) + 2):
            cell_value = ws.cell(row=row_idx, column=col_idx).value
            if cell_value is not None:
                max_length = max(max_length, len(str(cell_value)))
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        ws.column_dimensions[col_letter].width = min(max_length + 4, 50)

    ws.freeze_panes = "A2"
    wb.save(filepath)
    return filepath
