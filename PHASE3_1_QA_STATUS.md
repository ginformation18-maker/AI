# Phase 3.1 verification status — 2026-11-21

- Source: the user's `MT5_AI_Trading_Agents_Phase3_Engineering_Hardening.zip`.
- Changes: full Parquet schema + readback gate, ATR/spread/UTC-session stress model, broker-native SL/TP preflight/post-ACK checks, durable protection-event requirement, five-session Demo archive auditor, Windows preflight script.
- Local check with Python 3.13 and a **stub MetaTrader5 module** to test pure code without a broker: **73 passed**; **1 mandatory real-Parquet test deselected**; additionally **4 strict Parquet integration tests not run** because they require `pyarrow`.
- The actual Parquet test is **NOT PASSED** in this environment: `pyarrow` is missing, and `pip install pyarrow` failed due network / DNS unavailability. The required test now FAILS rather than SKIPS when dependency is absent.
- **No Windows / MT5 terminal / real Demo account was available.** No real broker order, broker protection, or five-day live tick recording has been tested.
- Release / training decision: **NOT APPROVED** until Windows test suite, five valid broker Demo sessions, ten-session pipeline stability audit, and separate multi-regime ML readiness review (not a ten-session training gate).
