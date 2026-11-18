from __future__ import annotations

from ai_server_monitor.llm.text import strip_reasoning


def test_strip_reasoning() -> None:
    assert strip_reasoning("<think>hmm</think>\nAnswer") == "Answer"
    assert strip_reasoning("<THINKING>a\nb</THINKING>Hi") == "Hi"
    assert strip_reasoning("<think>cut off mid thought") == ""
    assert strip_reasoning("no opening tag</think> result") == "result"
    assert strip_reasoning("plain") == "plain"
