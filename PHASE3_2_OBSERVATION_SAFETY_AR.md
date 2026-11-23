# MT5 AI Trading Agents — Phase 3.2: بوابات سلامة المراقبة (22 نوفمبر 2026)

**الهدف: مراقبة الـTicks على حساب Demo فقط؛ لا تدريب نماذج ولا تنفيذ صفقات حقيقية.**

هذا ملف تحديث تراكمي للإصدار 3.1. الملفات السابقة موجودة للسجل التاريخي لكن أي كلام فيها يربط **10 جلسات بجاهزية تدريب ML** أصبح ملغى، وصححته هذه الوثيقة. تم تأليف التعديلات على بيئة Linux مع اختبارات معزولة، **ولم تتوفر pyarrow / MT5 Windows في بيئة التطوير** لذلك لم يتحقق الاختبار الكامل بعد؛ يجب إنجازه على جهاز Windows قبل الاستخدام.

## 1. Gate A — اختبار Parquet الحقيقي قبل جمع أي بيانات

يفحص `mt5_agents.parquet_preflight` كتابة Parquet حقيقي بـZSTD داخل **نفس قرص/مسار الأرشفة**، ثم قراءة كل الأعمدة والتحقق من `int64 time_msc` و`UTC timestamp[ms]` و`float64` و`int32` والـTicks المتكررة في الميلي ثانية، ثم يختبر رفض ملف تالف. نجاح قراءة ملف واحد اختبار أساسي وليس ضمانًا لصلاحية كل أرشيف مستقبلي، لذلك كل نافذة جديدة تُقرأ بعد الكتابة قبل تحديث الـmanifest، والفحص النهائي يعيد قراءة الملفات والتأكد من SHA-256.

**في Windows 10/11 — PowerShell من جذر `mt5_ai_agents`:**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m mt5_agents.parquet_preflight --archive-root runtime\tick_archive
.\.venv\Scripts\python.exe -m pytest tests -q
```

ينبغي أن يظهر `"status":"PASS"` من أداة الاختبار، وأن تنجح **كل** اختبارات `pytest`. أي نتيجة غير ذلك = **ممنوع تشغيل المجمع**. بديل مجمّع: `powershell -NoProfile -File .\scripts\preflight_windows.ps1` بعد إنشاء البيئة. لا توجد آلية CSV/SQLite بديلة تلتف على غياب pyarrow.

داخل CLI نفسه يوجد preflight إلزامي عند **كل بداية تشغيل** قبل الاتصال بـMT5. حساب الوسيط يجب أن يكون `DEMO` حتى عند استخدام `MODE=DRY_RUN`.

## 2. Gate B — خمس/عشر جلسات = استقرار خط البيانات فقط

- **5 جلسات تداول كاملة** على Demo: فحص فجوات جمع البيانات، أخطاء المنصة، التوقيت، بقاء الملفات قابلة للقراءة، والـmanifest، ومقارنة ساعات تشغيل السوق بالوسيط.
- **10 جلسات تداول كاملة**: فترة تشغيل أطول لفحص استقرار الـPipeline ومراقبة اتجاه استهلاك الذاكرة (RSS) وسجل الأعطال/إعادة التشغيل؛ هذه *ليست* عتبة تدريب إحصائية.
- التدريب على الذهب **مغلق تمامًا في هذا الإصدار**: لا توجد بوابة رقمية في `require_training_approval` تسمح بتدريب LightGBM بعد عدد جلسات. القرار التالي يتطلب بيانات فعلية أوسع تمتد على أنظمة سوق متعددة وأخبار NFP/CPI/FOMC، مع فصل زمني للتدريب/التحقق، واختبار خارج العينة وWalk-Forward، وتوثيق رسوم الحساب/السبريد/التكلفة/التسرب المستقبلي (look-ahead). ليس شرطًا ثابتًا تكفيه بضعة أيام.

لا تعتمد أداة `observation_audit.py` تقويم جلسات الوسيط تلقائيًا: قدّم ملف `observation_sessions.example.json` معدّلًا بجلسات كاملة فعلية UTC وبهويات الحساب/السيرفر الصحيحة، ثم:

```powershell
.\.venv\Scripts\python.exe -m mt5_agents.observation_audit --archive runtime\tick_archive --schedule observation_sessions.example.json --required-days 5 --out runtime\observation_5_days.json
# ثم بعد اكتمال عشر جلسات فعلية، لخطة اختبار الاستقرار فقط:
.\.venv\Scripts\python.exe -m mt5_agents.observation_audit --archive runtime\tick_archive --schedule observation_sessions.example.json --required-days 10 --out runtime\pipeline_10_days.json
```

آخر أمر يفشل حتى يتضمن ملف الجلسات عشر جلسات صحيحة. لا تنس فحص سجل `runtime/tick_archive/collector_health_history_XAUUSD.jsonl` و`runtime/collector_logs/` لمراجعة نمو RSS والانقطاعات؛ أخذ عيّنة RSS لا يثبت وحده انعدام تسرب الذاكرة.

## 3. Gate C — قياس الانزلاق الفعلي عند وقف الخسارة

MT5 يعرض بيانات صفقات الخروج و`reason` و`entry` و`price` و`position_id` و`sl` عبر سجل `history_deals_get`. أداة **قراءة فقط** `mt5_agents.stop_loss_audit`:

- تربط أمر الدخول المؤكد في سجلنا بالـopening deal بواسطة `deal` أو `order` المؤكدين، ثم تربط صفقات الخروج بمعرّف `position_id`، لا بمجرد `magic` الذي قد يتكرر.
- تقبل سبب `DEAL_REASON_SL` مع خروج `DEAL_ENTRY_OUT/OUT_BY`، وتُسجل كل إغلاق جزئي على حدة.
- تسجل `original_requested_stop` و`broker_reported_stop_at_exit` و`actual_fill_price`، والفارق بالنقاط **الموقّع** والعقوبة السلبية فقط، مع تمييز ما إذا تغيّر الوقف.
- لو كانت قيمة `sl` غير موجودة في صفقة الخروج تسجل `STOP_REFERENCE_UNAVAILABLE` وانزلاقًا `null` **وليس صفرًا**. إذا غاب الدخول من نافذة التاريخ أو شارك عدة إشارات نفس مركز netting = حالة غير محسومة، ولا نختلق ربطًا.
- تحفظ دليل التدقيق مرة لكل `signal_id` و`deal_ticket` في `events` (جدول SQLite)، دون إرسال أو إعادة محاولة أي أمر.

للتنفيذ **لاحقًا** فقط عند وجود صفقات Demo فعلية سجلتها المنظومة:

```powershell
.\.venv\Scripts\python.exe -m mt5_agents.stop_loss_audit --days 30 --out runtime\stop_loss_slippage.json
```

**تذكير مهم:** وقف الخسارة server-side لا يضمن سعر الخروج المحدد. سعر التنفيذ قد ينزلق بشكل يجعل الخسارة تتجاوز 0.25% بعد إضافة العمولة؛ الحد البرمجي 0.25% هو حجم مخاطرة **تقديري** وليس حدًا ثابتًا مضمونًا عند فجوات الأخبار.

## 4. Gate D — الإشراف على Windows 10 دون تعطيل التحديثات الأمنية

1. على جهاز مُخصَّص وموصول بالكهرباء، افحص إعدادات الطاقة: `powershell -File .\scripts\windows_power_ac.ps1` (قراءة فقط). لتعديل إعدادات **AC فقط** بتصريح منك: `powershell -File .\scripts\windows_power_ac.ps1 -Apply`، فتتوقف مهلة Sleep/Disk timeout أثناء توصيل الكهرباء. راقب الحرارة والطاقة والاتصال والقرص.
2. **لا تعطّل Windows Update نهائيًا.** استخدم **Active Hours** وفترة صيانة/إعادة تشغيل مخططة، ولا تعتمد على Active Hours كضمان 24/7؛ Windows 10 القياسي انتهى دعمه المجاني الأمني في 14 أكتوبر 2025، لذا يلزم نظام مدعوم أو ترتيبات ESU ملائمة. تعطيل التصحيحات يزيد المخاطر الأمنية.
3. اضبط `.env` على `MODE=DRY_RUN` وببيانات حساب **Demo**، ثم شغّل `scripts\run_collector.bat`. يجري اختبار Parquet الحقيقي تلقائيًا قبل بدء مجمع البيانات، ويعمل المجمع بنوافذ قصيرة فقط دون أوامر تداول.
4. يحتفظ المشرف بسجل في `runtime\collector_logs`، ويعيد المحاولة **بعد فحص manifest والأقفال والملفات اليتيمة واختبار قراءة ملفات حديثة**، وبعدد محدود (الافتراضي 8) وتأخير 30 ثانية. لا يحذف الأقفال اليتيمة ولا يتجاهل الـbacklog أو الفجوات. إذا فشل الفحص أو بلغ حد المحاولات، يتوقف مع رمز خروج غير صفري وتتطلب الحالة مراجعة بشرية.
5. للاطلاع على صحة آخر نافذة:

```powershell
.\.venv\Scripts\python.exe -m mt5_agents.collector_health --archive-root runtime\tick_archive --symbol XAUUSD --max-age-seconds 180
```

تحقق حالة `HEALTHY_LATEST_WINDOW_ONLY` من آخر نافذة فقط. **لا** تعني اكتمال خمس جلسات. `NO_TICKS_UNVERIFIED` تنبيه لا يمكن استبداله بأسعار وهمية.

### حدود التشغيل المستمر

- ملف BAT لا يستطيع معالجة إغلاق **الجهاز** أو إيقاف **PowerShell** نفسه أو انقطاع الكهرباء؛ يمكن تشغيله من Windows Task Scheduler عند بدء الجهاز، مع سياسة `Restart on failure` مناسبة، وإجراء الفحص نفسه عند كل إعادة تشغيل.
- إذا انقطع الاتصال طويلًا وتجاوز `--max-backlog-seconds`، يتوقف المجمع. تجنب السحب التاريخي العشوائي واصنع خطة Backfill منفصلة بعد مراجعة أسباب الفجوة.
- `*.lock` يتيم، `*.tmp`، ملفات Parquet دون manifest، أو تلف SQLite = **لا إعادة تشغيل آلية** حتى فحص الحالة يدويًا. لا تحذف ملفات سليمة أو تعدل التاريخ بلا تدقيق.
- حفظ CSV/تدريب LightGBM/إرسال الصفقات ليس جزءًا من مشرف جمع الـTicks.

## 5. قيود الاختبار التي لم تُحل بعد

- هذه البيئة لا توفر `pyarrow` ولا عميل MetaTrader5 الخاص بـWindows. محاولة تثبيت pyarrow هنا فشلت بسبب عدم توفر اتصال إلى مستودع الحزم. لذلك اختبار Parquet الحقيقي **لم ينجح هنا**، ولا توجد بيانات 5/10 جلسات حقيقية، ولا استنتاج عن جودة وسيط حقيقي.
- اختبارات الأوامر والوسيط هنا تعتمد على stubs/mocks. يجب التحقق من قيم `DEAL_REASON_SL` وحقول `deal.sl` وسلوك المراكز على خادم Demo الحقيقي؛ قد يغيّر الوسيط صيغة السجل أو يقيد التاريخ.
- استقرار الموارد وإعادة التشغيل على Windows PowerShell 5.1 لم يتأكد هنا. اختبر ملف BAT وTask Scheduler والانقطاع المتعمّد قبل اعتماد التشغيل الطويل.

**حالة الإصدار: Phase 3.2 — مراقبة فقط / NOT RELEASED FOR ML OR LIVE TRADING.**
