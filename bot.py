# bot.py
import os
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "data.json"

GUILD_DAILYLOG_CHANNEL = "dailylogs"
LEADERBOARD_CHANNEL = "leaderboard"
BOT_NAME = "ChadTalks Accountability Bot"

# thresholds
MIN_REPS = 150
MIN_STEPS = 15000
MIN_WORK_HOURS = 5.0

# points
PTS_REPS = 3
PTS_STEPS = 3
PTS_WORK = 3
PTS_MEDITATION = 2

# sick leave settings
SICK_LEAVES_PER_MONTH = 3
SICK_PROTECT_DAYS = 2  # 1 sick leave protects this many calendar days (today + next day)

# inactivity / times
TZ = ZoneInfo("Asia/Kolkata")
WARNING_AFTER = timedelta(hours=36)
KICK_AFTER = timedelta(hours=48)

# daily evaluation hour/minute in IST
DAILY_EVAL_HOUR = 0
DAILY_EVAL_MINUTE = 0

# ---------- BOT SETUP ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- DATA HELPERS ----------
def load_data():
    if not os.path.exists(DATA_FILE):
        # structure:
        # users: { user_id: { last_valid_log: iso, weekly_points: int, total_points: int, daily_points: {date: pts or "SICK"},
        #                     sick_used: { "YYYY-MM": count }, rest_until: "YYYY-MM-DD" , warned_at: iso, joined_at: iso } }
        return {"users": {}, "leaderboard_message_id": None, "_last_daily_run": None}
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
            "sick_used": {},      # month_key -> used_count
            "rest_until": None,   # "YYYY-MM-DD" inclusive
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

def month_key(dt=None):
    dt = dt or now_ist()
    return dt.strftime("%Y-%m")

# ---------- PARSING HELPERS ----------
def parse_number_with_k(text):
    text = text.strip().lower()
    m = re.match(r"^([\d.,]+)\s*k$", text)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1000)
    try:
        return int(float(text.replace(",", "")))
    except:
        return None

def extract_metrics_from_text(text):
    reps = None
    steps = None
    work_hours = None
    meditation = False

    lowered = text.lower()
    if "meditation" in lowered or "meditate" in lowered or "meditated" in lowered:
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

    return {"reps": reps, "steps": steps, "work_hours": work_hours, "meditation": meditation}

# ---------- SCORING ----------
def score_metrics(metrics):
    pts = 0
    details = {}
    reps = metrics.get("reps")
    steps = metrics.get("steps")
    work_hours = metrics.get("work_hours")
    meditation = metrics.get("meditation", False)

    # reps
    if reps is not None and reps >= MIN_REPS:
        pts += PTS_REPS
        details["reps"] = {"ok": True, "value": reps, "pts": PTS_REPS}
    else:
        details["reps"] = {"ok": False, "value": reps, "pts": -PTS_REPS}
        pts -= PTS_REPS

    # steps
    if steps is not None and steps >= MIN_STEPS:
        pts += PTS_STEPS
        details["steps"] = {"ok": True, "value": steps, "pts": PTS_STEPS}
    else:
        details["steps"] = {"ok": False, "value": steps, "pts": -PTS_STEPS}
        pts -= PTS_STEPS

    # work
    if work_hours is not None and work_hours >= MIN_WORK_HOURS:
        pts += PTS_WORK
        details["work"] = {"ok": True, "value": work_hours, "pts": PTS_WORK}
    else:
        details["work"] = {"ok": False, "value": work_hours, "pts": -PTS_WORK}
        pts -= PTS_WORK

    # meditation
    if meditation:
        pts += PTS_MEDITATION
        details["meditation"] = {"ok": True, "value": True, "pts": PTS_MEDITATION}
    else:
        details["meditation"] = {"ok": False, "value": False, "pts": -PTS_MEDITATION}
        pts -= PTS_MEDITATION

    return pts, details

# ---------- LEADERBOARD UTIL ----------
async def edit_or_send_leaderboard_message(channel, content):
    lb_id = data.get("leaderboard_message_id")
    if lb_id:
        try:
            msg = await channel.fetch_message(lb_id)
            await msg.edit(content=content)
            return msg
        except Exception:
            pass
    # send new
    msg = await channel.send(content)
    data["leaderboard_message_id"] = msg.id
    save_data(data)
    return msg

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
    lines = [f"🏆 **Leaderboard (running total)**"]
    if not rankings:
        lines.append("No entries yet.")
    else:
        for i, (name, pts, mention) in enumerate(rankings[:20], start=1):
            lines.append(f"{i}. {mention} — **{pts} pts**")
    await edit_or_send_leaderboard_message(ch, "\n".join(lines))

# ---------- EVENTS ----------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online as {BOT_NAME}")
    # ensure user records for existing members
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            rec = ensure_user_record(member.id)
            if rec.get("joined_at") is None and member.joined_at is not None:
                rec["joined_at"] = member.joined_at.isoformat()
    save_data(data)
    if not midnight_task.is_running():
        midnight_task.start()
    if not hourly_inactivity_check.is_running():
        hourly_inactivity_check.start()

@bot.event
async def on_member_join(member):
    rec = ensure_user_record(member.id)
    rec["joined_at"] = member.joined_at.isoformat() if member.joined_at else datetime.utcnow().isoformat()
    save_data(data)

# ---------- MESSAGE HANDLING ----------
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # only process messages in dailylogs channel
    if message.channel and message.channel.name == GUILD_DAILYLOG_CHANNEL:
        uid = str(message.author.id)
        rec = ensure_user_record(uid)

        today = now_ist().date()
        # 1) If user is under rest protection, reject (Option A)
        rest_until = rec.get("rest_until")
        if rest_until:
            try:
                rest_date = datetime.fromisoformat(rest_until).date()
                if today <= rest_date:
                    await message.channel.send(f"⚠️ {message.author.mention} — You are currently on a rest day. This log will NOT be accepted.")
                    return
            except:
                pass

        # 2) If user already logged today, reject
        last_iso = rec.get("last_valid_log")
        if last_iso:
            try:
                last_date = datetime.fromisoformat(last_iso).astimezone(TZ).date()
                if last_date == today:
                    await message.channel.send(f"⚠️ {message.author.mention} — You’ve already completed today’s log. This entry will **not be accepted.**")
                    return
            except:
                pass

        # 3) parse metrics and score
        metrics = extract_metrics_from_text(message.content)
        pts, details = score_metrics(metrics)

        # record
        rec["last_valid_log"] = now_ist().isoformat()
        # record daily points keyed by date
        rec["daily_points"][date_str()] = pts
        rec["weekly_points"] = rec.get("weekly_points", 0) + pts
        rec["total_points"] = rec.get("total_points", 0) + pts
        # reset warned flag on valid activity
        rec["warned_at"] = None
        save_data(data)

        # reply with details
        if pts > 0 and all(v["ok"] for v in details.values()):
            await message.channel.send(f"✅ {message.author.mention} — Great job! Today's score: **{pts} pts**.")
        else:
            # build reasons
            fail_parts = [k for k,v in details.items() if not v["ok"]]
            reasons = []
            for k in fail_parts:
                val = details[k]["value"]
                min_req = ""
                if k == "reps":
                    min_req = f"min {MIN_REPS} reps"
                elif k == "steps":
                    min_req = f"min {MIN_STEPS} steps"
                elif k == "work":
                    min_req = f"min {MIN_WORK_HOURS} hrs"
                elif k == "meditation":
                    min_req = f"meditation (optional)"
                reasons.append(f"{k}: {val} ({min_req})")
            await message.channel.send(f"❌ {message.author.mention} — Your log doesn't meet the standards. Today's score: **{pts} pts**.\nIssues: " + "; ".join(reasons))

        # update leaderboard immediately
        await update_leaderboard_quick(message.guild)

    await bot.process_commands(message)

# ---------- COMMANDS ----------
@bot.command(name="sick")
async def cmd_sick(ctx, *, reason: str = ""):
    uid = str(ctx.author.id)
    rec = ensure_user_record(uid)
    mk = month_key()
    used = rec.get("sick_used", {}).get(mk, 0)
    if used >= SICK_LEAVES_PER_MONTH:
        await ctx.send(f"❌ {ctx.author.mention} — you have used all {SICK_LEAVES_PER_MONTH} sick leaves this month.")
        return
    # consume one sick leave
    rec.setdefault("sick_used", {})
    rec["sick_used"][mk] = rec["sick_used"].get(mk, 0) + 1
    # grant rest days: today + (SICK_PROTECT_DAYS -1)
    rest_until_date = now_ist().date() + timedelta(days=SICK_PROTECT_DAYS - 1)
    rec["rest_until"] = rest_until_date.isoformat()
    # mark today's daily_points as "SICK" (so midnight won't penalize)
    rec["daily_points"][date_str()] = "SICK"
    rec["last_valid_log"] = now_ist().isoformat()
    save_data(data)
    await ctx.send(f"🤒 {ctx.author.mention} — Sick day recorded. Reason: {reason}. You are covered for {SICK_PROTECT_DAYS} days (today included).")

@bot.command(name="mystats")
async def cmd_mystats(ctx):
    uid = str(ctx.author.id)
    rec = ensure_user_record(uid)
    mk = month_key()
    sick_used = rec.get("sick_used", {}).get(mk, 0)
    sick_left = max(0, SICK_LEAVES_PER_MONTH - sick_used)
    await ctx.send(f"📊 {ctx.author.mention} — Weekly: {rec.get('weekly_points',0)} pts | Total: {rec.get('total_points',0)} pts | Sick left this month: {sick_left}")

@bot.command(name="leaderboard")
async def cmd_leaderboard(ctx):
    await update_leaderboard_quick(ctx.guild)

@bot.command(name="resetpoints")
async def cmd_resetpoints(ctx):
    # only allow role TFL-OG to reset
    role = discord.utils.get(ctx.author.roles, name="TFL-OG")
    if role is None:
        await ctx.send("❌ Only members with the **TFL-OG** role can run this command.")
        return
    for uid, rec in data["users"].items():
        rec["weekly_points"] = 0
        rec["total_points"] = 0
        rec["daily_points"] = {}
    save_data(data)
    await ctx.send("🧹 All leaderboard points have been reset to 0.")

# ---------- MIDNIGHT EVALUATION (00:00 IST) ----------
@tasks.loop(minutes=1)
async def midnight_task():
    ist = now_ist()
    if ist.hour == DAILY_EVAL_HOUR and ist.minute == DAILY_EVAL_MINUTE:
        today = date_str()
        # prevent double-run
        if data.get("_last_daily_run") == today:
            return

        # If it's the 1st of the month, reset monthly sick_used for everyone
        if now_ist().day == 1:
            for uid, rec in data["users"].items():
                rec.setdefault("sick_used", {})
                # reset this month key to 0
                rec["sick_used"][month_key()] = 0

        # Apply penalties for those who didn't log today and are not on SICK/rest
        penalty = -(PTS_REPS + PTS_STEPS + PTS_WORK + PTS_MEDITATION)
        for uid, rec in data["users"].items():
            # if today's entry exists, skip (either pts or "SICK")
            if today in rec.get("daily_points", {}):
                continue
            # if rest_until present and covers today, skip
            rest_until = rec.get("rest_until")
            if rest_until:
                try:
                    rest_date = datetime.fromisoformat(rest_until).date()
                    if today <= rest_date:
                        # Mark today's daily_points as "SICK-COVERED" so we don't penalize
                        rec["daily_points"][today] = "SICK"
                        continue
                except:
                    pass
            # apply penalty
            rec["daily_points"][today] = penalty
            rec["weekly_points"] = rec.get("weekly_points", 0) + penalty
            rec["total_points"] = rec.get("total_points", 0) + penalty

        # update leaderboard in each guild
        for guild in bot.guilds:
            await update_leaderboard_quick(guild)

        data["_last_daily_run"] = today
        save_data(data)

# ---------- HOURLY INACTIVITY CHECK (warn/kick) ----------
@tasks.loop(hours=1)
async def hourly_inactivity_check():
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            uid = str(member.id)
            rec = ensure_user_record(uid)
            last_iso = rec.get("last_valid_log")
            # fallback to joined_at if no valid log ever posted
            if not last_iso:
                ja = rec.get("joined_at")
                if ja:
                    try:
                        last_dt = datetime.fromisoformat(ja)
                    except:
                        last_dt = now_ist()
                else:
                    last_dt = now_ist()
            else:
                try:
                    last_dt = datetime.fromisoformat(last_iso)
                except:
                    last_dt = now_ist()

            delta = now_ist() - last_dt.astimezone(TZ)

            warned = rec.get("warned_at")
            if delta > KICK_AFTER:
                # Kick
                try:
                    await member.send("❌ You have been removed from ChadTalks for missing daily logs for 48 hours.")
                except:
                    pass
                try:
                    await member.kick(reason="Inactive: no valid daily logs for 48 hours.")
                    print(f"Kicked {member}")
                except Exception as e:
                    print("Failed to kick:", e)
            elif delta > WARNING_AFTER and not warned:
                # Warn
                try:
                    await member.send("⚠️ Brother — you haven't posted your daily log in 36 hours. Post your log in #dailylogs now to stay in the community 💪")
                    rec["warned_at"] = now_ist().isoformat()
                    save_data(data)
                except Exception as e:
                    print("Failed to DM warning:", e)

# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN:
        print("Please set DISCORD_TOKEN in .env or Railway variables.")
        exit(1)
    bot.run(TOKEN)
