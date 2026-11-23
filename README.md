## Phase 3.2 — Data observation only / تشغيل مراقبة البيانات فقط (CURRENT)

**Read `PHASE3_2_OBSERVATION_SAFETY_AR.md` FIRST.** This patch supersedes the training-readiness claims of all earlier phase-3 docs: five or ten sessions certify neither an ML dataset nor an executable strategy. Windows preflight with real `pyarrow` round-trip is mandatory; collection starts only after it passes. Read-only stop-loss deal analytics and bounded Windows supervisor included. No live money trading and no LightGBM training authorized.

# MT5 AI Trading Agents — مشروع تعليمي تجريبي للذهب والفوركس

> **Phase 3 (21 نوفمبر 2026):** إعادة بناء هندسية للإصدار الثالث انطلاقاً من ملف المرحلة الثانية المرفق: Collector مستقل للـTicks بنوافذ UTC قصيرة وParquet/ZSTD مقسّم يومياً؛ مهلة Reconciliation قدرها 60 ثانية مع استمرار المنع حتى التسوية البشرية؛ ونموذج انزلاق سعري محافظ من ATR في Backtest. [التوثيق الكامل](PHASE3_ENGINEERING_HARDENING.md). **هذا ليس استرجاعاً لملف إصدار ثالث أصلي، ولا جاهزاً للتداول الحقيقي.**

> **ملاحظة تاريخية عن المرحلة الأولى — 21 نوفمبر 2026:** أُضيفت وحدة `mt5_agents/memory/` لتسجيل الإشارات وإذن تنفيذ منفرد ومحاكاة Triple Barrier (SL-first عند الغموض). عند فشل حفظ إذن التنفيذ تتوقف الأوامر الجديدة. التفاصيل والقيود: [`PHASE1_MEMORY.md`](PHASE1_MEMORY.md). **هذه ليست نسخة تعلّم ذاتي مكتملة**؛ راجع تحديث المرحلتين الثانية والثالثة أعلاه لتنفيذ المصالحة والجامع المنفصل.

> **تحذير مالي:** هذا كود بداية قابل للتطوير، وليس استراتيجية مثبتة الربحية، وليس خدمة تداول مُدارة. لا يوجد ضمان للعوائد. الذهب والفوركس باستخدام الرافعة المالية قد يسببان خسارة كبيرة، والـ SL ليس ضماناً للسعر عند الفجوات والانزلاق. **لم يتم التحقق من هذا المشروع مع وسيطك أو خادم MT5 حقيقي**. تم تضمين اختبارات وحدات ومحاكاة تاريخية تقريبية، لكن ذلك لا يعادل اختبار الدمج مع الوسيط.

## الهيكل

```text
mt5_ai_agents/
├── .env.example             # إعدادات، لا تتضمن بيانات حقيقية
├── .gitignore
├── requirements.txt
├── news_blackouts.json      # قائمة منع تداول حول الأخبار بتوقيت UTC؛ [] افتراضياً
├── mt5_agents/
│   ├── settings.py          # فحص الإعدادات وحماية التداول الحقيقي
│   ├── market.py            # الاتصال بالمنصة وقراءة الشموع المكتملة
│   ├── analysis.py          # وكيل EMA/RSI/ATR
│   ├── ai_review.py         # وكيل اختياري: مراجعة وحجب الإشارات فقط
│   ├── news.py              # منع التداول ضمن فترات أخبار تُدخل يدوياً
│   ├── risk.py              # حدود الخسارة، الهامش، الأوامر القائمة، حساب اللوت
│   ├── math_helpers.py      # التقريب لسلم أحجام العقود وتسعير Tick
│   ├── execution.py         # التنفيذ الوحيد المسموح له باستدعاء order_send
│   ├── monitor.py           # كشف المراكز الخاصة بالبوت التي فقدت وقف الخسارة/الهدف
│   ├── state.py             # منع تكرار تنفيذ إشارة الشمعة بعد إعادة التشغيل
│   ├── logging_tools.py     # الأحداث بصيغة JSONL
│   ├── main.py              # التشغيل المتواصل
│   └── backtest.py          # محاكاة تاريخية تقريبية باستخدام OHLC
└── tests/                  # اختبارات وحدات قابلة للتشغيل دون MT5
```

## 1) التجهيز على Windows 10/11 بنسخة 64-bit

1. ثبّت Python **3.12 (64-bit)** وMetaTrader 5 الرسمي من وسيطك.
2. افتح حساب تداول **Demo** على منصة MT5، وسجّل رقم الحساب وكلمة المرور واسم الخادم حرفياً.
3. افتح MT5 وسجّل الدخول، وأضف رمز الذهب وزوج العملات إلى Market Watch. قد يكون الاسم `XAUUSDm` أو `GOLD` بدلاً من `XAUUSD`، ويجب إدخال الرمز الفعلي الذي يظهر لدى وسيطك.
4. فك ضغط المشروع ثم افتح PowerShell **داخل المجلد** ونفّذ:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

إذا منع PowerShell تفعيل البيئة، يمكنك تشغيل Python مباشرة عبر `./.venv/Scripts/python.exe` (في PowerShell المسار ` .\.venv\Scripts\python.exe `) بدلاً من تغيير سياسة أمان الجهاز.

## 2) إعداد ملف `.env`

غيّر **MT5_LOGIN** و**MT5_PASSWORD** و**MT5_SERVER** للقيم الخاصة بالحساب التجريبي. لا ترسلها إلى أي أحد ولا تضعها في Git. استخدم `MT5_TERMINAL_PATH` إذا كان لديك أكثر من نسخة MT5.

الإعدادات الأساسية:

```dotenv
MODE=DRY_RUN
SYMBOLS=XAUUSD,EURUSD
TIMEFRAME=M15
RISK_PER_TRADE_PCT=0.25
MAX_DAILY_LOSS_PCT=1
MAX_OPEN_POSITIONS=2
```

- `DRY_RUN`: يسجل الصفقات الافتراضية المحتملة ولا يرسل أي أمر.
- `DEMO`: يرسل أوامر تلقائياً إلى **حساب Demo فقط** بعد تفعيل السماح بالتداول الآلي في MT5.
- `LIVE`: معطل بقفلين صريحين يتطلبان تعديلك اليدوي، وفحص نوع الحساب الحقيقي.

مهم: قيود النسب هي **أهداف للحد من المخاطر على حجم الصفقة عند الدخول**، وقد تُتجاوز الخسارة الفعلية بسبب الانزلاق والفجوات/التصفية.

## 3) الاختبارات

```powershell
python -m pytest -q
python -m compileall -q mt5_agents
```

لا تتطلب اختبارات الوحدات اتصال MT5. **تشغيل الكود على بيئة مختلفة عن وسيطك لا يكفي للتحقق من صحة التنفيذ الحقيقي.**

## 4) محاكاة تاريخية (بحث أولي فقط)

أولاً حافظ على `MODE=DRY_RUN` في `.env`، ثم:

```powershell
python -m mt5_agents.backtest --symbol XAUUSD --bars 3000
python -m mt5_agents.backtest --symbol EURUSD --bars 3000
```

تظهر النتائج في `runtime/backtest_XAUUSD.csv` أو `runtime/backtest_EURUSD.csv` مع عدد الصفقات، نسبة الفوز، عائد **R** ومقدار التراجع بوحدات **R**.

### قيود المحاكاة المهمة

- تستخدم شموع OHLC والسبريد التاريخي المسجل مع الشمعة على نحو **تقريبي**، وليس بيانات Tick دقيقة.
- تدخل عند افتتاح **الشمعة التالية** لمنع التطلع للمستقبل.
- تفترض احتساب ضرب وقف الخسارة أولاً لو ضرب الوقف والهدف في الشمعة نفسها.
- تتضمن منذ المرحلة الثالثة **افتراض عقوبة انزلاق قائم على ATR على الدخول والخروج**، لكنها لا تتضمن عمولات أو تبييت أو السبريد اللحظي داخل الشمعة أو سيولة حقيقية أو متطلبات الهامش؛ لا تعتمد على النتائج وحدها.
- الأداة ليست اختبار MT5 Strategy Tester الحقيقي. للثقة الأعلى استخدم Tick-level simulation وتحقق من فروق التوقيت، والتكاليف، واختبار خارج العينة وWalk-Forward.

## 5) اختبار قراءة البيانات دون أوامر

```powershell
python -m mt5_agents.main --once
```

ثم تشغيل مراقبة دائمة (ما دام برنامج Python وMT5 والحاسوب يعملون):

```powershell
python -m mt5_agents.main
```

يسجل كل إشارة في `runtime/events.jsonl`. يجب أن يكون السوق مفتوحاً وأن تكون الشموع حديثة. المنطق ينفذ على **الشموع المكتملة** فقط، ولا يكرر نفس الشمعة حتى بعد إعادة تشغيل البرنامج. استخدام `Ctrl+C` يوقف البرنامج لكنه لا يغلق أي مراكز موجودة لدى الوسيط.

## 6) التداول التجريبي الآلي

بعد دراسة نتائج Backtest وتحديث الاستراتيجية واختبار سلامة التحكم بالأوامر:

1. غيّر `MODE=DEMO` في `.env`؛ لا يجوز أن يكون حساب MT5 من نوع REAL.
2. من MT5 تأكد من تمكين **Algo Trading** وأن الحساب يسمح بالتداول. في إعدادات Expert Advisors، قد تحتاج رفع الحظر عن التداول عبر Python API الخارجي حسب إصدار MT5 (راجِع إعدادات الأمان بنفسك).
3. ابدأ بـ `python -m mt5_agents.main` بعد التحقق من الرمز، وحجم اللوت الأدنى، وقيود الوسيط، وملف الأخبار.
4. اتركه على **حساب تجريبي** عدة أسابيع في ظروف مختلفة، ثم قارن سعر الطلب الفعلي والسبريد ونتيجة التنفيذ والـ SL مع السجل.

الاستراتيجية هنا لا تفتح أكثر من مركزين على مستوى الحساب ككل، ولا تفتح مركزاً إذا كان هناك مركز أو أمر معلّق على الرمز نفسه، ولا تعيد إرسال أمر عند حالة تنفيذ غير مؤكدة.

## 7) إضافة الذكاء الاصطناعي (اختياري)

تم وضع وكيل AI يستعمل OpenAI Responses API للمراجعة فقط. لإضافته:

```dotenv
AI_VETO_ENABLED=true
OPENAI_API_KEY=ضع_المفتاح_الحقيقي_هنا
OPENAI_MODEL=gpt-4.1-mini
```

هو **لا يولّد صفقات من نفسه ولا يحدد عدد العقود ولا يستدعي واجهة الأوامر**؛ يستطيع حجب إشارة تقنية قائمة فقط. إذا فشل الاتصال أو تحليل المخرجات، يمنع الصفقة (Fail Closed). لا يستطيع التحقق من الأخبار المباشرة من هذه البيانات المحدودة، ولا تضمن مراجعته دقة أو ربحية قرار التداول. قد تترتب رسوم على استخدام OpenAI API.

## 8) الأخبار الاقتصادية

الملف `news_blackouts.json` يبدأ بمصفوفة فارغة `[]`، أي **لا توجد تصفية أخبار مباشرة افتراضياً**. أضف الفترات التي تريد منع التداول خلالها بالتوقيت العالمي UTC بالشكل:

```json
[
  {
    "start_utc": "2027-01-01T12:00:00Z",
    "end_utc": "2027-01-01T14:00:00Z",
    "reason": "Example scheduled high-impact event (illustrative only)"
  }
]
```

هذا **مثال تنسيقي افتراضي، وليس موعد خبر فعلي**. للانتقال للإنتاج يلزم مصدر تقويم اقتصادي موثوق ومرخّص وتحديث تلقائي وفحوص عند تعطل المصدر.

## 9) إيقاف طارئ

أنشئ ملفاً باسم `STOP` في جذر المشروع لإيقاف إرسال **أي صفقات جديدة**. عند حالات تنفيذ غير مؤكدة أو تعبئة جزئية أو اكتشاف مركز تابع للبوت بدون SL/TP ينشئ البرنامج الملف تلقائياً ويتوقف عن الإرسال حتى التحقيق. هذا الملف **لا يغلق الصفقات المفتوحة**؛ راقب MT5 والوسيط بشكل مستقل، ولا تعتمد على هذا الإيقاف وحده عند تعطل الحاسوب أو الشبكة.

```powershell
New-Item -ItemType File STOP
```

افحص أي صفقة أو أمر في سجل الوسيط قبل مسح `STOP`؛ لاتحذف الملف تلقائياً دون تحقيق.

## 10) وضع الحساب الحقيقي — غير موصى به قبل تأهيل مستقل

الكود يهيئ قفلين يدوياً في `settings.py` و`execution.py`: وجود حساب REAL فعلي لدى الوسيط، وقيمة `LIVE_ENABLE_TEXT` المطابقة، وملف `ENABLE_REAL_TRADING.txt` يحتوي عبارة التفويض المطلوبة. لن نتعامل مع هذه الأقفال كدليل على جاهزية التداول الحقيقي؛ يجب إضافة عمليات مراقبة مستقلة وتعامل مع انقطاع الطاقة، والمراكز القائمة، وسجلات التنفيذ، وحدود المخاطرة المجمعّة وعمليات قبول تشغيل مضبوطة مع وسيطك. **لا تفعّل الوضع الحقيقي اعتماداً على نجاح اختبارات الوحدات وحدها.**

## نقاط تقنية مؤثرة على صحة التداول

- قياس اللوت يتم عبر `mt5.order_calc_profit` بعملة الحساب، وليس افتراض أن النقطة تساوي قيمة نقدية ثابتة للذهب أو EURUSD.
- عند تغير السعر أثناء الإرسال يعاد فحص الخسارة المحتملة وفق السعر الجديد، ولا تُرسل أوامر دون وقف وهدف.
- لا يُسمح بتكرار إرسال أمر إذا كانت نتيجة التنفيذ غير مؤكدة؛ يجب المصالحة مع سجلات الوسيط يدوياً.
- خط خسارة اليوم يعتمد على Equity لحظة أول تشغيل في **اليوم بالتوقيت UTC**، ويخزّن ملفه محلياً. لا يعرف بالضرورة التغيرات قبل تشغيله لأول مرة في ذلك اليوم. لا يعتبر هذا رقابة شاملة على حساب يضم استراتيجيات أخرى.
- إعداد `POLL_SECONDS=15` مناسب لشموع M15، وليس HFT/Scalping عالي السرعة.
- الأسعار والساعات والرموز وقواعد Filling ومضاعفات العقود تختلف باختلاف الوسيط؛ أي اختلاف قد يؤدي إلى رفض الأمر بأمان.

## المصادر التقنية

- MetaQuotes Python Integration: https://www.mql5.com/en/docs/python_metatrader5
- MetaQuotes `order_send`: https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py
- MetaQuotes `order_calc_profit`: https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py
- MetaQuotes `order_check`: https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py
- OpenAI Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs

---

## تحديث المرحلة الثانية: In-Flight Reconciliation + Historical Bid/Ask Ticks

اقرأ `PHASE2_RECONCILIATION_TICKS.md` قبل تشغيل وضع `DEMO`. النظام الآن يمنع إعادة تنفيذ أي إشارة لها Intent غير محسوم ويتعامل مع نتائج `order_send` غير المؤكدة بوضع STOP؛ توجد أداة تقرير قراءة فقط `python -m mt5_agents.reconcile --report`. تم إدخال محول Tick Bid/Ask للتحليل، لكنه **غير مفعّل بعد ضمن التدريب أو المحاكاة الإنتاجية**.

> **مهم:** لا تزيل STOP أو تنشئ ملف SQLite جديدًا لتجاوز فقدان السجلات. إذا فُقد ملف قاعدة البيانات الأصلية، لا يمكن استنتاج أن الوسيط لم ينفذ أوامر معلقة. يجب إجراء مصالحة بشرية من سجل الوسيط قبل أي تشغيل جديد.

## Phase 3.1: Important safety update

See `PHASE3_1_OPERATIONAL_HARDENING_AR.md` **before running the collector**. Real PyArrow integration tests are mandatory (not skipped), SL/TP requires post-fill verification and a durable audit record, and model training remains blocked irrespective of five/ten observation sessions until a separate multi-regime research release. This update supersedes Phase 3's earlier unverified PyArrow-test claim.

### Phase 3.3 — Discord single-channel AI discussions (read-only)

This build adds a *new* bot entry point, `python -m mt5_agents.discord_hub`, and
`PHASE3_3_DISCORD_DISCUSSION_AR.md` with Arabic onboarding instructions.
It does NOT place trades, start the trading execution agent, or replace the
original safeguards. Requires a separate Discord bot token and optionally an
OpenAI API key. Do not run the legacy `discord_observer` and new `discord_hub`
concurrently, and do not run two collectors for the same archive.
