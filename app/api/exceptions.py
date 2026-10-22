"""全局异常处理器：将 HTTPException / 参数校验错误 / 业务异常 / 未捕获异常
统一包装为 {"code", "msg", "data"} 结构（接入层 M1）。

- HTTP 状态码保留（401/403/410/422/500…），body 内 code 为映射后的业务码；
- 业务异常 BizException 可自定义 http_status（默认 200，前端读 code 判断）。
"""
from fastapi import Request, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.logger import logger
from common.response import fail, BizException, ErrCode

HTTP_CODE_MAP = {
    400: ErrCode.PARAM_ERROR,
    401: ErrCode.MISSING_TENANT,
    403: ErrCode.FORBIDDEN,
    404: ErrCode.NOT_FOUND,
    410: ErrCode.FILE_GONE,
    422: ErrCode.PARAM_ERROR,
    429: ErrCode.RATE_LIMIT,
    500: ErrCode.INTERNAL,
    502: ErrCode.DEP_UNAVAILABLE,
    503: ErrCode.TASK_PENDING,
}


async def http_exc_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """HTTP 异常处理器：映射状态码到业务码并包装响应。"""
    code = HTTP_CODE_MAP.get(exc.status_code, ErrCode.INTERNAL)
    msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content=fail(code, msg).model_dump())


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """参数校验失败处理器。"""
    return JSONResponse(
        status_code=422,
        content=fail(ErrCode.PARAM_ERROR, "参数校验失败", data=exc.errors()).model_dump(),
    )


async def biz_exc_handler(request: Request, exc: BizException) -> JSONResponse:
    """业务异常处理器。"""
    return JSONResponse(status_code=exc.http_status, content=fail(exc.code, exc.msg).model_dump())


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常兜底处理器。"""
    logger.exception(f"未捕获异常: {exc}")
    return JSONResponse(status_code=500, content=fail(ErrCode.INTERNAL, "服务内部错误").model_dump())


def register_exception_handlers(app: FastAPI) -> None:
    """统一注册上述异常处理器到 FastAPI 应用。"""
    app.add_exception_handler(StarletteHTTPException, http_exc_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(BizException, biz_exc_handler)
    app.add_exception_handler(Exception, unhandled_handler)
