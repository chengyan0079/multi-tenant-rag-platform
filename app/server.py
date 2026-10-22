"""
server - Manager 启动项 + AppServer 编排（设计文档 §11）

每个启动项封装成 XXManager（统一 start()/stop()），按"阶段(stage)"分批启动：
同一 stage 内相互独立的 Manager 用 asyncio.gather 并发执行以缩短启动耗时，
stage 之间按序执行以满足依赖；任一启动项失败即记 "启动失败" 日志并回滚已启动项。
HTTP 服务由 uvicorn 以 workers 多进程方式拉起（见 app/main.py）。
"""
import asyncio
import time
from typing import Dict, List, Tuple

from common.logger import logger


class StartupError(RuntimeError):
    """任一启动项初始化失败，阻断服务启动。"""


class Manager:
    """启动项基类，统一生命周期接口。"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class AppServer:
    """按 stage 分批并发启动 Manager；停机时按相反顺序释放。"""

    def __init__(self, name: str = "rag") -> None:
        self.name = name
        self._items: List[Tuple[int, int, str, Manager]] = []  # (stage, order, name, mgr)

    def register(self, manager: Manager, order: int = 100, stage: int = 0) -> "AppServer":
        """登记启动项；stage 越小越先启动（同 stage 内并发），order 仅决定 stage 内日志顺序。"""
        self._items.append((stage, order, manager.__class__.__name__, manager))
        self._items.sort(key=lambda x: (x[0], x[1]))
        return self

    async def _start_one(self, name: str, mgr: Manager) -> None:
        logger.info(f'[{self.name}] starting {name} ...')
        t0 = time.perf_counter()
        await mgr.start()
        cost_ms = (time.perf_counter() - t0) * 1000
        logger.info(f'[{self.name}] {name} 启动完成，耗时 {cost_ms:.1f} ms')

    async def start_all(self) -> None:
        t0 = time.perf_counter()          # 启动计时起点（perf_counter 单调时钟，ms 精度）
        started: List[Tuple[str, Manager]] = []
        # 按 stage 分桶（_items 已按 (stage, order) 排序）
        stages: Dict[int, List[Tuple[str, Manager]]] = {}
        for stage, _order, name, mgr in self._items:
            stages.setdefault(stage, []).append((name, mgr))

        for stage in sorted(stages):
            group = stages[stage]
            names = [n for n, _ in group]
            logger.info(f'[{self.name}] 阶段 {stage} 并发启动: {names}')
            # 并发执行本阶段全部启动项；return_exceptions 保证单个失败不取消其他
            results = await asyncio.gather(
                *[self._start_one(n, m) for n, m in group],
                return_exceptions=True,
            )
            failed = None
            for (name, mgr), res in zip(group, results):
                if isinstance(res, BaseException):
                    logger.opt(exception=res).error(f'[{self.name}] 启动失败：{name} - {res}')
                    if failed is None:
                        failed = (name, res)
                else:
                    started.append((name, mgr))
            if failed is not None:
                fname, exc = failed
                await self._reverse_stop(started)
                raise StartupError(f'{fname} 启动失败') from exc
        total_ms = (time.perf_counter() - t0) * 1000
        logger.info(f'[{self.name}] 全部启动项完成，总计 {len(started)} 项，启动耗时 {total_ms:.1f} ms')

    async def stop_all(self) -> None:
        """逆序释放全部已登记 Manager（忽略单个释放异常，配合幂等 stop）。"""
        await self._reverse_stop([(n, m) for _, _, n, m in self._items])

    async def _reverse_stop(self, started: List[Tuple[str, Manager]]) -> None:
        for name, mgr in reversed(started):
            try:
                logger.info(f'[{self.name}] stopping {name} ...')
                await mgr.stop()
            except Exception:
                logger.exception(f'[{self.name}] stop failed: {name}')
