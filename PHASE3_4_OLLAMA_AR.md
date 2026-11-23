# Phase 3.4 — Ollama محلي لوكلاء نقاش Discord

## الهدف والحالة

- إضافة مزود محلي للذكاء الاصطناعي `AGENT_AI_PROVIDER=ollama` إلى بوت Discord الموجود في Phase 3.3.1.
- يستخدم **نموذجًا واحدًا** بأربعة أدوار متسلسلة: MARKET وQUANT وRISK وCOORDINATOR؛ ليست أربعة عمليات نموذج مستقلة أو أربعة حسابات Discord.
- **لا يحتوي موصل Ollama على اتصال بالأوامر أو صلاحية للتداول**. محرك تنفيذ MT5، جمع Ticks، والـParquet خارج استدعاءات النموذج.
- يرفض أي Ollama URL ليس `http://localhost:<port>` أو `http://127.0.0.1:<port>` (أو loopback IPv6). كما يلغي HTTP proxy وإعادة التوجيه.
- لا ترفع بيانات الحساب أو كلمات المرور إلى LLM؛ لقطات السوق تُنقى من الحقول غير المسموحة قبل أي طلب.
- نقاش تلقائي بحد 6 في اليوم (افتراضي)، بدون تكرار متزامن، مع `num_predict=220` ومهلة محلية 120 ثانية لكل دور؛ قد يحتاج جهاز ضعيف وقتًا أطول.
- **لا توجد تكلفة API عند استخدام نموذج محلي محمل مسبقًا**. توجد تكلفة الكهرباء وموارد RAM/CPU/GPU. لا تختَر نماذج `:cloud`.

## التثبيت على Windows

1. أوقف البوت الحالي بـ `Ctrl+C` ثم فك محتويات إضافة ZIP *داخل* `C:\Users\user\Desktop\mt5_ai_agents`، مع الحفاظ على المجلدات واستبدال الملفات المشتركة.
2. لا تحذف ولا تستبدل `.env` أو `.venv` أو `runtime`.
3. نزّل Ollama من https://ollama.com/download/windows وشغّله على Windows. ينبغي أن تكون الخدمة المحلية على المنفذ `11434`.
4. في CMD جديد:

```bat
ollama --version
ollama pull qwen3:4b
ollama list
```

يبلغ حجم ملف نموذج `qwen3:4b` قرابة 2.5 GB (غير شامل الذاكرة أثناء التشغيل). إن كان جهازك محدود RAM، استخدم `ollama pull qwen3:1.7b` واضبط `OLLAMA_MODEL=qwen3:1.7b`.

## تعديل ملف `.env` (محلي فقط)

افتح:

```bat
notepad .env
```

**احذف أي تكرار للمفاتيح الموجودة أدناه**، واضبط التعريف النهائي الوحيد لكل مفتاح:

```ini
MODE=DRY_RUN
AI_VETO_ENABLED=false
AGENT_AI_PROVIDER=ollama
AGENT_AI_ENABLED=true
AGENT_AUTO_DISCUSS_ENABLED=true
AGENT_AUTO_DISCUSS_INTERVAL_SECONDS=3600
AGENT_MAX_DISCUSSIONS_PER_DAY=6
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:4b
OLLAMA_TIMEOUT_SECONDS=120
OLLAMA_MAX_PREDICT=220
OPENAI_API_KEY=
```

**احتفظ بإعدادات `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID` الحالية دون نشرها**. كذلك لا تضع كلمة مرور MT5 في أي محادثة أو لقطة شاشة.

لا تحتاج إلى `OPENAI_API_KEY` للنقاش المحلي. تأكد أن `AI_VETO_ENABLED=false` يبقى ثابتًا؛ `AGENT_AI_ENABLED` يفعل *نقاش Discord* فقط.

## الفحص والتشغيل

من داخل مجلد المشروع:

```bat
.\.venv\Scripts\python.exe -m pytest tests -q
scripts\check_ollama.bat
scripts\check_discord_hub.bat
scripts\run_discord_hub.bat
```

- يجب أن يعرض `check_ollama.bat` حالة `PASS` وتفاصيل `inference_test: true`، لأن الفحص يولد إجابة فعلية من النموذج المحلي.
- `check_discord_hub.bat` سيعيد فحص Ollama إذا كان مفعلاً، إلى جانب Parquet وفحص سلامة الأرشيف.
- لا تفتح نسختين من البوت. اترك نافذة CMD مفتوحة أثناء التشغيل، أو استخدم آلية بدء Windows الحالية بعد نجاح التشغيل اليدوي.
- افتح Discord في `#mt5-agents-room` واكتب `/agents` ثم `/discuss` أثناء توافر لقطة أسعار حديثة صالحة.
- أثناء عطلة السوق قد يظل البوت متصلاً لكن المناقشات عن بيانات السوق ستُرفض لأن الـQuotes قديمة/غير صالحة؛ **هذا سلوك مقصود**. اختبار Ollama المحلي يعمل حتى لو كان السوق مغلقًا.

## استكشاف الأخطاء

- `ollama is not recognized`: افتح CMD جديداً بعد تثبيت التطبيق أو أعد تشغيل Ollama.
- `Local model is not installed`: نفذ `ollama pull qwen3:4b` وانتظر نهاية التنزيل.
- `Ollama local request failed`: افتح تطبيق Ollama وتأكد أن `http://127.0.0.1:11434` متاح؛ ربما النموذج أكبر من قدرة جهازك أو انتهت المهلة. راجع سجلات Ollama محلياً.
- إذا كان توليد 4 أدوار بطيئًا: جرب `qwen3:1.7b` أو `OLLAMA_MAX_PREDICT=128`؛ لا تغير حدود التنفيذ أو فحوص MT5.
- لا ترسل رموز Discord أو كلمات مرور MT5 أو ملف `.env` لأي طرف.

## حالة الاختبارات والقيود

الاختبارات المستهدفة أُجريت محلياً باستخدام عميل وهمي وخادم HTTP على loopback. لم يُختبر النموذج الحقيقي `qwen3:4b` أو Discord Gateway أو MT5/Parquet على Windows داخل بيئة التطوير؛ نجاح فحصك على Windows مطلوب قبل اعتبار المسار جاهزًا.
