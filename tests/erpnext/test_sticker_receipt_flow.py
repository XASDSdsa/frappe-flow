"""Pure request rules for customer-sticker zero-valuation receipts."""
import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/sticker_receipt_flow.py"


class InputError(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or []


@pytest.fixture
def rules():
    tree = ast.parse(SOURCE.read_text())
    names = {"REQUEST_FIELDS", "_text", "_number", "_request"}
    nodes = [node for node in tree.body if
             (isinstance(node, ast.FunctionDef) and node.name in names) or
             (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))]
    fake = SimpleNamespace()
    namespace = {"InputError": InputError, "math": __import__("math"), "date": date,
                 "nowdate": lambda: "2026-10-06", "frappe": fake}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return SimpleNamespace(**namespace)


def request(**overrides):
    return {"sales_order": "SAL-ORD-1", "customer": "Driven25",
            "zero_valuation_reason": "贴纸定制服务成本已另行入账，本次只增加实际库存数量",
            "items": [{"item_code": "巧克粉贴纸-Driven25-山东中性灰方模版-v1", "qty": 1000}], **overrides}


def test_sticker_receipt_requires_explicit_zero_cost_reason(rules):
    with pytest.raises(InputError, match="zero_valuation_reason"):
        rules._request({**request(), "zero_valuation_reason": ""})


def test_sticker_receipt_defaults_to_draft_and_today(rules):
    result = rules._request(request())
    assert result["submit"] is False
    assert result["posting_date"] == "2026-10-06"


def test_submit_requires_actual_arrival_confirmation(rules):
    with pytest.raises(InputError, match="实际到货"):
        rules._request(request(submit=True))
    assert rules._request(request(submit=True, actual_receipt=True))["submit"] is True


@pytest.mark.parametrize("bad", [0, -1, True, "0", float("nan"), float("inf")])
def test_quantities_are_positive_finite_numbers(rules, bad):
    with pytest.raises(InputError):
        rules._request(request(items=[{"item_code": "STICKER", "qty": bad}]))


def test_unknown_controls_and_duplicate_rows_are_rejected(rules):
    with pytest.raises(InputError):
        rules._request(request(rate=0))
    with pytest.raises(InputError, match="不能重复"):
        rules._request(request(items=[{"item_code": "STICKER", "qty": 1},
                                      {"item_code": "STICKER", "qty": 1}]))


def test_future_or_ambiguous_posting_date_is_rejected(rules):
    for value in ("2026-10-07", "20261006", "2026-10-06T10:00:00"):
        with pytest.raises(InputError):
            rules._request(request(posting_date=value))
