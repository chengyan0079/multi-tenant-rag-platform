"""统一 API 响应与错误码（接入层 M1）

所有接口统一返回: {"code": 0, "msg": "", "data": obj}
- code == 0 成功；非 0 见 ErrCode
- 业务异常抛出 BizException；HTTP / 校验 / 未捕获异常由全局处理器统一包装
"""
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar('T')


class ApiResponse(BaseModel, Generic[T]):
    """统一响应信封：{"code":0,"msg":"","data": <T>}。泛型 T 描述 data 的具体结构。"""
    code: int = 0
    msg: str = ""
    data: T | None = None


def success(data: T | None = None, msg: str = "") -> "ApiResponse[T]":
    return ApiResponse(code=0, msg=msg, data=data)


def fail(code: int, msg: str, data: Any = None) -> ApiResponse:
    return ApiResponse(code=code, msg=msg, data=data)


class BizException(Exception):
    """业务异常：携带业务错误码，由全局异常处理器包装为 {code,msg,data}。"""
    def __init__(self, code: int, msg: str, http_status: int = 200):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.http_status = http_status


class ErrCode:
    OK = 0
    PARAM_ERROR = 40000
    MISSING_TENANT = 40001
    UNAUTHORIZED = 40100      # 调用方签名缺失/过期/不匹配
    FORBIDDEN = 40003
    NOT_FOUND = 40400
    FILE_GONE = 40410
    RATE_LIMIT = 42900
    INTERNAL = 50000
    DEP_UNAVAILABLE = 50200
    TASK_PENDING = 50300
