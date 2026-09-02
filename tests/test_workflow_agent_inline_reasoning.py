"""Inline reasoning strip + curtain-call skip for workflow agent outputs.

MiniMax M-series models (a) inline their reasoning as a leading
``<think>...</think>`` block in the assistant content instead of a separate
reasoning field (DeepSeek style), and (b) append a short closing
acknowledgement message ("任务已完成…") after the real payload. Workflow
output contracts (pure JSON, chapter prose) expect the clean payload only.
"""

from src.workflow.nodes.agent import AgentNode

strip = AgentNode._strip_inline_reasoning


def _patch_resolve(monkeypatch, record):
    from types import SimpleNamespace

    session = SimpleNamespace(record=record)
    monkeypatch.setattr(
        AgentNode, "_resolve_session", staticmethod(lambda sm, sid: session)
    )


def test_strips_leading_think_block_and_keeps_payload():
    text = "<think>reasoning here</think>\n{\"character_states\": []}"
    assert strip(text) == "{\"character_states\": []}"


def test_plain_output_passes_through_unchanged():
    assert strip("第一章 雪崩临界\n\n正文……") == "第一章 雪崩临界\n\n正文……"


def test_no_think_marker_passes_through():
    assert strip("{\"a\": 1}") == "{\"a\": 1}"


def test_think_containing_json_like_text_is_removed():
    text = "<think>the JSON should be {\"fake\": true}</think>{\"real\": 1}"
    assert strip(text) == "{\"real\": 1}"


def test_multiline_think_block_is_removed():
    text = "<think>\nline1\nline2\n</think>\n\n正文内容"
    assert strip(text) == "正文内容"


def test_unclosed_think_block_is_left_intact():
    # 未闭合的 <think> 说明模型没有产出净载荷，保留原文让上层校验报错
    text = "<think>never closed..."
    assert strip(text) == text


def test_curtain_call_message_is_skipped(monkeypatch):
    """MiniMax 实测形态：载荷消息之后还有一条「已完成」寒暄。"""
    record = [
        {"type": "assistant", "content": '{"character_states": []}'},
        {"type": "tool", "content": '{"success": true}'},
        {"type": "assistant", "content": "角色状态维护已完成，第33章JSON已输出。"},
    ]
    _patch_resolve(monkeypatch, record)
    assert AgentNode._get_latest_ai_message(None, "x") == '{"character_states": []}'


def test_think_plus_fence_plus_curtain_full_chain(monkeypatch):
    record = [
        {"type": "assistant", "content": "<think>推理</think>```json\n{\"a\": 1}\n```"},
        {"type": "assistant", "content": "任务已完成。JSON 已输出。"},
    ]
    _patch_resolve(monkeypatch, record)
    out = AgentNode._get_latest_ai_message(None, "x")
    # 围栏保留给 JSON 修复器剥离
    assert out == "```json\n{\"a\": 1}\n```"


def test_curtain_call_alone_is_returned(monkeypatch):
    """只有一条寒暄消息时仍返回它（保留上游「空输出」报错语义）。"""
    record = [{"type": "assistant", "content": "任务已完成。"}]
    _patch_resolve(monkeypatch, record)
    assert AgentNode._get_latest_ai_message(None, "x") == "任务已完成。"


def test_prose_output_never_treated_as_curtain_call(monkeypatch):
    """正常长正文不会被寒暄规则误伤。"""
    prose = "第一章 雪崩临界\n\n" + "正文内容。" * 200
    record = [{"type": "assistant", "content": prose}]
    _patch_resolve(monkeypatch, record)
    assert AgentNode._get_latest_ai_message(None, "x") == prose


def test_empty_latest_message_is_never_replaced_by_older_payload(monkeypatch):
    """上游契约：最新 assistant 为空时返回空，不得回退到更早消息。"""
    record = [
        {"type": "assistant", "content": '{"older": "payload"}'},
        {"type": "assistant", "content": ""},
    ]
    _patch_resolve(monkeypatch, record)
    assert AgentNode._get_latest_ai_message(None, "x") == ""


def test_empty_message_after_curtain_call_stays_empty(monkeypatch):
    """寒暄被跳过后遇到的空消息仍按契约返回空。"""
    record = [
        {"type": "assistant", "content": '{"older": "payload"}'},
        {"type": "assistant", "content": "任务已完成。"},
        {"type": "assistant", "content": ""},
    ]
    _patch_resolve(monkeypatch, record)
    assert AgentNode._get_latest_ai_message(None, "x") == ""
