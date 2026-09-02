"""Inline reasoning strip for workflow agent outputs.

MiniMax M-series models inline their reasoning as a leading
``<think>...</think>`` block in the assistant content instead of a separate
reasoning field (DeepSeek style). Workflow output contracts (pure JSON,
chapter prose) expect the clean payload only.
"""

from src.workflow.nodes.agent import AgentNode

strip = AgentNode._strip_inline_reasoning


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
