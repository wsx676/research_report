# -*- coding: utf-8 -*-
"""code/tests 公共夹具：让 tests 可直接 import minifars。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


class FakeLLM:
    """替身 LLMClient：按序返回固定 text 响应，记录 prompt 供断言。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def bind(self, stage, agent):
        self.last_bind = (stage, agent)
        return self

    def chat(self, prompt, system=None, max_tokens=None, history=None):
        self.prompts.append(prompt)
        text = self.responses.pop(0)
        return {"content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 10, "output_tokens": 10}}


@pytest.fixture()
def fake_llm():
    return FakeLLM
