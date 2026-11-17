"""Neo4j 异步存储封装：约束初始化 + 幂等 upsert + Cypher 查询。"""
from __future__ import annotations

from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from common.config import config
from common.logger import logger
from core.graph.schema import (
    EdgeType, GraphEdge, GraphNode, NodeLabel,
)

# 约束/索引：id 唯一保证 upsert 幂等；tenant_id 索引支撑隔离查询
_CONSTRAINTS = tuple(
    f'CREATE CONSTRAINT {label.value.lower()}_id IF NOT EXISTS '
    f'FOR (n:{label.value}) REQUIRE n.id IS UNIQUE'
    for label in NodeLabel
)
_TENANT_INDEXES = tuple(
    f'CREATE INDEX {label.value.lower()}_tenant IF NOT EXISTS '
    f'FOR (n:{label.value}) ON (n.tenant_id)'
    for label in NodeLabel
    if label.value != 'ConfigItem'   # 系统级配置无 tenant_id，建索引无意义
)


class Neo4jStore:
    """图存储：与 MilvusStore 同层的异步驱动封装，租户校验在 schema 层完成。"""

    def __init__(self, driver: AsyncDriver | None = None) -> None:
        self.driver = driver

    async def connect(self) -> None:
        """建立驱动连接；由 AppServer 或离线 CLI 调用。"""
        if self.driver is not None:
            return
        self.driver = AsyncGraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        )
        logger.info(f'Neo4j 连接就绪: {config.NEO4J_URI}')

    async def close(self) -> None:
        if self.driver is not None:
            await self.driver.close()
            self.driver = None

    async def ensure_schema(self) -> None:
        """建唯一约束与租户索引；重复调用安全。"""
        if self.driver is None:
            raise RuntimeError('Neo4j 未连接，请先调用 connect()')
        async with self.driver.session() as session:
            for stmt in (*_CONSTRAINTS, *_TENANT_INDEXES):
                await session.run(stmt)

    async def upsert_nodes(self, nodes: list[GraphNode]) -> int:
        """MERGE 节点：按 id 幂等覆盖属性，保留首次写入的 source 之外的字段由本次决定。"""
        if not nodes:
            return 0
        if self.driver is None:
            raise RuntimeError('Neo4j 未连接，请先调用 connect()')
        async with self.driver.session() as session:
            for node in nodes:
                props = dict(node.props)
                props.update({
                    'id': node.node_id,
                    'tenant_id': node.tenant_id,
                    'source': node.source.value,
                })
                await session.run(
                    f'MERGE (n:{node.label.value} {{id: $id}}) SET n += $props',
                    id=node.node_id, props=props,
                )
        return len(nodes)

    async def upsert_edges(self, edges: list[GraphEdge]) -> int:
        """MERGE 边：两端节点必须已存在；租户校验由 schema.build_edge 在构造时完成。"""
        if not edges:
            return 0
        if self.driver is None:
            raise RuntimeError('Neo4j 未连接，请先调用 connect()')
        async with self.driver.session() as session:
            for edge in edges:
                props = dict(edge.props)
                props.update({'tenant_id': edge.tenant_id})
                # 带 label 匹配，避免不同标签恰好同 id 时误连
                a = f':{edge.start_label.value}' if edge.start_label else ''
                b = f':{edge.end_label.value}' if edge.end_label else ''
                await session.run(
                    f'MATCH (a{a} {{id: $start_id}}), (b{b} {{id: $end_id}}) '
                    f'MERGE (a)-[r:{edge.edge_type.value}]->(b) SET r += $props',
                    start_id=edge.start_id, end_id=edge.end_id, props=props,
                )
        return len(edges)

    async def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """执行只读 Cypher，返回 dict 列表；写操作用 execute。"""
        return await self.execute(cypher, **params)

    async def execute(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """执行带写操作的 Cypher（如 MERGE），调用方需自行保证幂等与租户过滤。"""
        if self.driver is None:
            raise RuntimeError('Neo4j 未连接，请先调用 connect()')
        async with self.driver.session() as session:
            result = await session.run(cypher, **params)
            return await result.data()
