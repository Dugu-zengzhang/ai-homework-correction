# -*- coding: utf-8 -*-
"""T-C01 大模型输出 Schema 校验 & T-B04 提示词学情范围注入 —— 单元测试。

覆盖点：
  - 必填字段缺失（question / is_correct）→ 单题丢弃
  - is_correct 类型非法（"yes" / 1）→ 单题丢弃；null 合法保留
  - 得分非法（"优秀"）→ 置空
  - questions 非数组 → 置空
  - 混合场景：好题保留、坏题丢弃
  - 学段 / 教材版本 / 地区注入提示词；未传时保持原行为
"""
import json

import pytest

pytestmark = pytest.mark.unit


class TestSchemaValidation:
    """T-C01：_validate_result 的清洗与丢弃策略。"""

    def test_valid_result_passes_through(self):
        from llm_service import _validate_result
        payload = {
            "score": "85",
            "overall_comment": "整体不错",
            "questions": [
                {"question": "题1", "is_correct": True, "score": "10"},
                {"question": "题2", "is_correct": None, "score": None},
            ],
        }
        cleaned, warnings = _validate_result(payload)
        assert warnings == []
        assert len(cleaned["questions"]) == 2

    def test_missing_question_field_dropped(self):
        from llm_service import _validate_result
        cleaned, warnings = _validate_result({
            "questions": [
                {"is_correct": True},
                {"question": "   ", "is_correct": True},
                {"question": "题3", "is_correct": True},
            ]
        })
        assert len(cleaned["questions"]) == 1
        assert cleaned["questions"][0]["question"] == "题3"
        assert any("question" in w for w in warnings)

    def test_missing_is_correct_field_dropped(self):
        from llm_service import _validate_result
        cleaned, warnings = _validate_result({
            "questions": [{"question": "题1"}, {"question": "题2", "is_correct": False}]
        })
        assert len(cleaned["questions"]) == 1
        assert cleaned["questions"][0]["question"] == "题2"
        assert any("is_correct" in w for w in warnings)

    def test_is_correct_wrong_type_dropped(self):
        from llm_service import _validate_result
        cleaned, warnings = _validate_result({
            "questions": [
                {"question": "题1", "is_correct": "yes"},
                {"question": "题2", "is_correct": 1},
                {"question": "题3", "is_correct": None},
            ]
        })
        assert [q["question"] for q in cleaned["questions"]] == ["题3"]
        assert len(warnings) == 2

    def test_non_dict_question_dropped(self):
        from llm_service import _validate_result
        cleaned, warnings = _validate_result({
            "questions": ["这是一个字符串", 123, {"question": "题1", "is_correct": True}]
        })
        assert len(cleaned["questions"]) == 1
        assert len(warnings) == 2

    def test_invalid_score_cleared(self):
        from llm_service import _validate_result
        cleaned, warnings = _validate_result({
            "score": "优秀",
            "overall_comment": "好",
            "questions": [{"question": "题1", "is_correct": True, "score": "满分"}],
        })
        assert cleaned["score"] is None
        assert cleaned["questions"][0]["score"] is None
        assert len(warnings) == 2

    def test_questions_not_list_cleared(self):
        from llm_service import _validate_result
        cleaned, warnings = _validate_result({
            "score": "90", "overall_comment": "", "questions": {"a": 1}
        })
        assert cleaned["questions"] == []
        assert any("questions" in w for w in warnings)

    def test_non_dict_result_falls_back(self):
        from llm_service import _validate_result
        cleaned, warnings = _validate_result(["不是对象"])
        assert cleaned == {"score": None, "overall_comment": "", "questions": []}
        assert warnings

    def test_grade_homework_exposes_warnings(self, monkeypatch):
        """脏题被丢弃后，告警通过 result["warnings"] 透出，且不影响好题。"""
        import llm_service as llm_mod

        payload = {
            "score": "90",
            "overall_comment": "还行",
            "questions": [
                {"question": "题1", "is_correct": True, "score": "10"},
                {"question": "题2", "is_correct": "yes"},
            ],
        }
        monkeypatch.setattr(
            llm_mod, "_post_chat",
            lambda messages, timeout=60: json.dumps(payload, ensure_ascii=False),
        )
        result = llm_mod.grade_homework("数学", "五年级上", "题1 与 题2", [])
        assert len(result["questions"]) == 1
        assert result["questions"][0]["question"] == "题1"
        assert result["warnings"]


class TestPromptScope:
    """T-B04：学段 / 教材版本 / 地区注入提示词。"""

    @staticmethod
    def _capture(monkeypatch):
        import llm_service as llm_mod

        captured = {}

        def fake_post(messages, timeout=60):
            captured["prompt"] = messages[1]["content"]
            return json.dumps(
                {"score": None, "overall_comment": "", "questions": []},
                ensure_ascii=False,
            )

        monkeypatch.setattr(llm_mod, "_post_chat", fake_post)
        return llm_mod, captured

    def test_scope_injected_when_provided(self, monkeypatch):
        llm_mod, captured = self._capture(monkeypatch)
        llm_mod.grade_homework(
            "数学", "七年级上", "1+1=2", [],
            stage="初中", textbook_version="人教版", region="福建省",
        )
        prompt = captured["prompt"]
        assert "【学情范围】" in prompt
        assert "学段：初中" in prompt
        assert "教材版本：人教版" in prompt
        assert "地区：福建省" in prompt

    def test_scope_absent_when_not_provided(self, monkeypatch):
        """旧版调用方式（不传五级参数）必须与原有提示词一致。"""
        llm_mod, captured = self._capture(monkeypatch)
        llm_mod.grade_homework("数学", "五年级", "1+1=2", [])
        assert "【学情范围】" not in captured["prompt"]

    def test_partial_scope_only_renders_given_parts(self, monkeypatch):
        llm_mod, captured = self._capture(monkeypatch)
        llm_mod.grade_homework("数学", "五年级上", "1+1=2", [], stage="小学")
        prompt = captured["prompt"]
        assert "学段：小学" in prompt
        assert "教材版本：" not in prompt
        assert "地区：" not in prompt
