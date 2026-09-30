import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preview():
    return {"status": "preview", "will_create_customer": True, "profile": {
        "customer": {"customer_name": "客户甲", "default_currency": "CNY", "account_manager": "sales@example.com",
            "customer_group": "Individual", "territory": "Malaysia"},
        "contact": {"first_name": "客户甲", "phone_nos": []},
        "shipping_address": {"address_type": "货代收货", "address_title": "运道仓", "country": "China",
            "city": "广州市", "address_line1": "中和路53号", "pincode": "510545", "phone": "+86 13809242282"},
        "billing_address": None, "address_policy": {"shipping_type": "货代收货", "billing_mode": "not_provided"},
        "warnings": ["客户本人电话未提供。"],
    }}


def test_guidance_allows_currency_to_be_derived_from_existing_defaults(monkeypatch):
    monkeypatch.setitem(sys.modules, "frappe", types.ModuleType("frappe"))
    module = load("customer_install_guidance_test", "flow/integrations/erpnext/customer_install.py")
    assert "已有可用的用户或系统默认值时由后端自动带入" in module.DETAILS
    assert "只有没有可用默认值时才补问币种" in module.DETAILS


def test_confirmation_prompt_explains_unexpected_preview_failure(monkeypatch):
    module = load("customer_review_failure_test", "flow/integrations/erpnext/customer_profile_review.py")
    profile = types.ModuleType("flow.integrations.erpnext.customer_profile")
    profile.preview_customer_profile = lambda **args: (_ for _ in ()).throw(ValueError("币种默认值读取失败"))
    monkeypatch.setitem(sys.modules, profile.__name__, profile)
    result = module.customer_confirmation_prompt({"customer_name": "客户甲"})
    assert "审核摘要生成失败" in result
    assert "币种默认值读取失败" in result
    assert "尚未保存" in result


def test_forwarder_review_shows_true_customer_and_receiver_separately():
    module = load("customer_review_test", "flow/integrations/erpnext/customer_profile_review.py")
    result = module.render_customer_review(preview())
    assert "客户地区：Malaysia" in result
    assert "客户联系人：客户甲｜电话：未提供" in result
    assert "收货方：运道仓｜收货电话：+86 13809242282" in result
    assert "每次开单核对地址" in result
    assert "发票地址：未提供，本次不设开票地址" in result
    assert "尚未保存" in result


def test_shared_direct_and_separate_billing_are_distinct():
    module = load("customer_review_test", "flow/integrations/erpnext/customer_profile_review.py")
    result = preview()
    result["profile"]["shipping_address"]["address_type"] = "Shipping"
    result["profile"]["address_policy"]["billing_mode"] = "same_as_shipping"
    assert "发票地址：已明确与上述直收地址相同" in module.render_customer_review(result)
    result["profile"]["address_policy"]["billing_mode"] = "separate"
    result["profile"]["billing_address"] = {"country": "Malaysia", "city": "Kuala Lumpur", "address_line1": "10 Jalan A"}
    content = module.render_customer_review(result)
    assert "发票地址：Malaysia，Kuala Lumpur，10 Jalan A" in content
    assert "未提供，本次不设开票地址" not in content


def test_bad_preview_cannot_look_ready_and_markdown_is_escaped():
    module = load("customer_review_test", "flow/integrations/erpnext/customer_profile_review.py")
    assert "尚未通过预检" in module.render_customer_review({"status": "conflict", "message": "已有开票地址冲突"})
    result = preview()
    result["profile"]["customer"]["customer_name"] = "[伪链接](https://example.com)\n**已保存**"
    content = module.render_customer_review(result)
    assert "[伪链接]" not in content
    assert "**已保存**" not in content


def test_existing_defaults_are_not_presented_as_cleared_or_replaced():
    module = load("customer_review_test", "flow/integrations/erpnext/customer_profile_review.py")
    result = preview()
    result["will_create_customer"] = False
    result["profile"]["existing_address_preferences"] = {"customer_primary_address": "客户-发票", "has_preferred_billing": True, "has_preferred_shipping": True}
    content = module.render_customer_review(result)
    assert "保留原开票地址 客户-发票" in content
    assert "已有首选送货地址保留，本次不会自动替换" in content
    assert "未提供，本次不设开票地址" not in content


def test_guidance_replaces_old_defaults_and_preserves_unrelated_instructions(monkeypatch):
    monkeypatch.setitem(sys.modules, "frappe", types.ModuleType("frappe"))
    module = load("customer_install_review_test", "flow/integrations/erpnext/customer_install.py")
    old = "其他业务：保留。\n" + module.HINT_MARKER + "旧版把收货地址自动用于发票\n" + module.AUTOMATIC_TERRITORY_MARKER + "旧版总按仓库城市\n"
    content = module.with_customer_guidance(old)
    assert "其他业务：保留。" in content
    assert "旧版" not in content
    assert content.count(module.ADDRESS_HINT_MARKER) == 1
    assert "不能使用货代仓所在地" in content
    assert module.with_customer_guidance(content) == content


def test_unknown_customer_country_review_does_not_show_warehouse_as_customer_location():
    module = load("customer_review_test", "flow/integrations/erpnext/customer_profile_review.py")
    result = preview()
    result["profile"]["customer"]["territory"] = ""
    result["profile"]["territory_location"] = {"country_not_provided": True, "country": ""}
    content = module.render_customer_review(result)
    assert "客户地区：暂未提供，销售地区留空（待补充）" in content
    assert "收货地址：China，广州市" in content
    assert "客户地区：China" not in content
    result["profile"]["customer"]["territory"] = "Malaysia"
    assert "客户地区：Malaysia" in module.render_customer_review(result)
    assert "销售地区留空" not in module.render_customer_review(result)
