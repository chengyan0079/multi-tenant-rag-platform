"""配置中心端到端冒烟测试（需 Nacos 已启动、MySQL 可达）。

运行：
    NACOS_CONFIG_SERVER_ADDR=http://<host>:8848 \
    python tests/test_config_center_e2e.py

依赖 common/config 已加载 NACOS_CONFIG_* 与 MySQL 连接配置。
流程覆盖：新增→通过→执行→loader 热读→修改→回滚→sync_history_nids 补偿。
"""
import asyncio
import sys
import uuid
from pathlib import Path

# 直接运行本脚本时 sys.path[0] 是 tests/，会把 `import app` 解析到 site-packages 里
# 同名的游离包（若存在）；故显式把项目根置前，保证命中本项目 app 包。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.app_state import app_state
from common.config import config
from store.orm_models import Base, ConfigChangeRequest
from core.config_center import service
from core.config_center.loader import get_config_center_loader
from core.config_center.nacos_config import get_config_center_client


def _build_engine():
    dsn = (
        f'mysql+aiomysql://{config.MYSQL_USER}:{config.MYSQL_PASSWORD}'
        f'@{config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DATABASE}?charset=utf8mb4'
    )
    return create_async_engine(dsn, pool_pre_ping=True)


async def _main() -> None:
    engine = _build_engine()
    app_state.db_engine = engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    client = get_config_center_client()
    loader = get_config_center_loader()
    await loader.start()

    data_id = f'e2e.test.{uuid.uuid4().hex}'
    ns = config.NACOS_CONFIG_NAMESPACE
    group = config.NACOS_CONFIG_GROUP

    async with maker() as session:
        # 1) 新增 → 通过 → 执行
        req = await service.create_request(
            session, namespace=ns, group_id=group, data_id=data_id,
            domain='prompt', title='e2e测试', op_type=service.OP_ADD, content_new='{"k": 1}', content_format='json')
        assert req.status == service.ST_PENDING
        req = await service.review(session, request_id=req.id, action=service.RV_PASS)
        assert req.status == service.ST_APPROVED
        req = await service.execute_request(session, request_id=req.id)
        assert req.status == service.ST_EXECUTED
        assert (client.get_config(data_id, group)) == '{"k": 1}'
        print('[OK] 新增 + 通过 + 执行')

        # 2) loader 热读取
        for _ in range(20):
            if await loader.get(data_id, group) == '{"k": 1}':
                break
            await asyncio.sleep(0.3)
        assert await loader.get(data_id, group) == '{"k": 1}'
        print('[OK] loader 热读取')

        # 3) 修改 → 执行
        req = await service.create_request(
            session, namespace=ns, group_id=group, data_id=data_id,
            domain='prompt', title='e2e测试', op_type=service.OP_MODIFY, content_new='{"k": 2}', content_format='json')
        await service.review(session, request_id=req.id, action=service.RV_PASS)
        req = await service.execute_request(session, request_id=req.id)
        assert (client.get_config(data_id, group)) == '{"k": 2}'
        print('[OK] 修改 + 执行')

        # 4) 回滚到首个历史版本
        hist = client.list_history(data_id, group)
        items = (hist or {}).get('pageItems') or []
        first_nid = min(it['nid'] for it in items if 'nid' in it)
        rb = await service.create_rollback(
            session, namespace=ns, group_id=group, data_id=data_id,
            domain='prompt', src_history_nid=first_nid)
        await service.review(session, request_id=rb.id, action=service.RV_PASS)
        rb = await service.execute_request(session, request_id=rb.id)
        assert rb.status == service.ST_EXECUTED
        print('[OK] 回滚 + 执行')

        # 5) sync_history_nids 补偿：人为清空 nid 后重补
        req2 = await service.create_request(
            session, namespace=ns, group_id=group, data_id=data_id,
            domain='prompt', title='e2e测试', op_type=service.OP_MODIFY, content_new='{"k": 3}', content_format='json')
        await service.review(session, request_id=req2.id, action=service.RV_PASS)
        req2 = await service.execute_request(session, request_id=req2.id)
        req2.nacos_history_nid = None
        req2.old_history_nid = None
        await session.commit()
        await service.sync_history_nids(session, request_id=req2.id)
        fixed = (await session.execute(
            select(ConfigChangeRequest).where(ConfigChangeRequest.id == req2.id))).scalar_one()
        assert fixed.nacos_history_nid is not None, 'sync_history_nids 补偿失败'
        print('[OK] sync_history_nids 补偿')

    # 清理 Nacos
    try:
        await asyncio.to_thread(client.remove_config, data_id, group)
    except Exception:
        pass
    await engine.dispose()
    print('ALL PASS')


if __name__ == '__main__':
    try:
        asyncio.run(_main())
    except Exception as e:  # noqa: BLE001
        print(f'FAIL: {e}')
        raise
