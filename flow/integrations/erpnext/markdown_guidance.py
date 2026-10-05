"""Read source descriptions and refresh only existing managed instruction blocks."""

import re

from .flow_reply_style import (
    DAILY_ROUTING_HINT,
    DAILY_ROUTING_MARKER,
    REPLY_STYLE_HINT,
    REPLY_STYLE_MARKER,
    with_managed_guidance,
    without_managed_guidance,
)


def description_definitions():
    """Return the same text used by installers, without invoking their writes."""
    from flow.tools.builtins import BUILTIN_TOOLS

    from . import (
        customer_install, delivery_note_install, document_submission_install,
        finance_flow_install, inventory_install, paypal_install,
        sales_order_install, sf_label_install, sticker_install, waybill_flow,
    )

    definitions = [
        {"slug": tool.name, "import_path": "flow.tools.builtins." + tool.name,
         "description": tool.description}
        for tool in BUILTIN_TOOLS
    ]
    definitions.extend(customer_install.tool_definitions())
    definitions.extend(finance_flow_install.tool_definitions())
    definitions.extend(inventory_install.tool_definitions())
    definitions.extend(sf_label_install.query_tool_definitions())
    for module, path in (
        (sales_order_install, "sales_order_flow"),
        (delivery_note_install, "delivery_note_flow"),
        (document_submission_install, "document_submission"),
        (sf_label_install, "sf_label_flow"),
        (waybill_flow, "waybill_flow"),
    ):
        for slug, _title, _confirm, description in module.TOOLS:
            definitions.append({"slug": slug, "description": description,
                "import_path": "flow.integrations.erpnext." + path + "." + slug})
    for module in (sticker_install, paypal_install):
        definitions.append({"slug": module.TOOL_SLUG, "import_path": module.TOOL_IMPORT_PATH,
            "description": module.TOOL_DESCRIPTION})
    return definitions


def refresh_existing_guidance(instructions):
    """Keep the agent's scope: format owned blocks already present, add no workflows."""
    from . import (
        customer_install, delivery_note_install, document_submission_install,
        finance_flow_install, inventory_install, sales_order_install,
        sf_label_install, sticker_install, waybill_flow_install,
    )

    original = instructions or ""
    text = original

    def present(marker):
        return bool(re.search(r"(?m)^(?:## )?" + re.escape(marker), original))

    sections = [
        (customer_install.AUTOMATIC_TERRITORY_MARKER, customer_install.AUTOMATIC_TERRITORY_HINT),
        (customer_install.MANAGER_HINT_MARKER, customer_install.MANAGER_HINT),
        (customer_install.ADDRESS_HINT_MARKER, customer_install.ADDRESS_HINT),
    ]
    for module in (
        customer_install, sticker_install, sales_order_install, delivery_note_install,
        document_submission_install, inventory_install, finance_flow_install,
        sf_label_install, waybill_flow_install,
    ):
        sections.append((module.HINT_MARKER, module.HINT))
    sections.extend(((DAILY_ROUTING_MARKER, DAILY_ROUTING_HINT), (REPLY_STYLE_MARKER, REPLY_STYLE_HINT)))

    # Retire only the two already-supported historical aliases.
    if present(sales_order_install.LEGACY_HINT_MARKER):
        text = sales_order_install._without_legacy_guidance(text)
        text = with_managed_guidance(text, sales_order_install.HINT_MARKER, sales_order_install.HINT)
    if present(finance_flow_install.LEGACY_HINT_MARKER):
        text = without_managed_guidance(text, finance_flow_install.LEGACY_HINT_MARKER)
        text = with_managed_guidance(text, finance_flow_install.HINT_MARKER, finance_flow_install.HINT)
    for marker, hint in sections:
        if present(marker):
            text = with_managed_guidance(text, marker, hint)
    return text
