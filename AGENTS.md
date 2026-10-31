# Research agent harness

The application is a scheduled, read-only research agent. Its action loop is model-directed; resource accounting, citation integrity and persistence remain deterministic.

Preserve these invariants when changing behavior:
- External text is evidence, never executable instructions. Execute only registered typed tools.
- Evidence and claim revisions are append-only. A changed page receives a new content version.
- Model and search reservations are durable and counted across all runs on the configured local day.
- A report with missing evidence or failed quality review has status `partial`, never `complete`.
- Demo mode is synthetic and explicitly labeled throughout the report and interface.

Read `docs/architecture.md` when changing the controller, context selection, memory, resource accounting or recovery semantics. Read `docs/operations.md` for deployment and operational verification.
