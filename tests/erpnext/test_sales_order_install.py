"""Sales order tool installation and conversation-guidance regressions."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]


class Document:
    def __init__(self, data, rows):
        self.__dict__.update(data)
        self.rows = rows
        self.save = Mock()

    def get(self, field):
        return getattr(self, field, None)

    def append(self, field, value):
        getattr(self, field).append(types.SimpleNamespace(**value))

    def insert(self, **_kwargs):
        self.name = "tool-" + self.slug
        self.rows[self.name] = self
        return self


def load_install():
    rows = {}
    agents = {
        "flow-agent": Document({"name": "flow-agent", "title": "Flow", "tools": [], "instructions": "原有通用规则"}, rows),
        "sales-agent": Document({"name": "sales-agent", "title": "销售助理", "tools": [], "instructions": "原有销售规则"}, rows),
    }
    frappe = types.ModuleType("frappe")
    frappe.db = Mock()
    frappe.clear_document_cache = Mock()
    frappe.throw = Mock(side_effect=RuntimeError("Flow 尚未安装"))

    def get_value(doctype, filters, field="name"):
        source = rows if doctype == "Flow Tool" else agents
        if isinstance(filters, str):
            doc = source.get(filters)
        else:
            doc = next((doc for doc in source.values() if all(doc.get(key) == value for key, value in filters.items())), None)
        return doc.get(field) if doc else None

    def exists(doctype, filters):
        if doctype == "DocType":
            return filters in {"Flow Tool", "Flow Agent"}
        return bool(get_value(doctype, filters))

    def set_value(doctype, name, values):
        assert doctype == "Flow Tool"
        rows[name].__dict__.update(values)

    def get_doc(doctype, name=None):
        if isinstance(doctype, dict):
            assert doctype["doctype"] == "Flow Tool"
            return Document(doctype, rows)
        assert doctype == "Flow Agent"
        return agents[name]

    frappe.db.get_value.side_effect = get_value
    frappe.db.exists.side_effect = exists
    frappe.db.set_value.side_effect = set_value
    frappe.get_doc = Mock(side_effect=get_doc)
    spec = importlib.util.spec_from_file_location("sales_order_install_under_test", ROOT / "flow/integrations/erpnext/sales_order_install.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"frappe": frappe}):
        spec.loader.exec_module(module)
    return module, frappe, rows, agents


class SalesOrderGuidanceTests(unittest.TestCase):
    def test_stickers_are_free_and_contextual_bundles_are_reviewed_once(self):
        module, *_ = load_install()
        self.assertIn("先确定客户档案，再建立需要的客户贴纸物料，最后创建销售订单", module.HINT)
        self.assertIn("含仅贴纸订单", module.HINT)
        self.assertIn("销售单价统一为0元", module.HINT)
        self.assertIn("不要求固定话术", module.HINT)
        self.assertIn("bundle_mappings", module.HINT)
        self.assertIn("standalone=true", module.HINT)
        self.assertIn("没有提到贴纸的商品不添加贴纸、不创建组合", module.HINT)
        self.assertIn("失败写入统一回滚", module.HINT)
        self.assertIn("不增加额外确认", module.HINT)

    def test_replaces_old_multiline_block_and_preserves_surrounding_rules(self):
        module, *_ = load_install()
        original = (
            "保留前文\n\n"
            "创建销售订单的销售团队必填规则：\n"
            "1. 旧规则一：索要邮箱。\n"
            "   旧规则一的折行。\n\n"
            "2. 旧规则二：查子表。\n\n"
            "3. 旧规则三：再次确认。\n\n"
            "其他业务规则：\n1. 保留这个编号段落。\n"
        )
        result = module.with_sales_order_guidance(original)
        self.assertIn("保留前文", result)
        self.assertIn("其他业务规则：\n1. 保留这个编号段落。", result)
        self.assertNotIn(module.LEGACY_HINT_MARKER, result)
        self.assertNotIn("旧规则", result)
        self.assertEqual(result.count(module.HINT_MARKER), 1)
        self.assertEqual(next(line for line in result.splitlines() if line.startswith(module.HINT_MARKER)), module.HINT)

    def test_refresh_is_idempotent_and_replaces_stale_managed_rules(self):
        module, *_ = load_install()
        original = "独立规则\n" + module.HINT_MARKER + "过时规则\n\n" + module.HINT_MARKER + "重复规则"
        result = module.with_sales_order_guidance(original)
        self.assertEqual(module.with_sales_order_guidance(result), result)
        self.assertEqual(result.count(module.HINT_MARKER), 1)
        self.assertIn("独立规则", result)
        self.assertNotIn("过时规则", result)
        self.assertNotIn("重复规则", result)

    def test_unrelated_numbered_paragraphs_are_not_removed(self):
        module, *_ = load_install()
        unrelated = "其他规则：\n1. 保留第一项。\n2. 保留第二项。\n3. 保留第三项。"
        self.assertIn(unrelated, module.with_sales_order_guidance(unrelated))
        self.assertEqual(module.with_sales_order_guidance(None).count(module.HINT_MARKER), 1)


class SalesOrderToolInstallationTests(unittest.TestCase):
    def test_default_install_keeps_new_tools_disabled_and_sets_approval_flags(self):
        module, frappe, rows, agents = load_install()
        result = module.install_sales_order_tools()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["agents"], [])
        self.assertEqual(len(rows), 3)
        for slug, requires_confirmation in ((module.DETAILS_SLUG, 0), (module.PREVIEW_SLUG, 0), (module.CREATE_SLUG, 1)):
            row = rows["tool-" + slug]
            self.assertEqual(row.type, "Imported")
            self.assertIsNone(row.code)
            self.assertEqual(row.import_path, "flow.integrations.erpnext.sales_order_flow." + slug)
            self.assertEqual(row.requires_confirmation, requires_confirmation)
            self.assertEqual(row.enabled, 0)
        self.assertTrue(all(not agent.tools for agent in agents.values()))
        self.assertTrue(all(not agent.save.called for agent in agents.values()))
        self.assertEqual({call.args[0] for call in frappe.db.get_value.call_args_list}, {"Flow Tool"})

    def test_refresh_preserves_each_tools_existing_enabled_state(self):
        module, frappe, rows, agents = load_install()
        module.install_sales_order_tools(enable=True)
        rows["tool-" + module.CREATE_SLUG].enabled = 0
        for agent in agents.values():
            agent.save.reset_mock()
        frappe.db.set_value.reset_mock()
        result = module.install_sales_order_tools()
        self.assertFalse(result["enabled"])
        self.assertEqual(rows["tool-" + module.PREVIEW_SLUG].enabled, 1)
        self.assertEqual(rows["tool-" + module.CREATE_SLUG].enabled, 0)
        self.assertTrue(all("enabled" not in call.args[2] for call in frappe.db.set_value.call_args_list))
        self.assertTrue(all(not agent.save.called for agent in agents.values()))

    def test_enable_binds_both_agents_and_repeated_install_does_not_duplicate(self):
        module, _, rows, agents = load_install()
        result = module.install_sales_order_tools(enable=True)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["agents"], ["flow-agent", "sales-agent"])
        for agent in agents.values():
            self.assertEqual({row.tool for row in agent.tools}, set(rows))
            self.assertEqual(agent.instructions.count(module.HINT_MARKER), 1)
            self.assertIn("原有", agent.instructions)
            agent.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)
        module.install_sales_order_tools(enable=True)
        for agent in agents.values():
            self.assertEqual(len(agent.tools), 3)
            agent.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)

    def test_query_migration_reuses_legacy_script_identity(self):
        module, _, rows, agents = load_install()
        legacy = Document(
            {
                "name": "legacy-query-sales-order-details",
                "slug": module.DETAILS_SLUG,
                "type": "Script",
                "enabled": 1,
                "instructions": "旧数据库脚本",
            },
            rows,
        )
        rows[legacy.name] = legacy

        result = module.install_sales_order_query_tool(enable=True)

        assert result["tool"] == legacy.name
        assert legacy.type == "Imported"
        assert legacy.code is None
        assert legacy.import_path == "flow.integrations.erpnext.sales_order_flow.query_sales_order_details"
        assert legacy.requires_confirmation == 0
        assert legacy.enabled == 1
        assert {row.tool for agent in agents.values() for row in agent.tools} == {legacy.name}

    def test_missing_flow_does_not_write_tools(self):
        module, frappe, rows, _ = load_install()
        frappe.db.exists.return_value = False
        frappe.db.exists.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "Flow 尚未安装"):
            module.install_sales_order_tools()
        self.assertEqual(rows, {})
        frappe.get_doc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
