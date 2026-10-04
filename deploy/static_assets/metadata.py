#!/usr/bin/env python3
"""Verify unchanged metadata for static releases; every database operation is read-only."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import frappe


SITES = Path("/home/frappe/frappe-bench/sites")
ISOLATED_PREFIXES = ("flow-check-", "shipment-check-")


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("mode", choices=("snapshot", "migrate", "validate", "restore", "compare", "fail-after-owner"))
	parser.add_argument("--site", required=True)
	parser.add_argument("--snapshot", required=True, type=Path)
	parser.add_argument("--db-host", required=True)
	args = parser.parse_args()
	if args.mode == "fail-after-owner":
		assert args.db_host.startswith(ISOLATED_PREFIXES), "failure_injection_requires_isolated_database"
	args.snapshot = args.snapshot.resolve()
	os.chdir(SITES)
	for directory in (SITES.parent / "logs", SITES / args.site / "logs"):
		assert directory.is_dir() and os.access(directory, os.W_OK), "missing_or_unwritable_log_directory"
	frappe.init(args.site, sites_path=str(SITES))
	try:
		assert frappe.conf.db_host == args.db_host, "unexpected_database_host"
		if args.db_host.startswith(ISOLATED_PREFIXES):
			prefix = next(prefix for prefix in ISOLATED_PREFIXES if args.db_host.startswith(prefix))
			assert all(
				(urlparse(frappe.conf.get(key) or "").hostname or "").startswith(prefix)
				for key in ("redis_cache", "redis_queue", "redis_socketio")
			), "unexpected_isolation_redis_host"
		frappe.connect()
		frappe.set_user("Administrator")
		path = Path(frappe.get_app_path("flow")).parent / "deploy/customer_service_workflows/metadata.py"
		spec = importlib.util.spec_from_file_location("flow_static_metadata_snapshot_library", path)
		assert spec and spec.loader, "flow_snapshot_library_unavailable"
		library = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(library)
		if args.mode == "snapshot":
			assert not args.snapshot.exists(), "snapshot_exists"
			encoded = library._encoded(library.capture())
			temporary = args.snapshot.with_suffix(".pending")
			with temporary.open("xb") as handle:
				os.chmod(temporary, 0o600)
				handle.write(encoded)
			os.replace(temporary, args.snapshot)
			print("FLOW_METADATA_SNAPSHOT " + hashlib.sha256(encoded).hexdigest())
		else:
			snapshot = json.loads(args.snapshot.read_text())
			assert snapshot["site"] == frappe.local.site, "snapshot_site_mismatch"
			# No migration writes exist, so rollback must verify, never restore rows.
			library.compare(snapshot)
			if args.mode == "fail-after-owner":
				raise RuntimeError("ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA")
			if args.mode == "migrate":
				digest = hashlib.sha256(library._encoded(snapshot)).hexdigest()
				print(json.dumps({"result": "MIGRATION_OK", "before": digest, "after": digest, "actions": {"metadata_changed": False}}))
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
