"""
logger - 日志配置（基于 loguru）

日志级别（由低到高）：
TRACE(5) < DEBUG(10) < INFO(20) < SUCCESS(25) < WARNING(30) < ERROR(40) < CRITICAL(50)

loguru 级别数值与标准库 logging 一致，config 里的级别字段可直接用整型，
例如 logging.WARNING == 30 对应 loguru 的 'WARNING'。
"""
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger as _logger

from common.config import config, BASE_DIR

# 全链路 trace_id：由 TraceMiddleware / resolve_tenant 写入 contextvars，
# 日志 patcher 读取并注入每条日志，便于按 trace_id 收集观察调用链路。
trace_id_ctx: ContextVar[str | None] = ContextVar('trace_id', default=None)


def _inject_trace_id(record: dict) -> None:
    """把当前上下文 trace_id 注入日志 record（无则 '-'）。"""
    record['extra']['trace_id'] = trace_id_ctx.get() or '-'


# 统一日志格式：时间 | 级别 | trace_id | 名称:文件:行 - 消息
LOG_FORMAT = (
    '{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | trace={extra[trace_id]} | '
    '{name}:{file}:{line} - {message}'
)

logger = (
    _logger
    .patch(lambda r: r.update(name=f'{BASE_DIR.name}.{r["name"]}'))
    .patch(_inject_trace_id)
)


class DatedSizeRotatingSink:
    """按「日期 + 大小」切片、文件名形如 app-YYYYMMDD-<index>.log 的文件写入器。

    跨天序号归零并清理过期文件；同天内单文件超上限则序号自增切片。
    """

    _PATTERN = re.compile(r'^app-(\d{8})-.*\.log$')

    def __init__(
        self,
        log_dir: Path,
        max_bytes: int = 1024 ** 3,
        encoding: str = 'utf-8',
        retention_days: int = 7,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.max_bytes = max_bytes
        self.encoding = encoding
        self.retention_days = retention_days
        self._date = ''
        self._index = 0
        self._file = None
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _open(self) -> None:
        if self._file is not None:
            self._file.close()
        path = self.log_dir / f'app-{self._date}-{self._index}.log'
        self._file = open(path, 'a', encoding=self.encoding)

    def _cleanup(self) -> None:
        """删掉超过保留天数的历史日志文件。"""
        if self.retention_days <= 0:
            return
        today = datetime.strptime(self._date, '%Y%m%d')
        cutoff = (today - timedelta(days=self.retention_days)).strftime('%Y%m%d')
        for p in self.log_dir.glob('app-*-*.log'):
            m = self._PATTERN.match(p.name)
            if m and m.group(1) < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass

    def __call__(self, message: Any) -> None:
        record = message.record
        day = record['time'].strftime('%Y%m%d')
        text = str(message)
        size = len(text.encode(self.encoding, errors='replace'))

        if day != self._date:
            self._date = day
            self._index = 0
            self._cleanup()
            self._open()
        elif self._file.tell() + size > self.max_bytes:
            self._index += 1
            self._open()

        self._file.write(text)
        self._file.flush()

    def stop(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def setup_logging(*, level: int | str | None = None) -> None:
    """配置日志（项目启动时调用一次）。

    控制台始终输出；文件按「日期 + 大小」切片，目录取 config.LOG_FILE 的父目录。
    level 提供时优先，否则用 config.LOG_LEVEL；控制台与文件共用同一等级。
    """
    logger.remove()

    lvl = level or config.LOG_LEVEL

    logger.add(sys.stderr, level=lvl, colorize=False, format=LOG_FORMAT)

    log_dir = config.LOG_FILE.parent
    file_sink = DatedSizeRotatingSink(
        log_dir,
        max_bytes=config.LOG_SIZE,
        encoding='utf-8',
        retention_days=config.LOG_FILE_DAYS,
    )
    logger.add(file_sink, level=lvl, enqueue=True, format=LOG_FORMAT)


def get_logger(name: str | None = None) -> Any:
    """获取 logger（loguru 为单例，name 仅用于语义，不创建独立 logger）。"""
    return logger


__all__ = ['logger', 'setup_logging', 'get_logger', 'trace_id_ctx', 'LOG_FORMAT']
