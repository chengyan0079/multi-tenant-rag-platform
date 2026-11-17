"""故障图谱离线建图：五个直读采集器（只读项目自身资产）+ 幂等 upsert CLI。

直读侧只写能回指到表字段/接口返回值的属性；语义属性（根因/处置）由 LoRA 路线补。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import Any

# 直接运行脚本（python offline/graph_build.py）时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import config
from common.logger import logger, setup_logging
from core.graph.schema import (
    EdgeType, GraphEdge, GraphNode, GraphSnapshot, NodeLabel, NodeSource,
)
from store.neo4j_store import Neo4jStore


def _text(sql: str) -> Any:
    """惰性构造 SQL 文本：让采集逻辑不依赖 SQLAlchemy 即可测试。"""
    from sqlalchemy import text
    return text(sql)


async def collect_api_symptoms(session: Any, tenant_id: int,
                               window_minutes: int = 5) -> GraphSnapshot:
    """从 rag_api_call_log 聚合近窗口的 5xx 占比，写 Symptom（metric_source=log_agg）。"""
    snap = GraphSnapshot(tenant_id)
    since = datetime.now() - timedelta(minutes=window_minutes)
    row = (await session.execute(_text(
        'SELECT count(*) AS total, '
        'sum(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS bad '
        'FROM rag_api_call_log WHERE tenant_id = :tid AND req_time >= :since'
    ), {'tid': tenant_id, 'since': since})).one()
    total, bad = int(row.total or 0), int(row.bad or 0)
    if total == 0:
        return snap
    ratio = bad / total
    snap.add(GraphNode(
        label=NodeLabel.SYMPTOM,
        key=f'5xx-{tenant_id}',
        props={
            'name': f'5xx 占比 {ratio:.0%}',
            'observed_at': datetime.now().isoformat(),
            'metric_source': 'log_agg',
        },
        tenant_id=tenant_id,
    ))
    return snap


async def collect_pool_state(engine: Any, session: Any,
                             tenant_id: int) -> GraphSnapshot:
    """读连接池状态 + pg_stat_activity，写 PgInstance。

    pool.status() 只有 size/checkedout/overflow，没有 waiting()；
    pool_waiting 从 pg_stat_activity 的 active 会话数近似。
    """
    snap = GraphSnapshot(tenant_id)
    status = str(engine.pool.status())
    size = checked = 0
    for part in status.split(','):
        if 'Size:' in part:
            size = int(part.split(':')[-1])
        elif 'Checkedout:' in part:
            checked = int(part.split(':')[-1])
    row = (await session.execute(_text(
        "SELECT count(*) AS waiting FROM pg_stat_activity "
        "WHERE datname = :db AND state = 'active'"
    ), {'db': config.POSTGRES_DATABASE})).one()
    snap.add(GraphNode(
        label=NodeLabel.PG_INSTANCE,
        key=f'{config.POSTGRES_DATABASE}-{tenant_id}',
        props={
            'db_name': config.POSTGRES_DATABASE,
            'pool_size': size,
            'pool_used': checked,
            'pool_waiting': int(row.waiting or 0),
        },
        tenant_id=tenant_id,
    ))
    return snap


async def collect_instance_state(tenant_id: int,
                                 list_instances: Any) -> GraphSnapshot:
    """读 Nacos 服务实例列表，写 RegisteredInstance；status=removed 由规则判定。

    list_instances 是可注入的异步可调用对象（SDK NamingService 或测试 mock），
    返回实例 dict 列表，字段取 ip/port/healthy/enabled。
    """
    snap = GraphSnapshot(tenant_id)
    instances = await list_instances()
    for item in instances or []:
        ip, port = item.get('ip', ''), item.get('port', 0)
        status = 'up' if item.get('healthy') and item.get('enabled') else 'removed'
        snap.add(GraphNode(
            label=NodeLabel.REGISTERED_INSTANCE,
            key=f'{ip}:{port}',
            props={
                'instance_id': f'{ip}:{port}',
                'ip': ip,
                'port': int(port or 0),
                'health': bool(item.get('healthy')),
                'status': status,
                'status_time': datetime.now().isoformat(),
            },
            tenant_id=tenant_id,
        ))
    return snap


async def collect_nacos_changes(client: Any, tenant_id: int) -> GraphSnapshot:
    """读 Nacos v3 配置列表，写 Change / ConfigItem + MODIFIES 边。"""
    snap = GraphSnapshot(tenant_id)
    listing = await asyncio.to_thread(client.list_configs)
    data = listing.get('data') or {} if isinstance(listing, dict) else {}
    items = data.get('configItems') or []
    for item in items:
        data_id = item.get('dataId') or ''
        if not data_id:
            continue
        change = snap.add(GraphNode(
            label=NodeLabel.CHANGE,
            key=data_id,
            props={
                'type': 'config', 'content': data_id,
                'time': datetime.now().isoformat(),
                'source': NodeSource.STRUCTURED.value,
            },
            tenant_id=tenant_id,
        ))
        cfg = snap.add(GraphNode(
            label=NodeLabel.CONFIG_ITEM,
            key=data_id,
            props={'key': data_id, 'scope': 'nacos-SYSTEM'},
            tenant_id=None,   # 系统级配置豁免租户
        ))
        snap.link(change, cfg, EdgeType.MODIFIES)
    return snap


async def collect_config_requests(session: Any, tenant_id: int) -> GraphSnapshot:
    """读 rag_config_change_request（已执行），写 Change（source=structured）。"""
    snap = GraphSnapshot(tenant_id)
    rows = (await session.execute(_text(
        'SELECT id, data_id, title, created_at FROM rag_config_change_request '
        'WHERE status = 3 ORDER BY created_at DESC LIMIT 50'
    ))).all()
    for r in rows:
        snap.add(GraphNode(
            label=NodeLabel.CHANGE,
            key=f'CHG-{r.id}',
            props={
                'type': 'config',
                'content': r.title or r.data_id,
                'time': r.created_at.isoformat() if r.created_at else '',
                'source': NodeSource.STRUCTURED.value,
            },
            tenant_id=tenant_id,
        ))
    return snap


async def build_graph(tenant_id: int, store: Neo4jStore, session: Any,
                      engine: Any, list_instances: Any,
                      nacos_client: Any | None = None) -> dict[str, int]:
    """跑采集器并写入图；单个采集器失败只告警，不阻断其余写入。"""
    collectors = [
        collect_api_symptoms(session, tenant_id),
        collect_pool_state(engine, session, tenant_id),
        collect_instance_state(tenant_id, list_instances),
        collect_config_requests(session, tenant_id),
    ]
    if nacos_client is not None:
        collectors.append(collect_nacos_changes(nacos_client, tenant_id))

    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for coro in collectors:
        try:
            snap = await coro
            nodes.update(snap.nodes)
            edges.extend(snap.edges)
        except Exception as e:  # noqa: BLE001  旁路采集失败不影响其它数据
            logger.warning(f'图采集失败（跳过）: {e}')

    from core.graph.ingest import commit_snapshot
    return await commit_snapshot(store, GraphSnapshot(tenant_id, nodes, edges))


async def _make_nacos_instance_lister() -> Any:
    """构造 Nacos 实例列表回调；离线 CLI 自建 NamingService（与在线 Manager 独立）。"""
    from common.nacos_compat import normalize_namespace_id
    from v2.nacos import ClientConfig, ListInstanceParam, NacosNamingService

    cc = ClientConfig(
        server_addresses=config.NACOS_SERVER_ADDR,
        namespace_id=normalize_namespace_id(config.NACOS_NAMESPACE),
    )
    naming = await NacosNamingService.create_naming_service(cc)

    async def list_instances() -> list[dict[str, Any]]:
        try:
            instances = await naming.list_instances(ListInstanceParam(
                service_name=config.SERVICE_NAME,
                group_name=config.NACOS_GROUP,
                healthy_only=False,
            ))
            return [i.model_dump() for i in instances or []]
        finally:
            await naming.shutdown()

    return list_instances


def _make_nacos_config_client() -> Any:
    """配置中心 client（同步 REST，采集器内部 to_thread 调用）。"""
    from core.config_center.nacos_config import get_config_center_client
    return get_config_center_client()


def main() -> None:
    """CLI：python offline/graph_build.py --tenant 100。"""
    parser = argparse.ArgumentParser(description='构建租户的故障图谱（直读侧）')
    parser.add_argument('--tenant', type=int, default=config.DEFAULT_TENANT_ID)
    args = parser.parse_args()

    setup_logging()
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    async def run() -> None:
        engine = create_async_engine(
            f'postgresql+asyncpg://{config.POSTGRES_USER}:{config.POSTGRES_PASSWORD}'
            f'@{config.POSTGRES_HOST}:{config.POSTGRES_PORT}/{config.POSTGRES_DATABASE}',
            pool_size=config.POSTGRES_POOL_MIN_SIZE,
            max_overflow=config.POSTGRES_POOL_MAX_SIZE - config.POSTGRES_POOL_MIN_SIZE,
        )
        store = Neo4jStore()
        await store.connect()
        try:
            # Nacos 服务发现：按开关接真实实例列表；未启用时传空回调（采集器跳过）
            list_instances: Any = (await _make_nacos_instance_lister()
                                   if config.NACOS_ENABLED else _empty)
            nacos_client = (_make_nacos_config_client()
                            if config.NACOS_CONFIG_ENABLED else None)
            async with async_sessionmaker(engine, class_=AsyncSession)() as session:
                stats = await build_graph(args.tenant, store, session, engine,
                                          list_instances, nacos_client)
                logger.info(f'图构建完成: {stats}')
        finally:
            await store.close()
            await engine.dispose()


async def _empty() -> list:
    return []

    asyncio.run(run())


if __name__ == '__main__':
    main()
