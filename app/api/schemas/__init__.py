"""API 契约包：请求体(request)与响应 data(response)分离维护，本 __init__ 统一再导出。"""
from app.api.schemas.request import *  # noqa: F401,F403
from app.api.schemas.response import *  # noqa: F401,F403

from app.api.schemas.request import __all__ as _req_all
from app.api.schemas.response import __all__ as _resp_all

__all__ = list(_req_all) + list(_resp_all)
