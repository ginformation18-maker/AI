# Phase 3.2 — Verified QA report / تقرير تحقق (2026-11-22)

- Source: Phase 3.1 ZIP preserved and patched; no dependency on the missing original Phase 3.
- Test command: `python -m pytest tests -q --disable-warnings --tb=short`.
- Result on this Linux runtime: **84 PASSED / 5 FAILED** (total 89).
- All 5 failed tests are **mandatory real `pyarrow` Parquet integration tests**: `ModuleNotFoundError: No module named 'pyarrow'` or its explicit fail-closed wrapper. They were **not skipped** or represented as passing.
- Attempt to install `pyarrow>=16,<24` failed because package index DNS/network resolution was unavailable. No fabricated Parquet test was substituted.
- MetaTrader5 Python library unavailable (Windows-only integration); no real demo broker, SL fills, Task Scheduler, PowerShell execution, or 5/10-day data have been tested here.
- Added 11 new isolated tests covering: training prohibition, fail-closed Parquet preflight, archive resume locks and orphans, heartbeat alerts, read-only real-deal stop slippage math for BUY and SELL, modified/missing stops, idempotent SQL event persistence, refusal to match by magic alone and lack of history.
- `python -m compileall -q mt5_agents` passed. No trading orders were sent.
- Release status: **BLOCKED** until required tests pass on Windows with a real Arrow wheel installed, followed by observation of five full broker Demo sessions and ten-session pipeline soak testing. These do NOT grant training approval.
