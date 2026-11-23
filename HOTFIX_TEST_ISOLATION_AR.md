# Phase 3.5.2 — إصلاح عزل اختبار LIVE

## سبب المشكلة
كان الاختبار `tests/test_settings.py::test_real_mode_requires_two_independent_unlocks` يفترض الوصول إلى شرط التفويض النصي لوضع LIVE، لكنه ورث من ملف `.env` إعداد `MT5_OBSERVER_TIMEBASE_MODE=PROVISIONAL_BROKER_OFFSET`؛ في هذه الحالة يوقف `Settings.load()` التشغيل مبكرًا برسالة حماية صحيحة: `Provisional broker-clock mapping forbids DEMO/LIVE execution`.

## ما تغير
أضيف سطر `monkeypatch.setenv("MT5_OBSERVER_TIMEBASE_MODE", "STRICT_UTC")` **داخل الاختبار فقط** قبل فحص إذن LIVE. يختبر ذلك إذن LIVE بصورة مستقلة ولا يغير ملف `.env` أو سلوك التنفيذ أو الحماية.

## التطبيق
1. أوقف نسخ البوت إن كانت تعمل ثم فك ضغط ZIP في جذر المشروع، مع استبدال `tests/test_settings.py` فقط.
2. من CMD في جذر المشروع نفّذ:
   ` .\.venv\Scripts\python.exe -m pytest tests -q `
3. ثم نفّذ `scripts\check_discord_hub.bat`، وبعد نجاح جميع الفحوص فقط `scripts\run_discord_hub.bat`.

## حدود التعديل
- لا تغيّر `MT5_OBSERVER_TIMEBASE_MODE=PROVISIONAL_BROKER_OFFSET` في `.env`.
- يظل `MODE=DRY_RUN` إلزاميًا في الوضع المبدئي. يمنع تشغيل DEMO/LIVE وكتابة أرشيف Ticks في هذا الوضع.
- التعديل لا يضمن حل مشكلة زمن الوسيط نهائيًا، ولا يسمح بتنفيذ تداول.
