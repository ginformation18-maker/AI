PHASE 3.3.1 - Complete Discord addon for existing mt5_ai_agents installation

CAUSE OF THE ERROR:
The previous Phase3_3_1_Nonblocking_Hotfix.zip had only modified modules and tests.
It did NOT contain scripts/check_discord_hub.bat or scripts/run_discord_hub.bat.

INSTALL:
1. Close any Python/Discord bot processes started from this project.
2. Keep your existing .env, .venv, and runtime folders intact. DO NOT delete them.
3. Extract this ZIP's CONTENTS into C:\Users\user\Desktop\mt5_ai_agents
   (IMPORTANT: do not create an extra nested mt5_ai_agents folder).
4. In CMD, run:
   cd /d C:\Users\user\Desktop\mt5_ai_agents
   dir scripts\*discord*hub*.bat
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m pytest tests -q
   scripts\check_discord_hub.bat
5. Only if all checks pass, run: scripts\run_discord_hub.bat
6. The hub is read-only; keep MODE=DRY_RUN and a connected Demo account.
   For first Discord connectivity test set AGENT_AI_ENABLED=false and
   AGENT_AUTO_DISCUSS_ENABLED=false; do not share .env or bot token.

This addon intentionally does not contain .env, .venv, STOP, or runtime files.
