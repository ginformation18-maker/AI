# Phase 3.6 — Out-of-band Shadow Watchdog (مراقبة مستقلة)

هذه الإضافة تقرأ بيانات عمليات Windows وملفات `*.manifest.json` ولا تتصل بمنصة MT5، ولا تستورد `MetaTrader5`، ولا تنشئ أو تعدل أي ملف Parquet أو Manifest، ولا ترسل أوامر تداول. تعمل كعملية Python **منفصلة تمامًا** عن ملتقط Shadow الموجود، وبالتالي **لا يلزم إيقاف أو إعادة تشغيل الملتقط الحالي**.

## التثبيت

1. فك ضغط الإضافة في `C:\Users\user\Desktop\mt5_ai_agents` مع دمج مجلد `scripts` و `tests` فقط.
2. اترك نافذة `phase36_shadow_capture.py --loop` الموجودة تعمل. لا تشغّل جامعًا ثانيًا.
3. افتح نافذة CMD **ثانية** في جذر المشروع:

```bat
cd /d C:\Users\user\Desktop\mt5_ai_agents
.\.venv\Scripts\python.exe -m pytest tests\test_phase36_shadow_watchdog.py -q
.\.venv\Scripts\python.exe scripts\phase36_shadow_watchdog.py --once
```

تحقق أن `collector_alive_bool` = `true` و`collector_root_count` = `1`. قد يظهر PID الأب والابن معًا، وهذا **ليس** بالضرورة تشغيلين مستقلين.

بعد نجاح الفحص شغّل في نفس نافذة CMD الثانية:

```bat
.\.venv\Scripts\python.exe scripts\phase36_shadow_watchdog.py --loop
```

اترك نافذة Watchdog الثانية مفتوحة. من نافذة CMD ثالثة تقدر تعرض آخر نبضة:

```bat
.\.venv\Scripts\python.exe scripts\phase36_shadow_watchdog.py --status
```

إيقاف Watchdog مستقل بالضغط Ctrl+C في نافذته، ولا يوقف Shadow. **لا تضف Watchdog داخل حلقة جمع التكات ولا تشغّل ملتقط Shadow مرة ثانية.**

## بنية النبض

الملف النشط `runtime\telemetry\heartbeat.jsonl`، ويُرحَّل يوميًا بتوقيت UTC إلى `heartbeat-YYYY-MM-DD.jsonl`، مع منع استبدال نسخة يوم موجودة.

* `utc_iso`: توقيت نظام Windows (للعرض).
* `monotonic_ns`: مرجع الفترات الزمنية بين النبضات داخل العملية نفسها؛ ليس توقيت تداول.
* `watchdog_interval_ms`: الزمن المقاس بين نبضتي Watchdog باستخدام monotonic، والتنبيه عند تجاوز 1.5× الفترة المستهدفة.
* `collector_alive_bool`: هل ظهرت عملية Shadow --loop لهذه النسخة من المشروع؛ وجود العملية لا يثبت تقدّم حلقة الجمع.
* `collector_pids`, `collector_root_count`: يسمحان بمعرفة أن Python الطفل والأب تشغيل واحد.
* `mt5_connected_bool`: **null عمدًا**؛ لأن التأكد من اتصال MT5 يتطلب فحصًا مستقلاً وقد يسبب التداخل في مسار الجمع. لا يُستنتج الاتصال من وجود عملية Python أو terminal64.exe.
* `last_saved_batch_id`: من أحدث Manifest فقط (بدون فتح Parquet).
* `lag_ms`: عمر أحدث Manifest منذ `host_response_end_utc_ms` حتى الآن؛ ليس تأخر الشبكة أو زمن تنفيذ التكة، وقد يزيد طبيعيًا عند إغلاق السوق.
* `alerts`: انقطاع مشاهدة العملية، تأخر النبضة، غياب دفعة جديدة، أو قفزة ساعة مشتبه بها — **لا يوجد منها إثبات قاطع على سكون Windows**.

يُفحص Manifest مرة كل 60 ثانية افتراضيًا، والعمليات كل 10 ثوانٍ. لا يستخدم ملف `.env` ولا يطبع أسرارًا أو يقرأ سجلات الصفقات. إذا ظهر `PSUTIL_NOT_INSTALLED` فتأكد من تثبيت متطلبات المشروع داخل `.venv`.

## ضوابط وقيود

- لا يغيّر `UNVERIFIED_PENDING_DST`، ولا يعتمد توقيت UTC/DST أو الجلسات الخمس.
- لا يوجد اتصال بـDiscord أو Ollama أو MT5 في هذا الإصدار.
- غياب التكات أو عدم حفظ دفعة خلال إغلاق السوق **ليس دليلاً على توقف الجهاز**.
- Watchdog وملتقط Shadow موجودان على **نفس الكمبيوتر**؛ عند انقطاع الكهرباء أو السكون لن يكون Watchdog قادرًا على إرسال نبضات أثناء الانقطاع. يسجل تأخرًا عند عودته فقط. للتأكد من السبب استخدم أحداث Windows/Power-Troubleshooter منفصلة.
- لا تشغّل Watchdog مرتين (`.shadow_watchdog.lock` يحمي ذلك). إذا ظهرت مشكلة قفل، افحص العملية أولًا ولا تحذف ملف القفل أثناء التشغيل.
- التقرير التالي لتغطية الجلسات يمكنه لاحقًا **قراءة** سجلات `runtime/telemetry/heartbeat*.jsonl` بجانب ملفات Shadow دون تعديل الأصل.
