"""API 契约包：请求体（request）与响应 data（response）分离维护。

- request.py ：各接口请求体模型（XXRequest / XXIn）
- response.py：各接口响应 data 模型（success(<模型>) 的 data 结构）

本 __init__ 统一再导出，路由可继续 `from app.api.schemas import X`；
也可按需 `from app.api.schemas.request import X` / `.response import X` 精确引用。
"""
from app.api.schemas.request import *  # noqa: F401,F403
from app.api.schemas.response import *  # noqa: F401,F403

from app.api.schemas.request import __all__ as _req_all
from app.api.schemas.response import __all__ as _resp_all

__all__ = list(_req_all) + list(_resp_all)
