"""Regression tests for daily Flow routing and orphan-tool cleanup."""
import ast
import importlib.util
import types
from pathlib import Path
from unittest.mock import Mock
ROOT = Path(__file__).resolve().parents[2]

def load_reply_style():
    spec = importlib.util.spec_from_file_location('flow_reply_style_under_test', ROOT / 'flow/integrations/erpnext/flow_reply_style.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_daily_routing_is_idempotent_and_preserves_custom_guidance():
    module = load_reply_style()
    original = '保留这条客服专属规则。\n\n' + module.DAILY_ROUTING_MARKER + '旧规则'
    result = module.with_daily_routing(original)
    assert '保留这条客服专属规则。' in result
    assert '先调用对应的专用 Imported 工具' in result
    assert '不要先调用 find_doctypes 或 describe' in result
    assert result.count(module.DAILY_ROUTING_MARKER) == 1
    assert module.with_daily_routing(result) == result
