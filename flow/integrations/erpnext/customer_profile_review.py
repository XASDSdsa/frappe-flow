"""Concise approval content built from the actual read-only customer preview."""

import re


def _text(value):
    value = re.sub(r"<[^>]*>", "", str(value or ""))
    value = " ".join(value.split())
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", value)


def _address(value):
    return "，".join(_text(value.get(key)) for key in
        ("country", "state", "city", "county", "address_line1", "address_line2", "pincode") if value.get(key))


def render_customer_review(result):
    if result.get("status") != "preview":
        problems = result.get("missing") or []
        return "🔴 客户资料尚未通过预检，不能完成建档。\n" + _text(result.get("message") or "请先核对缺项或冲突。") + (
            "\n请拒绝本次操作并补齐资料后重新预检。" if problems else "\n请拒绝本次操作并修正所示问题。")
    profile = result["profile"]
    customer = profile["customer"]
    contact = profile.get("contact") or {}
    shipping = profile.get("shipping_address") or {}
    billing = profile.get("billing_address")
    policy = profile.get("address_policy") or {}
    preferences = profile.get("existing_address_preferences") or {}
    mode = policy.get("billing_mode")
    forwarder = shipping.get("address_type") == "货代收货"
    phones = [row["phone"] for row in contact.get("phone_nos", []) if row.get("phone")]
    emails = [row["email_id"] for row in contact.get("email_ids", []) if row.get("email_id")]
    operation = "新建" if result.get("will_create_customer") else "补充／复用已有"
    lines = [f"🔵 请审核：{operation}客户档案（尚未保存）",
        "客户：" + _text(customer.get("customer_name")) + "｜币种：" + _text(customer.get("default_currency")),
        "负责人：" + _text(customer.get("account_manager")) + "｜分组：" + _text(customer.get("customer_group")),
        "客户地区：" + (_text(customer.get("territory")) or "暂未提供，销售地区留空（待补充）"),
        "客户联系人：" + _text(" ".join(contact.get(k) or "" for k in ("first_name", "middle_name", "last_name"))) +
            "｜电话：" + (_text(" / ".join(phones)) or "未提供"),
        "客户邮箱：" + (_text(" / ".join(emails)) or "未提供"),
        "**收货方式：" + ("货代代收，每次开单核对地址" if forwarder else "客户直收") + "**",
        "收货地址：" + _address(shipping),
        "收货方：" + (_text(shipping.get("address_title")) or ("未单独提供" if forwarder else "同客户联系人")) +
            "｜收货电话：" + (_text(shipping.get("phone")) or "未提供")]
    if mode == "same_as_shipping":
        lines.append("发票地址：已明确与上述直收地址相同")
    elif billing:
        lines.append("发票地址：" + _address(billing))
    else:
        existing_billing = preferences.get("customer_primary_address") or ("（沿用原设置）" if preferences.get("has_preferred_billing") else "")
        lines.append("发票地址：本次未提供，保留原开票地址 " + _text(existing_billing) if existing_billing else
            "**发票地址：未提供，本次不设开票地址**")
    if preferences.get("has_preferred_shipping"):
        lines.append("已有首选送货地址保留，本次不会自动替换。")
    if mode != "not_provided" and preferences.get("customer_primary_address"):
        lines.append("已有客户首选开票地址保留：" + _text(preferences["customer_primary_address"]))
    for warning in dict.fromkeys(profile.get("warnings") or []):
        lines.append("⚠️ " + _text(warning))
    lines.append("请整体核对后批准一次；需要修改请拒绝并说明。")
    return "\n".join(lines)


def customer_confirmation_prompt(args):
    from flow.integrations.erpnext.customer_profile import preview_customer_profile
    try:
        return render_customer_review(preview_customer_profile(**args))
    except Exception as exc:
        # The preview is part of the approval contract. Hiding the exception
        # leaves the agent unable to tell the客服 why approval is blocked. Keep
        # the reason short and escaped; the write tool still performs its own
        # validation and transaction rollback.
        reason = _text(str(exc)) or type(exc).__name__
        return ("🔴 客户审核摘要生成失败，尚未保存。\n"
                "原因：" + reason[:300] + "。请拒绝本次操作，修正原因后重新预检。")
