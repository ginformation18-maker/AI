# Phase 3.5 QA status — 22 November 2026

- Source base: Phase 3.4 Ollama Local Full, **read-only hub**.
- Targeted tests for Phase 3.5 and adjacent Discord/Ollama paths: **51 passed** (local Python 3.13).
- The rest of the suite excluding a collection-time `pyarrow` import: **138 passed, 5 failed** solely because real `pyarrow` is not installed in this container. The full suite also has a collection-time import error when including `test_parquet_timestamp_units_regression.py` in this environment. **Not marked as passed or skipped.**
- User's last-known Windows result before this patch: 126 passed and real Parquet preflight PASS, but **the new Phase 3.5 full suite remains unverified on Windows**.
- No live Discord Webhook messages, Ollama GPU use, MT5 integration, or live quote ingestion were tested from this environment.
- Webhook sender names are per-message overrides using ONE webhook, with guild/channel fetch verification, no mentions, and loopback-only inference for Ollama.
- `discord_hub.lock` inspection and manual stale-lock removal are fail-closed; no blanket process kill or automatic lock deletion.
- Default fail-closed DRY_RUN and no-order paths remain in place. No trade execution components modified.
