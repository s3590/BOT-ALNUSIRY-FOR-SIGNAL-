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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
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
    'atr_sensitivity': 0.0002,
}

TIMEFRAME = "5min"
OUTPUT_SIZE = 300  # <-- تم التعديل: زيادة حجم البيانات للاحتياط
BASE_PAIRS = ["EUR/USD", "AUD/USD", "USD/CAD", "USD/CHF", "USD/JPY", "EUR/JPY", "AUD/JPY", "CAD/JPY", "CHF/JPY", "EUR/AUD", "EUR/CAD", "EUR/CHF", "AUD/CAD", "AUD/CHF", "CAD/CHF"]

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

bot_state = {
    'is_running': False, 'active_pairs': [], 'strategy': DEFAULT_STRATEGY.copy(),
    'api_key_index': 0, 'last_final_signal_time': {},
    'awaiting_input': None, 'message_to_delete': None,
}

# --- 2. وظائف مساعدة ---
def get_next_api_key():
    if not API_KEYS: return None
    key = API_KEYS[bot_state['api_key_index']]
    bot_state['api_key_index'] = (bot_state['api_key_index'] + 1) % len(API_KEYS)
    return key

async def fetch_data(pair, context):
    api_key = get_next_api_key()
    if not api_key:
        logger.error("لا توجد مفاتيح API متاحة.")
        return None
    try:
        td = TDClient(apikey=api_key)
        ts = td.time_series(symbol=pair, interval=TIMEFRAME, outputsize=OUTPUT_SIZE, timezone="UTC")
        if ts is None or ts.empty:
            logger.warning(f"لم يتم استلام بيانات للزوج {pair}.")
            return None
        # عكس البيانات بحيث تكون الأحدث في النهاية
        df = ts.as_pandas().iloc[::-1].reset_index()
        return df
    except Exception as e:
        logger.error(f"فشل جلب البيانات للزوج {pair}: {e}")
        return None

# --- 3. حساب المؤشرات (مع الإصلاح الكامل) ---
def calculate_indicators(df, strategy):
    try:
        # التحقق من أن البيانات كافية للحسابات
        required_length = max(strategy['ema_period'], strategy['rsi_period'], strategy['stoch_k'], 14) + 5 # إضافة هامش أمان
        if df is None or df.empty or len(df) < required_length:
            logger.warning(f"بيانات غير كافية لحساب المؤشرات. الطول المطلوب: {required_length}, الطول الحالي: {len(df) if df is not None else 0}")
            return None, None

        # حساب المؤشرات باستخدام pandas_ta
        df.ta.ema(length=strategy['ema_period'], append=True)
        df.ta.rsi(length=strategy['rsi_period'], append=True)
        df.ta.atr(length=14, append=True)

        # ***** الإصلاح الرئيسي هنا: تحديد 3 أسماء للأعمدة بشكل صحيح *****
        stoch_k = strategy['stoch_k']
        stoch_d = strategy['stoch_d']
        smooth_k = strategy['stoch_smooth_k']
        stoch_col_names = [
            f'STOCHk_{stoch_k}_{stoch_d}_{smooth_k}',
            f'STOCHd_{stoch_k}_{stoch_d}_{smooth_k}',
            f'STOCHh_{stoch_k}_{stoch_d}_{smooth_k}' # العمود الثالث المطلوب
        ]
        df.ta.stoch(k=stoch_k, d=stoch_d, smooth_k=smooth_k, append=True, col_names=stoch_col_names)

        # التأكد من أن جميع الأعمدة المطلوبة موجودة
        all_cols = list(df.columns)
        if not all(col in all_cols for col in stoch_col_names):
            logger.error("فشل إنشاء أعمدة الاستوكاستك.")
            return None, None

        # ***** الإصلاح الثاني: إرجاع الشموع الصحيحة (الأحدث في النهاية) *****
        # df.iloc[-1] هي الشمعة الحالية (الأحدث)
        # df.iloc[-2] هي الشمعة السابقة
        return df.iloc[-1], df.iloc[-2]

    except Exception as e:
        logger.error(f"خطأ فادح في calculate_indicators: {e}\n{traceback.format_exc()}")
        return None, None


# --- 4. منطق تحليل السوق ---
async def analyze_single_pair(pair, context, strategy):
    df = await fetch_data(pair, context)
    if df is None:
        return pair, None, "⚠️ فشل جلب البيانات."

    # نستخدم فقط الشمعة الحالية لتحليل التقلب
    current_candle, _ = calculate_indicators(df, strategy)
    if current_candle is None:
        return pair, None, "⚠️ فشل حساب المؤشرات."

    # التحقق من وجود الأعمدة المطلوبة وأنها ليست فارغة
    if 'ATR_14' in current_candle and not pd.isna(current_candle['ATR_14']) and 'close' in current_candle and current_candle['close'] > 0:
        volatility_ratio = current_candle['ATR_14'] / current_candle['close']
        is_active = volatility_ratio > strategy['atr_sensitivity']
        report_line = f"{'✅' if is_active else '❌'} | النسبة: {volatility_ratio:.6f}"
        return pair, is_active, report_line
    else:
        # رسالة خطأ أكثر تفصيلاً
        missing_info = []
        if 'ATR_14' not in current_candle or pd.isna(current_candle['ATR_14']):
            missing_info.append("ATR")
        if 'close' not in current_candle or current_candle['close'] <= 0:
            missing_info.append("Close price")
        return pair, None, f"⚠️ فشل حساب التقلب (بيانات مفقودة: {', '.join(missing_info)})"

async def market_analysis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ جاري تحليل السوق لجميع الأزواج...")
    strategy = bot_state['strategy']
    tasks = [analyze_single_pair(pair, context, strategy) for pair in BASE_PAIRS]
    results = await asyncio.gather(*tasks)

    active_pairs_found = []
    volatility_report = "--- تقرير التقلب ---\n"
    for pair, is_active, report_line in results:
        volatility_report += f"{pair}: {report_line}\n"
        if is_active:
            active_pairs_found.append(pair)
    
    # استخدام MarkdownV2 يتطلب تهريب الأحرف الخاصة، لذا سنستخدم Markdown العادي لتبسيط الأمر
    await msg.edit_text(f"```\n{volatility_report}\n```", parse_mode='Markdown')

    if not active_pairs_found:
        await context.bot.send_message(chat_id=update.message.chat_id, text="تحليل السوق اكتمل: لم يتم العثور على أزواج نشطة حسب حساسية ATR الحالية.")
        return

    keyboard = [[InlineKeyboardButton(f"🔲 {pair}", callback_data=f"select_{pair}")] for pair in active_pairs_found]
    keyboard.append([InlineKeyboardButton("✅ بدء المراقبة", callback_data="confirm_selection")])
    await context.bot.send_message(
        chat_id=update.message.chat_id,
        text="**تحليل السوق اكتمل.**\nاختر الأزواج للمراقبة من القائمة أدناه:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# --- 5. منطق فحص الإشارة والإرسال ---
def check_strategy(current_candle, prev_candle, strategy):
    if current_candle is None or prev_candle is None: return None
    signals = {'buy': [], 'sell': []}
    close_price = current_candle['close']

    # EMA Signal
    ema_col = f'EMA_{strategy["ema_period"]}'
    if ema_col in current_candle and not pd.isna(current_candle[ema_col]):
        if close_price > current_candle[ema_col]: signals['buy'].append('EMA')
        elif close_price < current_candle[ema_col]: signals['sell'].append('EMA')

    # RSI Signal
    rsi_col = f'RSI_{strategy["rsi_period"]}'
    if rsi_col in current_candle and not pd.isna(current_candle[rsi_col]):
        if current_candle[rsi_col] < strategy['rsi_oversold']: signals['buy'].append('RSI')
        elif current_candle[rsi_col] > strategy['rsi_overbought']: signals['sell'].append('RSI')

    # Stochastic Signal
    stoch_k_col = f'STOCHk_{strategy["stoch_k"]}_{strategy["stoch_d"]}_{strategy["stoch_smooth_k"]}'
    if stoch_k_col in current_candle and not pd.isna(current_candle[stoch_k_col]):
        if current_candle[stoch_k_col] < strategy['stoch_oversold']: signals['buy'].append('Stochastic')
        elif current_candle[stoch_k_col] > strategy['stoch_overbought']: signals['sell'].append('Stochastic')

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
                    f"**الوقت:** الآن\n"
                    f"**الفريم:** 5 دقائق")
    await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message_text, parse_mode='Markdown')

# --- 6. الأوامر ومعالجات التلغرام ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [["📊 تحليل السوق", "▶️ تشغيل"], ["⏸️ إيقاف", "⚙️ الإعدادات"], ["ℹ️ الحالة"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("أهلاً بك في بوت التداول. استخدم الأزرار للتحكم.", reply_markup=reply_markup)

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_pairs_str = ", ".join(bot_state['active_pairs']) if bot_state['active_pairs'] else "لا توجد أزواج قيد المراقبة."
    strategy = bot_state['strategy']
    status_message = (f"**📊 حالة البوت:**\n\n"
                    f"**المراقبة:** {'✅ يعمل' if bot_state['is_running'] else '❌ متوقف'}\n"
                    f"**الأزواج المحددة:** {active_pairs_str}\n\n"
                    f"**⚙️ الإعدادات الحالية:**\n"
                    f"- مستوى الثقة للإشارة: {strategy['signal_threshold']}\n"
                    f"- حساسية ATR للتقلب: {strategy['atr_sensitivity']}")
    
    target_message = update.message or update.callback_query.message
    await target_message.reply_text(status_message, parse_mode='Markdown')


async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_state['active_pairs']:
        await update.message.reply_text("❌ يجب اختيار زوج واحد على الأقل للمراقبة. ابدأ بـ 'تحليل السوق' أولاً.")
        return
    if not bot_state['is_running']:
        bot_state['is_running'] = True
        await update.message.reply_text(f"✅ تم تشغيل المراقبة للأزواج: {', '.join(bot_state['active_pairs'])}")
    else:
        await update.message.reply_text("ℹ️ البوت يعمل بالفعل.")

async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_state['is_running']:
        bot_state['is_running'] = False
        await update.message.reply_text("⏸️ تم إيقاف المراقبة.")
    else:
        await update.message.reply_text("ℹ️ البوت متوقف بالفعل.")

async def pair_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair = query.data.split('_')[1]

    if 'selected_pairs' not in context.user_data:
        context.user_data['selected_pairs'] = set()

    if pair in context.user_data['selected_pairs']:
        context.user_data['selected_pairs'].remove(pair)
    else:
        context.user_data['selected_pairs'].add(pair)

    selected = context.user_data['selected_pairs']
    old_keyboard = query.message.reply_markup.inline_keyboard
    new_keyboard = []
    for row in old_keyboard:
        button = row[0]
        if button.callback_data.startswith("select_"):
            p = button.callback_data.split('_')[1]
            text = f"✅ {p}" if p in selected else f"🔲 {p}"
            new_keyboard.append([InlineKeyboardButton(text, callback_data=button.callback_data)])
        else: # زر التأكيد
            new_keyboard.append(row)
            
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))

async def confirm_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected_pairs = context.user_data.get('selected_pairs', set())

    if not selected_pairs:
        await query.answer("لم تختر أي زوج!", show_alert=True)
        return

    bot_state['active_pairs'] = sorted(list(selected_pairs)) # ترتيب أبجدي
    bot_state['is_running'] = True
    
    message = f"✅ تم بدء المراقبة.\n\n**الأزواج المحددة:**\n" + "\n".join(f"- {p}" for p in bot_state['active_pairs'])
    await query.edit_message_text(message, parse_mode='Markdown', reply_markup=None)
    
    context.user_data.pop('selected_pairs', None)

async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("حساسية ATR", callback_data="set_atr")],
        [InlineKeyboardButton("🔄 استعادة الافتراضي", callback_data="reset_strategy")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_main")]
    ]
    await update.message.reply_text("اختر الإعداد للتعديل:", reply_markup=InlineKeyboardMarkup(keyboard))

async def ask_for_input(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_key, prompt_message):
    query = update.callback_query
    if query: await query.answer()
    
    bot_state['awaiting_input'] = setting_key
    message_to_send = query.message if query else update.message
    
    msg = await message_to_send.reply_text(f"{prompt_message}\n\n*لإلغاء العملية، أرسل /cancel*", parse_mode='Markdown')
    bot_state['message_to_delete'] = msg.message_id

async def set_atr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_for_input(update, context, 'atr_sensitivity', "أرسل القيمة الجديدة لـ **حساسية ATR** (مثال: `0.0005`):")

async def reset_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot_state['strategy'] = DEFAULT_STRATEGY.copy()
    await query.answer("✅ تم استعادة الإعدادات الافتراضية!", show_alert=True)
    await show_status(update, context)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    setting_key = bot_state.get('awaiting_input')

    if not setting_key:
        handler_map = {
            "📊 تحليل السوق": market_analysis_handler,
            "▶️ تشغيل": start_bot,
            "⏸️ إيقاف": stop_bot,
            "⚙️ الإعدادات": settings_handler,
            "ℹ️ الحالة": show_status,
        }
        handler = handler_map.get(user_input)
        if handler:
            await handler(update, context)
        return

    # إذا كان البوت ينتظر إدخالاً
    await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
    if bot_state.get('message_to_delete'):
        try:
            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=bot_state['message_to_delete'])
        except Exception:
            pass # تجاهل الخطأ إذا تم حذف الرسالة بالفعل

    try:
        value = float(user_input)
        if setting_key == 'atr_sensitivity':
            bot_state['strategy']['atr_sensitivity'] = value
        
        await update.message.reply_text(f"✅ تم تحديث `{setting_key}` إلى `{value}`.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ إدخال خاطئ. يرجى إرسال رقم صحيح.")
    
    bot_state['awaiting_input'] = None
    bot_state['message_to_delete'] = None
    await show_status(update, context)

async def cancel_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state['awaiting_input'] = None
    if bot_state.get('message_to_delete'):
        try:
            await context.bot.delete_message(chat_id=update.message.chat.id, message_id=bot_state['message_to_delete'])
        except Exception:
            pass
    await update.message.reply_text("تم إلغاء العملية.")

# --- 7. المهمة الدورية لفحص الإشارات ---
async def check_signals_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not bot_state['is_running'] or not bot_state['active_pairs']:
        return

    current_time = datetime.now(pytz.utc)
    # تشغيل المهمة في بداية كل شمعة 5 دقائق (الثواني الأولى)
    if not (current_time.minute % 5 == 0 and current_time.second < 10):
        return

    logger.info(f"بدء فحص الإشارات لـ: {bot_state['active_pairs']}")

    async def run_check_for_pair(pair):
        candle_start_time = current_time.replace(second=0, microsecond=0)
        
        # التأكد من عدم إرسال إشارة لنفس الشمعة أكثر من مرة
        if bot_state['last_final_signal_time'].get(pair) == candle_start_time:
            return

        df = await fetch_data(pair, context)
        if df is not None:
            current_candle, prev_candle = calculate_indicators(df, bot_state['strategy'])
            if current_candle is not None and prev_candle is not None:
                signals = check_strategy(current_candle, prev_candle, bot_state['strategy'])
                buy_conf = len(signals.get('buy', []))
                sell_conf = len(signals.get('sell', []))

                if buy_conf >= bot_state['strategy']['signal_threshold']:
                    await send_signal(context, pair, "صعود", buy_conf, signals['buy'])
                    bot_state['last_final_signal_time'][pair] = candle_start_time
                elif sell_conf >= bot_state['strategy']['signal_threshold']:
                    await send_signal(context, pair, "هبوط", sell_conf, signals['sell'])
                    bot_state['last_final_signal_time'][pair] = candle_start_time
    
    tasks = [run_check_for_pair(pair) for pair in bot_state['active_pairs']]
    await asyncio.gather(*tasks)

# --- 8. إعداد وتشغيل البوت ---
app = Flask(__name__)
@app.route('/')
def index():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def main() -> None:
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_KEYS]):
        logger.critical("FATAL: متغيرات البيئة الأساسية مفقودة (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_KEY_1).")
        return

    # تشغيل Flask في خيط منفصل للحفاظ على استجابة Render
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_input_handler))
    application.add_handler(CallbackQueryHandler(pair_selection_handler, pattern='^select_'))
    application.add_handler(CallbackQueryHandler(confirm_selection_handler, pattern='^confirm_selection'))
    application.add_handler(CallbackQueryHandler(set_atr, pattern='^set_atr$'))
    application.add_handler(CallbackQueryHandler(reset_strategy, pattern='^reset_strategy$'))
    application.add_handler(CallbackQueryHandler(lambda u,c: start(u.callback_query,c), pattern='^back_to_main$')) # زر الرجوع
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    # جدولة المهمة الدورية
    application.job_queue.run_repeating(check_signals_task, interval=10, first=5) # فحص كل 10 ثوانٍ

    # تشغيل البوت
    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
