import os
import asyncio
import logging
from datetime import datetime
import pytz
import pandas as pd
import pandas_ta as ta
from flask import Flask
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# --- 1. إعدادات البوت والمتغيرات الأساسية ---

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
API_KEY_1 = os.getenv('API_KEY_1')
API_KEY_2 = os.getenv('API_KEY_2')

API_KEYS = [key for key in [API_KEY_1, API_KEY_2] if key]

BASE_PAIRS = [
    "EUR/USD", "AUD/USD", "USD/CAD", "USD/CHF", "USD/JPY", "EUR/JPY", 
    "AUD/JPY", "CAD/JPY", "CHF/JPY", "EUR/AUD", "EUR/CAD", "EUR/CHF", 
    "AUD/CAD", "AUD/CHF", "CAD/CHF"
]

DEFAULT_STRATEGY_SETTINGS = {
    'signal_threshold': 3, 'ema_length': 50, 'rsi_length': 14,
    'rsi_oversold': 30, 'rsi_overbought': 70, 'stoch_k': 14, 'stoch_d': 3,
    'stoch_smooth_k': 3, 'stoch_oversold': 20, 'stoch_overbought': 80,
    'atr_length': 14, 'atr_threshold_ratio': 0.0005
}

PRE_SIGNAL_ALERT_TIME = 30

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

bot_state = {
    'is_running': False, 'active_pairs': [], 'last_signal_time': {},
    'api_key_index': 0, 'selected_for_monitoring': set(),
    'strategy_settings': DEFAULT_STRATEGY_SETTINGS.copy(),
    'awaiting_input': None, 'message_to_delete': None,
}

async def send_error_to_telegram(context: ContextTypes.DEFAULT_TYPE, error_message: str):
    logger.error(error_message)
    await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔴 **خطأ في البوت** 🔴\n\n{error_message}", parse_mode='Markdown')

# --- 2. وظائف مساعدة واستراتيجية ---

def get_next_api_key():
    key = API_KEYS[bot_state['api_key_index']]
    bot_state['api_key_index'] = (bot_state['api_key_index'] + 1) % len(API_KEYS)
    return key

async def fetch_data(pair):
    try:
        api_key = get_next_api_key()
        from twelvedata import TDClient
        td = TDClient(apikey=api_key)
        ts = td.time_series(symbol=pair, interval="5min", outputsize=200, timezone="UTC")
        if ts is None: return None
        return ts.as_pandas().iloc[::-1].reset_index()
    except Exception as e:
        logger.error(f"خطأ في جلب البيانات لـ {pair}: {e}")
        return None

def calculate_indicators(df):
    if df is None or df.empty: return None
    s = bot_state['strategy_settings']
    df.ta.ema(length=s['ema_length'], append=True, col_names=('EMA',))
    df.ta.rsi(length=s['rsi_length'], append=True, col_names=('RSI',))
    df.ta.stoch(k=s['stoch_k'], d=s['stoch_d'], smooth_k=s['stoch_smooth_k'], append=True, col_names=('STOCHk', 'STOCHd'))
    df.ta.atr(length=s['atr_length'], append=True, col_names=('ATR',))
    return df.iloc[0]

def check_strategy(data):
    if data is None: return None
    s = bot_state['strategy_settings']
    signals = {'buy': [], 'sell': []}
    close_price = data['close']
    
    if 'EMA' in data and not pd.isna(data['EMA']):
        if close_price > data['EMA']: signals['buy'].append('EMA')
        if close_price < data['EMA']: signals['sell'].append('EMA')
        
    if 'RSI' in data and not pd.isna(data['RSI']):
        if data['RSI'] < s['rsi_oversold']: signals['buy'].append('RSI')
        if data['RSI'] > s['rsi_overbought']: signals['sell'].append('RSI')
        
    if 'STOCHk' in data and 'STOCHd' in data and not pd.isna(data['STOCHk']) and not pd.isna(data['STOCHd']):
        if data['STOCHk'] < s['stoch_oversold'] and data['STOCHd'] < s['stoch_oversold'] and data['STOCHk'] > data['STOCHd']:
            signals['buy'].append('Stochastic')
        if data['STOCHk'] > s['stoch_overbought'] and data['STOCHd'] > s['stoch_overbought'] and data['STOCHk'] < data['STOCHd']:
            signals['sell'].append('Stochastic')
            
    return signals

def get_display_pair(pair):
    today = datetime.now(pytz.utc).weekday()
    if today == 5 or today == 6:
        return f"{pair} OTC"
    return pair

async def send_signal(context: ContextTypes.DEFAULT_TYPE, chat_id, pair, direction, confidence, confirmations):
    display_pair = get_display_pair(pair)
    emoji = "⬆️" if direction == "صعود" else "⬇️"
    stars = "⭐" * confidence
    confirmations_text = ", ".join(confirmations)
    message_text = (
        f"🔔 إشارة تداول جديدة من بوت النصيري! 🔔\n\n"
        f"زوج العملات: {display_pair}\n"
        f"الاتجاه: {direction} {emoji}\n"
        f"مستوى الثقة: {stars} ({confidence} تأكيدات)\n"
        f"المؤشرات المؤكدة: {confirmations_text}\n"
        f"وقت الدخول: الآن (عند افتتاح الشمعة الجديدة)\n"
        f"الفريم الزمني: 5 دقائق\n\n"
        f"تذكر: هذه إشارة تحليلية. قم بالتحقق بنفسك قبل الدخول في الصفقة."
    )
    await context.bot.send_message(chat_id=chat_id, text=message_text)

async def send_pre_signal_alert(context: ContextTypes.DEFAULT_TYPE, chat_id, pair, direction, confidence, confirmations):
    display_pair = get_display_pair(pair)
    emoji = "⬆️" if direction == "صعود" else "⬇️"
    stars = "⭐" * confidence
    confirmations_text = ", ".join(confirmations)
    message_text = (
        f"⚠️ تنبيه إشارة وشيكة من بوت النصيري! ⚠️\n\n"
        f"زوج العملات: {display_pair}\n"
        f"الاتجاه المحتمل: {direction} {emoji}\n"
        f"مستوى الثقة: {stars} ({confidence} تأكيدات)\n"
        f"المؤشرات المؤكدة: {confirmations_text}\n"
        f"الوقت المتبقي: حوالي {PRE_SIGNAL_ALERT_TIME} ثانية\n"
        f"الفريم الزمني: 5 دقائق\n\n"
        f"استعد للدخول في الصفقة إذا استمرت الشروط حتى إغلاق الشمعة."
    )
    await context.bot.send_message(chat_id=chat_id, text=message_text)

# --- 3. وظائف لوحة التحكم الرئيسية ---

REPLY_KEYBOARD_MARKUP = ReplyKeyboardMarkup([
    ["▶️ تشغيل", "⏸️ إيقاف"],
    ["📊 تحليل السوق", "⚙️ الإعدادات"],
    ["ℹ️ الحالة"]
], resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not API_KEYS:
        await send_error_to_telegram(context, "لم يتم العثور على أي مفاتيح API في متغيرات البيئة. لن يعمل تحليل السوق.")
    
    await update.message.reply_text(
        "أهلاً بك في بوت النصيري (نسخة مطورة)!\n\nاستخدم الأزرار في الأسفل للتحكم.",
        reply_markup=REPLY_KEYBOARD_MARKUP
    )

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active_pairs_str = ", ".join(bot_state['active_pairs']) if bot_state['active_pairs'] else "لا توجد أزواج قيد المراقبة."
    status_message = (
        f"**📊 حالة بوت النصيري:**\n\n"
        f"حالة المراقبة: {'✅ يعمل' if bot_state['is_running'] else '❌ متوقف'}\n"
        f"الأزواج قيد المراقبة: {active_pairs_str}\n"
        f"مستوى الثقة المطلوب: {bot_state['strategy_settings']['signal_threshold']} مؤشرات"
    )
    await update.message.reply_text(status_message, parse_mode='Markdown')

async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not bot_state['active_pairs']:
        await update.message.reply_text("❌ لا يمكن بدء المراقبة. الرجاء تحليل السوق واختيار زوج واحد على الأقل أولاً.")
        return
    if not bot_state['is_running']:
        bot_state['is_running'] = True
        await update.message.reply_text(f"✅ تم تشغيل المراقبة لـ: {', '.join(bot_state['active_pairs'])}")
        logger.info("المراقبة بدأت.")
    else:
        await update.message.reply_text("البوت يعمل بالفعل.")

async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if bot_state['is_running']:
        bot_state['is_running'] = False
        await update.message.reply_text("⏸️ تم إيقاف المراقبة.")
        logger.info("المراقبة توقفت.")
    else:
        await update.message.reply_text("البوت متوقف بالفعل.")

# --- 4. منطق تحليل السوق التفاعلي ---

async def market_analysis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not API_KEYS:
        await send_error_to_telegram(context, "فشل تحليل السوق لأنه لم يتم العثور على مفاتيح API.")
        return

    await update.message.reply_text("⏳ جاري تحليل السوق لتحديد الأزواج النشطة...")
    tasks = [fetch_data(pair) for pair in BASE_PAIRS]
    results = await asyncio.gather(*tasks)
    
    data_found_count = sum(1 for df in results if df is not None and not df.empty)

    if data_found_count == 0:
        await send_error_to_telegram(context, "فشل تحليل السوق: لم يتمكن البوت من جلب البيانات لأي زوج. قد تكون مفاتيح API غير صالحة أو أن هناك مشكلة في الاتصال بـ Twelve Data.")
        return

    active_pairs_found = []
    s = bot_state['strategy_settings']
    for pair, df in zip(BASE_PAIRS, results):
        if df is not None and not df.empty:
            latest_data = calculate_indicators(df)
            if latest_data is not None and 'ATR' in latest_data and 'close' in latest_data and latest_data['close'] > 0:
                if (latest_data['ATR'] / latest_data['close']) > s['atr_threshold_ratio']:
                    active_pairs_found.append(pair)
    
    if not active_pairs_found:
        await update.message.reply_text("تحليل السوق اكتمل: لم يتم العثور على أزواج نشطة حاليًا (السوق هادئ). حاول مرة أخرى لاحقًا.")
        return
        
    bot_state['selected_for_monitoring'] = set()
    keyboard = [[InlineKeyboardButton(f"🔲 {pair}", callback_data=f"select_{pair}")] for pair in active_pairs_found]
    keyboard.append([InlineKeyboardButton("✅ بدء المراقبة بهذه الأزواج", callback_data="confirm_selection")])
    keyboard.append([InlineKeyboardButton("✖️ إلغاء", callback_data="cancel_selection")])
    await update.message.reply_text("**تحليل السوق اكتمل.**\n\nالرجاء تحديد الأزواج التي تريد مراقبتها:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def pair_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pair = query.data.split('_', 1)[1]
    selected_set = bot_state['selected_for_monitoring']
    if pair in selected_set:
        selected_set.remove(pair)
    else:
        selected_set.add(pair)
    old_keyboard = query.message.reply_markup.inline_keyboard
    new_keyboard = []
    for row in old_keyboard:
        button = row[0]
        if button.callback_data.startswith("select_"):
            p = button.callback_data.split('_', 1)[1]
            if p in selected_set:
                new_keyboard.append([InlineKeyboardButton(f"✅ {p}", callback_data=button.callback_data)])
            else:
                new_keyboard.append([InlineKeyboardButton(f"🔲 {p}", callback_data=button.callback_data)])
        else:
            new_keyboard.append(row)
    await query.edit_message_text(query.message.text, reply_markup=InlineKeyboardMarkup(new_keyboard))

async def confirm_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    selected_pairs = bot_state['selected_for_monitoring']
    if not selected_pairs:
        await query.answer("لم تختر أي زوج!", show_alert=True)
        return
    bot_state['active_pairs'] = list(selected_pairs)
    bot_state['is_running'] = True
    message = f"✅ تم تحديث القائمة وبدء المراقبة.\n\nالأزواج قيد المراقبة الآن:\n" + "\n".join(f"- {p}" for p in bot_state['active_pairs'])
    await query.edit_message_text(message)

async def cancel_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("تم إلغاء عملية الاختيار.")

# --- 5. منطق إعدادات الاستراتيجية المتقدمة ---

async def strategy_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = bot_state['strategy_settings']
    text = (f"**⚙️ إعدادات الاستراتيجية الحالية:**\n\n"
            f"- مستوى الثقة: {s['signal_threshold']} مؤشرات\n"
            f"- إعدادات EMA: {s['ema_length']}\n"
            f"- إعدادات RSI: {s['rsi_length']}, {s['rsi_oversold']}/{s['rsi_overbought']}\n"
            f"- إعدادات Stochastic: {s['stoch_k']},{s['stoch_d']},{s['stoch_smooth_k']}\n"
            f"- حساسية ATR: {s['atr_threshold_ratio']}")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ مستوى الثقة ({s['signal_threshold']})", callback_data='set_confidence')],
        [InlineKeyboardButton("🔧 قيم المؤشرات", callback_data='edit_indicator_values')],
        [InlineKeyboardButton("🔄 إعادة تعيين الكل", callback_data='reset_strategy')],
        [InlineKeyboardButton("✖️ إغلاق", callback_data='close_menu')]
    ])
    
    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def set_confidence_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("★★☆☆ (مؤشرين)", callback_data='set_thresh_2')],
        [InlineKeyboardButton("★★★☆ (ثلاثة مؤشرات)", callback_data='set_thresh_3')],
        [InlineKeyboardButton("🔙 رجوع", callback_data='back_to_strategy_settings')]
    ])
    await query.edit_message_text("اختر الحد الأدنى من المؤشرات المتوافقة لإرسال إشارة:", reply_markup=keyboard)

async def set_threshold_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    threshold = int(query.data.split('_')[-1])
    bot_state['strategy_settings']['signal_threshold'] = threshold
    await query.answer(f"تم تعيين مستوى الثقة على {threshold} مؤشرات.")
    await strategy_settings_menu(update, context)

async def edit_indicator_values_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("EMA", callback_data='edit_ema')],
        [InlineKeyboardButton("RSI", callback_data='edit_rsi')],
        [InlineKeyboardButton("Stochastic", callback_data='edit_stoch')],
        [InlineKeyboardButton("ATR (حساسية السوق)", callback_data='edit_atr')], # <-- الزر الجديد
        [InlineKeyboardButton("🔙 رجوع", callback_data='back_to_strategy_settings')]
    ])
    await query.edit_message_text("اختر المؤشر الذي تريد تعديل قيمه:", reply_markup=keyboard)

async def edit_indicator_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    indicator = query.data.split('_')[1]
    bot_state['awaiting_input'] = indicator
    
    prompts = {
        'ema': "أرسل القيمة الجديدة لفترة EMA (مثال: 20)",
        'rsi': "أرسل القيم الجديدة لـ RSI بالتنسيق: فترة,تشبع بيع,تشبع شراء (مثال: 7,25,75)",
        'stoch': "أرسل القيم الجديدة لـ Stochastic بالتنسيق: k,d,smooth_k (مثال: 10,5,5)",
        'atr': "أرسل القيمة الجديدة لحساسية ATR (مثال: 0.0004)" # <-- الرسالة الجديدة
    }
    
    msg = await query.edit_message_text(f"**{prompts[indicator]}**\n\nلإلغاء العملية، أرسل /cancel", parse_mode='Markdown')
    bot_state['message_to_delete'] = msg.message_id

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "▶️ تشغيل": await start_bot(update, context); return
    if text == "⏸️ إيقاف": await stop_bot(update, context); return
    if text == "📊 تحليل السوق": await market_analysis_handler(update, context); return
    if text == "⚙️ الإعدادات": await strategy_settings_menu(update, context); return
    if text == "ℹ️ الحالة": await show_status(update, context); return
        
    if bot_state.get('awaiting_input') is None:
        await update.message.reply_text("أمر غير مفهوم. الرجاء استخدام الأزرار في الأسفل.")
        return

    indicator = bot_state['awaiting_input']
    user_input = update.message.text
    s = bot_state['strategy_settings']
    
    try:
        if indicator == 'ema':
            s['ema_length'] = int(user_input)
        elif indicator == 'rsi':
            parts = [int(p.strip()) for p in user_input.split(',')]
            s['rsi_length'], s['rsi_oversold'], s['rsi_overbought'] = parts
        elif indicator == 'stoch':
            parts = [int(p.strip()) for p in user_input.split(',')]
            s['stoch_k'], s['stoch_d'], s['stoch_smooth_k'] = parts
        elif indicator == 'atr': # <-- المنطق الجديد
            s['atr_threshold_ratio'] = float(user_input)
        
        await update.message.reply_text(f"✅ تم تحديث قيم مؤشر {indicator.upper()} بنجاح.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ صيغة الإدخال غير صحيحة. الرجاء المحاولة مرة أخرى.")
    
    bot_state['awaiting_input'] = None
    if bot_state.get('message_to_delete'):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=bot_state['message_to_delete'])
        except Exception as e:
            logger.warning(f"Could not delete message: {e}")
        bot_state['message_to_delete'] = None
    
    await strategy_settings_menu(update, context)

async def cancel_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state['awaiting_input'] = None
    if bot_state.get('message_to_delete'):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=bot_state['message_to_delete'])
        except Exception as e:
            logger.warning(f"Could not delete message: {e}")
        bot_state['message_to_delete'] = None
    await update.message.reply_text("تم إلغاء العملية.")
    await strategy_settings_menu(update, context)

async def reset_strategy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot_state['strategy_settings'] = DEFAULT_STRATEGY_SETTINGS.copy()
    await query.answer("تم إعادة تعيين جميع الإعدادات إلى الوضع الافتراضي.", show_alert=True)
    await strategy_settings_menu(update, context)

async def close_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("تم إغلاق القائمة.")

# --- 6. مهمة التحقق من الإشارات الرئيسية ---

async def check_signals_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not bot_state['is_running'] or not bot_state['active_pairs']: return
    current_time = datetime.now(pytz.utc)
    seconds_to_next_minute = 60 - current_time.second
    
    async def run_check(is_pre_alert):
        tasks = [fetch_data(pair) for pair in bot_state['active_pairs']]
        results = await asyncio.gather(*tasks)
        for pair, df in zip(bot_state['active_pairs'], results):
            if df is not None and not df.empty:
                latest_data = calculate_indicators(df)
                if latest_data is not None:
                    signals = check_strategy(latest_data)
                    buy_conf, sell_conf = len(signals['buy']), len(signals['sell'])
                    candle_time = latest_data['datetime']
                    if pair not in bot_state['last_signal_time'] or bot_state['last_signal_time'].get(pair) < candle_time:
                        s_thresh = bot_state['strategy_settings']['signal_threshold']
                        if buy_conf >= s_thresh and buy_conf > sell_conf:
                            if is_pre_alert: await send_pre_signal_alert(context, TELEGRAM_CHAT_ID, pair, "صعود", buy_conf, signals['buy'])
                            else: await send_signal(context, TELEGRAM_CHAT_ID, pair, "صعود", buy_conf, signals['buy'])
                            bot_state['last_signal_time'][pair] = candle_time
                        elif sell_conf >= s_thresh and sell_conf > buy_conf:
                            if is_pre_alert: await send_pre_signal_alert(context, TELEGRAM_CHAT_ID, pair, "هبوط", sell_conf, signals['sell'])
                            else: await send_signal(context, TELEGRAM_CHAT_ID, pair, "هبوط", sell_conf, signals['sell'])
                            bot_state['last_signal_time'][pair] = candle_time

    if PRE_SIGNAL_ALERT_TIME + 1 >= seconds_to_next_minute >= PRE_SIGNAL_ALERT_TIME - 1:
        await run_check(is_pre_alert=True)
    
    if current_time.second in [2, 3] and current_time.minute % 5 == 0:
        await run_check(is_pre_alert=False)

# --- 7. إعداد خادم الويب وتشغيل البوت ---
app = Flask(__name__)
@app.route('/health')
def health_check(): return "Bot is running", 200
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- 8. الوظيفة الرئيسية (Main Function) ---
def main() -> None:
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    logger.info("بدء تشغيل خادم الويب لإبقاء الخدمة مستيقظة...")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cancel", cancel_input_handler))
    
    application.add_handler(CallbackQueryHandler(pair_selection_handler, pattern='^select_'))
    application.add_handler(CallbackQueryHandler(confirm_selection_handler, pattern='^confirm_selection$'))
    application.add_handler(CallbackQueryHandler(cancel_selection_handler, pattern='^cancel_selection$'))
    application.add_handler(CallbackQueryHandler(close_menu_handler, pattern='^close_menu$'))
    application.add_handler(CallbackQueryHandler(strategy_settings_menu, pattern='^back_to_strategy_settings$'))
    application.add_handler(CallbackQueryHandler(set_confidence_menu, pattern='^set_confidence$'))
    application.add_handler(CallbackQueryHandler(set_threshold_handler, pattern='^set_thresh_'))
    application.add_handler(CallbackQueryHandler(edit_indicator_values_menu, pattern='^edit_indicator_values$'))
    application.add_handler(CallbackQueryHandler(edit_indicator_prompt, pattern='^edit_'))
    application.add_handler(CallbackQueryHandler(reset_strategy_handler, pattern='^reset_strategy$'))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    application.job_queue.run_repeating(check_signals_task, interval=1, first=5)
    
    logger.info("بدء تشغيل بوت التليجرام (نسخة مطورة)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
