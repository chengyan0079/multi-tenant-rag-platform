"""检索片段压缩：按大小（字符数）对 chunks 做 L0 截取 / L1 精选 / L2 摘要。

设计方案见 docs/上下文压缩设计方案.md §5。定位是**兜底**：
多轮场景的常规裁剪（记忆 / 历史）不在这里，见 core.rag_generator.assembly；
本模块只在"chunks 自身就让 prompt 超窗"时才真正动手。

  L0 按大小截取（零成本）→ L1 相关性精选（零成本）→ L2 LLM 摘要（付费，默认关）

做任何一步都不允许抛错到主链路：异常一律回退原样并告警。
"""
from dataclasses import dataclass, field, replace

from common.config import config
from common.logger import logger
from core.rag_generator.assembly import truncate_by_sentence
from core.rag_retriever.retriever import RetrievedChunk

_TRUNCATE_SUFFIX = '…（本片段已截断）'

_SUMMARY_PROMPT = (
    "下面是检索系统为一个问题召回的若干资料片段。请把它们压缩成一段保留全部关键信息的摘要，"
    "只输出摘要正文，不要加前缀或解释。\n\n"
    "问题：{query}\n\n资料片段：\n{chunks}\n\n摘要："
)


@dataclass
class CompressResult:
    """压缩结果：保留的片段 + 过程信息（供可观测与 RAGAS 取数，见设计方案 §10.2）。"""
    chunks: list[RetrievedChunk]
    method: str = 'none'                      # none | truncate | select | llm_summary
    chars_before: int = 0
    chars_after: int = 0
    kept_indices: list[int] = field(default_factory=list)
    dropped: int = 0

    def to_dict(self) -> dict:
        return {
            'method': self.method,
            'chars_before': self.chars_before,
            'chars_after': self.chars_after,
            'kept_indices': list(self.kept_indices),
            'dropped': self.dropped,
        }


def _score_order(chunks: list[RetrievedChunk]) -> list[int]:
    """按 rerank_score 降序返回下标；分数相同时保持原始顺序（sorted 稳定）。"""
    return sorted(range(len(chunks)), key=lambda i: chunks[i].rerank_score, reverse=True)


def _shortened_texts(chunks: list[RetrievedChunk]) -> list[str]:
    """L0 单片段截短：超过 COMPRESS_MAX_CHUNK_CHARS 的按句截到该长度。"""
    limit = config.COMPRESS_MAX_CHUNK_CHARS
    texts: list[str] = []
    for c in chunks:
        text = c.content or ''
        if limit > 0 and len(text) > limit:
            texts.append(truncate_by_sentence(text, limit, _TRUNCATE_SUFFIX))
        else:
            texts.append(text)
    return texts


def _select_by_budget(texts: list[str], order: list[int], limit: int) -> list[int]:
    """按分数降序累加，累计将超过 limit 就停止；至少保留 1 条，避免丢空上下文。"""
    kept: list[int] = []
    total = 0
    for i in order:
        if kept and total + len(texts[i]) > limit:
            break
        kept.append(i)
        total += len(texts[i])
    return kept


def _build_chunks(chunks: list[RetrievedChunk], texts: list[str], kept: list[int]) -> list[RetrievedChunk]:
    """按保留下标组装输出片段：内容被截短过的用新内容替换（溯源字段保持不变）。"""
    out: list[RetrievedChunk] = []
    for i in kept:
        c = chunks[i]
        out.append(c if texts[i] == c.content else replace(c, content=texts[i]))
    return out


async def _llm_summarize(query: str, texts: list[str]) -> str:
    """L2：对保留片段做摘要合并。失败返回空串，由调用方回退到截取结果。"""
    from core.rag_model.llm_client import acomplete

    joined = "\n\n".join(f"[{i + 1}] {t}" for i, t in enumerate(texts))
    prompt = _SUMMARY_PROMPT.format(query=query, chunks=joined)
    return await acomplete(prompt, temperature=0.0, max_tokens=1024)


def _summary_result(
    chunks: list[RetrievedChunk], kept: list[int], summary: str, chars_before: int
) -> CompressResult:
    """把 L2 摘要包装成一个合成片段：正文是摘要，溯源字段汇总所有被合并的来源。"""
    base = chunks[kept[0]]
    doc_names: list[str] = []
    for i in kept:
        name = chunks[i].doc_name
        if name and name not in doc_names:
            doc_names.append(name)
    merged = replace(
        base,
        content=summary,
        doc_name='、'.join(doc_names) or base.doc_name,
        page=0,
        rerank_score=max(chunks[i].rerank_score for i in kept),
        meta={**base.meta, 'source_indices': list(kept)},
    )
    return CompressResult(
        chunks=[merged], method='llm_summary', chars_before=chars_before,
        chars_after=len(summary), kept_indices=list(kept), dropped=len(chunks) - len(kept),
    )


async def compress_context(
    query: str, chunks: list[RetrievedChunk], session_id: str | None = None
) -> CompressResult:
    """按配置对检索片段做压缩，返回 CompressResult（不抛错，失败由调用方兜底）。"""
    total_count = len(chunks)
    chars_before = sum(len(c.content or '') for c in chunks)
    if total_count == 0 or config.COMPRESS_STRATEGY == 'none':
        return CompressResult(chunks, 'none', chars_before, chars_before, list(range(total_count)), 0)

    order = _score_order(chunks)
    texts = _shortened_texts(chunks)

    # L0：单片段截短（上面已完成）+ 总量截取
    kept = _select_by_budget(texts, order, config.COMPRESS_MAX_TOTAL_CHARS)
    method = 'truncate'

    # L1：丢弃低分片段；若整批都被判为低分则保留原集合，宁可超一点也不丢空上下文
    if config.COMPRESS_STRATEGY in ('select', 'summary'):
        threshold = config.COMPRESS_SIM_THRESHOLD
        filtered = [i for i in kept if chunks[i].rerank_score >= threshold]
        if filtered:
            kept, method = filtered, 'select'

    # 硬上限：优先走 L2 摘要，L2 不可用则强制继续截取
    if sum(len(texts[i]) for i in kept) > config.COMPRESS_HARD_TOTAL_CHARS:
        summary = ''
        if config.COMPRESS_STRATEGY == 'summary' and config.COMPRESS_LLM_SUMMARY_ENABLED:
            try:
                summary = await _llm_summarize(query, [texts[i] for i in kept])
            except Exception as e:  # noqa: BLE001  L2 是可选增益，失败不影响主链路
                logger.warning(f'L2 摘要失败，回退截取结果 [session={session_id}]: {e}')
        if summary:
            return _summary_result(chunks, kept, summary, chars_before)
        kept = _select_by_budget(texts, [i for i in order if i in kept], config.COMPRESS_HARD_TOTAL_CHARS)
        method = 'truncate'

    # 既没丢片段、也没截短内容 → 按 none 计。否则每个请求都会记成 truncate，
    # "压缩触发率"这个指标会虚高到 100%，失去观测意义（设计方案 §7 要靠它判断上游是否该调参数）
    if len(kept) == total_count and all(texts[i] == chunks[i].content for i in kept):
        method = 'none'

    kept.sort()  # 输出保持原始顺序，便于前端按检索次序展示
    out_chunks = _build_chunks(chunks, texts, kept)
    result = CompressResult(
        chunks=out_chunks, method=method, chars_before=chars_before,
        chars_after=sum(len(c.content or '') for c in out_chunks),
        kept_indices=kept, dropped=total_count - len(kept),
    )
    if result.dropped or result.method != 'none':
        logger.info(
            '检索片段压缩 [method=%s, chars=%d→%d, kept=%d, dropped=%d, session=%s]'
            % (result.method, result.chars_before, result.chars_after, len(kept), result.dropped, session_id)
        )
    return result
