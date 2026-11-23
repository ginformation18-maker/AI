# Phase 3.6.5 — Session Evidence & Capture Continuity

**إضافة قراءة فقط للتوثيق البحثي. ليست بوت تداول ولا تُغيّر Shadow أو Watchdog أو Discord أو Ollama.**

## التركيب

فك ضغط ZIP **داخل جذر المشروع** `C:\Users\user\Desktop\mt5_ai_agents` بحيث يظهر:

- `scripts\phase365_session_continuity.py`
- `tests\test_phase365_session_continuity.py`

تحتاج الإضافة إلى الملفات الموجودة من المراحل السابقة:

- `mt5_agents\phase36_session_coverage.py`
- `scripts\phase364_window_attestation.py`
- في وضع `--deep`: `pyarrow` و`numpy` المثبتان مسبقًا.

لا تستبدل ملفات `runtime` ولا `.env` ولا أي ملف من الملفات الأصلية. افتح CMD منفصلًا وأبقِ نوافذ Shadow وWatchdog تعملان.

## الأوامر

```bat
cd /d C:\Users\user\Desktop\mt5_ai_agents
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_phase365_session_continuity.py -v
.\.venv\Scripts\python.exe scripts\phase365_session_continuity.py --deep
```

ولعرض كل النوافذ بالتفصيل:

```bat
.\.venv\Scripts\python.exe scripts\phase365_session_continuity.py --deep --include-windows
```

ليس من الضروري تكرار الفحص باستمرار؛ يكفي عند مراجعة التغطية. السكريبت لا يكتب ملفات إطلاقًا، بل يعرض JSON في CMD.

## المعاني الدقيقة للنتائج

- `coverage_audit`: فحص SHA256 وسلامة Manifest وParquet بواسطة أداة التغطية السابقة. `--deep` يضيف التحقق من عدد الصفوف والطوابع الخام.
- `WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE`: نبضتان متتاليتان سليمتان تحيطان بوقت **استجابة استعلام MT5 لدى جهاز Windows** المسجل في Manifest. هذا **لا يثبت** اتصال MT5 أو عدم فقدان تكات أو صحة توقيت الوسيط أو إكمال الحفظ قبل النبضة.
- `UNVERIFIED_MISSING_BRACKET`: سجلات النبض لا تكفي لتوثيق النافذة؛ **لا يعني** بالضرورة فقدان تكات.
- `COLLECTOR_OR_WATCHDOG_UNVERIFIED_OR_ALERT`: حالة عمليات غير سليمة أو تنبيه في إحدى النبضات.
- `WATCHDOG_CADENCE_GAP_CAUSE_UNKNOWN`: فاصل نبض تجاوز 25 ثانية أو تأخر معلن؛ السبب غير معروف (ولا يُثبت سكون Windows).
- `CLOCK_OR_SEQUENCE_UNCERTAIN`: تغير مريب في تسلسل الوقت الجداري أو الرتيب.
- `capture_batch_id_seen_in_watchdog`: هل صادف أن ظهر معرّف الدفعة في حقل آخر دفعة حفظها المراقب بعد استجابة الاستعلام بـ180 ثانية؟ عدم ظهوره لا يعني فقدان البيانات (الـWatchdog لا يسرد كل الدفعات).
- `PROVISIONAL_SOURCE_AND_PROCESS_EVIDENCE`: سلامة الأرشيف المرصود + رصد عمليات الملتقط حول استجابات الاستعلام؛ **ليس** اعتماد UTC/DST أو الجلسات أو بيانات التداول.
- `PROVISIONAL_SOURCE_EVIDENCE_PARTIAL_MONITORING`: سلامة المصدر مع دليل مراقبة غير مكتمل لبعض النوافذ؛ يعيد كود خروج 3 لتمييزه عن فشل فحص المصدر.
- `PHASE365_FAIL_CLOSED`: تناقض في البيانات أو سجل غير صالح؛ كود خروج 2. عاود التشغيل دون التعديل على السجلات الخام.

## الحدود

1. أوقات `raw_time_msc` للوسيط **ليست UTC معتمدة**. المقارنة مع Heartbeat مبنية على `manifest.host_response_end_utc_ms` من ساعة المضيف فقط.
2. تصنيف لندن/طوكيو/نيويورك يظل **مرشحًا** وفق offset غير معتمد؛ لا يوجد إثبات لخمس جلسات سائلة.
3. النبضات تثبت **مشاهدة العمليات عند نقاط زمنية متقطعة**، لا حضورها المستمر بالمعنى الرياضي.
4. أي نقص نافذة لا يكفي لتحديد هل السوق أغلق، أو الشبكة انقطعت، أو الجهاز نام، أو حدث خلل في جمع الأسعار.
5. التحقق من سلامة كل Parquet يتم في لحظة الفحص؛ ليس توقيعًا رقميًا موثوقًا من طرف ثالث. للشهادات التفصيلية استمر باستخدام Phase 3.6.4.
6. عدد ملفات Parquet الصغيرة تنبيه أداء مستقبلي فقط؛ لا تضغط ولا تحذف الأرشيف الأصلي.
7. لا ينشئ قرارات تداول، ولا يعدل ملفات `runtime`، ولا يتصل بـMT5، ولا يتطلب تشغيل Qwen.

## كيف نستخدم النتيجة؟

بعد تشغيل الأمر الرئيسي أرسل قيم `status` و`coverage_audit` و`heartbeat` و`groups`، وأي `exception_or_window_details`. عندها يمكننا إصدار تقرير تغطية مراقبة يميز بين "رُصد" و"لم يُوثَّق" دون اختلاق أسباب أو اعتماد زمني غير موجود.
