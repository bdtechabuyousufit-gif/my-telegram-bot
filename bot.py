import os, re, random, time, json
from datetime import datetime
from telegram import ReplyKeyboardMarkup, KeyboardButton, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import config

# ════════════════════════════════════════════════════════
#                      GLOBALS
# ════════════════════════════════════════════════════════
USERS             = set()
ADMINS            = set(config.ADMIN_IDS)
BANNED            = set()
USER_STATS        = {}
USER_LAST_NUMBERS = {}
USER_LAST_ACTIVE  = {}
USER_HISTORY      = {}   # uid → [{service, number, time}]
OTP_LOG           = []
UPLOAD_MODE       = {}   # uid → service_name
NUMBER_LIMIT      = 4
DATA_FILE         = "user_data.json"

# ডিফল্ট সার্ভিস তালিকা
DEFAULT_SERVICES = ["WhatsApp", "Telegram", "Facebook"]
SERVICES         = list(DEFAULT_SERVICES)

os.makedirs(config.NUMBER_DIR, exist_ok=True)
os.makedirs(config.SEEN_DIR,   exist_ok=True)

# ════════════════════════════════════════════════════════
#                   SAVE / LOAD
# ════════════════════════════════════════════════════════
def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump({
            "USER_STATS":        USER_STATS,
            "USER_LAST_NUMBERS": USER_LAST_NUMBERS,
            "USER_LAST_ACTIVE":  USER_LAST_ACTIVE,
            "USER_HISTORY":      USER_HISTORY,
            "BANNED":            list(BANNED),
            "ADMINS":            list(ADMINS),
            "USERS":             list(USERS),
            "OTP_LOG":           OTP_LOG[-50:],
            "NUMBER_LIMIT":      NUMBER_LIMIT,
            "SERVICES":          SERVICES,
        }, f, ensure_ascii=False, indent=2)

def load_data():
    global USER_STATS, USER_LAST_NUMBERS, USER_LAST_ACTIVE
    global BANNED, ADMINS, USERS, OTP_LOG, NUMBER_LIMIT, SERVICES, USER_HISTORY
    if not os.path.exists(DATA_FILE):
        return
    with open(DATA_FILE) as f:
        d = json.load(f)
    USER_STATS        = d.get("USER_STATS", {})
    USER_LAST_NUMBERS = d.get("USER_LAST_NUMBERS", {})
    USER_LAST_ACTIVE  = d.get("USER_LAST_ACTIVE", {})
    USER_HISTORY      = d.get("USER_HISTORY", {})
    BANNED            = set(d.get("BANNED", []))
    ADMINS.update(d.get("ADMINS", []))
    USERS             = set(d.get("USERS", []))
    OTP_LOG           = d.get("OTP_LOG", [])
    NUMBER_LIMIT      = d.get("NUMBER_LIMIT", 4)
    SERVICES          = d.get("SERVICES", list(DEFAULT_SERVICES))

# ════════════════════════════════════════════════════════
#                 NUMBER UTILITIES
# ════════════════════════════════════════════════════════
def service_dir(service):
    path = os.path.join(config.NUMBER_DIR, service)
    os.makedirs(path, exist_ok=True)
    return path

def service_seen_dir(service):
    path = os.path.join(config.SEEN_DIR, service)
    os.makedirs(path, exist_ok=True)
    return path

def get_countries(service):
    d = service_dir(service)
    return [
        f[:-4] for f in os.listdir(d)
        if f.endswith(".txt") and not f.endswith("_Backup.txt")
    ]

def get_numbers(service, country):
    p = os.path.join(service_dir(service), f"{country}.txt")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [x.strip() for x in f if x.strip()]

def get_seen(service, country):
    p = os.path.join(service_seen_dir(service), f"global_{country}.txt")
    if not os.path.exists(p):
        return set()
    with open(p) as f:
        return set(x.strip() for x in f)

def add_seen(service, country, numbers):
    with open(os.path.join(service_seen_dir(service), f"global_{country}.txt"), "a") as f:
        f.write("\n".join(numbers) + "\n")

def cleanup_seen():
    now = time.time()
    for root, dirs, files in os.walk(config.SEEN_DIR):
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.isfile(p) and now - os.path.getmtime(p) > config.CLEANUP_DAYS * 86400:
                os.remove(p)

def remove_duplicates(service, country):
    nums = list(dict.fromkeys(get_numbers(service, country)))
    with open(os.path.join(service_dir(service), f"{country}.txt"), "w") as f:
        f.write("\n".join(nums))
    return len(nums)

def format_number(n):
    """নম্বরের আগে + যোগ করে"""
    n = n.strip()
    if not n.startswith("+"):
        return "+" + n
    return n

def track(uid, service, country, count, numbers=None):
    s = str(uid)
    if s not in USER_STATS:
        USER_STATS[s] = {"total": 0, "services": {}}
    USER_STATS[s]["total"] += count
    svc = USER_STATS[s]["services"]
    if service not in svc:
        svc[service] = {}
    svc[service][country] = svc[service].get(country, 0) + count

    if numbers:
        USER_LAST_NUMBERS[s] = numbers
        # History সংরক্ষণ
        if s not in USER_HISTORY:
            USER_HISTORY[s] = []
        for n in numbers:
            USER_HISTORY[s].append({
                "service": service,
                "country": country,
                "number":  format_number(n),
                "time":    datetime.now().strftime("%d %b %Y %H:%M")
            })
        USER_HISTORY[s] = USER_HISTORY[s][-50:]  # সর্বোচ্চ ৫০টি রাখব

    USER_LAST_ACTIVE[s] = datetime.now().strftime("%d %b %Y  %H:%M")
    save_data()

# ════════════════════════════════════════════════════════
#              OTP MATCHING ENGINE
# ════════════════════════════════════════════════════════
def parse_masked(text):
    results = []
    for m in re.finditer(r'(\d+)([\u24B6-\u24E9]+)(\d+)', text):
        results.append((m.group(1), len(m.group(2)), m.group(3)))
    return results

def clean(n):
    return re.sub(r'[\s\-\+\(\)]', '', str(n))

def is_match(prefix, hidden, suffix, real):
    r = clean(real)
    for v in ([r, r[1:]] if r.startswith('0') else [r]):
        if not v.endswith(suffix):
            continue
        pos = len(v) - len(suffix) - hidden
        if pos >= len(prefix) and v[pos - len(prefix):pos] == prefix:
            return True
    return False

def find_users(prefix, hidden, suffix):
    out = []
    for uid_s, nums in USER_LAST_NUMBERS.items():
        for n in (nums or []):
            if is_match(prefix, hidden, suffix, clean(n)):
                out.append((int(uid_s), n))
                break
    return out

def get_otp(text):
    for p in [
        r'(?i)(?:otp|code|verification|pin|কোড)[:\s\-]+(\d{4,8})',
        r'(?i)(?:is|হলো)\s*[:\-]?\s*(\d{4,8})',
        r'\b(\d{4,8})\b',
    ]:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None

async def handle_otp(context, text):
    masked_list = parse_masked(text)
    if not masked_list:
        return 0
    otp = get_otp(text)
    sent = 0
    for prefix, hidden, suffix in masked_list:
        for uid, real_num in find_users(prefix, hidden, suffix):
            try:
                if otp:
                    msg = (
                        f"╔══════════════════════╗\n"
                        f"║  🔔  OTP এসেছে!      ║\n"
                        f"╚══════════════════════╝\n\n"
                        f"📱 নম্বর\n"
                        f"┗ `{format_number(real_num)}`\n\n"
                        f"🔢 OTP কোড\n"
                        f"┗ `{otp}`\n\n"
                        f"📩 মূল মেসেজ\n"
                        f"┌─────────────────────\n"
                        f"│ {text[:200]}\n"
                        f"└─────────────────────\n\n"
                        f"⚡ _দ্রুত ব্যবহার করো!_"
                    )
                else:
                    msg = (
                        f"╔══════════════════════╗\n"
                        f"║  🔔  মেসেজ এসেছে!   ║\n"
                        f"╚══════════════════════╝\n\n"
                        f"📱 নম্বর\n"
                        f"┗ `{format_number(real_num)}`\n\n"
                        f"📩 মূল মেসেজ\n"
                        f"┌─────────────────────\n"
                        f"│ {text[:200]}\n"
                        f"└─────────────────────"
                    )
                await context.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
                OTP_LOG.append({
                    "time":   datetime.now().strftime("%d %b %H:%M"),
                    "number": format_number(real_num),
                    "otp":    otp or "N/A",
                    "uid":    uid
                })
                save_data()
                sent += 1
            except Exception as e:
                print(f"[OTP ❌] uid={uid} | {e}")
    return sent

async def otp_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat.id != config.OTP_GROUP_ID:
        return
    text = msg.text or msg.caption or ""
    if text:
        count = await handle_otp(context, text)
        if count:
            print(f"[OTP] ✅ {count} জনকে forward করা হয়েছে")

# ════════════════════════════════════════════════════════
#                FILE UPLOAD (Service based)
# ════════════════════════════════════════════════════════
async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message or not update.message.document:
        return
    uid = update.effective_user.id
    if uid not in UPLOAD_MODE:
        return
    service = UPLOAD_MODE[uid]
    doc = update.message.document
    try:
        raw   = await (await doc.get_file()).download_as_bytearray()
        lines = [x.strip() for x in raw.decode("utf-8", errors="ignore").splitlines() if x.strip()]
        if not lines:
            await update.message.reply_text("❌ ফাইল খালি!")
            UPLOAD_MODE.pop(uid, None)
            return
        country = doc.file_name.replace(".txt", "").strip()
        with open(os.path.join(service_dir(service), f"{country}.txt"), "a") as f:
            f.write("\n" + "\n".join(lines))
        await update.message.reply_text(
            f"✅ সফলভাবে যোগ হয়েছে!\n\n"
            f"📱 Service: *{service}*\n"
            f"🌍 Country: *{country}*\n"
            f"📲 নম্বর: *{len(lines)}টি*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    UPLOAD_MODE.pop(uid, None)

# ════════════════════════════════════════════════════════
#                 USER PANEL
# ════════════════════════════════════════════════════════
def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📱 Get Number"),    KeyboardButton("📦 Services")],
        [KeyboardButton("📊 Live Stock"),    KeyboardButton("🕘 My History")],
        [KeyboardButton("☎️ Support")],
    ], resize_keyboard=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id
    if uid in BANNED:
        await update.message.reply_text("🚫 তুমি banned।")
        return
    name = update.effective_user.first_name or "বন্ধু"
    USERS.add(uid)
    save_data()
    welcome = (
        f"╔═══════════════════════╗\n"
        f"║   ✨ Number Bot ✨     ║\n"
        f"╚═══════════════════════╝\n\n"
        f"👋 স্বাগতম, *{name}*!\n\n"
        f"🌍 বিভিন্ন দেশের নম্বর পাও\n"
        f"🔔 OTP আসলে বট নিজেই জানাবে\n"
        f"⚡ দ্রুত, সহজ, নির্ভরযোগ্য\n\n"
        f"👇 নিচের মেনু থেকে শুরু করো"
    )
    if update.message:
        await update.message.reply_text(welcome, parse_mode="Markdown", reply_markup=main_keyboard())

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    t   = update.message.text
    uid = update.effective_user.id

    if t in ("📱 Get Number", "📦 Services"):
        await show_service_list(update, context)

    elif t == "📊 Live Stock":
        lines = []
        for svc in SERVICES:
            for c in get_countries(svc):
                total = len(get_numbers(svc, c))
                used  = len(get_seen(svc, c))
                left  = total - used
                bar   = "🟢" if left > 10 else ("🟡" if left > 0 else "🔴")
                lines.append(f"{bar} *{svc} › {c}*\n    ┗ বাকি: {left}  |  মোট: {total}  |  ব্যবহৃত: {used}")
        msg = "📊 *লাইভ স্টক রিপোর্ট*\n\n" + ("\n\n".join(lines) if lines else "⚠️ কোনো নম্বর নেই।")
        await update.message.reply_text(msg, parse_mode="Markdown")

    elif t == "🕘 My History":
        s    = str(uid)
        hist = USER_HISTORY.get(s, [])
        if not hist:
            await update.message.reply_text("📭 তোমার কোনো ইতিহাস নেই।")
            return
        lines = []
        for h in hist[-15:][::-1]:
            lines.append(f"📱 `{h['number']}`  ›  *{h['service']}*  ›  {h['country']}\n    🕐 {h['time']}")
        await update.message.reply_text(
            "🕘 *তোমার শেষ নম্বরগুলো:*\n\n" + "\n\n".join(lines),
            parse_mode="Markdown"
        )

    elif t == "☎️ Support":
        await update.message.reply_text(
            f"☎️ *সাপোর্ট*\n\n"
            f"যেকোনো সমস্যায় যোগাযোগ করো:\n"
            f"👉 {config.SUPPORT_LINK}",
            parse_mode="Markdown"
        )

# ════════════════════════════════════════════════════════
#          SERVICE → COUNTRY → NUMBER SCREENS
# ════════════════════════════════════════════════════════
async def show_service_list(update, context):
    if not SERVICES:
        text = "⚠️ কোনো সার্ভিস নেই।"
        kb   = []
    else:
        text = "📦 *সার্ভিস বেছে নাও*"
        icons = {"WhatsApp": "💬", "Telegram": "✈️", "Facebook": "📘"}
        kb = []
        for svc in SERVICES:
            icon = icons.get(svc, "📱")
            # মোট বাকি নম্বর গণনা
            total_left = sum(
                len(set(get_numbers(svc, c)) - get_seen(svc, c))
                for c in get_countries(svc)
            )
            bar = "🟢" if total_left > 10 else ("🟡" if total_left > 0 else "🔴")
            kb.append([InlineKeyboardButton(
                f"{bar} {icon} {svc}  ({total_left})",
                callback_data=f"svc_{svc}"
            )])
        kb.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_services")])

    markup = InlineKeyboardMarkup(kb)
    if hasattr(update, "callback_query") and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

async def show_country_list(update, context, service):
    countries = get_countries(service)
    if not countries:
        text = f"⚠️ *{service}* এ কোনো দেশ নেই।"
        kb   = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_services")]]
    else:
        text = f"📦 *{service}* › দেশ বেছে নাও\n\n🟢 পর্যাপ্ত  🟡 কম  🔴 শেষ"
        kb   = []
        for c in countries:
            left   = len(set(get_numbers(service, c)) - get_seen(service, c))
            status = "🟢" if left > 10 else ("🟡" if left > 0 else "🔴")
            kb.append([InlineKeyboardButton(
                f"{status}  {c}  ({left})",
                callback_data=f"country_{service}|{c}"
            )])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_services")])

    q = update.callback_query
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def show_numbers(update, context, service, country):
    q   = update.callback_query
    uid = q.from_user.id

    unseen = list(set(get_numbers(service, country)) - get_seen(service, country))
    if not unseen:
        await q.edit_message_text(
            f"╔══════════════════════╗\n"
            f"║  ❌  নম্বর শেষ!      ║\n"
            f"╚══════════════════════╝\n\n"
            f"📦 *{service}*  ›  🌍 *{country}*\n\n"
            f"এখন কোনো নম্বর নেই। পরে চেষ্টা করো।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data=f"svc_{service}")]
            ])
        )
        return

    limit    = min(NUMBER_LIMIT, len(unseen))
    selected = random.sample(unseen, limit)
    add_seen(service, country, selected)
    track(uid, service, country, len(selected), selected)

    number_text = ""
    for n in selected:
        number_text += f"📲  `{format_number(n)}`\n"

    header = (
        f"╔══════════════════════╗\n"
        f"║  ⚡  নতুন নম্বর      ║\n"
        f"╚══════════════════════╝\n\n"
        f"📦 *{service}*  ›  🌍 *{country}*  ┄  {limit}টি\n"
        f"🔔 OTP আসলে বট জানাবে!\n\n"
        f"┌─────────────────────\n"
        f"{number_text}"
        f"└─────────────────────"
    )

    kb = [
        [InlineKeyboardButton("🔄 নতুন নম্বর", callback_data=f"country_{service}|{country}")],
        [
            InlineKeyboardButton("🔐 OTP Group", url=config.OTP_GROUP_LINK),
            InlineKeyboardButton("⬅️ Back",      callback_data=f"svc_{service}")
        ]
    ]
    await q.edit_message_text(header, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# ════════════════════════════════════════════════════════
#               CALLBACK HANDLER
# ════════════════════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global NUMBER_LIMIT, SERVICES
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = q.from_user.id
    cleanup_seen()

    # ── USER ──────────────────────────────────
    if data in ("back_to_services", "refresh_services"):
        await show_service_list(update, context)

    elif data.startswith("svc_"):
        service = data[4:]
        await show_country_list(update, context, service)

    elif data.startswith("country_"):
        parts   = data[8:].split("|", 1)
        service = parts[0]
        country = parts[1]
        await show_numbers(update, context, service, country)

    elif data == "back_to_start":
        await cmd_start(update, context)

    # ── ADMIN ─────────────────────────────────
    elif uid in ADMINS:

        if data == "back_to_admin":
            await show_admin_panel(q.message, edit=True)

        # ── Number Limit ──
        elif data == "set_limit":
            kb = []
            row = []
            for i in range(1, 11):
                mark = " ✅" if i == NUMBER_LIMIT else ""
                row.append(InlineKeyboardButton(f"{i}{mark}", callback_data=f"limit_{i}"))
                if len(row) == 5:
                    kb.append(row); row = []
            if row:
                kb.append(row)
            kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin")])
            await q.message.edit_text(
                f"🔢 *নম্বর লিমিট সেট করো*\n\nবর্তমান: *{NUMBER_LIMIT}টি*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data.startswith("limit_"):
            NUMBER_LIMIT = int(data[6:])
            save_data()
            await q.message.edit_text(
                f"✅ লিমিট আপডেট: *{NUMBER_LIMIT}টি*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin Panel", callback_data="back_to_admin")]])
            )

        # ── Service Management ──
        elif data == "manage_services":
            kb = []
            for svc in SERVICES:
                kb.append([
                    InlineKeyboardButton(f"🗑 {svc} মুছো", callback_data=f"del_svc_{svc}")
                ])
            kb.append([InlineKeyboardButton("➕ নতুন Service যোগ করো", callback_data="add_service")])
            kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin")])
            await q.message.edit_text(
                "📦 *Service Management*\n\nবর্তমান সার্ভিস তালিকা:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data == "add_service":
            context.user_data["mode"] = "add_service"
            await q.message.reply_text("📦 নতুন Service এর নাম লিখো (যেমন: Instagram):")

        elif data.startswith("del_svc_"):
            svc = data[8:]
            if svc in SERVICES:
                SERVICES.remove(svc)
                save_data()
            await q.message.edit_text(
                f"✅ *{svc}* সার্ভিস মুছে গেছে।",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="manage_services")]])
            )

        # ── Bulk Add (Service based) ──
        elif data == "bulk_add":
            kb = [[InlineKeyboardButton(f"📦 {svc}", callback_data=f"upload_svc_{svc}")] for svc in SERVICES]
            kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin")])
            await q.message.edit_text(
                "📥 *কোন Service এ নম্বর যোগ করবে?*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data.startswith("upload_svc_"):
            service = data[11:]
            UPLOAD_MODE[uid] = service
            await q.message.reply_text(
                f"📥 *{service}* এ নম্বর যোগ করো\n\n"
                f"একটি `.txt` ফাইল পাঠাও।\n"
                f"📌 ফাইলের নাম = দেশের নাম\n"
                f"📌 প্রতিটি লাইনে একটি নম্বর",
                parse_mode="Markdown"
            )

        # ── Bulk Remove ──
        elif data == "bulk_remove":
            kb = []
            for svc in SERVICES:
                for c in get_countries(svc):
                    kb.append([InlineKeyboardButton(f"🗑 {svc} › {c}", callback_data=f"del_country_{svc}|{c}")])
            kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin")])
            await q.message.edit_text(
                "🗑 *কোন দেশের নম্বর মুছবে?*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data.startswith("del_country_"):
            parts   = data[12:].split("|", 1)
            service = parts[0]
            country = parts[1]
            removed = len(get_numbers(service, country))
            open(os.path.join(service_dir(service), f"{country}.txt"), "w").close()
            await q.message.edit_text(
                f"✅ *{service} › {country}* থেকে *{removed}টি* নম্বর মুছে গেছে।",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin Panel", callback_data="back_to_admin")]])
            )

        # ── Statistics Dashboard ──
        elif data == "statistics":
            total_numbers = 0
            service_stats = {}
            for svc in SERVICES:
                svc_total = 0
                svc_left  = 0
                for c in get_countries(svc):
                    t = len(get_numbers(svc, c))
                    l = len(set(get_numbers(svc, c)) - get_seen(svc, c))
                    svc_total += t
                    svc_left  += l
                service_stats[svc] = {"total": svc_total, "left": svc_left}
                total_numbers += svc_total

            # ইউজার স্ট্যাটিস্টিক্স
            top_users = sorted(USER_STATS.items(), key=lambda x: x[1].get("total", 0), reverse=True)[:5]

            msg = (
                f"╔═══════════════════════╗\n"
                f"║  📊  Statistics       ║\n"
                f"╚═══════════════════════╝\n\n"
                f"👥 মোট ইউজার: *{len(USERS)}জন*\n"
                f"🚫 Banned: *{len(BANNED)}জন*\n"
                f"📲 মোট নম্বর: *{total_numbers}টি*\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📦 *Service Breakdown:*\n\n"
            )
            for svc, d in service_stats.items():
                bar = "🟢" if d["left"] > 10 else ("🟡" if d["left"] > 0 else "🔴")
                msg += f"{bar} *{svc}*\n    ┗ মোট: {d['total']}  |  বাকি: {d['left']}\n\n"

            if top_users:
                msg += "━━━━━━━━━━━━━━━\n🏆 *Top 5 Users:*\n\n"
                for i, (uid_s, stats) in enumerate(top_users, 1):
                    last = USER_LAST_ACTIVE.get(uid_s, "N/A")
                    msg += f"{i}. `{uid_s}`  ┄  *{stats.get('total', 0)}টি*  ┄  {last}\n"

            await q.message.edit_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="statistics"),
                     InlineKeyboardButton("⬅️ Back",   callback_data="back_to_admin")]
                ])
            )

        # ── Clean Dupes ──
        elif data == "clean_dupes":
            total = 0
            for svc in SERVICES:
                for c in get_countries(svc):
                    total += remove_duplicates(svc, c)
            await q.message.edit_text(
                f"✅ *ডুপ্লিকেট ক্লিন সম্পন্ন!*\n\nমোট নম্বর বাকি: *{total}টি*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin Panel", callback_data="back_to_admin")]])
            )

        # ── Broadcast ──
        elif data == "broadcast":
            context.user_data["mode"] = "broadcast"
            await q.message.reply_text("📢 *Broadcast*\n\nসব ইউজারকে যে মেসেজ পাঠাতে চাও সেটা লিখো:", parse_mode="Markdown")

        # ── Admin/Ban Management ──
        elif data in ["add_admin", "remove_admin", "ban_user", "unban_user"]:
            context.user_data["mode"] = data
            labels = {
                "add_admin":    "➕ নতুন Admin এর Telegram ID দাও:",
                "remove_admin": "➖ যে Admin বাদ দেবে তার ID দাও:",
                "ban_user":     "🚫 যে ইউজার Ban করবে তার ID দাও:",
                "unban_user":   "✅ যে ইউজার Unban করবে তার ID দাও:",
            }
            await q.message.reply_text(labels[data], parse_mode="Markdown")

        # ── Total Users ──
        elif data == "total_users":
            await q.message.edit_text(
                f"👥 *ইউজার পরিসংখ্যান*\n\n"
                f"মোট ইউজার: *{len(USERS)}জন*\n"
                f"Banned: *{len(BANNED)}জন*\n"
                f"Admin: *{len(ADMINS)}জন*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin")]])
            )

        # ── OTP Status ──
        elif data == "otp_status":
            active = [(u, n) for u, n in USER_LAST_NUMBERS.items() if n]
            if not active:
                msg = "📊 *OTP Status*\n\nএখন কোনো ইউজার সক্রিয় নেই।"
            else:
                lines = []
                for uid_s, nums in active[:15]:
                    last = USER_LAST_ACTIVE.get(uid_s, "N/A")
                    lines.append(f"👤 `{uid_s}`\n    ┗ {len(nums)}টি নম্বর  |  {last}")
                msg = "📊 *সক্রিয় ইউজার ও নম্বর:*\n\n" + "\n\n".join(lines)

            if OTP_LOG:
                msg += "\n\n━━━━━━━━━━━━━━━\n📋 *শেষ ৫টি OTP:*\n"
                for log in OTP_LOG[-5:][::-1]:
                    msg += f"\n🕐 {log['time']}\n    📱 `{log['number']}`  🔢 `{log['otp']}`"

            await q.message.edit_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="otp_status"),
                     InlineKeyboardButton("⬅️ Back",   callback_data="back_to_admin")]
                ])
            )

# ════════════════════════════════════════════════════════
#                  ADMIN PANEL
# ════════════════════════════════════════════════════════
async def show_admin_panel(message, edit=False):
    text = (
        f"╔═══════════════════════╗\n"
        f"║   ⚙️  Admin Panel     ║\n"
        f"╚═══════════════════════╝\n\n"
        f"👥 মোট ইউজার: *{len(USERS)}জন*\n"
        f"📲 নম্বর লিমিট: *{NUMBER_LIMIT}টি* প্রতি ইউজার\n"
        f"📦 সার্ভিস: *{', '.join(SERVICES)}*\n"
        f"🕐 {datetime.now().strftime('%d %b %Y  %H:%M')}"
    )
    kb = [
        [InlineKeyboardButton("📊 Statistics",       callback_data="statistics"),
         InlineKeyboardButton("👥 Total Users",      callback_data="total_users")],
        [InlineKeyboardButton("📊 OTP Status",       callback_data="otp_status"),
         InlineKeyboardButton("🔢 Number Limit",     callback_data="set_limit")],
        [InlineKeyboardButton("📦 Services",         callback_data="manage_services"),
         InlineKeyboardButton("📢 Broadcast",        callback_data="broadcast")],
        [InlineKeyboardButton("📥 Bulk Add",         callback_data="bulk_add"),
         InlineKeyboardButton("📤 Bulk Remove",      callback_data="bulk_remove")],
        [InlineKeyboardButton("➕ Add Admin",         callback_data="add_admin"),
         InlineKeyboardButton("➖ Remove Admin",      callback_data="remove_admin")],
        [InlineKeyboardButton("🚫 Ban User",          callback_data="ban_user"),
         InlineKeyboardButton("✅ Unban User",        callback_data="unban_user")],
        [InlineKeyboardButton("🗑 Clean Duplicates",  callback_data="clean_dupes")],
    ]
    markup = InlineKeyboardMarkup(kb)
    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("❌ তুমি অ্যাডমিন না।")
        return
    await show_admin_panel(update.message)

# ════════════════════════════════════════════════════════
#               ADMIN TEXT INPUT
# ════════════════════════════════════════════════════════
async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SERVICES
    if not update.effective_user or not update.message:
        return
    uid  = update.effective_user.id
    if uid not in ADMINS:
        return
    mode = context.user_data.get("mode")
    if not mode:
        return
    txt = update.message.text.strip()
    try:
        if mode == "add_admin":
            ADMINS.add(int(txt)); save_data()
            await update.message.reply_text(f"✅ Admin যোগ হয়েছে: `{txt}`", parse_mode="Markdown")
        elif mode == "remove_admin":
            ADMINS.discard(int(txt)); save_data()
            await update.message.reply_text(f"❌ Admin বাদ: `{txt}`", parse_mode="Markdown")
        elif mode == "broadcast":
            sent = 0
            for u in list(USERS):
                try:
                    await context.bot.send_message(u, f"📢 *বট নোটিশ*\n\n{txt}", parse_mode="Markdown")
                    sent += 1
                except: pass
            await update.message.reply_text(f"✅ *{sent}জন* ইউজারকে পাঠানো হয়েছে।", parse_mode="Markdown")
        elif mode == "ban_user":
            BANNED.add(int(txt)); save_data()
            await update.message.reply_text(f"🚫 Banned: `{txt}`", parse_mode="Markdown")
        elif mode == "unban_user":
            BANNED.discard(int(txt)); save_data()
            await update.message.reply_text(f"✅ Unbanned: `{txt}`", parse_mode="Markdown")
        elif mode == "add_service":
            if txt not in SERVICES:
                SERVICES.append(txt)
                save_data()
                await update.message.reply_text(f"✅ *{txt}* সার্ভিস যোগ হয়েছে!", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"⚠️ *{txt}* আগে থেকেই আছে।", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    context.user_data["mode"] = None

# ════════════════════════════════════════════════════════
#                     MAIN
# ════════════════════════════════════════════════════════
def main():
    print(">>> Bot starting...")
    load_data()
    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))

    app.add_handler(MessageHandler(filters.Regex("^📱 Get Number$"),  menu_handler))
    app.add_handler(MessageHandler(filters.Regex("^📦 Services$"),    menu_handler))
    app.add_handler(MessageHandler(filters.Regex("^📊 Live Stock$"),  menu_handler))
    app.add_handler(MessageHandler(filters.Regex("^🕘 My History$"),  menu_handler))
    app.add_handler(MessageHandler(filters.Regex("^☎️ Support$"),     menu_handler))

    app.add_handler(MessageHandler(filters.Chat(config.OTP_GROUP_ID), otp_listener), group=0)
    app.add_handler(MessageHandler(filters.Document.ALL, receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("=" * 40)
    print(f"✅ Bot LIVE!")
    print(f"📲 Number Limit: {NUMBER_LIMIT}")
    print(f"📡 OTP Group: {config.OTP_GROUP_ID}")
    print(f"📦 Services: {', '.join(SERVICES)}")
    print("=" * 40)

    app.run_polling(allowed_updates=["message", "callback_query", "channel_post"])

if __name__ == "__main__":
    main()
