"""业务错误码定义与统一错误响应。

统一响应结构：{"code": <int>, "message": <str>, "data": null}
- code == 0 表示成功
- code > 0 表示业务错误（见下方常量）
"""

# ===== 文件上传（10xxx） =====
FILE_FORMAT_NOT_SUPPORTED = 10001   # 文件格式不支持
FILE_SIZE_EXCEEDED = 10002          # 文件大小超限
FILE_ENCRYPTED_OR_CORRUPTED = 10003 # 文件已加密/损坏

# ===== AI 解析（20xxx） =====
OCR_FAILED = 20001                  # OCR识别失败
AI_PARSE_TIMEOUT = 20002            # AI解析超时
AI_PARSE_FAILED = 20003             # AI解析失败

# ===== 案件（40xxx） =====
CASE_NUMBER_EXISTS = 40001          # 案号已存在
MISSING_REQUIRED_FIELD = 40002      # 必填字段缺失

# ===== 认证（50xxx） =====
AUTH_FAILED = 50001                 # 认证失败
PERMISSION_DENIED = 50002           # 权限不足
ACCOUNT_LOCKED = 50003              # 帐户已锁定

ERROR_MESSAGES = {
    FILE_FORMAT_NOT_SUPPORTED: "文件格式不支持",
    FILE_SIZE_EXCEEDED: "文件大小超限",
    FILE_ENCRYPTED_OR_CORRUPTED: "文件已加密/损坏",
    OCR_FAILED: "OCR识别失败",
    AI_PARSE_TIMEOUT: "AI解析超时",
    AI_PARSE_FAILED: "AI解析失败",
    CASE_NUMBER_EXISTS: "案号已存在",
    MISSING_REQUIRED_FIELD: "必填字段缺失",
    AUTH_FAILED: "认证失败",
    PERMISSION_DENIED: "权限不足",
    ACCOUNT_LOCKED: "帐户已锁定",
}


class BizError(Exception):
    """业务异常：携带业务错误码。

    - code：业务错误码（见本模块常量）
    - message：错误提示
    - http_status：HTTP 状态码（与业务码分离）
    """

    def __init__(self, code: int, message: str = None, http_status: int = 400):
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, "未知错误")
        self.http_status = http_status
        super().__init__(self.message)


def error_response(code: int, message: str = None, data=None) -> dict:
    """构造统一响应体。code=0 表示成功。"""
    return {
        "code": code,
        "message": message or (ERROR_MESSAGES.get(code, "成功") if code == 0 else ERROR_MESSAGES.get(code, "未知错误")),
        "data": data,
    }
