"""文档（索引）DAO（store 数据访问层）：rag_document / rag_chunk 的 PostgreSQL 操作。

文档解析流程：POST /admin/docs 落 rag_document(status=0) → 投递索引任务 →
IndexManager 消费后由 core.rag_indexer.pipeline 完成切分 + 向量化，并把切片写 rag_chunk、
向量写 Milvus，最后回写 rag_document.status(2=完成 / 3=失败)。

实体 → 响应模型的映射在路由层完成，本模块不依赖 app.api.schemas。
"""
from sqlalchemy import delete, select, update, func

from app.app_state import app_state
from store.postgres_store import get_sessionmaker
from store.orm_models.file import File
from store.orm_models.knowledge import Chunk, Doc
from store.milvus_store import stable_chunk_id


class DocDao:
    """文档解析记录与切片落库。"""

    def __init__(self, factory=None) -> None:
        self._factory = factory or get_sessionmaker()

    async def create(self, tenant_id: int, file_id: int, doc_type: int = 0) -> int:
        """落 rag_document（status=0 待解析），返回 doc_id；file_id 关联 rag_file.id。"""
        async with self._factory() as session:
            f = (await session.execute(
                select(File).where(File.id == file_id, File.tenant_id == tenant_id)
            )).scalar_one_or_none()
            if f is None:
                raise ValueError(f'file not found: tenant={tenant_id} file_id={file_id}')
            doc = Doc(
                tenant_id=tenant_id, file_upload_id=file_id, doc_type=doc_type,
                file_hash=f.current_file_hash, status=0, chunk_count=0,
            )
            session.add(doc)
            await session.flush()
            await session.commit()
            return doc.id

    async def get_status(self, doc_id: int) -> dict:
        """返回 {status, chunk_count, err_msg, version, doc_type}；不存在抛 KeyError。"""
        async with self._factory() as session:
            doc = (await session.execute(select(Doc).where(Doc.id == doc_id))).scalar_one_or_none()
            if doc is None:
                raise KeyError(f'doc not found: {doc_id}')
            return {
                'status': doc.status,
                'chunk_count': doc.chunk_count,
                'err_msg': doc.err_msg,
                'version': doc.version,
                'doc_type': doc.doc_type,
            }

    async def get_version_chunks(self, tenant_id: int, doc_id: int, version: int) -> list[dict]:
        """取某版本切片的完整内容，用于回滚时重新写入 Milvus。"""
        async with self._factory() as session:
            rows = (await session.execute(
                select(Chunk.seq, Chunk.content, Chunk.meta).where(
                    Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id, Chunk.version == version
                )
            )).all()
        out: list[dict] = []
        for seq, content, meta in rows:
            meta = meta or {}
            out.append({
                'seq': seq,
                'text': content,
                'parent_id': meta.get('parent_id', ''),
                'parent_content': meta.get('parent_content', content),
                'doc_name': meta.get('doc_name', ''),
                'page': meta.get('page', 0),
            })
        return out

    async def update_status(self, doc_id: int, status: int,
                            chunk_count: int = 0, err_msg: str | None = None) -> None:
        """回写解析状态：0=待解析 1=解析中 2=完成 3=失败。"""
        async with self._factory() as session:
            doc = (await session.execute(select(Doc).where(Doc.id == doc_id))).scalar_one_or_none()
            if doc is None:
                return
            doc.status = status
            if chunk_count:
                doc.chunk_count = chunk_count
            if err_msg is not None:
                doc.err_msg = err_msg[:512]
            await session.commit()

    async def next_version(self, tenant_id: int, doc_id: int) -> int:
        """返回该 doc 的下一个版本号（当前最大版本 +1；无切片则为 1）。"""
        async with self._factory() as session:
            row = (await session.execute(
                select(Chunk.version).where(
                    Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id
                ).order_by(Chunk.version.desc()).limit(1)
            )).scalar_one_or_none()
            return (row or 0) + 1

    async def save_chunks(self, tenant_id: int, doc_id: int, scope: int,
                         chunks: list[dict], version: int | None = None) -> int:
        """写切片并维护版本：旧版本 soft-delete（enabled=0，保留可回滚），新版本插入 enabled=1。

        chunks 元素需含 text / parent_id / parent_content / doc_name / page（来自 pipeline）。
        版本号默认自增；显式传入时（如 pipeline 已预取）直接使用，保证 PG 与 Milvus 主键值一致。
        """
        if version is None:
            version = await self.next_version(tenant_id, doc_id)
        async with self._factory() as session:
            # 旧版本软删：仅停用，不物理删除，供版本回滚时重新启用
            await session.execute(
                update(Chunk).where(
                    Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id, Chunk.enabled == 1
                ).values(enabled=0)
            )
            rows = [
                Chunk(
                    tenant_id=tenant_id, doc_id=doc_id, scope=scope, ref_id=c.get('ref_id', 0),
                    chunk_id=stable_chunk_id(tenant_id, doc_id, version, c.get('parent_id', ''), seq),
                    seq=seq, version=version, content=c['text'], token_count=len(c['text']),
                    meta={
                        'parent_id': c.get('parent_id', ''),
                        'doc_name': c.get('doc_name', ''),
                        'page': c.get('page', 0),
                    },
                    enabled=1,
                )
                for seq, c in enumerate(chunks)
            ]
            session.add_all(rows)
            await session.execute(
                update(Doc).where(Doc.id == doc_id).values(version=version)
            )
            await session.commit()
            return len(rows)

    async def list_versions(self, tenant_id: int, doc_id: int) -> list[dict]:
        """列出该 doc 各版本（version / chunk_count / enabled），用于回滚 UI 展示。"""
        async with self._factory() as session:
            rows = (await session.execute(
                select(
                    Chunk.version, Chunk.enabled,
                    func.count(Chunk.id),
                ).where(Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id)
                .group_by(Chunk.version, Chunk.enabled)
            )).all()
        # 同一 version 可能因启用态不同分两行，按 version 聚合
        merged: dict[int, dict] = {}
        for version, enabled, cnt in rows:
            item = merged.setdefault(version, {'version': version, 'chunk_count': 0, 'enabled': 0})
            item['chunk_count'] += cnt
            item['enabled'] = max(item['enabled'], enabled)
        return [merged[v] for v in sorted(merged)]

    async def rollback_version(self, tenant_id: int, doc_id: int, target_version: int) -> int:
        """版本回滚：停用该 doc 全部切片，启用目标版本切片，并把文档当前版本指向目标。

        返回目标版本切片数（启用条数）。Milvus 侧实体增删由调用方（pipeline/路由）负责。
        """
        async with self._factory() as session:
            await session.execute(
                update(Chunk).where(
                    Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id
                ).values(enabled=0)
            )
            result = await session.execute(
                update(Chunk).where(
                    Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id,
                    Chunk.version == target_version,
                ).values(enabled=1)
            )
            await session.execute(
                update(Doc).where(Doc.id == doc_id).values(version=target_version)
            )
            await session.commit()
            return result.rowcount or 0

    async def chunk_ids_of_version(self, tenant_id: int, doc_id: int, version: int) -> list[int]:
        """计算某版本全部切片在 Milvus 的主键（chunk_id），用于向量实体删除/重插。

        parent_id 存于 meta JSON，需取出后按 stable_chunk_id 复算。
        """
        async with self._factory() as session:
            rows = (await session.execute(
                select(Chunk.seq, Chunk.meta).where(
                    Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id, Chunk.version == version
                )
            )).all()
        ids: list[int] = []
        for seq, meta in rows:
            parent_id = (meta or {}).get('parent_id', '')
            ids.append(stable_chunk_id(tenant_id, doc_id, version, parent_id, seq))
        return ids
