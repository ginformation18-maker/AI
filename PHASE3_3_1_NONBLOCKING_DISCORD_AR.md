# Phase 3.3.1 — Discord Non-blocking Queue + API Limits

## ما تم إصلاحه

- نقل إشعارات Discord من انتظار متزامن `Future.result(timeout=20)` إلى `Queue(maxsize=128)` داخل عملية Hub؛ `send()` يضع الرسالة فوراً أو يرفضها مع log عند الامتلاء. `asyncio` يقوم بالإرسال على مسار منفصل عن خيط MT5 والأرشفة.
- مهلة إرسال Discord: 10 ثوانٍ لكل رسالة؛ أخطاء الإرسال لا توقف دورة جمع Ticks لكنها تظهر في سجلات التشغيل. لا ضمان بتسليم كل رسالة حين يتعطل Discord أو تمتلئ القائمة.
- استدعاء OpenAI لكل دور: `timeout=10.0` و`max_retries=0` و`max_output_tokens=330` (موجود مسبقاً)، وأربع استدعاءات لكل نقاش. الحد اليومي الافتراضي 6 نقاشات (حتى 24 طلب)، **ليس حداً مالياً بالدولار**.
- Discord Hub يُعلن اتصال القناة ويقول إن Observer في STARTING؛ الحالة الفعلية تظهر عبر `/agents`.
- التشغيل `MODE=DRY_RUN` وMT5 Demo فقط، بدون أي أوامر تداول من البوت.

## التثبيت على Windows

1. فك ضغط Hotfix داخل مجلد `mt5_ai_agents` مع الحفاظ على المجلدات؛ ثم افتح CMD داخل هذا المجلد.
2. نفّذ ` .\.venv\Scripts\python.exe -m pip install -r requirements.txt `.
3. نفّذ ` .\.venv\Scripts\python.exe -m pytest tests -q `.
4. أنشئ Discord Bot داخل سيرفر وقناة `mt5-agents-room`، واحتفظ برمز Bot محلياً.
5. أضف إلى `.env` محلياً `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID` وتأكد من `MODE=DRY_RUN` و`AI_VETO_ENABLED=false`.
6. ابدأ أولاً بـ `AGENT_AI_ENABLED=false` و`AGENT_AUTO_DISCUSS_ENABLED=false` لاختبار Discord دون إرسال طلبات AI مدفوعة.
7. نفّذ `scripts\check_discord_hub.bat` ثم `scripts\run_discord_hub.bat` وتحقق من أول رسالة في القناة، ثم `/agents`.
8. بعد نجاح قناة Discord والتحقق من سلامة بيانات السوق، أضف `OPENAI_API_KEY` وفعّل `AGENT_AI_ENABLED=true` إذا أردت نقاشات فعلية، ثم `/discuss`. لن تبدأ نقاشات تلقائية إذا لم تكن بيانات الأسعار جديدة.

## القيود

- لا يوجد هنا اتصال مباشر بحساب Discord الخاص بك، ولا تشغيل لبوت على جهازك. اختبار مصادقة القناة وبيانات الوسيط يجب أن يتم على Windows.
- عندما تكون الأسعار متوقفة/قديمة فلن يستدعي `/discuss` النماذج، حتى لو نجح الاتصال بـDiscord.
- الأرشيف القديم يحوي نافذة من الجمعة؛ الانقطاعات توثق كـ `UNVERIFIED`؛ لا تحذف البيانات ولا تعطل حماية الاستئناف.
- لا تشغل `tick_collector --forever` أو `discord_observer` مع `discord_hub` على نفس قاعدة الأرشيف.
- إذا ظهرت `STOP` أو `HALTED`، اقرأ السجلات ولا تزيل التحذير تلقائياً.
- هذا إصلاح تقليل تأخير الإشعارات؛ لا يقدم ضمان تأخير كلي بالملي ثانية، أو تنفيذ صفقات، أو استمرارية 24/7.
