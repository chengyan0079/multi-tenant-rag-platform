"""
test_assembly - 多轮上下文装配单测（pytest）

覆盖设计方案 docs/上下文压缩设计方案.md §4：
记忆独立成块、按句截断、冗余去重、history 截取、memory 压缩与配额模型。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from unittest import mock

from common.config import config
from core.rag_generator import assembly


def test_truncate_by_sentence_keeps_complete_sentence():
    text = '第一句话。第二句话。第三句话。'
    out = assembly.truncate_by_sentence(text, 14)   # 每句 5 字符，14 只能容下两句
    assert out.startswith('第一句话。第二句话。')
    assert '第三句话' not in out


def test_truncate_by_sentence_noop_when_within_limit():
    assert assembly.truncate_by_sentence('短文本。', 100) == '短文本。'


def test_coverage_ratio_detects_overlap():
    memory = '用户问过年假怎么算，答案是按工龄计算。'
    assert assembly.coverage_ratio('年假怎么算', memory) == 1.0
    assert assembly.coverage_ratio('今天午饭吃什么', memory) < 0.5


def test_drop_covered_by_memory_keeps_recent_turn():
    """被记忆覆盖的旧轮次丢掉，最近一轮保留（指代消解要用）。"""
    history = [
        {'role': 'user', 'content': '年假怎么算'},
        {'role': 'assistant', 'content': '按工龄计算'},
        {'role': 'user', 'content': '那病假呢'},
    ]
    memory = '用户问过年假怎么算，答案是按工龄计算。用户接着问病假。'
    kept, dropped = assembly.drop_covered_by_memory(history, memory, threshold=0.6, keep_last=1)
    assert dropped == 2
    assert len(kept) == 1 and kept[0]['content'] == '那病假呢'


def test_limit_memory_marks_truncation():
    text = '一句话。' * 50
    out, truncated = assembly.limit_memory(text, 20)
    assert truncated is True and len(out) < len(text)
    out2, truncated2 = assembly.limit_memory('短', 20)
    assert truncated2 is False and out2 == '短'


def test_assemble_question_puts_memory_before_history():
    """记忆独立成块且不混进"当前问题"，顺序为 记忆 → 历史 → 当前问题。"""
    history = [{'role': 'user', 'content': '上一轮问了什么'}]
    text, stats = assembly.assemble_question('这次问什么', history, '摘要：用户关心年假')
    assert text.startswith('长期记忆:\n摘要：用户关心年假')
    assert '历史对话:\nuser: 上一轮问了什么' in text
    assert text.endswith('当前问题:\n这次问什么')
    assert stats['history_kept'] == 1 and stats['memory_truncated'] is False


def test_assemble_question_omits_empty_blocks():
    text, _ = assembly.assemble_question('只问一句', [], '')
    assert text == '当前问题:\n只问一句'


def test_assemble_question_clips_history_when_over_budget():
    """配额不够时先去重、再逐条截历史；够用了就停，不为了凑下限多截。"""
    history = [{'role': 'user', 'content': f'第{i}轮内容' * 5} for i in range(6)]
    with mock.patch.object(config, 'ASSEMBLY_MAX_PROMPT_CHARS', 120), \
         mock.patch.object(config, 'ASSEMBLY_MIN_HISTORY_MESSAGES', 2), \
         mock.patch.object(config, 'HISTORY_MAX_MESSAGES', 6):
        text, stats = assembly.assemble_question('问题', history, '')
    assert stats['history_in'] == 6 and 2 <= stats['history_kept'] < 6
    assert stats['history_kept'] >= config.ASSEMBLY_MIN_HISTORY_MESSAGES
    assert '第5轮内容' in text and '第0轮内容' not in text   # 保留最近的，丢最旧的


def test_assemble_question_compresses_memory_last():
    """历史已压到下限仍超配额时，才压缩记忆。"""
    history = [{'role': 'user', 'content': '短'}]
    memory = '一句话。' * 100
    with mock.patch.object(config, 'ASSEMBLY_MAX_PROMPT_CHARS', 60), \
         mock.patch.object(config, 'ASSEMBLY_MIN_HISTORY_MESSAGES', 1), \
         mock.patch.object(config, 'MEMORY_MAX_CHARS', 10000):
        text, stats = assembly.assemble_question('问题', history, memory)
    assert stats['memory_truncated'] is True
    assert text.startswith('长期记忆:\n')


def test_history_window_comes_from_config():
    history = [{'role': 'user', 'content': f'第{i}轮'} for i in range(10)]
    with mock.patch.object(config, 'HISTORY_MAX_MESSAGES', 3):
        text, stats = assembly.assemble_question('问题', history, '')
    assert stats['history_kept'] == 3
    assert '第9轮' in text and '第6轮' not in text
