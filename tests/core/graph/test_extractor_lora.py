"""LoRA 抽取单测：客户端全 mock，验证置信度投票、闸门与冲突裁决。"""
import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.graph import extractor_lora as el


def _run(coro):
    return asyncio.run(coro)


def test_get_nested_field():
    data = {'root_cause': {'category': 'capacity'}}
    assert el._get(data, 'root_cause.category') == 'capacity'
    assert el._get(data, 'change.type') is None
    assert el._get({'x': 1}, 'x.y') is None


def test_confidence_all_agree():
    outs = [
        {'severity': 'P1', 'start_time': 'T', 'root_cause': {'category': 'capacity'}, 'change': {'type': 'config'}},
        {'severity': 'P1', 'start_time': 'T', 'root_cause': {'category': 'capacity'}, 'change': {'type': 'config'}},
        {'severity': 'P1', 'start_time': 'T', 'root_cause': {'category': 'capacity'}, 'change': {'type': 'config'}},
    ]
    assert el.confidence_of(outs) == 1.0


def test_confidence_partial_agreement():
    outs = [
        {'severity': 'P1', 'start_time': 'T', 'root_cause': {'category': 'capacity'}},
        {'severity': 'P1', 'start_time': 'T', 'root_cause': {'category': 'capacity'}},
        {'severity': 'P2', 'start_time': 'T', 'root_cause': {'category': 'capacity'}},
    ]
    # severity 不一致，其余 2 项一致 => 2/3
    assert abs(el.confidence_of(outs) - round(2 / 3, 4)) < 1e-6


def test_confidence_null_fields_excluded_from_denominator():
    outs = [
        {'severity': 'P1', 'change': None},
        {'severity': 'P1', 'change': None},
    ]
    # change.type 三次都 null，不进分母；只剩 severity 一致 => 1.0
    assert el.confidence_of(outs) == 1.0


def test_confidence_empty_results():
    assert el.confidence_of([]) == 0.0
    assert el.confidence_of([{}, {}]) == 0.0  # 无可比字段不能判满分，否则空结果自动入图


def test_decide_gates():
    assert el.decide({'fault_id': 'x'}, 0.8) == 'high'
    assert el.decide({'fault_id': 'x'}, 0.5) == 'review'
    assert el.decide({'fault_id': 'x'}, 0.3) == 'drop'
    assert el.decide({}, 0.9) == 'drop'


def test_merge_conflict_structured_wins():
    structured = {'pool_size': 2, 'title': '结构化标题'}
    lora = {'pool_size': 8, 'root_cause': 'capacity'}
    merged = el.merge_conflict(structured, lora)
    assert merged['pool_size'] == 2
    assert merged['root_cause'] == 'capacity'
    assert merged['meta']['lora_conflict'] == {'pool_size': 8}


def test_llm_extract_parses_json_and_passes_guided_json():
    client = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = '{"fault_id": "INC-1", "severity": "P1"}'
    client.ainvoke = mock.AsyncMock(return_value=msg)
    schema = {'type': 'object'}
    data = _run(el.llm_extract('复盘正文', client, schema))
    assert data['fault_id'] == 'INC-1'
    kwargs = client.ainvoke.await_args.kwargs
    assert kwargs['extra_body']['guided_json'] == schema


def test_llm_extract_returns_none_on_bad_json():
    client = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = 'not json'
    client.ainvoke = mock.AsyncMock(return_value=msg)
    assert _run(el.llm_extract('x', client, {})) is None


def test_extract_with_confidence_gathers_samples():
    client = mock.MagicMock()
    seen = []

    async def fake(doc, cli, schema):
        seen.append(1)
        return {'severity': 'P1'}

    with mock.patch.object(el, 'llm_extract', side_effect=fake):
        data, conf = _run(el.extract_with_confidence('doc', samples=3, client=client, schema={}))
    assert len(seen) == 3
    assert data['severity'] == 'P1' and conf == 1.0
