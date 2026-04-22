# -*- coding: utf-8 -*-
"""SkillDefinition 领域实体单元测试。"""
from __future__ import annotations

import pytest

from agent_runtime.skill.skill_definition import SkillDefinition


def _make_skill(**overrides) -> SkillDefinition:
    defaults = {
        "skill_id": "pdf-processing",
        "name": "pdf-processing",
        "description": "Extract PDF text, fill forms, merge files.",
        "directory_path": "/skills/pdf-processing",
    }
    defaults.update(overrides)
    return SkillDefinition(**defaults)


class TestSkillDefinitionValidate:
    """validate() 业务规则校验。"""

    def test_valid_minimal(self) -> None:
        skill = _make_skill()
        skill.validate()

    def test_valid_with_all_optional_fields(self) -> None:
        skill = _make_skill(
            license="Apache-2.0",
            compatibility="Requires Python 3.14+",
            metadata={"author": "test-org", "version": "1.0"},
            allowed_tools="Bash(git:*) Read",
        )
        skill.validate()

    def test_name_uppercase_rejected(self) -> None:
        with pytest.raises(ValueError, match="格式无效"):
            _make_skill(name="PDF-Processing", skill_id="PDF-Processing").validate()

    def test_name_leading_hyphen_rejected(self) -> None:
        with pytest.raises(ValueError, match="格式无效"):
            _make_skill(name="-pdf", skill_id="-pdf").validate()

    def test_name_trailing_hyphen_rejected(self) -> None:
        with pytest.raises(ValueError, match="格式无效"):
            _make_skill(name="pdf-", skill_id="pdf-").validate()

    def test_name_consecutive_hyphens_rejected(self) -> None:
        with pytest.raises(ValueError, match="格式无效"):
            _make_skill(name="pdf--processing", skill_id="pdf--processing").validate()

    def test_name_mismatch_skill_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="不一致"):
            _make_skill(name="pdf-processing", skill_id="other-name").validate()

    def test_empty_description_rejected(self) -> None:
        with pytest.raises(ValueError, match="description 不能为空"):
            _make_skill(description="").validate()

    def test_whitespace_description_rejected(self) -> None:
        with pytest.raises(ValueError, match="description 不能为空"):
            _make_skill(description="   ").validate()

    def test_single_char_name_valid(self) -> None:
        _make_skill(name="a", skill_id="a").validate()

    def test_max_length_name_valid(self) -> None:
        name = "a" * 64
        _make_skill(name=name, skill_id=name).validate()

    def test_name_with_digits_valid(self) -> None:
        _make_skill(name="tool-v2", skill_id="tool-v2").validate()
