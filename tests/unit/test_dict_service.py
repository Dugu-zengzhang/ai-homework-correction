# -*- coding: utf-8 -*-
"""字典层单元测试：五级选择的合法性校验（T-B01）。

对应本 Sprint 评审整改项：地区此前未纳入校验，任意字符串都能落库。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


@pytest.mark.unit
class TestValidateSelection:
    """dict_service.validate_selection：学段 / 地区 / 学科 / 教材版本 / 年级。"""

    def test_valid_selection_passes(self):
        from dict_service import validate_selection

        ok, msg = validate_selection("初中", "数学", "人教版", "七年级上", "福建省")
        assert ok is True, msg
        assert msg == ""

    def test_stage_out_of_dict_rejected(self):
        from dict_service import validate_selection

        ok, msg = validate_selection("幼儿园", "数学")
        assert ok is False
        assert "学段" in msg

    def test_region_out_of_dict_rejected(self):
        """地区必须在字典范围内（整改项 CR-15）。"""
        from dict_service import validate_selection

        ok, msg = validate_selection("初中", "数学", "人教版", "七年级上", "火星省")
        assert ok is False
        assert "地区" in msg

    def test_region_empty_passes(self):
        """未传地区（旧版前端行为）不阻塞批改。"""
        from dict_service import validate_selection

        ok, msg = validate_selection("初中", "数学", "人教版", "七年级上", "")
        assert ok is True, msg

    def test_primary_stage_has_no_physics(self):
        """学科按学段分层：小学不含物理。"""
        from dict_service import validate_selection

        ok, msg = validate_selection("小学", "物理")
        assert ok is False
        assert "物理" in msg

    def test_textbook_not_in_stage_rejected(self):
        from dict_service import validate_selection

        ok, msg = validate_selection("小学", "数学", "苏科版")   # 苏科版只在初中
        assert ok is False
        assert "教材版本" in msg

    def test_grade_stage_mismatch_rejected(self):
        """年级推断出的学段必须与所选学段自洽（兼容「五年级」「五年级上」两种写法）。"""
        from dict_service import validate_selection

        ok, msg = validate_selection("初中", "数学", "人教版", "五年级")
        assert ok is False
        assert "不匹配" in msg

    def test_grade_without_stage_prefix_passes(self):
        """「五年级」这种不带学段前缀的写法，只要学段自洽就放行。"""
        from dict_service import validate_selection

        ok, msg = validate_selection("小学", "数学", "人教版", "五年级")
        assert ok is True, msg
