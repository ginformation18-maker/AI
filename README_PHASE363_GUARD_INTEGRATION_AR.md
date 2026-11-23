# Phase 3.6.3 — Read-only Quant Context + Strict Guard Integration

هذه إضافة اختبار **منفصلة** عن Shadow Capture وWatchdog وDiscord وOllama. لا تفتح أي صفقات، ولا تتصل بـMetaTrader5، ولا تعدّل ملفات Parquet أو Manifest أو JSON المشتق.

## ما الذي تختبره؟

1. تحميل أحدث JSON محفوظ من `runtime/quant_context_derived/phase363-v2/<SYMBOL>/raw-*.json` بنمط `--source derived`؛ أو حساب أحدث نافذة Shadow في الذاكرة ومقارنتها بالـJSON المحفوظ إن وجد بنمط `--source auto`.
2. فحص العقد `quant_context_v2` عبر `strict_guard.validate_context`.
3. **إعادة حساب نافذة Parquet بالكامل في وضع اختبار التكامل** من Manifest مكتمل وبصمة SHA256 والتحقق من تطابق JSON المشتق مع نتيجة Polars حرفيًا، بما يشمل معرّف النافذة والقيم.
4. بناء ردود **اختبارية حتمية** للأدوار الأربعة، وتمريرها عبر `strict_guard.load_reply_json` ثم `strict_guard.render_ar` وإظهار الردود العربية من البيانات المعتمدة في العقد.
5. محاولات رفض مقصودة: ادعاء `CLAMPED` مع حقل `null`، استخراج `BUY` من نسبة التكات الصاعدة، الزعم بأن UTC معتمد، تلاعب بالنافذة، نقص دليل، حقول تداول، إدخال مفاتيح JSON مكررة.

**تحذير:** هذا إثبات صلاحية المسار على ملفاتك المحلية عند تشغيله. لا يختبر Qwen الفعلي، ولا يثبت دقة التفسير الاقتصادي، ولا يعتمد UTC/DST، ولا يجيز التداول. إن كانت الحالة `HALTED` تُتخطى الردود.

## خطوات التثبيت على Windows

فك ZIP في جذر المشروع الحالي مع دمج مجلدي `scripts` و`tests` دون استبدال أي ملفات قائمة.

```bat
cd /d C:\Users\user\Desktop\mt5_ai_agents
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_phase363_context_guard_integration.py -v
```

**اختبار أحدث نوافذ Shadow مباشرة دون حفظ JSON جديد:**

```bat
.\.venv\Scripts\python.exe scripts\phase363_context_guard_integration.py --source auto --symbols XAUUSD EURUSD
```

**اختبار ملفات JSON المشتقة المحفوظة (اختبار الملفات المطلوبة):** أولًا إذا لم تكن أحدث ملفات الذهب واليورو محفوظة، أنشئ JSON مشتقًا لكل رمز. هذه الأوامر تكتب JSON مشتقًا جديدًا فقط (لا تعدّل Shadow):

```bat
.\.venv\Scripts\python.exe scripts\phase363_polars_engine.py --symbol XAUUSD
.\.venv\Scripts\python.exe scripts\phase363_polars_engine.py --symbol EURUSD
```

ثم:

```bat
.\.venv\Scripts\python.exe scripts\phase363_context_guard_integration.py --source derived --symbols XAUUSD EURUSD
```

يجب أن ترى `OFFLINE_GUARD_INTEGRATION_PASS`، و`GUARD_INTEGRATION_PASS_SIMULATED_REPLIES` لكل رمز، وجميع الحقول في `negative_probes` تساوي `REJECTED_AS_EXPECTED`.

إذا فشل مطابقة Parquet أو بصمة أو نافذة أو Manifest، يرفض السكريبت (`PHASE363_GUARD_INTEGRATION_FAIL_CLOSED`، كود خروج 2)، ولا يتجاوز الحماية.

## فصل زمن الاختبار عن زمن الإنتاج

**حاليًا: full integration verification فقط.** إعادة حساب النافذة كاملة مقصودة هنا لاختبار حتمية القيم. لم تُوصَل الأداة إلى مسار تنفيذ حي أو Discord.

في المستقبل، يمكن جعل حارس النشر السريع يتحقق من SHA256 لملف الخام وManifest ومعرّف النافذة، مع التخزين المؤقت لنتائج التحقق؛ لكن ذلك يحتاج فصل واجهة production عن أداة التدقيق. **عقد v2 لا يحتوي `capture_batch_id` في JSON المشتق**؛ لذلك لا يجوز الادعاء بأن الحارس الحالي يقارن batch_id بين JSON والـManifest. إعادة حساب Polars في وضع الاختبار تتحقق من `capture_batch_id` داخل صفوف Parquet بمقارنته بالـManifest. في عقد لاحق يمكن إضافة معرّف الدفعة بشكل صريح أو توقيع سياق مشتق موثّق.

## السلامة أثناء الجمع

- اترك `phase36_shadow_capture.py --loop` و`phase36_shadow_watchdog.py --loop` يعملان.
- افتح CMD منفصلًا للاختبار؛ يستخدم الحساب Polars ويستهلك بعض CPU/I/O، لذلك تجنّب تكراره بكثافة خلال ذروة النشاط، وراقب تأخير Shadow.
- لا تبدأ بوت Discord ولا Ollama لهذا الاختبار.
- لا تغيّر `MODE=DRY_RUN`، ولا تعلن تأكيد UTC/DST بسبب نجاح JSON.

## نتائج البيئة المحلية المرفقة

اجتازت 11 اختبارات `unittest` محلية، ونجحت حالة قبول الردود الاختبارية الأربع لكل رمز عند استخدام *قيم المعايير المنسوخة من سجل المستخدم*، مع رفض الحالات الأربع الضارة. لا توجد هنا نسخة من ملفات Parquet الحقيقية لجهاز المستخدم، ولا Polars/pyarrow في بيئة البناء؛ لذلك **اختبار ملف Windows الحقيقي يتم بالأوامر السابقة على جهاز المستخدم فقط**.
