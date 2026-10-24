"""文档（索引）DAO（store 数据访问层）：rag_document / rag_chunk 的 PostgreSQL 操作。

文档解析流程：POST /admin/docs 落 rag_document(status=0) → 投递索引任务 →
IndexManager 消费后由 core.rag_indexer.pipeline 完成切分 + 向量化，并把切片写 rag_chunk、
向量写 Milvus，最后回写 rag_document.status(2=完成 / 3=失败)。

实体 → 响应模型的映射在路由层完成，本模块不依赖 app.api.schemas。
"""
from sqlalchemy import delete, select

from app.app_state import app_state
from store.postgres_store import get_sessionmaker
from store.orm_models.file import File
from store.orm_models.knowledge import Chunk, Doc


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
        """返回 {status, chunk_count, err_msg}；不存在抛 KeyError。"""
        async with self._factory() as session:
            doc = (await session.execute(select(Doc).where(Doc.id == doc_id))).scalar_one_or_none()
            if doc is None:
                raise KeyError(f'doc not found: {doc_id}')
            return {
                'status': doc.status,
                'chunk_count': doc.chunk_count,
                'err_msg': doc.err_msg,
            }

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

    async def save_chunks(self, tenant_id: int, doc_id: int, scope: int,
                         chunks: list[dict]) -> int:
        """幂等写切片：先删该 doc 旧切片，再批量插入 rag_chunk。

        chunks 元素需含 text / parent_id / parent_content / doc_name / page（来自 pipeline）。
        """
        async with self._factory() as session:
            await session.execute(
                delete(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id)
            )
            rows = [
                Chunk(
                    tenant_id=tenant_id, doc_id=doc_id, scope=scope, ref_id=c.get('ref_id', 0),
                    seq=seq, content=c['text'], token_count=len(c['text']),
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
            await session.commit()
            return len(rows)
