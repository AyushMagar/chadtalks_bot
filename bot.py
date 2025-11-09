# bot.py
import re
import json
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
load_dotenv()

# -------- CONFIGURATION --------
DATA_FILE = "data.json"
GUILD_DAILYLOG_CHANNEL = "dailylogs"
LEADERBOARD_CHANNEL = "leaderboard"
BOT_NAME = "ChadTalks Accountability Bot"

# Thresholds (>=)
MIN_REPS = 150
MIN_STEPS = 15000
MIN_WORK_HOURS = 5.0

# Points
PTS_REPS = 3
PTS_STEPS = 3
PTS_WORK = 3
PTS_MEDITATION = 2

# Sick days
SICK_DAYS_WEEKLY_LIMIT = 2

# Inactivity rules
WARNING_AFTER = timedelta(hours=36)
KICK_AFTER = timedelta(hours=48)

# Timezone + timings
TZ = ZoneInfo("Asia/Kolkata")
DAILY_LEADERBOARD_HOUR = 0    # 12:00 AM IST
DOUBLE_LOG_INTERVAL = timedelta(hours=12)

# -------- BOT SETUP --------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -------- DATA HELPERS --------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "leaderboard_message_id": None}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

data = load_data()

def ensure_user_record(user_id):
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = {
            "last_valid_log": None,
            "weekly_points": 0,
            "total_points": 0,
            "daily_points": {},
            "sick_days": {},
            "warned_at": None,
            "joined_at": None,
        }
        save_data(data)
    return data["users"][uid]

def now_ist():
    return datetime.now(tz=TZ)

def date_str(dt=None):
    dt = dt or now_ist()
    return dt.date().isoformat()

# -------- PARSING --------
def parse_number_with_k(text):
    text = text.strip().lower()
    m = re.match(r"^([\d.,]+)\s*k$", text)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1000)
    try:
        return int(float(text.replace(",", "")))
    except Exception:
        return None

def extract_metrics_from_text(text):
    reps = None
    steps = None
    work_hours = None
    meditation = False

    lowered = text.lower()
    if "meditation" in lowered or "meditate" in lowered:
        if not re.search(r"\b(no|not|skip|skipped)\b.*meditat", lowered):
            meditation = True

    m = re.search(r"workout[:\s\-]*([\d.,kK]+)", text, flags=re.I)
    if not m:
        m = re.search(r"(\d[\d.,kK]*)\s*reps?\b", text, flags=re.I)
    if m:
        reps = parse_number_with_k(m.group(1))

    m = re.search(r"steps?[:\s\-]*([\d.,kK]+)", text, flags=re.I)
    if not m:
        m = re.search(r"([\d.,kK]+)\s*steps?\b", text, flags=re.I)
    if m:
        steps = parse_number_with_k(m.group(1))

    m = re.search(r"work(?:ing)?[:\s\-]*([\d.,]+)\s*(?:h|hr|hrs|hours?)", text, flags=re.I)
    if not m:
        m = re.search(r"([\d.,]+)\s*(?:h|hr|hrs|hours?)\s*(?:work|working)?\b", text, flags=re.I)
    if m:
        try:
            work_hours = float(m.group(1).replace(",", "."))
        except:
            work_hours = None

    return {
        "reps": reps,
        "steps": steps,
        "work_hours": work_hours,
        "meditation": meditation
    }

# -------- SCORING --------
def score_metrics(metrics):
    pts = 0
    details = {}
    reps = metrics.get("reps")
    steps = metrics.get("steps")
    work_hours = metrics.get("work_hours")
    meditation = metrics.get("meditation", False)

    # reps
    if reps and reps >= MIN_REPS:
        pts += PTS_REPS
    else:
        pts -= PTS_REPS
    details["reps"] = reps

    # steps
    if steps and steps >= MIN_STEPS:
        pts += PTS_STEPS
    else:
        pts -= PTS_STEPS
    details["steps"] = steps

    # work
    if work_hours and work_hours >= MIN_WORK_HOURS:
        pts += PTS_WORK
    else:
        pts -= PTS_WORK
    details["work"] = work_hours

    # meditation
    if meditation:
        pts += PTS_MEDITATION
    else:
        pts -= PTS_MEDITATION
    details["meditation"] = meditation

    return pts, details

# -------- BOT EVENTS --------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online as {BOT_NAME}")
    await bot.change_presence(activity=discord.Game("Keeping brothers accountable 💪"))
    if not hourly_inactivity_check.is_running():
        hourly_inactivity_check.start()
    if not daily_tasks.is_running():
        daily_tasks.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.name == GUILD_DAILYLOG_CHANNEL:
        rec = ensure_user_record(message.author.id)
        last_log_iso = rec.get("last_valid_log")

        if last_log_iso:
            last_log_time = datetime.fromisoformat(last_log_iso)
            if now_ist() - last_log_time < DOUBLE_LOG_INTERVAL:
                await message.channel.send(
                    f"⚠️ {message.author.mention} — You’ve already completed your daily log. This entry will **not be accepted.** 💪"
                )
                return

        metrics = extract_metrics_from_text(message.content)
        pts, details = score_metrics(metrics)

        rec["last_valid_log"] = now_ist().isoformat()
        rec["weekly_points"] += pts
        rec["total_points"] += pts
        rec["daily_points"][date_str()] = pts
        save_data(data)

        if pts > 0:
            await message.channel.send(f"✅ {message.author.mention} — Great job! **+{pts} pts**")
        else:
            await message.channel.send(f"❌ {message.author.mention} — Missed some goals. **{pts} pts**")

        await update_leaderboard_quick(message.guild)
    await bot.process_commands(message)

# -------- COMMANDS --------
@bot.command(name="sick")
async def cmd_sick(ctx, *, reason: str = ""):
    rec = ensure_user_record(ctx.author.id)
    wk = date_str()[:7]  # month-based tracking instead of weekly reset
    used = rec.get("sick_days", {}).get(wk, 0)
    if used >= SICK_DAYS_WEEKLY_LIMIT:
        await ctx.send(f"❌ {ctx.author.mention} — you already used {SICK_DAYS_WEEKLY_LIMIT} sick days recently.")
        return
    rec["sick_days"][wk] = used + 1
    rec["daily_points"][date_str()] = "SICK"
    rec["last_valid_log"] = now_ist().isoformat()
    save_data(data)
    await ctx.send(f"🤒 {ctx.author.mention} — Sick day recorded. Reason: {reason}")

@bot.command(name="mystats")
async def cmd_mystats(ctx):
    rec = ensure_user_record(ctx.author.id)
    await ctx.send(f"📊 {ctx.author.mention} — Weekly: {rec['weekly_points']} pts | Total: {rec['total_points']} pts")

# -------- LEADERBOARD --------
async def update_leaderboard_quick(guild):
    ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL)
    if not ch:
        return
    rankings = []
    for uid, rec in data["users"].items():
        member = guild.get_member(int(uid))
        if member:
            rankings.append((member.display_name, rec.get("weekly_points", 0), member.mention))
    rankings.sort(key=lambda x: x[1], reverse=True)
    msg = "🏆 **Leaderboard (Live)**\n"
    for i, r in enumerate(rankings[:10], start=1):
        msg += f"{i}. {r[2]} — {r[1]} pts\n"
    await ch.send(msg)

# -------- INACTIVITY CHECK --------
@tasks.loop(hours=1)
async def hourly_inactivity_check():
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            rec = ensure_user_record(member.id)
            last_iso = rec.get("last_valid_log")
            if not last_iso:
                continue
            delta = now_ist() - datetime.fromisoformat(last_iso)
            if delta > KICK_AFTER:
                try:
                    await member.send("❌ You’ve been removed from ChadTalks for missing daily logs for 48 hours.")
                except:
                    pass
                try:
                    await member.kick(reason="Inactive 48h")
                    print(f"Kicked {member}")
                except:
                    pass
            elif delta > WARNING_AFTER and not rec.get("warned_at"):
                try:
                    await member.send("⚠️ You haven’t logged in 36 hours. Post in #dailylogs to stay in!")
                    rec["warned_at"] = now_ist().isoformat()
                    save_data(data)
                except:
                    pass

# -------- DAILY TASKS --------
@tasks.loop(minutes=10)
async def daily_tasks():
    ist_now = now_ist()
    if ist_now.hour == DAILY_LEADERBOARD_HOUR:
        today = date_str()
        if data.get("_last_daily_run") == today:
            return
        for uid, rec in data["users"].items():
            if today not in rec["daily_points"]:
                penalty = -(PTS_REPS + PTS_STEPS + PTS_WORK + PTS_MEDITATION)
                rec["weekly_points"] += penalty
                rec["total_points"] += penalty
                rec["daily_points"][today] = penalty
        for guild in bot.guilds:
            await update_leaderboard_quick(guild)
        data["_last_daily_run"] = today
        save_data(data)

# -------- RUN BOT --------
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("Please set DISCORD_TOKEN environment variable or add it to .env")
        exit(1)
    bot.run(TOKEN)
