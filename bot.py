import os
import asyncio
import logging
from datetime import datetime
import pytz
import pandas as pd
import pandas_ta as ta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from twelvedata import TDClient

# --- 1. إعدادات البوت والمتغيرات الأساسية ---

# --- متغيرات البيئة (يجب إضافتها في Render) ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# استخدم مفتاحين API لمضاعفة الحدود
TWELVE_DATA_API_KEY_1 = os.getenv('TWELVE_DATA_API_KEY_1')
TWELVE_DATA_API_KEY_2 = os.getenv('TWELVE_DATA_API_KEY_2')

# قائمة المفاتيح للتبديل بينها
API_KEYS = [key for key in [TWELVE_DATA_API_KEY_1, TWELVE_DATA_API_KEY_2] if key]
if not API_KEYS:
    raise ValueError("يجب توفير مفتاح API واحد على الأقل من Twelve Data.")

# --- إعدادات الاستراتيجية ---
TIMEFRAME = "5min"
OUTPUT_SIZE = 200 # عدد الشموع التي سيتم جلبها للتحليل
ATR_THRESHOLD = 0.0005 # حد التقلب لتحديد الزوج "النشط"

# --- قائمة الأزواج الأساسية للتحليل ---
BASE_PAIRS = ["EUR/USD", "AUD/USD", "NZD/USD", "USD/CAD", "USD/CHF", "USD/JPY", "GBP/USD", "EUR/JPY", "AUD/JPY", "GBP/JPY", "NZD/JPY", "CAD/JPY", "CHF/JPY", "EUR/AUD", "EUR/NZD", "EUR/CAD", "EUR/CHF", "AUD/CAD", "AUD/CHF", "NZD/CAD", "NZD/CHF", "CAD/CHF"]

# --- إعدادات التنبيه ---
PRE_SIGNAL_ALERT_TIME = 30 # 30 ثانية قبل نهاية الشمعة

# --- إعدادات التسجيل (Logging) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- حالة البوت (يتم تخزينها في الذاكرة) ---
bot_state = {
    'is_running': False,
    'active_pairs': [], # سيتم تحديدها من قبل المستخدم
    'last_signal_time': {},
    'signal_threshold': 3, # عدد المؤشرات المتوافقة المطلوبة للإشارة
    'api_key_index': 0, # مؤشر لتتبع المفتاح المستخدم حاليًا
    'selected_for_monitoring': set(), # لتخزين الأزواج التي يختارها المستخدم مؤقتًا
}


# --- 2. وظائف مساعدة (Helper Functions) ---

def get_next_api_key():
    """الحصول على مفتاح API التالي في القائمة (Round Robin)."""
    key = API_KEYS[bot_state['api_key_index']]
    bot_state['api_key_index'] = (bot_state['api_key_index'] + 1) % len(API_KEYS)
    return key

async def fetch_data(pair):
    """جلب البيانات من Twelve Data."""
    try:
        api_key = get_next_api_key()
        td = TDClient(apikey=api_key)
        ts = td.time_series(
            symbol=pair,
            interval=TIMEFRAME,
            outputsize=OUTPUT_SIZE,
            timezone="UTC"
        )
        if ts is None:
            logger.warning(f"لم يتم إرجاع بيانات لـ {pair}. قد يكون رمزًا غير صالح أو لا توجد بيانات.")
            return None
            
        # تحويل البيانات إلى DataFrame وتنسيقها
        df = ts.as_pandas()
        # Twelve Data ترجع البيانات من الأقدم للأحدث، وهذا هو المطلوب
        return df.iloc[::-1].reset_index() # نعكس الترتيب ليصبح من الأحدث للأقدم
    except Exception as e:
        logger.error(f"خطأ في جلب البيانات لـ {pair} باستخدام مفتاح ينتهي بـ '...{api_key[-4:]}': {e}")
        return None

def calculate_indicators(df):
    """حساب المؤشرات باستخدام pandas-ta."""
    if df is None or df.empty:
        return None
    
    # حساب المؤشرات
    df.ta.ema(length=50, append=True, col_names=('EMA_50',))
    df.ta.rsi(length=14, append=True, col_names=('RSI_14',))
    df.ta.stoch(k=14, d=3, smooth_k=3, append=True, col_names=('STOCHk_14_3_3', 'STOCHd_14_3_3'))
    df.ta.atr(length=14, append=True, col_names=('ATR_14',))

    # إرجاع آخر صف (الشمعة المكتملة الأخيرة)
    return df.iloc[0]

def check_strategy(data):
    """التحقق من شروط الاستراتيجية."""
    if data is None:
        return None

    signals = {'buy': [], 'sell': []}
    
    close_price = data['close']
    
    # 1. EMA 50
    if 'EMA_50' in data and not pd.isna(data['EMA_50']):
        if close_price > data['EMA_50']: signals['buy'].append('EMA')
        if close_price < data['EMA_50']: signals['sell'].append('EMA')

    # 2. RSI
    if 'RSI_14' in data and not pd.isna(data['RSI_14']):
        if data['RSI_14'] < 30: signals['buy'].append('RSI')
        if data['RSI_14'] > 70: signals['sell'].append('RSI')

    # 3. Stochastic
    if 'STOCHk_14_3_3' in data and 'STOCHd_14_3_3' in data and not pd.isna(data['STOCHk_14_3_3']) and not pd.isna(data['STOCHd_14_3_3']):
        if data['STOCHk_14_3_3'] < 20 and data['STOCHd_14_3_3'] < 20 and data['STOCHk_14_3_3'] > data['STOCHd_14_3_3']:
            signals['buy'].append('Stochastic')
        if data['STOCHk_14_3_3'] > 80 and data['STOCHd_14_3_3'] > 80 and data['STOCHk_14_3_3'] < data['STOCHd_14_3_3']:
            signals['sell'].append('Stochastic')
            
    # يمكن إضافة تحليل نماذج الشموع هنا إذا أردت

    return signals

def get_confidence_stars(num_confirmations):
    """تحويل عدد التأكيدات إلى نجوم ثقة."""
    return "⭐" * num_confirmations

async def send_signal(context: ContextTypes.DEFAULT_TYPE, chat_id, pair, direction, confidence, confirmations):
    """إرسال رسالة الإشارة النهائية إلى تيليجرام (بالشكل الأصلي)."""
    emoji = "⬆️" if direction == "صعود" else "⬇️"
    stars = get_confidence_stars(confidence)
    confirmations_text = ", ".join(confirmations)
    
    message_text = (
        f"🔔 إشارة تداول جديدة من بوت النصيري! 🔔\n\n"
        f"زوج العملات: {pair}\n"
        f"الاتجاه: {direction} {emoji}\n"
        f"مستوى الثقة: {stars} ({confidence} تأكيدات)\n"
        f"المؤشرات المؤكدة: {confirmations_text}\n"
        f"وقت الدخول: الآن (عند افتتاح الشمعة الجديدة)\n"
        f"الفريم الزمني: 5 دقائق\n\n"
        f"تذكر: هذه إشارة تحليلية. قم بالتحقق بنفسك قبل الدخول في الصفقة."
    )
    await context.bot.send_message(chat_id=chat_id, text=message_text)

async def send_pre_signal_alert(context: ContextTypes.DEFAULT_TYPE, chat_id, pair, direction, confidence, confirmations):
    """إرسال تنبيه مسبق قبل الإشارة النهائية."""
    emoji = "⬆️" if direction == "صعود" else "⬇️"
    stars = get_confidence_stars(confidence)
    confirmations_text = ", ".join(confirmations)

    message_text = (
        f"⚠️ تنبيه إشارة وشيكة من بوت النصيري! ⚠️\n\n"
        f"زوج العملات: {pair}\n"
        f"الاتجاه المحتمل: {direction} {emoji}\n"
        f"مستوى الثقة: {stars} ({confidence} تأكيدات)\n"
        f"المؤشرات المؤكدة: {confirmations_text}\n"
        f"الوقت المتبقي: حوالي {PRE_SIGNAL_ALERT_TIME} ثانية\n"
        f"الفريم الزمني: 5 دقائق\n\n"
        f"استعد للدخول في الصفقة إذا استمرت الشروط حتى إغلاق الشمعة."
    )
    await context.bot.send_message(chat_id=chat_id, text=message_text)


# --- 3. وظائف لوحة التحكم والأوامر ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة ترحيبية مع لوحة التحكم."""
    welcome_message = (
        "أهلاً بك في بوت النصيري لإشارات التداول (نسخة مطورة)!\n\n"
        "**التحسينات الجديدة:**\n"
        "- يعتمد على بيانات دقيقة من **Twelve Data API**.\n"
        "- **تحليل سوق تفاعلي:** أنت تختار الأزواج التي تريد مراقبتها.\n"
        "- يستخدم **مفتاحي API** لمضاعفة قدرة التحليل.\n\n"
        "استخدم الأزرار أدناه للتحكم في البوت."
    )
    keyboard = [
        [InlineKeyboardButton("▶️ تشغيل المراقبة", callback_data='start_bot'),
         InlineKeyboardButton("⏸️ إيقاف المراقبة", callback_data='stop_bot')],
        [InlineKeyboardButton("📊 تحليل السوق واختيار الأزواج", callback_data='market_analysis')],
        [InlineKeyboardButton("ℹ️ عرض الحالة", callback_data='show_status')],
    ]
    await update.message.reply_text(welcome_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض حالة البوت الحالية."""
    query = update.callback_query
    if query: await query.answer()

    active_pairs_str = ", ".join(bot_state['active_pairs']) if bot_state['active_pairs'] else "لا توجد أزواج قيد المراقبة."
    status_message = (
        f"**📊 حالة بوت النصيري:**\n\n"
        f"حالة المراقبة: {'✅ يعمل' if bot_state['is_running'] else '❌ متوقف'}\n"
        f"الأزواج قيد المراقبة: {active_pairs_str}\n"
        f"مستوى التأكيد المطلوب: {bot_state['signal_threshold']} مؤشرات\n"
        f"عدد مفاتيح API المستخدمة: {len(API_KEYS)}"
    )
    
    keyboard = [
        [InlineKeyboardButton("▶️ تشغيل المراقبة", callback_data='start_bot'),
         InlineKeyboardButton("⏸️ إيقاف المراقبة", callback_data='stop_bot')],
        [InlineKeyboardButton("📊 تحليل السوق واختيار الأزواج", callback_data='market_analysis')],
        [InlineKeyboardButton("ℹ️ عرض الحالة", callback_data='show_status')],
    ]
    
    if query:
        await query.edit_message_text(status_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.message.reply_text(status_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يبدأ تشغيل المراقبة."""
    query = update.callback_query
    await query.answer()
    if not bot_state['active_pairs']:
        await query.edit_message_text("❌ لا يمكن بدء المراقبة. الرجاء تحليل السوق واختيار زوج واحد على الأقل أولاً.", reply_markup=query.message.reply_markup)
        return

    if not bot_state['is_running']:
        bot_state['is_running'] = True
        await query.edit_message_text(f"✅ تم تشغيل المراقبة لـ: {', '.join(bot_state['active_pairs'])}", reply_markup=query.message.reply_markup)
        logger.info("المراقبة بدأت.")
    else:
        await query.edit_message_text("البوت يعمل بالفعل.", reply_markup=query.message.reply_markup)

async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يوقف تشغيل المراقبة."""
    query = update.callback_query
    await query.answer()
    if bot_state['is_running']:
        bot_state['is_running'] = False
        await query.edit_message_text("⏸️ تم إيقاف المراقبة.", reply_markup=query.message.reply_markup)
        logger.info("المراقبة توقفت.")
    else:
        await query.edit_message_text("البوت متوقف بالفعل.", reply_markup=query.message.reply_markup)

# --- 4. منطق تحليل السوق التفاعلي ---

async def market_analysis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يبدأ عملية تحليل السوق."""
    query = update.callback_query
    await query.answer("جاري تحليل السوق... قد يستغرق الأمر بعض الوقت.")
    await query.edit_message_text("⏳ جاري تحليل السوق لتحديد الأزواج النشطة...")

    active_pairs_found = []
    tasks = [fetch_data(pair) for pair in BASE_PAIRS]
    results = await asyncio.gather(*tasks)

    for pair, df in zip(BASE_PAIRS, results):
        if df is not None and not df.empty:
            latest_data = calculate_indicators(df)
            if latest_data is not None and 'ATR_14' in latest_data and 'close' in latest_data:
                # معيار النشاط: ATR كنسبة من سعر الإغلاق
                if (latest_data['ATR_14'] / latest_data['close']) > ATR_THRESHOLD:
                    active_pairs_found.append(pair)
    
    if not active_pairs_found:
        await query.edit_message_text("لم يتم العثور على أزواج نشطة حاليًا. حاول مرة أخرى لاحقًا.", reply_markup=query.message.reply_markup)
        return

    # إعادة تعيين الاختيارات المؤقتة
    bot_state['selected_for_monitoring'] = set()
    
    keyboard = []
    for pair in active_pairs_found:
        keyboard.append([InlineKeyboardButton(f"🔲 {pair}", callback_data=f"select_{pair}")])
    
    keyboard.append([InlineKeyboardButton("✅ بدء المراقبة بهذه الأزواج", callback_data="confirm_selection")])
    keyboard.append([InlineKeyboardButton("🔙 إلغاء", callback_data="cancel_selection")])

    await query.edit_message_text(
        "**تحليل السوق اكتمل.**\n\n"
        "تم العثور على الأزواج النشطة التالية. الرجاء تحديد الأزواج التي تريد مراقبتها ثم اضغط 'بدء المراقبة'.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def pair_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يتعامل مع اختيار المستخدم للأزواج."""
    query = update.callback_query
    await query.answer()
    
    pair = query.data.split('_')[1]
    selected_set = bot_state['selected_for_monitoring']

    if pair in selected_set:
        selected_set.remove(pair)
    else:
        selected_set.add(pair)
        
    # إعادة بناء لوحة المفاتيح مع تحديث الاختيارات
    old_keyboard = query.message.reply_markup.inline_keyboard
    new_keyboard = []
    for row in old_keyboard:
        button = row[0]
        if button.callback_data.startswith("select_"):
            p = button.callback_data.split('_')[1]
            if p in selected_set:
                new_keyboard.append([InlineKeyboardButton(f"✅ {p}", callback_data=button.callback_data)])
            else:
                new_keyboard.append([InlineKeyboardButton(f"🔲 {p}", callback_data=button.callback_data)])
        else:
            new_keyboard.append(row) # أزرار التحكم (بدء، إلغاء)

    await query.edit_message_text(query.message.text, reply_markup=InlineKeyboardMarkup(new_keyboard))


async def confirm_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يؤكد اختيار المستخدم ويبدأ المراقبة."""
    query = update.callback_query
    await query.answer()
    
    selected_pairs = bot_state['selected_for_monitoring']
    if not selected_pairs:
        await query.answer("لم تختر أي زوج!", show_alert=True)
        return

    bot_state['active_pairs'] = list(selected_pairs)
    bot_state['is_running'] = True # بدء المراقبة تلقائيًا
    
    message = f"✅ تم تحديث القائمة وبدء المراقبة.\n\nالأزواج قيد المراقبة الآن:\n" + "\n".join(f"- {p}" for p in bot_state['active_pairs'])
    
    # العودة إلى لوحة التحكم الرئيسية
    keyboard = [
        [InlineKeyboardButton("▶️ تشغيل المراقبة", callback_data='start_bot'),
         InlineKeyboardButton("⏸️ إيقاف المراقبة", callback_data='stop_bot')],
        [InlineKeyboardButton("📊 تحليل السوق واختيار الأزواج", callback_data='market_analysis')],
        [InlineKeyboardButton("ℹ️ عرض الحالة", callback_data='show_status')],
    ]
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

async def cancel_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إلغاء عملية الاختيار والعودة للقائمة الرئيسية."""
    query = update.callback_query
    await query.answer()
    await show_status(update, context) # استدعاء show_status لإعادة عرض القائمة الرئيسية بالحالة الحالية


# --- 5. مهمة التحقق من الإشارات الرئيسية ---

async def check_signals_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    """المهمة الدورية للتحقق من الإشارات."""
    if not bot_state['is_running'] or not bot_state['active_pairs']:
        return

    logger.info(f"بدء مهمة التحقق من الإشارات لـ: {bot_state['active_pairs']}")
    
    current_time = datetime.now(pytz.utc)
    # حساب الثواني حتى بداية الدقيقة التالية
    seconds_to_next_minute = 60 - current_time.second
    
    # إذا كنا قريبين من نهاية الشمعة الحالية (قبل 30 ثانية)
    if PRE_SIGNAL_ALERT_TIME + 2 >= seconds_to_next_minute >= PRE_SIGNAL_ALERT_TIME - 2:
        tasks = [fetch_data(pair) for pair in bot_state['active_pairs']]
        results = await asyncio.gather(*tasks)

        for pair, df in zip(bot_state['active_pairs'], results):
            if df is not None:
                latest_data = calculate_indicators(df)
                if latest_data is not None:
                    signals = check_strategy(latest_data)
                    buy_conf = len(signals['buy'])
                    sell_conf = len(signals['sell'])

                    # تجنب إرسال تنبيهات متكررة
                    candle_time = latest_data['datetime']
                    if pair not in bot_state['last_signal_time'] or bot_state['last_signal_time'].get(pair) < candle_time:
                        if buy_conf >= bot_state['signal_threshold'] and buy_conf > sell_conf:
                            await send_pre_signal_alert(context, TELEGRAM_CHAT_ID, pair, "صعود", buy_conf, signals['buy'])
                            bot_state['last_signal_time'][pair] = candle_time
                        elif sell_conf >= bot_state['signal_threshold'] and sell_conf > buy_conf:
                            await send_pre_signal_alert(context, TELEGRAM_CHAT_ID, pair, "هبوط", sell_conf, signals['sell'])
                            bot_state['last_signal_time'][pair] = candle_time
    
    # إرسال الإشارة النهائية عند بداية الشمعة الجديدة (بعد 2-3 ثوانٍ)
    if current_time.second in [2, 3] and current_time.minute % 5 == 0:
        tasks = [fetch_data(pair) for pair in bot_state['active_pairs']]
        results = await asyncio.gather(*tasks)

        for pair, df in zip(bot_state['active_pairs'], results):
            if df is not None:
                latest_data = calculate_indicators(df)
                if latest_data is not None:
                    signals = check_strategy(latest_data)
                    buy_conf = len(signals['buy'])
                    sell_conf = len(signals['sell'])
                    
                    candle_time = latest_data['datetime']
                    if pair not in bot_state['last_signal_time'] or bot_state['last_signal_time'].get(pair) < candle_time:
                        if buy_conf >= bot_state['signal_threshold'] and buy_conf > sell_conf:
                            await send_signal(context, TELEGRAM_CHAT_ID, pair, "صعود", buy_conf, signals['buy'])
                            bot_state['last_signal_time'][pair] = candle_time
                        elif sell_conf >= bot_state['signal_threshold'] and sell_conf > buy_conf:
                            await send_signal(context, TELEGRAM_CHAT_ID, pair, "هبوط", sell_conf, signals['sell'])
                            bot_state['last_signal_time'][pair] = candle_time


# --- 6. الوظيفة الرئيسية (Main Function) ---

def main() -> None:
    """تشغيل البوت."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # إضافة معالجات الأوامر
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(start_bot, pattern='^start_bot$'))
    application.add_handler(CallbackQueryHandler(stop_bot, pattern='^stop_bot$'))
    application.add_handler(CallbackQueryHandler(show_status, pattern='^show_status$'))
    application.add_handler(CallbackQueryHandler(market_analysis_handler, pattern='^market_analysis$'))
    
    # معالجات الاختيار التفاعلي
    application.add_handler(CallbackQueryHandler(pair_selection_handler, pattern='^select_'))
    application.add_handler(CallbackQueryHandler(confirm_selection_handler, pattern='^confirm_selection$'))
    application.add_handler(CallbackQueryHandler(cancel_selection_handler, pattern='^cancel_selection$'))

    # جدولة مهمة التحقق من الإشارات كل ثانية للتحكم الدقيق في التوقيت
    application.job_queue.run_repeating(check_signals_task, interval=1, first=5)

    # بدء البوت
    logger.info("بدء تشغيل البوت (نسخة مطورة)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
