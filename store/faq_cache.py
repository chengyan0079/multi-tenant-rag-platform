"""
faq_cache - FAQ 高频问题缓存（数据库源 + Redis 缓存，字面精确匹配）

设计（与 rag-pro 一致，PostgreSQL+Redis，不用 Milvus）：
- 源数据在 rag_faq（数据库），按 tenant_id 隔离。
- 启动时全量载入 Redis：每条 faq:{id} 存完整记录，每个租户一份候选列表
  faq:tenant:{tid}:items（仅含匹配所需字段）。
- 查询时做归一化后的精确匹配：先比 question，再比 similar_questions 各条；
  命中即返回标准答案，避免误触发（不调用大模型）。
- 增删改走写穿透：改动后重载该租户缓存，保证 Redis 与数据库一致。
- 冷启动兜底：Redis 中某租户未载入时，回源数据库查询该租户（小集合）。
"""
from typing import Any, Optional, Tuple

from sqlalchemy import select

from app.app_state import app_state
from common.config import config
from common.logger import logger
from store.postgres_store import get_sessionmaker
from store.orm_models.faq import Faq


# ----------------------------- 键与归一化 -----------------------------

def _faq_key(faq_id: int) -> str:
    return f'faq:{faq_id}'


def _faq_tenant_key(tenant_id: int) -> str:
    return f'faq:tenant:{tenant_id}:items'


def _normalize(text: str) -> str:
    """去首尾空白，作为精确匹配的归一化键。"""
    return (text or '').strip()


def _parse_similar(raw: str | None) -> list[str]:
    """similar_questions 多行文本 → 候选列表。"""
    if not raw:
        return []
    return [s.strip() for s in raw.replace('\r\n', '\n').split('\n') if s.strip()]


# ----------------------------- 纯匹配（可单测） -----------------------------

def match_faq(items: list[dict], query: str) -> Optional[int]:
    """在候选列表里做字面精确匹配，返回命中的 faq_id；未命中返回 None。

    items 元素：{'faq_id': int, 'question': str, 'similar_questions': list[str]}
    """
    q = _normalize(query)
    if not q or not items:
        return None
    for it in items:
        if _normalize(it.get('question', '')) == q:
            return it['faq_id']
        for s in it.get('similar_questions', []):
            if _normalize(s) == q:
                return it['faq_id']
    return None


# ----------------------------- BM25 相似匹配（按租户） -----------------------------
# 精确匹配未命中时，用 BM25 对"标准问题 + 相似问"做模糊匹配，得分 ≥ 阈值才采纳（对齐 rag-pro）。
# jieba / rank_bm25 为可选依赖（见 requirements.txt）：缺失则 _tokenize 退化为字符粗切、
# _build_bm25 返回空，整体退化为仅精确匹配，不影响主链路。
_STOPWORDS = frozenset({
    '的', '了', '和', '是', '就', '都', '而', '及', '与', '着', '或', '并', '则', '之', '其', '该',
    '吗', '呢', '吧', '啊', '呀', '嘛', '么', '把', '被', '让', '向', '从', '往', '对', '对于', '将',
    '怎么', '怎样', '如何', '什么', '为什么', '哪些', '哪个', '多少', '请', '帮我', '我想', '想要',
    '可以', '能', '会', '要', '想', '知道', '一下', '有', '没', '没有', '这', '那', '个', '咱们',
})
_PUNCT = set('，。！？、；：""\'\'（）()【】《》<>~·-_\r\n\t ')


def _tokenize(text: str) -> list[str]:
    """中文分词 + 去停用词/标点（jieba 缺失时退化为字符级，保证可用）。"""
    s = (text or '').strip()
    if not s:
        return []
    try:
        import jieba
        words = jieba.lcut(s)
    except Exception:  # noqa: BLE001
        words = list(s)
    return [w for w in (x.strip() for x in words)
            if w and w not in _STOPWORDS and not all(ch in _PUNCT for ch in w)]


def _items_key(items: list[dict]) -> str:
    """候选列表内容指纹：FAQ 增删改后变化，用于触发 BM25 索引重建（自动失效）。"""
    return _json_dumps([(it.get('faq_id'), it.get('question'), it.get('similar_questions')) for it in items])


def _build_bm25(items: list[dict]):
    """基于候选列表构建 BM25 索引，返回 (bm25, faq_ids)；依赖缺失/空列表返回 (None, [])。"""
    try:
        from rank_bm25 import BM25Okapi
    except Exception:  # noqa: BLE001
        return None, []
    corpus = [f"{it.get('question', '')} {' '.join(it.get('similar_questions', []))}" for it in items]
    tokenized = [_tokenize(c) for c in corpus]
    if not any(tokenized):
        return None, []
    return BM25Okapi(tokenized), [it['faq_id'] for it in items]


# 按租户缓存 BM25 索引：tenant_id -> (items_key, bm25, faq_ids)；候选变化才重建（条目数≈租户数，有界）
_BM25_CACHE: dict[int, tuple[str, Any, list[int]]] = {}


def bm25_match(items: list[dict], query: str, threshold: float, tenant_id: int) -> Optional[int]:
    """BM25 相似匹配：对候选 FAQ 打分，返回得分最高且 ≥ 阈值者的 faq_id；否则 None。"""
    if not items or threshold <= 0:
        return None
    q_tokens = _tokenize(query)
    if not q_tokens:
        return None
    key = _items_key(items)
    cached = _BM25_CACHE.get(tenant_id)
    if cached and cached[0] == key:
        bm25, faq_ids = cached[1], cached[2]
    else:
        bm25, faq_ids = _build_bm25(items)
        _BM25_CACHE[tenant_id] = (key, bm25, faq_ids)
    if bm25 is None or not faq_ids:
        return None
    scores = bm25.get_scores(q_tokens)
    best_idx = int(scores.argmax())
    return faq_ids[best_idx] if float(scores[best_idx]) >= threshold else None


# ----------------------------- Redis 读写 -----------------------------

async def _reload_tenant(tenant_id: int) -> None:
    """从数据库重载某租户的 FAQ 缓存（单条记录 + 候选列表）。"""
    r = app_state.redis
    if r is None:
        return
    factory = get_sessionmaker()
    async with factory() as session:
        rows = (await session.execute(
            select(Faq).where(Faq.tenant_id == tenant_id, Faq.enabled == 1)
        )).scalars().all()
        items: list[dict] = []
        for row in rows:
            sims = _parse_similar(row.similar_questions)
            rec = {
                'tenant_id': row.tenant_id,
                'question': row.question,
                'answer': row.answer,
                'question_type': row.question_type,
                'similar_questions': sims,
                'enabled': row.enabled,
            }
            await r.set(_faq_key(row.id), _json_dumps(rec), ex=config.REDIS_TTL_FAQ)
            items.append({'faq_id': row.id, 'question': row.question, 'similar_questions': sims})
        await r.set(_faq_tenant_key(tenant_id), _json_dumps(items), ex=config.REDIS_TTL_FAQ)


async def load_all_faqs_to_redis() -> int:
    """启动全量载入：遍历所有租户并重载。返回覆盖的租户数。"""
    if app_state.redis is None:
        logger.warning('Redis 未初始化，跳过 FAQ 缓存载入')
        return 0
    factory = get_sessionmaker()
    async with factory() as session:
        tenant_ids = (await session.execute(select(Faq.tenant_id).distinct())).scalars().all()
    for tid in tenant_ids:
        try:
            await _reload_tenant(tid)
        except Exception:
            logger.exception(f'FAQ 缓存载入失败 [tenant={tid}]')
    logger.info(f'FAQ 缓存载入完成，覆盖 {len(tenant_ids)} 个租户')
    return len(tenant_ids)


async def get_faq_answer(tenant_id: int, query: str) -> Tuple[Optional[str], Optional[int]]:
    """字面精确匹配 FAQ，返回 (answer, faq_id)；未命中返回 (None, None)。"""
    q = _normalize(query)
    if not q:
        return None, None

    r = app_state.redis
    items: Optional[list] = None
    if r is not None:
        raw = await r.get(_faq_tenant_key(tenant_id))
        if raw:
            try:
                items = _json_loads(raw)
            except Exception:
                items = None
        # Redis 中该租户未载入（冷启动）：回源数据库兜底
        if items is None:
            items = await _match_from_db(tenant_id)

    if not items:
        return None, None

    faq_id = match_faq(items, query)
    if faq_id is None and config.FAQ_BM25_ENABLED:
        # 精确未命中 → BM25 相似 + 阈值兜底（对齐 rag-pro；按租户缓存，FAQ 变更自动失效）
        faq_id = bm25_match(items, query, config.FAQ_BM25_THRESHOLD, tenant_id)
    if faq_id is None:
        return None, None
    return await _answer_by_id(faq_id)


async def _answer_by_id(faq_id: int) -> Tuple[Optional[str], Optional[int]]:
    r = app_state.redis
    if r is not None:
        raw = await r.get(_faq_key(faq_id))
        if raw:
            try:
                return _json_loads(raw).get('answer'), faq_id
            except Exception:
                pass
    # Redis 缺失，回源 DB
    try:
        factory = get_sessionmaker()
        async with factory() as session:
            row = await session.get(Faq, faq_id)
            if row and row.enabled and row.tenant_id is not None:
                return row.answer, faq_id
    except Exception as e:
        logger.warning(f'FAQ 答案回源失败 [faq_id={faq_id}]: {e}')
    return None, faq_id


async def _match_from_db(tenant_id: int) -> Optional[list]:
    try:
        factory = get_sessionmaker()
        async with factory() as session:
            rows = (await session.execute(
                select(Faq).where(Faq.tenant_id == tenant_id, Faq.enabled == 1)
            )).scalars().all()
    except Exception as e:
        logger.warning(f'FAQ 冷启动回源失败 [tenant={tenant_id}]: {e}')
        return None
    items = [
        {'faq_id': row.id, 'question': row.question,
         'similar_questions': _parse_similar(row.similar_questions)}
        for row in rows
    ]
    return items or None


# ----------------------------- 写穿透 -----------------------------

async def upsert_faq_cache(tenant_id: int) -> None:
    """新增/更新 FAQ 后，重载该租户缓存。"""
    try:
        await _reload_tenant(tenant_id)
    except Exception:
        logger.exception(f'FAQ 写穿透失败 [tenant={tenant_id}]')


async def delete_faq_cache(faq_id: int, tenant_id: int) -> None:
    """删除 FAQ 后，清掉单条键并重载该租户缓存。"""
    r = app_state.redis
    if r is not None:
        try:
            await r.delete(_faq_key(faq_id))
        except Exception:
            logger.warning(f'FAQ 单条键删除失败 [faq_id={faq_id}]')
    await upsert_faq_cache(tenant_id)


# ----------------------------- JSON 辅助 -----------------------------

def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(raw) -> object:
    import json
    return json.loads(raw)
