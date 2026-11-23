# المرحلة الثانية — استعادة الأوامر غير المؤكدة وبيانات Tick الحقيقية (الذهب والفوركس)

**الحالة: كود جاهز للاختبارات البرمجية المحلية فقط.** لا يوجد اتصال تم اختباره فعليًا مع وسيط أو Terminal، ولا توجد أوامر حقيقية/تجريبية منفذة خلال الاختبارات. الحساب التجريبي هو نقطة البداية الإلزامية. هذا ليس نظامًا ذاتي التعلم بعد، ولا خدمة تعمل 24 ساعة دون استضافة ومراقبة.

## 1. معالجة حالة In-Flight / Uncertain Order

الملفات:

- `mt5_agents/broker_reconciliation.py`: معرّف تعليق `AI` + بصمة SHA-256 مختصرة ومستمرة لكل `(account, signal_id)`؛ فحص `positions_get`, `orders_get`, `history_orders_get`, `history_deals_get` باستخدام الحساب + الزوج + magic + comment.
- `mt5_agents/memory/event_logger.py`: دالة `unsettled_execution_intents` تطابق إذن التنفيذ المسجل مسبقًا مع رد الوسيط المحفوظ. أي نتيجة `None` أو Exception أو Partial أو Timeout أو PLACED أو رد غير مؤكد تبقى معلّقة.
- `mt5_agents/execution.py`: تسجيل Intent **قبل** `order_send`؛ تعليق فريد قصير داخل الطلب؛ تسجيل رد الوسيط مباشرة بعد الإرسال؛ عند عدم اليقين إنشاء `STOP` ومنع الإرسال/الإعادة.
- `mt5_agents/main.py`: تحقق من الأذونات غير المحسومة عند كل بدء تشغيل، ويمنع فتح الصفقات الجديدة إذا وجدت.
- `mt5_agents/reconcile.py`: أمر قراءة فقط لاستعراض السجلات بعد انقطاع الاتصال.

**الأمر التشخيصي (بدون تنفيذ أي صفقات):**

```powershell
python -m mt5_agents.reconcile --report
```

هذا الأمر يتطلب أن تكون MetaTrader 5 وبيانات حسابك التجريبي متاحة، وأن يكون ملف SQLite من جلسة التداول الأصلية موجودًا. كل نتيجة تسجل في `events` كحدث مراجعة. استعلامات MT5 تعطي معلومات متاحة في وقت الاستعلام؛ عدم ظهور صفقة **ليس** دليلاً قاطعًا على عدم التنفيذ. يمكن للوسيط تغيير أو قص نص تعليق الأمر، كما أن مراكز Netting قد تحمل تعليقات لا تعكس كل الصفقات السابقة. مطابقة `magic` وحده لا تميز إشارة بعينها، لذلك تؤدي لرفع حالة مراجعة يدوية.

**حالات التقرير:**

- `BROKER_ACTIVITY_FOUND_MANUAL_REVIEW`: وجدت أدلة تحمل نفس `magic + comment + symbol`، لكنها لا ترفع المنع تلقائيًا.
- `NO_MATCH_STILL_UNCERTAIN`: لا يوجد سجل مطابق في اللقطة الحالية؛ يُحظر إعادة الإرسال.
- `WEAK_MAGIC_ONLY_MANUAL_REVIEW`: تطابق جزئي (`magic` مع الرمز)، والتعليق غير مطابق؛ يلزم تدقيق يدوي.
- `MULTIPLE_MATCHES_MANUAL_REVIEW`: أكثر من مرجع أمر مستقل؛ احتمال التكرار أو عدة عمليات.
- `QUERY_FAILED`, `ACCOUNT_MISMATCH`, `HISTORY_WINDOW_UNSAFE`: تعذر إثبات الحالة؛ لا تداول.

**إجراء التعافي:** أوقف التداول الآلي من Terminal، وشغّل تقرير `--report`، وراجع يدويًا (بنفس حساب MT5) المراكز والأوامر المفتوحة وسجلات الأوامر والصفقات المنفذة، مع التذاكر والحجوم وأوامر الإغلاق. يجب حل اختلافات الحساب والبيانات وتسجيل تسوية بشرية موثقة قبل استعادة الخدمة. **لا يوجد فك منع تلقائي أو مسار إعادة إرسال للإشارة الأصلية.** إزالة ملف `STOP` وحدها لا تكفي؛ البوابة تفحص أيضًا حالة الأذونات في SQLite. المرحلة القادمة تحتاج سير عمل تسوية بشرية موثق وصلاحيات مستقلة، قبل الاستضافة الإنتاجية.

> ملاحظة عن الإصدار الأول: طلبات سابقة استخدمت تعليقًا عامًا `MT5AI-gated-v1`، ولذلك قد يكون الربط بين الصفقة وإشارتها القديمة غير حاسم. تعامل معها كمراجعة يدوية، ولا تفترض أن السجلات القديمة قابلة للمطابقة آليًا.

## 2. محول بيانات Tick/Spread

الملف: `mt5_agents/tick_data.py`

- يجلب تاريخ **Bid وAsk** عبر `copy_ticks_range(symbol, UTC_FROM, UTC_TO, COPY_TICKS_INFO)`.
- يتحقق من زمن UTC، وسعر `ask > bid > 0`، وترتيب ticks، وصلاحية `symbol_info.point`، وعدم وجود قيم تالفة أو متقاطعة.
- يحسب السبريد بوحدة **نقاط الوسيط**: `(ask - bid) / symbol.point`، دون افتراض أن 15 أو 80 نقطة قيمة عالمية ثابتة.
- يكوّن QuoteBar من عينات Bid/Ask الفعلية (Bid OHLC وAsk OHLC منفصلان)، مع `p50/p95/max` للسبريد لكل شمعة.
- يرفض شموعًا تفتقد قرب الحدّين الزمنيَّين أو بها فجوة كبيرة بين ticks، ولا يملأ الشموع المفقودة بأسعار وهمية، ويتجاهل الشموع غير المكتملة.
- يحتفظ `PendingLabelQueue` بالتسميات على `training_eligible=0`؛ فبيانات Bid/Ask التاريخية وحدها لا تكفي دون عمولات ورسوم تبييت وانزلاق وإعادة تمثيل قيود التنفيذ الفعلي.

**مثال تحليل فقط (لا يرسل أوامر):**

```python
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5
from mt5_agents.tick_data import TickDataAdapter, aggregate_quote_bars

# يتطلب اتصالاً سابقًا بحساب MT5 تجريبي مثبت الهوية.
symbol = "XAUUSD"  # عدل الرمز حسب الوسيط، قد يحمل لاحقة
info = mt5.symbol_info(symbol)
if info is None or info.point <= 0:
    raise RuntimeError("Unknown broker symbol / point")
now = datetime.now(timezone.utc)
# لتجنب الشمعة الجزئية اطلب فترة متاحة تغطي شموعاً مغلقة؛
# أيام العطلة/قلة السيولة قد ترفضها فحوص جودة التغطية.
start = now - timedelta(hours=4)
quotes = TickDataAdapter(mt5).fetch(symbol, start, now, point=float(info.point))
bars = aggregate_quote_bars(quotes, timeframe_seconds=900,
                            closed_until_utc=now,
                            max_gap_seconds=120,
                            max_edge_lag_seconds=5)
for bar in bars:
    print(bar.quote_bar.bar_open_utc, bar.spread.spread_points_p95)
```

**حدود مهمة:** هذا مثال تحليلي مستقل لا يرتبط بعد بمجدول التسجيل، ولا يحفظ سجل Ticks كاملاً على القرص. ليست كل وسائط MT5 تقدم التاريخ اللحظي بجودة أو تغطية متساوية؛ نجاح تجميع الشموع لا يثبت اكتمال كل حدث سعري في خوادم الوسيط. لا ينبغي استخدام هذا المحول مباشرة لربحية Backtest قبل إنشاء أرشفة Tick مستقلة، وفحوص جودة وتكاليف كاملة.

## 3. اختبارات المرحلة الثانية

```powershell
python -m pytest -q
python -m compileall -q mt5_agents
```

الاختبارات الجديدة تشمل: فصل الحسابات، تطابق `magic/comment`، تعدد الطلبات، غياب نتيجة الوسيط، Query failures، رفض إعادة الإرسال بعد `order_send=None` أو Exception أو تعبئة جزئية، واستخدام السبريد الحقيقي مع كشف الفجوات وساعات UTC.

## 4. ما لم ينجز بعد

1. اختبار تكامل فعلي مع Terminal MT5 والحساب التجريبي وسجل وسيط محدد، واختبارات انقطاع قسري على Windows.
2. سير عمل **تسوية بشرية موثقة** للإفراج عن STOP بعد التأكد من الوضع؛ ليس هناك فك آلي للمنع.
3. أرشفة Ticks في ملفات مقسمة خارج قاعدة بيانات سجل الأوامر (مثل Parquet) وتدقيق مصدر البيانات.
4. محاكاة كاملة للعمولات والانزلاق والتبييت والرسوم والهامش؛ وقياس جودة الإشارة خارج العينة.
5. تنبيهات Discord، مراقبة 24/7، خدمات Windows/VPS، فحص التعافي بعد انقطاع الشبكة/الطاقة، صلاحيات مفاتيح التشغيل، وترقية Champion/Challenger.

## مراجع رسمية

- https://www.mql5.com/en/docs/python_metatrader5/mt5positionsget_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5ordersget_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5historyordersget_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py
