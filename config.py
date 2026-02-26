import os

# ================== BOT CONFIG ==================

# 🤖 Bot Token
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8201237698:AAG1Ve1zTGbLcYvDCEr5URpHR1n1-4h_SH0")

# 🔐 Admin Telegram IDs (numeric)
ADMIN_IDS = [6593090863]

# 📣 OTP Group/Channel ID — এখানে OTP মেসেজ আসে, বট এটা monitor করবে
OTP_GROUP_ID = int(os.environ.get("OTP_GROUP_ID", "-1002827526018"))

# 🔗 OTP Group Link — ইউজার বাটন চাপলে এই লিংকে যাবে
OTP_GROUP_LINK = "https://t.me/+5zshtYBMFoo4OTRl"

# ☎️ Support Link
SUPPORT_LINK = "https://t.me/bdshantoips"

# 📂 Data folders
NUMBER_DIR = "numbers"
SEEN_DIR = "seen"

# 🧹 কতদিন পর seen নম্বর রিসেট হবে
CLEANUP_DAYS = 7
