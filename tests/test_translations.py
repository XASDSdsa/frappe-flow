"""Offline checks for Flow's Simplified Chinese catalog."""

import csv
from pathlib import Path


CATALOG = Path(__file__).parents[1] / "flow" / "translations" / "zh.csv"


def _catalog_rows():
	with CATALOG.open(newline="", encoding="utf-8") as stream:
		return list(csv.reader(stream))


def test_simplified_chinese_catalog_has_valid_rows_and_no_duplicates():
	rows = _catalog_rows()
	assert rows
	assert all(len(row) in (2, 3) and row[0] and row[1] for row in rows)
	keys = [(row[0], row[2] if len(row) == 3 else None) for row in rows]
	assert len(keys) == len(set(keys))


def test_simplified_chinese_catalog_keeps_flow_and_integration_keys():
	translations = {(row[0], row[2] if len(row) == 3 else None): row[1] for row in _catalog_rows()}
	assert translations[("Flow Voice Settings", None)] == "Flow 语音设置"
	assert translations[("Attach file", None)] == "添加附件"
	assert translations[("Searching Knowledge", None)] == "正在检索知识库"
	assert translations[("Flow Agent", None)] == "Flow 智能体"
	assert translations[("Agent", "Flow Session")] == "智能体"
	assert translations[("Please pass shipment, waybill, sales_order, or delivery_note.", None)] == "请提供运单号、顺丰运单号、销售订单或出库单。"
