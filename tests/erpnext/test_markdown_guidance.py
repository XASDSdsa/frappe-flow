"""Updating multiline instructions must never consume user-owned text."""

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("markdown_style_under_test", ROOT / "flow/integrations/erpnext/flow_reply_style.py")
style = importlib.util.module_from_spec(spec)
spec.loader.exec_module(style)
with_managed_guidance = style.with_managed_guidance
without_managed_guidance = style.without_managed_guidance


MARKER = "测试规则："
HINT = MARKER + "\n\n### 用途\n\n- 当前说明。"


def test_upgrade_legacy_and_preserve_custom_sections():
    original = "自定义前文\n\n" + MARKER + "旧说明\n\n### 客服自定义\n\n- 保留。"
    updated = with_managed_guidance(original, MARKER, HINT)
    assert "旧说明" not in updated
    assert "### 客服自定义\n\n- 保留。" in updated
    assert updated.count(MARKER) == 1
    assert with_managed_guidance(updated, MARKER, HINT) == updated


def test_multiline_refresh_preserves_unheaded_following_text():
    original = with_managed_guidance("自定义前文", MARKER, HINT) + "\n\n保留尾部自定义规则。"
    revised = HINT.replace("当前说明", "新版说明")
    updated = with_managed_guidance(original, MARKER, revised)
    assert "当前说明" not in updated
    assert "保留尾部自定义规则。" in updated
    assert with_managed_guidance(updated, MARKER, revised) == updated


def test_marker_mentioned_in_user_prose_is_not_a_managed_block():
    original = "请解释“" + MARKER + "”的用途。"
    assert without_managed_guidance(original, MARKER) == original


@pytest.mark.parametrize("suffix", ["", "\n## 其他规则\n\n不要删除\n<!-- /flow-guidance -->"])
def test_unclosed_block_is_rejected_instead_of_consuming_other_sections(suffix):
    with pytest.raises(ValueError, match="Incomplete managed"):
        with_managed_guidance("## " + HINT + suffix, MARKER, HINT)


def test_multiple_managed_sections_are_stable_across_refreshes():
    other = "其他规则："
    current = "保留原有说明。"
    def refresh(text):
        text = with_managed_guidance(text, MARKER, HINT)
        return with_managed_guidance(text, other, other + "\n\n### 用途\n\n- 其他说明。")
    current = refresh(current)
    for _ in range(3):
        assert refresh(current) == current


def test_crlf_owned_block_can_be_refreshed():
    old = with_managed_guidance("前文", MARKER, HINT).replace("\n", "\r\n")
    updated = with_managed_guidance(old, MARKER, HINT)
    assert updated.count(MARKER) == 1
    assert with_managed_guidance(updated, MARKER, HINT) == updated


def test_existing_guidance_refresh_does_not_add_other_business_rules(monkeypatch):
    with patch.dict(sys.modules):
        for name in ("flow", "flow.integrations", "flow.integrations.erpnext"):
            package = ModuleType(name)
            package.__path__ = [str(ROOT / name.replace(".", "/"))]
            monkeypatch.setitem(sys.modules, name, package)
        monkeypatch.setitem(sys.modules, "frappe", SimpleNamespace())
        module = importlib.import_module("flow.integrations.erpnext.markdown_guidance")
        original = "私有说明。\n\n销售订单专用工具规则：旧规则\n\n保留结尾。"
        updated = module.refresh_existing_guidance(original)
        assert "私有说明。" in updated and "保留结尾。" in updated
        assert "销售订单专用工具规则：" in updated
        assert "客户完整建档专用工具：" not in updated
        assert "客服操作回复显示规则：" not in updated
        assert module.refresh_existing_guidance(updated) == updated
        assert module.refresh_existing_guidance("只有自定义说明。") == "只有自定义说明。"
