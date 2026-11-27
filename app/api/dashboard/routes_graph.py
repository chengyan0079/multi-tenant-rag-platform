"""故障图谱调试接口（运营后台，只读）：直查图证据与统计，不进入生成链路。"""
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from app.api.deps import resolve_tenant
from app.app_state import app_state
from common.response import BizException, success
from core.graph.query import graph_search
from core.graph.render import render_evidence

router = APIRouter(
    prefix="/admin/graph",
    tags=["graph-debug"],
    dependencies=[Depends(resolve_tenant)],
)

_QTYPE_CHOICES = ('rootcause', 'impact', 'runbook')


def _store():
    if app_state.neo4j_store is None:
        raise BizException(50300, '图库不可用（GRAPH_ENABLED=false 或连接失败）')
    return app_state.neo4j_store


@router.get('/query', summary='直查图证据（不进入生成链路）')
async def query_graph(
    qtype: str = Query(..., description='查询类型：rootcause/impact/runbook'),
    tenant_id: int = Depends(resolve_tenant),
    fault_id: str | None = None,
):
    """返回渲染后的图证据候选，供联调与召回质量排查。租户取自身份头，不接受 query 覆盖。"""
    if qtype not in _QTYPE_CHOICES:
        raise BizException(40000, f'qtype 必须为 {_QTYPE_CHOICES}')
    rows = await graph_search(qtype, _store(), tenant_id,
                              fault_id=fault_id)
    return success([asdict(c) for c in render_evidence(qtype, rows)])


@router.get('/stats', summary='图数据统计')
async def graph_stats(tenant_id: int = Depends(resolve_tenant)):
    """返回当前租户各标签节点数与关系数。"""
    store = _store()
    nodes = await store.query(
        'MATCH (n) WHERE n.tenant_id = $tid RETURN labels(n)[0] AS label, count(*) AS count ORDER BY label',
        tid=tenant_id)
    edges = await store.query(
        'MATCH ()-[r]->() WHERE r.tenant_id = $tid RETURN type(r) AS type, count(*) AS count ORDER BY type',
        tid=tenant_id)
    return success({'nodes': nodes, 'edges': edges})
