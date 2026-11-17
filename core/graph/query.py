"""图查询：三条固定 Cypher（因果链 / 影响面 / 相似事故），全部带租户过滤。"""
from __future__ import annotations

from typing import Any

from common.config import config
from store.neo4j_store import Neo4jStore

# 因果链：变更 → 配置项 → PG 实例 → 被摘除实例，算时滞（文档 Q1）
Q_CAUSAL_CHAIN = """
MATCH (c:Change {tenant_id: $tid})-[m:MODIFIES]->(ci:ConfigItem)
MATCH (ci)-[:APPLIES_TO]->(pg:PgInstance {tenant_id: $tid})
MATCH (ri:RegisteredInstance {tenant_id: $tid})-[:DEPENDS_ON]->(pg)
WHERE ri.status = 'removed' AND ri.status_time > c.time
RETURN c.change_id AS change_id, c.content AS change,
       ci.key AS config_key, m.before AS before, m.after AS after,
       pg.pool_size AS pool_size, pg.pool_waiting AS pool_waiting,
       ri.instance_id AS instance_id, ri.status_time AS status_time,
       duration.between(datetime(c.time), datetime(ri.status_time)).seconds AS lag_s
ORDER BY ri.status_time DESC LIMIT 3
"""

# 影响面：某库被哪些注册实例依赖，及其关联故障（文档 Q2）
Q_IMPACT = """
MATCH (pg:PgInstance {tenant_id: $tid, db_name: $db_name})
MATCH (ri:RegisteredInstance {tenant_id: $tid})-[:DEPENDS_ON]->(pg)
OPTIONAL MATCH (f:Fault {tenant_id: $tid})-[:AFFECTS]->(ri)
RETURN ri.instance_id AS instance_id, ri.status AS status,
       ri.status_time AS status_time, ri.health AS health,
       collect(DISTINCT f.title) AS faults
ORDER BY ri.status_time DESC
"""

# 相似事故 + 处置手册（文档 Q3）；未给 fault_id 时取最近一条 Fault 的相似事故
Q_SIMILAR = """
MATCH (f:Fault {tenant_id: $tid})
WHERE $fault_id IS NULL OR f.fault_id = $fault_id
WITH f ORDER BY f.start_time DESC LIMIT 1
MATCH (f)-[:SIMILAR_TO]->(g:Fault)
OPTIONAL MATCH (g)-[:RESOLVED_BY]->(rb:Runbook)
RETURN g.fault_id AS fault_id, g.title AS title, g.start_time AS start_time,
       g.confidence AS fault_conf, rb.title AS runbook_title,
       rb.steps AS runbook_steps, rb.confidence AS rb_conf
ORDER BY g.start_time DESC LIMIT 3
"""

QUERIES: dict[str, str] = {
    'rootcause': Q_CAUSAL_CHAIN,
    'impact': Q_IMPACT,
    'runbook': Q_SIMILAR,
}


async def graph_search(qtype: str, store: Neo4jStore, tenant_id: int,
                       **params: Any) -> list[dict[str, Any]]:
    """按问题类型选 Cypher；未知类型返回空列表。params 为查询参数（如 db_name/fault_id）。"""
    cypher = QUERIES.get(qtype)
    if not cypher:
        return []
    # Cypher 里引用的参数要齐全，Neo4j 对未绑定参数直接报错
    full = {'fault_id': None, 'db_name': config.POSTGRES_DATABASE, **params}
    return await store.query(cypher, tid=tenant_id, **full)
