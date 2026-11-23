# Phase 3.3.1 QA

- Based on Phase 3.3 source. Nonblocking Discord outbound notification queue with capacity 128, asyncio consumer, 10s Discord send timeout.
- Four OpenAI role calls per discussion, client 10s timeout, zero automatic retries and existing output cap 330 tokens per call.
- **12 targeted local tests PASSED** (`test_phase3_3_discord_discussion.py` and `test_phase3_3_discord_queue.py`). Syntax compiled.
- **Full suite not passed in this build environment:** `pyarrow` is missing, so a timestamp regression module fails at collection and real Parquet tests fail. These are intentionally NOT skipped or declared successful.
- User previously reported 94 passing tests and successful real Parquet roundtrip on their own Windows system before the Phase 3.3/3.3.1 update. Rerun the complete suite locally after applying this patch.
- Live Discord authentication, network delivery, and OpenAI billable calls have **not** been exercised. No persistent background job was started; the user must configure their own Discord Bot and credentials on Windows.
