
import os, time, sqlite3, asyncio, logging, re
from collections import defaultdict, deque
from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "data.db")
DEFAULT_MUTE = int(os.getenv("DEFAULT_MUTE_SECONDS", "300"))
MAX_WARNINGS = int(os.getenv("MAX_WARNINGS", "3"))
FLOOD_LIMIT = int(os.getenv("FLOOD_LIMIT", "6"))
FLOOD_WINDOW = int(os.getenv("FLOOD_WINDOW", "8"))
LINK_BLOCK = os.getenv("BLOCK_LINKS", "true").lower() == "true"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("protection-bot")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS settings(
    chat_id INTEGER PRIMARY KEY,
    links INTEGER DEFAULT 1,
    flood INTEGER DEFAULT 1,
    welcome INTEGER DEFAULT 0
)""")
db.execute("""CREATE TABLE IF NOT EXISTS warnings(
    chat_id INTEGER,
    user_id INTEGER,
    count INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id,user_id)
)""")
db.commit()

spam = defaultdict(lambda: defaultdict(deque))
locks = defaultdict(asyncio.Lock)

def setting(chat_id, key, default=1):
    row = db.execute(f"SELECT {key} FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        db.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
        db.commit()
        return default
    return int(row[0])

def set_setting(chat_id, key, value):
    db.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
    db.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (int(value), chat_id))
    db.commit()

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id=None):
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return True
    uid = user_id or update.effective_user.id
    try:
        m = await chat.get_member(uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False

async def bot_is_admin(update: Update):
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return True
    try:
        m = await chat.get_member(context_bot_id(update))
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

def context_bot_id(update):
    # Application bot id is not directly stored on Update; caller should use
    # get_me when needed. This helper is only a safe fallback.
    return update.effective_user.id if update.effective_user else 0

async def require_admin(update, context):
    if not await is_admin(update, context):
        await update.effective_message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ مصنع بوتات الحماية\n\n"
        "البوت يوفر حماية للمجموعات من الروابط والفلود والرسائل المزعجة.\n"
        "أضفني إلى المجموعة ثم امنحني صلاحيات حذف الرسائل وتقييد الأعضاء.\n\n"
        "الأوامر:\n"
        "/help — المساعدة\n"
        "/settings — إعدادات الحماية\n"
        "/links_on /links_off\n"
        "/flood_on /flood_off\n"
        "/warn — تحذير العضو بالرد على رسالته\n"
        "/mute — كتم العضو بالرد\n"
        "/unmute — فك الكتم بالرد\n"
        "/ban — حظر العضو بالرد\n"
        "/unban — فك الحظر بالرد"
    )

async def help_cmd(update, context):
    await start(update, context)

async def settings_cmd(update, context):
    c=update.effective_chat.id
    await update.message.reply_text(
        f"🛡️ إعدادات المجموعة\n"
        f"الروابط: {'ON' if setting(c,'links') else 'OFF'}\n"
        f"الفlood: {'ON' if setting(c,'flood') else 'OFF'}\n"
        f"الترحيب: {'ON' if setting(c,'welcome') else 'OFF'}"
    )

async def toggle(update, context, key, value):
    if not await require_admin(update, context): return
    set_setting(update.effective_chat.id, key, value)
    await update.message.reply_text("✅ تم تحديث الإعداد.")

async def links_on(u,c): await toggle(u,c,"links",1)
async def links_off(u,c): await toggle(u,c,"links",0)
async def flood_on(u,c): await toggle(u,c,"flood",1)
async def flood_off(u,c): await toggle(u,c,"flood",0)

async def target_from_reply(update):
    if not update.message or not update.message.reply_to_message:
        return None
    return update.message.reply_to_message.from_user

async def warn(update, context):
    if not await require_admin(update, context): return
    user = await target_from_reply(update)
    if not user:
        await update.message.reply_text("↩️ استخدم /warn بالرد على رسالة العضو.")
        return
    if await is_admin(update, context, user.id):
        await update.message.reply_text("❌ لا يمكن تحذير مشرف.")
        return
    chat_id=update.effective_chat.id
    row=db.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id,user.id)).fetchone()
    count=(row[0] if row else 0)+1
    db.execute("INSERT OR REPLACE INTO warnings VALUES(?,?,?)",(chat_id,user.id,count))
    db.commit()
    if count >= MAX_WARNINGS:
        try:
            until=int(time.time())+DEFAULT_MUTE
            await context.bot.restrict_chat_member(
                chat_id, user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
            db.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id,user.id))
            db.commit()
            await update.message.reply_text(f"🔇 {user.mention_html()} تم كتمه بعد {MAX_WARNINGS} تحذيرات.", parse_mode="HTML")
        except TelegramError as e:
            await update.message.reply_text(f"⚠️ تعذر الكتم: {e}")
    else:
        await update.message.reply_text(f"⚠️ تحذير {count}/{MAX_WARNINGS} لـ {user.mention_html()}.", parse_mode="HTML")

async def mute(update, context):
    if not await require_admin(update, context): return
    user=await target_from_reply(update)
    if not user:
        await update.message.reply_text("↩️ استخدم /mute بالرد على رسالة العضو.")
        return
    if await is_admin(update, context, user.id):
        await update.message.reply_text("❌ لا يمكن كتم مشرف.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time())+DEFAULT_MUTE
        )
        await update.message.reply_text(f"🔇 تم كتم {user.mention_html()} لمدة {DEFAULT_MUTE//60} دقيقة.", parse_mode="HTML")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ تعذر تنفيذ الكتم: {e}")

async def unmute(update, context):
    if not await require_admin(update, context): return
    user=await target_from_reply(update)
    if not user:
        await update.message.reply_text("↩️ استخدم /unmute بالرد على رسالة العضو.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, user.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_other_messages=True,
                                        can_add_web_page_previews=True)
        )
        await update.message.reply_text("🔊 تم فك الكتم.")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ تعذر التنفيذ: {e}")

async def ban(update, context):
    if not await require_admin(update, context): return
    user=await target_from_reply(update)
    if not user:
        await update.message.reply_text("↩️ استخدم /ban بالرد على رسالة العضو.")
        return
    if await is_admin(update, context, user.id):
        await update.message.reply_text("❌ لا يمكن حظر مشرف.")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user.id)
        await update.message.reply_text("🚫 تم حظر العضو.")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ تعذر الحظر: {e}")

async def unban(update, context):
    if not await require_admin(update, context): return
    user=await target_from_reply(update)
    if not user:
        await update.message.reply_text("↩️ استخدم /unban بالرد على رسالة العضو.")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, user.id, only_if_banned=True)
        await update.message.reply_text("✅ تم فك الحظر.")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ تعذر فك الحظر: {e}")

URL_RE=re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|discord\.gg/)", re.I)

async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    chat=update.effective_chat
    user=update.effective_user
    if not msg or not chat or not user or chat.type=="private":
        return
    if await is_admin(update, context, user.id):
        return

    if setting(chat.id,"links",1) and msg.text and URL_RE.search(msg.text):
        try:
            await msg.delete()
            await context.bot.send_message(chat.id, f"🔗 تم حذف رابط من {user.mention_html()}.", parse_mode="HTML")
        except TelegramError:
            pass
        return

    if setting(chat.id,"flood",1):
        q=spam[chat.id][user.id]
        now=time.monotonic()
        q.append(now)
        while q and now-q[0] > FLOOD_WINDOW:
            q.popleft()
        if len(q) >= FLOOD_LIMIT:
            q.clear()
            try:
                await context.bot.restrict_chat_member(
                    chat.id,user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=int(time.time())+DEFAULT_MUTE
                )
                await msg.reply_text(f"🛡️ تم كتم {user.mention_html()} تلقائياً بسبب الفلود.", parse_mode="HTML")
            except TelegramError:
                pass

async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member: return
    c=update.effective_chat.id
    if not setting(c,"welcome",0): return
    old=update.chat_member.old_chat_member
    new=update.chat_member.new_chat_member
    if old.status in ("left","kicked") and new.status in ("member","restricted"):
        u=new.user
        try:
            await context.bot.send_message(c, f"👋 أهلاً {u.mention_html()} في المجموعة.", parse_mode="HTML")
        except TelegramError:
            pass

def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود. انسخ .env.example إلى .env وضع توكن البوت.")
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(CommandHandler("settings",settings_cmd))
    app.add_handler(CommandHandler("links_on",links_on))
    app.add_handler(CommandHandler("links_off",links_off))
    app.add_handler(CommandHandler("flood_on",flood_on))
    app.add_handler(CommandHandler("flood_off",flood_off))
    app.add_handler(CommandHandler("warn",warn))
    app.add_handler(CommandHandler("mute",mute))
    app.add_handler(CommandHandler("unmute",unmute))
    app.add_handler(CommandHandler("ban",ban))
    app.add_handler(CommandHandler("unban",unban))
    app.add_handler(ChatMemberHandler(new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate_message))
    log.info("Protection bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
