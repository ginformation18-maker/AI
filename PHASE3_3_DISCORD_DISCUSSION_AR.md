# Phase 3.3 — قناة Discord واحدة للنقاش بين وكلاء المراقبة

## الموارد والأمان

- **بيانات السوق:** حساب MT5 Demo النشط والرموز المكتوبة في `SYMBOLS`، دون أوامر تداول.
- **المعالجة:** كمبيوترك (Python + Pandas + Parquet)؛ لا يعمل البوت وجهازك مغلق.
- **الذكاء الاصطناعي:** طلبات OpenAI Responses API مستقلة لمهام MARKET / QUANT / RISK / COORDINATOR. يلزم `OPENAI_API_KEY` بمحاسبة API منفصلة عن اشتراك ChatGPT. لم نربطه بعد بأي مفتاح حقيقي.
- **التواصل:** Discord Bot واحد في **قناة نصية واحدة داخل Server** (ليس Group DM)، برسائل تحمل اسم كل دور. تفاعل المستخدم من `/agents` و`/discuss`.
- **الحماية:** لا تستدعي الوحدة `order_send` أو `ExecutionAgent` ولا تسمح بأوامر تداول من Discord. تشترط `MODE=DRY_RUN` وحساب Demo صحيح. عند تلف Manifest أو فشل Parquet تتوقف. الفجوات توثق UNVERIFIED دون استكمال طويل صامت.

## إعداد Discord (على حسابك، لا تشارك رموز الوصول)

1. افتح `https://discord.com/developers/applications`، واضغط **New Application** واختر `MT5 Research Agents`.
2. من **Bot** أنشئ Bot وخذ **Token**. خزّنه فقط في ملف `.env`. لا ترسله في دردشة أو لقطة شاشة.
3. من **Installation / OAuth2** أدخل `bot` و`applications.commands`، مع `View Channels`, `Send Messages`, `Read Message History`، ثم ثبّت البوت داخل سيرفر Discord الذي تملكه/تديره. لا حاجة لأي Administrator permission.
4. أنشئ قناة نصية باسم `mt5-agents-room` داخل السيرفر. فعّل **User Settings > Advanced > Developer Mode**، ثم **Copy Server ID** و**Copy Channel ID** من القوائم المختصرة.
5. افتح `.env` محليًا:

```ini
MODE=DRY_RUN
SYMBOLS=XAUUSD
DISCORD_BOT_TOKEN=PLACE_LOCAL_TOKEN_HERE
DISCORD_GUILD_ID=YOUR_DISCORD_SERVER_ID
DISCORD_CHANNEL_ID=YOUR_DISCORD_TEXT_CHANNEL_ID
AGENT_AI_ENABLED=true
OPENAI_API_KEY=PLACE_LOCAL_OPENAI_API_KEY_HERE
AGENT_AI_MODEL=gpt-4.1-mini
AGENT_AUTO_DISCUSS_ENABLED=true
AGENT_AUTO_DISCUSS_INTERVAL_SECONDS=3600
AGENT_MAX_DISCUSSIONS_PER_DAY=6
```

استخدم رقم الحساب وكلمة مرور التداول التجريبي السابقين داخل `.env` أيضًا. احتفظ بـ`AI_VETO_ENABLED=false` لأننا نشغّل مراقبة فقط؛ لا تربط هذا البوت بمحرك التنفيذ.

## التثبيت/التحقق (من CMD داخل مجلد المشروع)

```bat
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest tests -q
scripts\check_discord_hub.bat
scripts\run_discord_hub.bat
```

بعد نجاح تسجيل دخول Discord سيظهر في القناة `MT5 AI discussion hub ONLINE`. اكتب `/agents` للتحقق من المراقبة و`/discuss` لاختيار الرمز وطرح سؤال. النقاش التلقائي يحدث فقط مع **لقطة سوق حديثة** وفقط عند تفعيل AI والنقاش التلقائي. عند توقف سوق الذهب لا يخترع بيانات جديدة، ولا يرسل توصيات تداول.

## تشغيل تلقائي مع Windows

بعد نجاح التشغيل اليدوي، أغلقه أولًا (`Ctrl+C`) ثم شغّل **PowerShell** من مجلد المشروع:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_discord_hub_autostart.ps1
```

تُنشأ مهمة Task Scheduler *للمستخدم الحالي عند تسجيل الدخول*؛ يبدأ بعدها بعد 30 ثانية. لا يعمل جهازًا مطفأً؛ للاستمرار 24/7 يلزم جهاز دائم التشغيل أو VPS Windows مع MT5. **لا تشغّل** السكربت القديم `run_discord_agents.bat` أو `tick_collector --forever` بالتوازي لأن ذلك قد يتسبب بتصادم الأرشيف.

## حدود مهمة

- نحن لم ندخل سر Discord Bot أو OpenAI API فعليًا ولم نفحص اتصال Discord من هذا الجهاز؛ ربط الحساب والقناة يتم على جهازك.
- يستعمل أربعة استدعاءات LLM لكل نقاش مكتمل، مع حد يومي افتراضي **6 نقاشات = حتى 24 استدعاء API**، وقد تكون هناك رسوم. الحد اليومي يُحفظ في `runtime/discord_ai_budget.json` ليستمر عبر إعادة التشغيل، واضبط أيضًا حد الإنفاق لدى مزود API.
- بيانات MetaQuotes-Demo غير كافية لتقييم استراتيجية قابلة للتطبيق لدى وسيط آخر. تحليل السوق الحالي لغرض البحث والاختبار، وليس توصية تداول.
- الأوامر Slash لا تحتاج إلى **Message Content Intent**، وهي أسهل وأقل صلاحيات من قراءة كل المحادثات.
- حالات التعطل أو غموض البيانات تتوقف بأمان ولا تتجاوز STOP. إعادة التشغيل بعد الانقطاع تحتاج تدقيقًا، ولا يوجد التزام تلقائي بتعويض الساعات المفقودة.
