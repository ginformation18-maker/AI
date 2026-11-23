# Phase 3.6.3 — Polars Quant Engine (Research Only)

هذه إضافة مستقلة تقرأ **أحدث دقيقة مكتملة وموقعة بـ SHA256** في `runtime/shadow_tick_archive`، وتحسب مقاييس الأسعار، ثم تتحقق من أن JSON الناتج يطابق `phase363_contract_v2/strict_guard.py` قبل حفظ **نسخة مشتقة فقط** في `runtime/quant_context_derived/phase363-v2/`.

## قبل التركيب

- هذه **إضافة** وليست المشروع الكامل. يلزم تثبيت `phase363_contract_v2` الذي سبق اختباره.
- لا توقف `phase36_shadow_capture.py --loop` أو مراقب Watchdog ولا تشغّل نسخة ثانية منهما.
- لا تنسخ ملفات `.env` ولا كلمات المرور ولا تفتح أي صفقات.
- ثبّت Polars في `.venv` عند الحاجة: `\.venv\Scripts\python.exe -m pip install polars`.
- متطلبات الفحص: Windows + Python 3.12 + `polars`، بالإضافة إلى عقد v2 الموجود مسبقًا.

## التركيب

فك الضغط فوق **مجلد المشروع**، وستُضاف الملفات:

- `scripts/phase363_polars_engine.py`
- `tests/test_phase363_polars_engine.py`
- `README_PHASE363_POLARS_AR.md`

ثم من CMD داخل المشروع:

```bat
.\.venv\Scripts\python.exe -m pip show polars
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_phase363_polars_engine.py -v
.\.venv\Scripts\python.exe scripts\phase363_polars_engine.py --symbol XAUUSD --stdout-only
.\.venv\Scripts\python.exe scripts\phase363_polars_engine.py --symbol EURUSD
```

الأمر مع `--stdout-only` لا يكتب ملفات نهائيًا. الأمر الأخير يكتب **JSON مشتقًا** ولا يلمس أرشيف Shadow. عند تكرار التشغيل لن يُستبدل الملف الموجود، وإذا اختلف المحتوى سيظهر خطأ تعارض.

## التعريف الرياضي وحدوده

- **5s buckets:** استخدام قسمة صحيحة على 5000 لطابع الوسيط الخام `raw_time_msc`؛ **هذه ليست شهادة UTC**.
- **5s log-returns:** آخر متوسط `bid/ask` مرصود في كل فترة، مع قبول الانتقال فقط بين فترتين متجاورتين كل 5000ms، **بلا forward-fill**.
- **realized_volatility:** بموجب العقد الحالي، هي **الانحراف المعياري العيني** (`ddof=1`) لهذه العوائد؛ **ليست** الجذر التربيعي لمجموع مربعات العوائد ولا قيمة سنوية.
- **الحد الأدنى:** ثلاث عوائد متجاورة فعلية. إذا قلت عن ذلك: `NOT_COMPUTED`, `realized_volatility=null`, `rv_sample_count=0` (كما يفرض العقد).
- **tick_velocity_per_sec:** عدد التكات / مدة النافذة الكاملة (60 ثانية)، لا حجم صفقات التداول.
- **directional_tick_ratio:** نسبة ارتفاعات `mid` مقابل انخفاضاته بعد تجاهل التغيرات الصفرية؛ **لا تمثل تدفق الأوامر المؤسسية**.
- **spread_constant_observed_bool:** لا يعني `spread_clamped_bool`؛ الأخير يبقى `null`.
- **rolling_z_score_vol = null** و`rolling_baseline_window_count = 0`، لأن UTC/DST والجلسات الخمس لم تُعتمد بعد.
- **health:** `DEGRADED` إلزاميًا للتوقيت المؤقت؛ لا تستخدم JSON الناتج لاتخاذ قرارات تداول.

## ضوابط

- يقرأ ملفات **Manifest المنشورة المكتملة فقط** ويعيد حساب SHA256 قبل التحليل؛ يرفض أكثر من حساب في نفس Shadow ما لم تمرر `--identity-tag`.
- يرفض الصفوف المخالفة للنافذة أو الأسعار غير الصالحة، ويحترم ترتيب التكات الأصلي.
- لا يقرأ MT5، ولا يشغّل Qwen/Discord، ولا يكتب Parquet/Manifest، ولا يغير `.env`.
- لم تثبت بعد تكلفة الحساب تحت 2–5 مللي ثانية؛ يلزم قياسها على جهازك وفي حمل السوق الحقيقي. وحتى Polars قد يستهلك CPU، لذلك يحد عدد الخيوط افتراضيًا بواحد.
- لا يعالج فجوات ساعات الإغلاق أو يثبت التوقيت الصيفي ولا يصنع baseline موثوقًا من دقيقة واحدة.

## الاختبارات

تختبر سلامة Manifest، SHA256، تعدد حسابات الوسيط، عدم الكتابة فوق JSON مشتق، واختبارات تكامل Polars على بيانات اصطناعية (إن كانت المكتبة مثبتة).
