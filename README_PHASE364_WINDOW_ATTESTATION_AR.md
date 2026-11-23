# Phase 3.6.4 — Window Attestation & Heartbeat Correlation (Read-only)

هذه إضافة مستقلة للمشروع الحالي `mt5_ai_agents`، وليست مشروعًا كاملًا بديلًا.
لا تغيّر `phase363_contract_v2` أو Polars أو Shadow Capture أو Watchdog، ولا تنفّذ تداولًا أو ترسل إلى Discord.

## المحتويات

- `scripts/phase364_window_attestation.py` — مُدقّق مصدر البيانات وربط Heartbeat.
- `tests/test_phase364_window_attestation.py` — اختبارات `unittest` تعمل دون MT5 أو بيانات سوق حقيقية.

## متطلبات التشغيل

يجب أن تكون هذه الملفات موجودة مسبقًا في مشروعك:

- `scripts/phase363_polars_engine.py`
- `phase363_contract_v2/strict_guard.py`
- `runtime/shadow_tick_archive/parquet/.../*.manifest.json` وملفات Parquet المكتملة.
- `runtime/quant_context_derived/phase363-v2/XAUUSD/*.json` و/أو `EURUSD/*.json`.
- `runtime/telemetry/heartbeat.jsonl` أو ملفات `heartbeat-YYYY-MM-DD.jsonl` الدوّارة لإثبات الرصد؛ غيابها لا يثبت فشل الالتقاط، لكنه يجعل النتيجة **جزئية**.

## التركيب الآمن

1. اترك نافذتي Shadow Collector وWatchdog تعملان.
2. فك ZIP داخل `C:\Users\user\Desktop\mt5_ai_agents` مع الحفاظ على بنية المجلدات.
3. الملفات الجديدة فقط هي سكريبت مستقل واختبار ودليل؛ لا تستبدل الملفات القديمة.
4. افتح CMD **جديدة**:

```bat
cd /d C:\Users\user\Desktop\mt5_ai_agents
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_phase364_window_attestation.py -v
```

بعد نجاح الاختبارات شغّل الفحص الكامل للنافذتين المحفوظتين:

```bat
.\.venv\Scripts\python.exe scripts\phase364_window_attestation.py --mode deep --symbols XAUUSD EURUSD
```

هذا الأمر **لا يكتب أي ملف**: يقرأ JSON المشتق والمصدر، يطابق SHA256 والـManifest والهوية والدفعة والنافذة، ثم يعيد حساب Polars ويقارن السياق حرفيًا. ويفحص سجلات Heartbeat حول وقت **انتهاء استجابة طلب MT5** المسجّل في Manifest.

بعد تحقق الحسابات، تستطيع إنشاء شهادات Deep محلية اختيارية لإعادة استخدامها في فحص أسرع مستقبلاً:

```bat
.\.venv\Scripts\python.exe scripts\phase364_window_attestation.py --mode deep --symbols XAUUSD EURUSD --write-deep-certificate
```

الشهادات ملفات جديدة *فقط* في:

```
runtime\window_attestations\phase364-v1\<SYMBOL>\raw-<WINDOW_ID>.deep.json
```

لا يتم استبدال شهادة موجودة إذا تغيّرت بياناتها. ولا تُعدَّل ملفات Shadow / Manifest / derived / Heartbeat.

ثم فحص سريع يستفيد من شهادة Deep السابقة، دون إعادة حساب Polars:

```bat
.\.venv\Scripts\python.exe scripts\phase364_window_attestation.py --mode fast --symbols XAUUSD EURUSD
```

إذا أردت مراجعة آخر **10** نوافذ JSON مشتقة *محفوظة بالفعل* لكل رمز:

```bat
.\.venv\Scripts\python.exe scripts\phase364_window_attestation.py --mode deep --recent 10 --symbols XAUUSD EURUSD
```

> `--recent` لا ينشئ نوافذ مشتقة جديدة. إذا كان لديك ملف JSON محفوظ واحد للرمز، فلن يظهر غيره؛ لا يعني ذلك أن Shadow لم يجمع نوافذ أخرى.

## طبقات الأدلة الثلاث

1. **Provenance:** صحة SHA256 لـParquet، صحة Manifest، `identity_tag`، `capture_batch_id` UUID v4، تطابق `window_id` مع الخوارزمية المستخدمة في محرك Polars، ووجود وقت استجابة MT5 المضيف.
2. **Metrics:** في `--mode deep` نعيد حساب `calculate_metrics` كاملًا ونقارن القاموس المشتق حرفيًا. في `--mode fast` يلزم وجود شهادة Deep محلية مطابقة لبصمات المصدر وملف JSON وأكواد Polars وStrict Guard. **بصمة Parquet وحدها لا تثبت صحة الحسابات**. الشهادة المحلية ليست موقّعة رقمياً ولا تقاوم من يملك صلاحية تعديل كل ملفات المشروع.
3. **Heartbeat:** نقرأ النبضات قبل وبعد `manifest.host_response_end_utc_ms` (نافذة 30 ثانية لكل جهة)، نتحقق من تتابع UTC و`monotonic_ns` وتواتر النبضات، وحالة عملية الملتقط وصحة مسح العمليات. نبحث أيضًا عن ظهور `capture_batch_id` في النبضات التالية ضمن 180 ثانية. لأن مسح Manifest في Watchdog دوري، **غياب الدفعة في النبضات لا يساوي فقدان أرشيف**.

لا نربط `raw_time_msc` مع Heartbeat مباشرة: **وقت التيكات الخام غير معتمد UTC**. `host_response_end_utc_ms` يقيس نهاية الاستجابة للطلب قبل نشر ملفات Parquet، وليس لحظة الالتزام النهائي على القرص.

## دلالات النتائج

- `RESEARCH_ATTESTATION_EVIDENCE_COMPLETE`: ثبت تطابق المصدر والحساب (عبر Deep أو شهادة سابقة صالحة) **ورُصدت عملية الجمع والـWatchdog حول وقت انتهاء الاستجابة**. لا يعني اكتمال التكات ولا اتصال MT5 ولا اعتماد UTC.
- `RESEARCH_ATTESTATION_PARTIAL`: دليل واحد أو أكثر غير مكتمل، مثل غياب نبضات تحيط بالوقت أو غياب شهادة Deep في Fast. هذا **ليس إثباتًا** أن الملتقط متوقف.
- `WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE`: شهد Watchdog وجود عملية الجمع ضمن الفترة المرصودة، ولا يثبت اتصال الوسيط.
- `UNVERIFIED_MISSING_BRACKETING_HEARTBEATS`: لا توجد نبضة قبل وبعد وقت الاستجابة ضمن حدود الفحص؛ نمتنع عن الجزم بوجود فجوة فعلية.
- `WATCHDOG_CADENCE_GAP_DETECTED_CAUSE_UNCLASSIFIED`: تأخر ملحوظ بين نبضتين؛ سببه غير مصنّف (ليس إثباتًا لسكون Windows).
- `COLLECTOR_OR_WATCHDOG_HEALTH_UNVERIFIED_OR_ALERT`: يوجد تنبيه أو فحص عمليات غير مؤكد؛ لا يُرقّى إلى حالة سليمة.
- `CLOCK_OR_MONOTONIC_SEQUENCE_UNCERTAIN`: عدم اتساق الساعة الجدارية مع الساعة الرتيبة، أو تغير ترتيب النبضات.
- `NOT_RECOMPUTED_NO_PREVIOUS_DEEP_CERTIFICATE`: وضع Fast بدون شهادة سابقة؛ لا يجوز وصف المقاييس بأنها مُعاد التحقق منها.

رموز الخروج: `0` للأدلة المكتملة بالمفهوم البحثي المحدود أعلاه، و`3` لدليل جزئي، و`2` لتعارض/فساد/خطأ فحص. حتى عند الرمز `0` يبقى `ready_for_model_publication=false` و`timebase_certified=false` و`no_orders=true`.

## قيود الإصدار

- لن يعيد كتابة `quality.heartbeat_status` داخل JSON القديم؛ بل يُصدر تقييم Heartbeat مستقلًا ليظل السجل الأصلي ثابتًا.
- لا يفحص اتصال MT5، أو اكتمال تغطية جلسات آسيا/لندن/نيويورك، أو سجل أحداث سكون Windows، أو تأثير Qwen على التقاط الأسعار.
- مسح العمليات في الـWatchdog كل نبضة يعمل كما هو؛ هذه الإضافة لا تعدّل Watchdog أو تعالج ملاحظات Poke المتعلقة بمسح العمليات أو إعادة محاولة Manifest؛ تلك التحسينات مستقلة.
- `source_logs` ملفات محلية غير موقعة؛ هذا تقرير سلامة أدلة تشغيلية، وليس نظام إثبات جنائي مانع للتلاعب.
- أجريت الاختبارات محليًا على **بيانات اصطناعية ومُحاكاة للمحرك** (لا توجد بيانات Shadow الحية للمستخدم في هذه الحزمة). اختبارات الحساب الحقيقي والنبضات الحقيقية يجب أن تُشغّل على Windows بأوامر الفحص أعلاه.

**السياسة:** `MODE=DRY_RUN`، حساب DEMO، ولا أوامر تداول. لا تشغّل Qwen/Discord الجديد ولا توقف Shadow/Watchdog لهذه المرحلة.
