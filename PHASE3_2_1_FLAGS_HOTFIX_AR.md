# Phase 3.2.1 — إصلاح تصادم pandas DataFrame.flags مع عمود Ticks باسم flags

## سبب المشكلة

في `mt5_agents/parquet_integrity.py`، كانت `canonical_frame()` تقرأ عمود `flags` باستخدام `result.flags`. هذا التعبير في Pandas يُرجع خاصية `DataFrame.flags` الخاصة بالبيانات الوصفية، وليس العمود المطلوب. لذلك فشل `parquet_preflight` برسالة `AttributeError: 'Flags' object has no attribute 'dtype'`، قبل وصوله لاختبار الحفظ والاسترجاع.

## التعديل

تم استبدال مواضع `result.flags` الثلاثة بالتعبير الصريح `result["flags"]`، وإضافة ثلاثة اختبارات انحدار لحالات النوع الصحيح والسالب والنوع العشري.

## تعليمات التصحيح على جهاز Windows

انسخ محتويات حزمة Hotfix إلى **داخل مجلد** `C:\Users\user\Desktop\mt5_ai_agents` مع استبدال `mt5_agents\parquet_integrity.py`، دون حذف `runtime` أو `.env` أو `.venv`.

ثم من PowerShell في مجلد المشروع:

```powershell
.\.venv\Scripts\python.exe -m mt5_agents.parquet_preflight --archive-root .\runtime\tick_archive
.\.venv\Scripts\python.exe -m pytest tests -q
```

النجاح المتوقع من الخطوة الأولى يتضمن `"status": "PASS"` و `"real_arrow_roundtrip": true`، ثم مرور جميع اختبارات المشروع. لا تشغّل Tick Collector إذا فشل أي منهما. سجّل المخرجات كاملة لحل أي مشكلة إضافية.

## حالة التحقق

نجح محلياً 87 اختباراً من غير اختبارات `pyarrow` (بما فيها اختبارات التصحيح الجديدة). اختبارات Parquet الخمسة ودورة الحفظ/القراءة الحقيقية **لم تنفذ في بيئة التطوير** بسبب عدم توفر `pyarrow` فيها؛ يتعين تشغيلها في بيئة Windows المثبتة لديك. لا يوجد تحقق اتصال MT5 أو بيانات الوسيط.
