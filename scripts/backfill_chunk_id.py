"""回填 rag_chunk.chunk_id 历史行（新增列前默认 0）。按 stable_chunk_id(tenant_id,doc_id,version,parent_id,seq) 复算，幂等只处理 chunk_id=0；文档已重建则重索引即可。支持 --tenant/--dry-run/--batch。"""
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
