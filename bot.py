# -*- coding: utf-8 -*-
import os
import asyncio
import logging
from datetime import datetime
import pytz
import pandas as pd
import pandas_ta as ta
import traceback
import threading
from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from twelvedata import TDClient

# --- 1. إعدادات البوت والمتغيرات الأساسية ---

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
API_KEY_1 = os.getenv('API_KEY_1')
API_KEY_2 = os.getenv('API_KEY_2')

API_KEYS = [key for key in [API_KEY_1, API_KEY_2] if key]

DEFAULT_STRATEGY = {
    'signal_threshold': 1, 'ema_period': 5, 'rsi_period': 7,
    'rsi_oversold': 40, 'rsi_overbought': 60, 'stoch_k': 14, 'stoch_d': 3,
    'stoch_smooth_k': 3, 'stoch_oversold': 30, 'stoch_overbought': 70,
    'atr_sensitivity': 0.0002, # تم تحديث القيمة بناء على طلبك
}

TIMEFRAME = "5min"
OUTPUT_SIZE = 200 # <-- تم إصلاح الخطأ هنا
BASE_PAIRS = ["EUR/USD", "AUD/USD", "USD/CAD", "USD/CHF", "USD/JPY", "EUR/JPY", "AUD/JPY", "CAD/JPY", "CHF/JPY", "EUR/AUD", "EUR/CAD", "EUR/CHF", "AUD/CAD", "AUD/CHF", "CAD/CHF"] # <-- تم إصلاح الخطأ هنا

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

bot_state = {
    'is_running': False, 'active_pairs': [], 'strategy': DEFAULT_STRATEGY.copy(),
    'api_key_index': 0, 'last_pre_signal_time': {}, 'last_final_signal_time': {},
    'awaiting_input': None, 'message_to_delete': None,
}

async def send_telegram_error(context: ContextTypes.DEFAULT_TYPE, error_message: str):
    try:
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔴 **خطأ في البوت** 🔴\n\n{error_message}", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send error message to Telegram: {e}")

# --- 2. وظائف مساعدة (Helper Functions) ---

def get_next_api_key():
    if not API_KEYS: return None
    key = API_KEYS[bot_state['api_key_index']]
    bot_state['api_key_index'] = (bot_state['api_key_index'] + 1) % len(API_KEYS)
    return key

async def fetch_data(pair, context):
    api_key = get_next_api_key()
    if not api_key:
        logger.error("No API keys available.")
        await send_telegram_error(context, "لا توجد مفاتيح API متاحة.")
        return None
    try:
        td = TDClient(apikey=api_key)
        ts = td.time_series(symbol=pair, interval=TIMEFRAME, outputsize=OUTPUT_SIZE, timezone="UTC")
        if ts is None:
            logger.warning(f"No data returned for {pair}.")
            return None
        df = ts.as_pandas()
        return df.iloc[::-1].reset_index()
    except Exception as e:
        if "You have run out of API credits" not in str(e):
            logger.error(f"خطأ في جلب البيانات لـ {pair}: {e}")
        return None

def calculate_indicators(df, strategy):
    if df is None or df.empty or len(df) < max(strategy['ema_period'], strategy['rsi_period'], strategy['stoch_k']):
        return None, None
    try:
        df.ta.ema(length=strategy['ema_period'], append=True, col_names=(f'EMA_{strategy["ema_period"]}',))
        df.ta.rsi(length=strategy['rsi_period'], append=True, col_names=(f'RSI_{strategy["rsi_period"]}',))
        df.ta.stoch(k=strategy['stoch_k'], d=strategy['stoch_d'], smooth_k=strategy['stoch_smooth_k'], append=True, col_names=(f'STOCHk_{strategy["stoch_k"]}_{strategy["stoch_d"]}_{strategy["stoch_smooth_k"]}', f'STOCHd_{strategy["stoch_k"]}_{strategy["stoch_d"]}_{strategy["stoch_smooth_k"]}'))
        df.ta.atr(length=14, append=True, col_names=('ATR_14',))
        return df.iloc[0], df.iloc[1]
    except Exception as e:
        logger.error(f"Error calculating indicators: {e}\n{traceback.format_exc()}")
        return None, None

def check_strategy(current_candle, prev_candle, strategy):
    if current_candle is None: return None
    signals = {'buy': [], 'sell': []}
    close_price = current_candle['close']
    ema_col = f'EMA_{strategy["ema_period"]}'
    if ema_col in current_candle and not pd.isna(current_candle[ema_col]):
        if close_price > current_candle[ema_col]: signals['buy'].append('EMA')
        if close_price < current_candle[ema_col]: signals['sell'].append('EMA')
    rsi_col = f'RSI_{strategy["rsi_period"]}'
    if rsi_col in current_candle and not pd.isna(current_candle[rsi_col]):
        if current_candle[rsi_col] < strategy['rsi_oversold']: signals['buy'].append('RSI')
        if current_candle[rsi_col] > strategy['rsi_overbought']: signals['sell'].append('RSI')
    stoch_k_col = f'STOCHk_{strategy["stoch_k"]}_{strategy["stoch_d"]}_{strategy["stoch_smooth_k"]}'
    if stoch_k_col in current_candle and not pd.isna(current_candle[stoch_k_col]):
        if current_candle[stoch_k_col] < strategy['stoch_oversold']: signals['buy'].append('Stochastic')
        if current_candle[stoch_k_col] > strategy['stoch_overbought']: signals['sell'].append('Stochastic')
    return signals

async def send_signal(context: ContextTypes.DEFAULT_TYPE, pair, direction, confidence, confirmations):
    is_otc = datetime.now(pytz.utc).weekday() >= 5
    pair_name = f"{pair} OTC" if is_otc else pair
    emoji = "⬆️" if direction == "صعود" else "⬇️"
    stars = "⭐" * confidence
    confirmations_text = ", ".join(confirmations)
    message_text = (f"🔔 **إشارة تداول نهائية** 🔔\n\n"
                    f"**الزوج:** {pair_name}\n"
                    f"**الاتجاه:** {direction} {emoji}\n"
                    f"**الثقة:** {stars} ({confidence} تأكيدات)\n"
                    f"**المؤشرات:** {confirmations_text}\n"
                    f"**الوقت:** الآن (افتتاح الشمعة الجديدة)\n"
                    f"**الفريم:** 5 دقائق")
    await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message_text, parse_mode='Markdown')

# --- 3. وظائف التحكم الرئيسية ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [["📊 تحليل السوق", "▶️ تشغيل"], ["⏸️ إيقاف", "⚙️ الإعدادات"], ["ℹ️ الحالة"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً بك في بوت النصيري (نسخة مستقرة). استخدم الأزرار للتحكم.", reply_markup=reply_markup)

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_pairs_str = ", ".join(bot_state['active_pairs']) if bot_state['active_pairs'] else "لا توجد أزواج قيد المراقبة."
    strategy = bot_state['strategy']
    status_message = (f"**📊 حالة بوت النصيري:**\n\n"
                    f"**حالة المراقبة:** {'✅ يعمل' if bot_state['is_running'] else '❌ متوقف'}\n"
                    f"**الأزواج قيد المراقبة:** {active_pairs_str}\n\n"
                    f"**⚙️ إعدادات الاستراتيجية الحالية:**\n"
                    f"- مستوى الثقة: {strategy['signal_threshold']} مؤشرات\n"
                    f"- إعدادات EMA: {strategy['ema_period']}\n"
                    f"- إعدادات RSI: {strategy['rsi_period']}, {strategy['rsi_oversold']}/{strategy['rsi_overbought']}\n"
                    f"- إعدادات Stochastic: {strategy['stoch_k']},{strategy['stoch_d']},{strategy['stoch_smooth_k']} | {strategy['stoch_oversold']}/{strategy['stoch_overbought']}\n"
                    f"- حساسية ATR: {strategy['atr_sensitivity']}")
    message_to_send = update.message if hasattr(update, 'message') else update
    await message_to_send.reply_text(status_message, parse_mode='Markdown')

async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_state['active_pairs']:
        await update.message.reply_text("❌ لا يمكن بدء المراقبة. الرجاء تحليل السوق واختيار زوج واحد على الأقل أولاً.")
        return
    if not bot_state['is_running']:
        bot_state['is_running'] = True
        await update.message.reply_text(f"✅ تم تشغيل المراقبة لـ: {', '.join(bot_state['active_pairs'])}")
    else:
        await update.message.reply_text("البوت يعمل بالفعل.")

async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_state['is_running']:
        bot_state['is_running'] = False
        await update.message.reply_text("⏸️ تم إيقاف المراقبة.")
    else:
        await update.message.reply_text("البوت متوقف بالفعل.")

# --- 4. منطق تحليل السوق التفاعلي ---
async def market_analysis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ جاري تحليل السوق لتحديد الأزواج النشطة...")
    active_pairs_found = []
    tasks = {pair: context.application.create_task(fetch_data(pair, context)) for pair in BASE_PAIRS}
    await asyncio.sleep(1) # Give tasks a moment to start
    results = {pair: await task for pair, task in tasks.items()}
    
    successful_fetches = 0
    volatility_report = "--- تقرير التقلب ---\n"

    for pair, df in results.items():
        if df is not None and not df.empty:
            successful_fetches += 1
            current_candle, _ = calculate_indicators(df, bot_state['strategy'])
            if current_candle is not None and 'ATR_14' in current_candle and not pd.isna(current_candle['ATR_14']) and 'close' in current_candle and current_candle['close'] > 0:
                volatility_ratio = current_candle['ATR_14'] / current_candle['close']
                is_active_str = "✅" if volatility_ratio > bot_state['strategy']['atr_sensitivity'] else "❌"
                volatility_report += f"{pair}: {is_active_str} | النسبة: {volatility_ratio:.6f} | العتبة: {bot_state['strategy']['atr_sensitivity']}\n"
                if volatility_ratio > bot_state['strategy']['atr_sensitivity']:
                    active_pairs_found.append(pair)
            else:
                 volatility_report += f"{pair}: ⚠️ بيانات غير كافية لحساب التقلب.\n"

    try:
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"```\n{volatility_report}\n```", parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Failed to send volatility report: {e}")


    if successful_fetches == 0:
        await msg.edit_text("فشل تحليل السوق: لم يتمكن البوت من جلب البيانات لأي زوج.")
        return

    if not active_pairs_found:
        await msg.edit_text("تحليل السوق اكتمل: لم يتم العثور على أزواج نشطة حاليًا (السوق هادئ).")
        return

    keyboard = [[InlineKeyboardButton(f"🔲 {pair}", callback_data=f"select_{pair}")] for pair in active_pairs_found]
    keyboard.append([InlineKeyboardButton("✅ بدء المراقبة", callback_data="confirm_selection_0")])
    await msg.edit_text("**تحليل السوق اكتمل.**\nاختر الأزواج للمراقبة:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def pair_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    pair = query.data.split('_')[1]
    if 'selected_pairs' not in context.user_data: context.user_data['selected_pairs'] = set()
    if pair in context.user_data['selected_pairs']: context.user_data['selected_pairs'].remove(pair)
    else: context.user_data['selected_pairs'].add(pair)
    old_keyboard = query.message.reply_markup.inline_keyboard
    new_keyboard = []
    for row in old_keyboard:
        button_data = row[0].callback_data
        if button_data.startswith("select_"):
            p = button_data.split('_')[1]
            if p in context.user_data['selected_pairs']: new_keyboard.append([InlineKeyboardButton(f"✅ {p}", callback_data=button_data)])
            else: new_keyboard.append([InlineKeyboardButton(f"🔲 {p}", callback_data=button_data)])
        else: new_keyboard.append(row)
    await query.edit_message_text(query.message.text, reply_markup=InlineKeyboardMarkup(new_keyboard))

async def confirm_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    selected_pairs = context.user_data.get('selected_pairs', set())
    if not selected_pairs: await query.answer("لم تختر أي زوج!", show_alert=True); return
    bot_state['active_pairs'] = list(selected_pairs)
    bot_state['is_running'] = True
    message = f"✅ تم تحديث القائمة وبدء المراقبة.\n\n**الأزواج قيد المراقبة:**\n" + "\n".join(f"- {p}" for p in bot_state['active_pairs'])
    await query.edit_message_text(message, parse_mode='Markdown')
    context.user_data['selected_pairs'] = set()

async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("مستوى الثقة", callback_data="set_threshold")], [InlineKeyboardButton("إعدادات EMA", callback_data="set_ema")], [InlineKeyboardButton("إعدادات RSI", callback_data="set_rsi")], [InlineKeyboardButton("إعدادات Stochastic", callback_data="set_stoch")], [InlineKeyboardButton("حساسية ATR", callback_data="set_atr")], [InlineKeyboardButton("🔄 استعادة الإعدادات الافتراضية", callback_data="reset_strategy")]]
    await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=InlineKeyboardMarkup(keyboard))

async def ask_for_input(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_key, prompt_message):
    query = update.callback_query
    if query: await query.answer()
    bot_state['awaiting_input'] = setting_key
    if bot_state.get('message_to_delete'):
        try: await context.bot.delete_message(chat_id=query.message.chat.id, message_id=bot_state['message_to_delete'])
        except: pass
    msg = await (query.message if query else update.message).reply_text(f"{prompt_message}\n\nلإلغاء العملية، أرسل /cancel")
    bot_state['message_to_delete'] = msg.message_id

async def set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE): await ask_for_input(update, context, 'signal_threshold', "أرسل **مستوى الثقة** (رقم، مثال: 2):")
async def set_ema(update: Update, context: ContextTypes.DEFAULT_TYPE): await ask_for_input(update, context, 'ema_period', "أرسل **فترة EMA** (رقم، مثال: 20):")
async def set_rsi(update: Update, context: ContextTypes.DEFAULT_TYPE): await ask_for_input(update, context, 'rsi_settings', "أرسل **إعدادات RSI** (الفترة, تشبع البيع, تشبع الشراء) مفصولة بفاصلة. مثال: 14,30,70")
async def set_stoch(update: Update, context: ContextTypes.DEFAULT_TYPE): await ask_for_input(update, context, 'stoch_settings', "أرسل **إعدادات Stochastic** (k, d, smooth_k, تشبع البيع, تشبع الشراء) مفصولة بفاصلة. مثال: 14,3,3,20,80")
async def set_atr(update: Update, context: ContextTypes.DEFAULT_TYPE): await ask_for_input(update, context, 'atr_sensitivity', "أرسل **حساسية ATR** (رقم عشري، مثال: 0.0005):")

async def reset_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot_state['strategy'] = DEFAULT_STRATEGY.copy()
    await query.answer("✅ تم استعادة الإعدادات الافتراضية!", show_alert=True)
    await show_status(query.message, context)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text; setting_key = bot_state.get('awaiting_input')
    if not setting_key:
        if user_input == "📊 تحليل السوق": await market_analysis_handler(update, context)
        elif user_input == "▶️ تشغيل": await start_bot(update, context)
        elif user_input == "⏸️ إيقاف": await stop_bot(update, context)
        elif user_input == "⚙️ الإعدادات": await settings_handler(update, context)
        elif user_input == "ℹ️ الحالة": await show_status(update, context)
        else: await update.message.reply_text("أمر غير مفهوم. استخدم الأزرار.")
        return
    await context.bot.delete_message(chat_id=update.message.chat.id, message_id=update.message.message_id)
    if bot_state.get('message_to_delete'):
        try: await context.bot.delete_message(chat_id=update.message.chat.id, message_id=bot_state['message_to_delete'])
        except: pass
    bot_state['message_to_delete'] = None
    try:
        if setting_key == 'signal_threshold': bot_state['strategy']['signal_threshold'] = int(user_input)
        elif setting_key == 'ema_period': bot_state['strategy']['ema_period'] = int(user_input)
        elif setting_key == 'atr_sensitivity': bot_state['strategy']['atr_sensitivity'] = float(user_input)
        elif setting_key == 'rsi_settings':
            p, os, ob = map(int, user_input.split(',')); bot_state['strategy'].update({'rsi_period': p, 'rsi_oversold': os, 'rsi_overbought': ob})
        elif setting_key == 'stoch_settings':
            k, d, sk, os, ob = map(int, user_input.split(',')); bot_state['strategy'].update({'stoch_k': k, 'stoch_d': d, 'stoch_smooth_k': sk, 'stoch_oversold': os, 'stoch_overbought': ob})
        await update.message.reply_text(f"✅ تم تحديث {setting_key} بنجاح.")
    except (ValueError, IndexError) as e: await update.message.reply_text(f"❌ إدخال خاطئ. الخطأ: {e}")
    bot_state['awaiting_input'] = None
    await show_status(update, context)

async def cancel_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state['awaiting_input'] = None
    if bot_state.get('message_to_delete'):
        try: await context.bot.delete_message(chat_id=update.message.chat.id, message_id=bot_state['message_to_delete'])
        except: pass
        bot_state['message_to_delete'] = None
    await update.message.reply_text("تم إلغاء العملية.")

# --- 6. مهمة التحقق من الإشارات الرئيسية ---
async def check_signals_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not bot_state['is_running'] or not bot_state['active_pairs']: return
    async def run_check():
        tasks = {pair: context.application.create_task(fetch_data(pair, context)) for pair in bot_state['active_pairs']}
        await asyncio.sleep(1)
        results = {pair: await task for pair, task in tasks.items()}
        for pair, df in results.items():
            if df is not None:
                current_candle, prev_candle = calculate_indicators(df, bot_state['strategy'])
                if current_candle is not None:
                    signals = check_strategy(current_candle, prev_candle, bot_state['strategy'])
                    buy_conf = len(signals['buy']); sell_conf = len(signals['sell'])
                    candle_time = current_candle['datetime']
                    if pair not in bot_state['last_final_signal_time'] or bot_state['last_final_signal_time'].get(pair) < candle_time:
                        if buy_conf >= bot_state['strategy']['signal_threshold']:
                            await send_signal(context, pair, "صعود", buy_conf, signals['buy']); bot_state['last_final_signal_time'][pair] = candle_time
                        elif sell_conf >= bot_state['strategy']['signal_threshold']:
                            await send_signal(context, pair, "هبوط", sell_conf, signals['sell']); bot_state['last_final_signal_time'][pair] = candle_time
    current_time = datetime.now(pytz.utc)
    if current_time.second in [3, 4, 5] and current_time.minute % 5 == 0:
        await run_check()

# --- 7. إعداد وتشغيل خادم الويب ---
app = Flask(__name__)
@app.route('/')
def index(): return "Bot is running!"
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- 8. الوظيفة الرئيسية (Main Function) ---
def main() -> None:
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_KEY_1]):
        logger.critical("FATAL: Missing one or more environment variables.")
        return
    flask_thread = threading.Thread(target=run_flask); flask_thread.daemon = True; flask_thread.start()
    logger.info("Web server started.")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_input_handler))
    application.add_handler(CallbackQueryHandler(pair_selection_handler, pattern='^select_'))
    application.add_handler(CallbackQueryHandler(confirm_selection_handler, pattern='^confirm_selection_'))
    application.add_handler(CallbackQueryHandler(set_threshold, pattern='^set_threshold$'))
    application.add_handler(CallbackQueryHandler(set_ema, pattern='^set_ema$'))
    application.add_handler(CallbackQueryHandler(set_rsi, pattern='^set_rsi$'))
    application.add_handler(CallbackQueryHandler(set_stoch, pattern='^set_stoch$'))
    application.add_handler(CallbackQueryHandler(set_atr, pattern='^set_atr$'))
    application.add_handler(CallbackQueryHandler(reset_strategy, pattern='^reset_strategy$'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    application.job_queue.run_repeating(check_signals_task, interval=1, first=5)
    logger.info("Starting Telegram bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
