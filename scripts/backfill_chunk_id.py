"""回填 rag_chunk.chunk_id（迁移前默认 0 的历史行）。

背景：`rag_chunk` 新增 `chunk_id` 列（= Milvus 稳定主键 stable_chunk_id）后，存量行因 ALTER
拿到默认值 0。离线评测 `offline/rag_eval.py` 靠它从 `chunks_json.chunk_id` 回查正文，
因此需把历史行的 chunk_id 复算补齐，否则这些轮次的 retrieved_contexts 会落空。

复算公式与写入时一致：stable_chunk_id(tenant_id, doc_id, version, parent_id, seq)，
其中 parent_id 取自 meta JSON。幂等：只处理 chunk_id = 0 的行。

若某文档已被重建（版本变化），复算值可能无法匹配 Milvus 中当前实体——此时该文档重索引即可
（重索引会按新版本重新落正确 chunk_id）。

用法：
  python scripts/backfill_chunk_id.py                 # 全量回填
  python scripts/backfill_chunk_id.py --tenant 7      # 只回填某租户
  python scripts/backfill_chunk_id.py --dry-run       # 只统计，不写库
  python scripts/backfill_chunk_id.py --batch 500     # 控制每批行数
"""
import argparse
import asyncio

from common.config import config
from common.logger import logger, setup_logging


async def _main_async(args: argparse.Namespace) -> None:
    from sqlalchemy import select, update

    from store.milvus_store import stable_chunk_id
    from store.orm_models.knowledge import Chunk
    from store.postgres_store import get_sessionmaker

    factory = get_sessionmaker()
    total = 0
    last_id = 0
    while True:
        async with factory() as session:
            stmt = (
                select(
                    Chunk.id, Chunk.tenant_id, Chunk.doc_id,
                    Chunk.version, Chunk.seq, Chunk.meta,
                )
                .where(Chunk.chunk_id == 0, Chunk.id > last_id)
                .order_by(Chunk.id)
                .limit(args.batch)
            )
            if args.tenant:
                stmt = stmt.where(Chunk.tenant_id == args.tenant)
            rows = (await session.execute(stmt)).all()
            if not rows:
                break
            mappings = []
            for cid, tid, did, ver, seq, meta in rows:
                parent = (meta or {}).get('parent_id', '') or ''
                new_id = stable_chunk_id(tid, did, ver, parent, seq)
                mappings.append({'id': cid, 'chunk_id': new_id})
            if args.dry_run:
                logger.info(f'[dry-run] 将更新 {len(mappings)} 行（不写库）')
            else:
                await session.execute(update(Chunk), mappings)
                await session.commit()
                logger.info(f'已更新 {len(mappings)} 行')
            total += len(mappings)
            last_id = rows[-1][0]
    logger.info(f'回填完成：共处理 {total} 行' + (' [dry-run]' if args.dry_run else ''))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='回填 rag_chunk.chunk_id（迁移前默认 0 的历史行）')
    p.add_argument('--tenant', type=int, default=0, help='只回填指定租户（0=全部）')
    p.add_argument('--batch', type=int, default=1000, help='每批处理行数')
    p.add_argument('--dry-run', action='store_true', help='只统计，不写库')
    return p


if __name__ == '__main__':
    setup_logging()
    asyncio.run(_main_async(_build_parser().parse_args()))
