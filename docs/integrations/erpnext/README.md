# ERPNext workflows in Flow

`flow.integrations.erpnext` owns conversational input, review cards, approval
tokens, Flow tool wrappers and their explicit setup functions. ERPNext owns native
stock/accounting validation and document lifecycles. The optional Shipping app's
`erpnext_shipping.sf_international` module owns SF International requests,
waybills, carrier validation and the approved booking input contract.

The integration does not add ERPNext or Shipping as mandatory dependencies of
Flow. `install.migrate_legacy_tool_paths()` checks installed applications and
only rewrites the 34 exact legacy paths in `legacy_tool_paths.json`. It preserves
tool names, slugs, enabled state, approval settings, agent links and history. It
does not run workflow installers, change instructions/roles, create business
records, seed stock, or alter accounting. The caller owns the transaction.

Existing workflow installers remain explicit setup operations. They must not be
used as a data migration because their purpose includes managed guidance/setup.
PayPal's Flow entry point delegates the unchanged arguments to ERPNext's receipt
API; it does not duplicate accounting. SF review and execution share Shipping's
`reviewed_booking` helpers so the approved payload digest has one definition.

Before retiring the legacy app, drain its two queued booking entry points:
`sf_international.sf_international.shipping.book_sf_order_after_commit` and
`sf_international.sf_international.sf_label_flow.finish_sf_label`. Preserve the
Integration Request ledger, approved plan schema, `Flow SF Label` service name,
job identities and review token keys. Failed carrier attempts must not be retried
as part of migration.

The retired legacy `flow_compat` and delivery-submit monkey patches were not
registered by the deployed app. They are not copied or reactivated. Existing
Flow runtime behavior stays in Flow's current runtime modules.

Offline regression suite: `tests/erpnext`. Set `PYTHONPATH` to the ERPNext and
ERPNext Shipping source roots (or install those apps) when checking the optional
cross-app workflows. The tests use temporary in-memory document/approval state
and do not call a carrier, production site or database.
