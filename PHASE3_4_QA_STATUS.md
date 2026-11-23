# Phase 3.4 QA

- New provider: Ollama loopback HTTP `/api/chat` and `/api/tags`, explicit `stream:false`, `think:false`, num_predict cap.
- Remote URLs, proxies, redirects rejected; sanitized market snapshot; no broker execution imports in provider.
- Provider config backwards compatible with the older OpenAI opt-in, default remains openai unless `AGENT_AI_PROVIDER=ollama`.
- `AGENT_AI_ENABLED=true` with provider ollama requires no OpenAI key.
- Bounded one-at-a-time discussion and persistent max-per-day discussion count.
- Offline `check_discord_hub.bat` now verifies real local model inference when Ollama is enabled.
- Developer-side targeted suite: **32 passed** (`tests/test_phase3_4_ollama.py`, `tests/test_phase3_3_discord_discussion.py`, `tests/test_phase3_3_discord_queue.py`).
- NOT yet tested: actual Ollama installation/model on user Windows, actual Discord chat invocation after the patch, full Python suite with pyarrow and MT5 on Windows. User must run tests and preflight.
