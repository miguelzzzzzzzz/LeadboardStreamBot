
import os
import asyncio
import datetime as dt
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from dotenv import load_dotenv

# ========= Config =========
DB_PATH = "stream_stats.sqlite3"

TOKEN = "MTQxNDAzMDQzNTczODA2Mjk3MQ.GgM-ku.ih_BC35_uTfKXInk_x0xzzHkgwDhJp7EmC4XlY"
# ==========================
# Separate log channels (replace with real text-channel IDs)
STREAM_LOG_CHANNEL_ID = 1385644400012689532  # for stream start/stop logs
VOICE_LOG_CHANNEL_ID  = 1385644401090494518  # for join/leave/move logs


intents = discord.Intents.default()
intents.voice_states = True
intents.members = True  # needed to resolve member names on leaderboards

bot = commands.Bot(command_prefix="!", intents=intents)  # prefix not used; we use slash cmds
tree = bot.tree

# --------- Utilities ---------
def fmt_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

# --------- Database Layer ---------
CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS totals (
  guild_id INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  seconds  REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS active (
  guild_id  INTEGER NOT NULL,
  user_id   INTEGER NOT NULL,
  started_at TEXT NOT NULL, -- ISO8601 with timezone
  PRIMARY KEY (guild_id, user_id)
);

-- Optional audit trail (not used by commands, but helpful to keep)
CREATE TABLE IF NOT EXISTS sessions (
  guild_id   INTEGER NOT NULL,
  user_id    INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at   TEXT NOT NULL,
  duration_s REAL NOT NULL
);
"""

async def send_log(channel_id: int, embed: discord.Embed):
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception:
            return
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

async def log_stream_event(member: discord.Member, started: bool, vc: Optional[discord.VoiceChannel]):
    title = "📺 Stream Started" if started else "🛑 Stream Ended"
    desc = f"{member.mention} {'started' if started else 'ended'} streaming" + (f" in **{vc.name}**" if isinstance(vc, discord.VoiceChannel) else "")
    color = discord.Color.green() if started else discord.Color.red()
    embed = discord.Embed(title=title, description=desc, color=color, timestamp=now_utc())
    try:
        embed.set_author(name=f"{member.name}#{member.discriminator}", icon_url=member.display_avatar.url)
    except Exception:
        pass
    await send_log(STREAM_LOG_CHANNEL_ID, embed)

async def log_voice_event(member: discord.Member, action: str, before_ch: Optional[discord.VoiceChannel], after_ch: Optional[discord.VoiceChannel]):
    # action ∈ {"join","leave","move"}
    color = {"join": discord.Color.green(), "leave": discord.Color.red(), "move": discord.Color.blurple()}.get(action, discord.Color.greyple())
    if action == "join" and after_ch:
        desc = f"{member.mention} joined **{after_ch.name}**"
    elif action == "leave" and before_ch:
        desc = f"{member.mention} left **{before_ch.name}**"
    elif action == "move" and before_ch and after_ch:
        desc = f"{member.mention} moved **{before_ch.name} → {after_ch.name}**"
    else:
        return
    embed = discord.Embed(title="🔊 Voice Activity", description=desc, color=color, timestamp=now_utc())
    try:
        embed.set_author(name=f"{member.name}#{member.discriminator}", icon_url=member.display_avatar.url)
    except Exception:
        pass
    await send_log(VOICE_LOG_CHANNEL_ID, embed)


async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def add_seconds(guild_id: int, user_id: int, seconds: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO totals (guild_id, user_id, seconds) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET seconds = seconds + excluded.seconds",
            (guild_id, user_id, max(0.0, seconds)),
        )
        await db.commit()

async def set_seconds(guild_id: int, user_id: int, seconds: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO totals (guild_id, user_id, seconds) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET seconds = ?",
            (guild_id, user_id, max(0.0, seconds), max(0.0, seconds)),
        )
        await db.commit()

async def get_seconds(guild_id: int, user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT seconds FROM totals WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0

async def top_users(guild_id: int, limit: int = 10) -> list[Tuple[int, float]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, seconds FROM totals WHERE guild_id = ? "
            "ORDER BY seconds DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [(int(u), float(s)) for (u, s) in rows]

async def start_active(guild_id: int, user_id: int, started_at: dt.datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO active (guild_id, user_id, started_at) VALUES (?, ?, ?)",
            (guild_id, user_id, started_at.isoformat()),
        )
        await db.commit()

async def end_active(guild_id: int, user_id: int) -> Optional[Tuple[dt.datetime, float]]:
    """Returns (started_at, duration_secs) if an active record existed; otherwise None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT started_at FROM active WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        started_at = dt.datetime.fromisoformat(row[0])
        ended_at = now_utc()
        duration = (ended_at - started_at).total_seconds()

        # remove active + write session + add to totals
        await db.execute(
            "DELETE FROM active WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await db.execute(
            "INSERT INTO sessions (guild_id, user_id, started_at, ended_at, duration_s) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, started_at.isoformat(), ended_at.isoformat(), duration),
        )
        await db.execute(
            "INSERT INTO totals (guild_id, user_id, seconds) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET seconds = totals.seconds + excluded.seconds",
            (guild_id, user_id, max(0.0, duration)),
        )
        await db.commit()
        return (started_at, duration)

async def clear_all(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM totals WHERE guild_id = ?", (guild_id,))
        await db.commit()

async def clear_user(guild_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM totals WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await db.commit()

# --------- Bot Events ---------
@bot.event
async def on_ready():
    await db_init()
    # Register slash commands globally (first time may need up to ~1 hour to propagate; guild sync is instant)
    try:
        await tree.sync()
    except Exception as e:
        print("Slash command sync error:", e)

    # Attempt recovery of currently streaming users (approximate: start from 'now')
    # We can't know their real start moment after a restart.
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.voice and member.voice.self_stream:
                    # If not already active, mark active starting now
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute(
                            "SELECT 1 FROM active WHERE guild_id=? AND user_id=?",
                            (guild.id, member.id),
                        ) as cur:
                            exists = await cur.fetchone()
                    if not exists:
                        await start_active(guild.id, member.id, now_utc())
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # --- Voice join/leave/move logging ---
    before_ch = getattr(before, "channel", None)
    after_ch  = getattr(after, "channel", None)
    if before_ch is None and after_ch is not None:
        await log_voice_event(member, "join", before_ch, after_ch)
    elif before_ch is not None and after_ch is None:
        await log_voice_event(member, "leave", before_ch, after_ch)
    elif before_ch is not None and after_ch is not None and before_ch.id != after_ch.id:
        await log_voice_event(member, "move", before_ch, after_ch)

    # --- Stream (screen/camera) start/stop detection + logging ---
    def is_streaming(vs: Optional[discord.VoiceState]) -> bool:
        return bool(vs and (getattr(vs, "self_stream", False) or getattr(vs, "self_video", False)))

    before_stream = is_streaming(before)
    after_stream  = is_streaming(after)

    # Started streaming
    if not before_stream and after_stream:
        await start_active(member.guild.id, member.id, now_utc())
        await log_stream_event(member, True, after_ch)
        return

    # Stopped streaming OR left VC while streaming
    if before_stream and not after_stream:
        await end_active(member.guild.id, member.id)
        await log_stream_event(member, False, before_ch or after_ch)
        return

    # Edge: user disconnects and API still shows True in 'before'
    if before_stream and (after_ch is None):
        await end_active(member.guild.id, member.id)
        await log_stream_event(member, False, before_ch)
        return


@tree.command(name="streamstate", description="Debug: what the bot sees for a user's voice state (ephemeral).")
@app_commands.describe(member="User to inspect (defaults to you)")
async def streamstate(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    vs = getattr(member, "voice", None)
    if not isinstance(member, discord.Member):
        # try to resolve to a Member when the command mentions a User
        member = interaction.guild.get_member(member.id) or member
        vs = getattr(member, "voice", None)

    if not isinstance(member, discord.Member) or vs is None:
        await interaction.response.send_message("Not in a voice channel (or I can’t see the channel).", ephemeral=True)
        return

    msg = (
        f"channel: {vs.channel.name}\n"
        f"self_stream: {getattr(vs, 'self_stream', False)}\n"
        f"self_video:  {getattr(vs, 'self_video', False)}\n"
        f"self_mute:   {vs.self_mute}, self_deaf: {vs.self_deaf}\n"
        f"mute:        {vs.mute}, deaf: {vs.deaf}\n"
    )
    await interaction.response.send_message(f"```\n{msg}```", ephemeral=True)


@tree.command(name="whatisee", description="List voice channels & members I can see (ephemeral).")
async def whatisee(interaction: discord.Interaction):
    lines = []
    for vc in interaction.guild.voice_channels:
        try:
            name = vc.name
            members = [m.display_name for m in vc.members]
            lines.append(f"{name}: {', '.join(members) if members else '(empty)'}")
        except Exception:
            pass
    msg = "\n".join(lines) or "I don't see any voice channels."
    await interaction.response.send_message(f"```\n{msg}\n```", ephemeral=True)

@tree.command(name="leaderboard", description="Show the top users by total screen-share time.")
@app_commands.describe(limit="How many users to list (min 5, max 25).")
async def leaderboard(interaction: discord.Interaction, limit: app_commands.Range[int, 5, 25] = 5):
    await interaction.response.defer(thinking=True, ephemeral=False)
    rows = await top_users(interaction.guild.id, limit)
    if not rows:
        await interaction.followup.send("No data yet. Once members start screen sharing, totals will appear here.")
        return

    lines = []
    for i, (user_id, secs) in enumerate(rows, 1):
        user = interaction.guild.get_member(user_id) or await bot.fetch_user(user_id)
        name = user.display_name if isinstance(user, discord.Member) else (getattr(user, "global_name", None) or user.name)
        lines.append(f"**{i}. {discord.utils.escape_markdown(name)}** — {fmt_duration(secs)}")

    embed = discord.Embed(
        title=f"Top {len(rows)} Streamers — Screen Share Time (All-Time)",
        description="\n".join(lines),
        color=discord.Color.blurple(),
        timestamp=now_utc(),
    )
    await interaction.followup.send(embed=embed)

@tree.command(name="user", description="Show a user's total screen-share time.")
@app_commands.describe(member="The user to look up")
async def user(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(thinking=True, ephemeral=False)
    secs = await get_seconds(interaction.guild.id, member.id)
    embed = discord.Embed(
        title="Screen Share Total",
        description=f"**{member.display_name}** — {fmt_duration(secs)}",
        color=discord.Color.green(),
        timestamp=now_utc(),
    )
    await interaction.followup.send(embed=embed)

# ---- Admin helpers ----



def is_guild_admin():
    def predicate(inter: discord.Interaction) -> bool:
        perms = inter.user.guild_permissions
        return perms.manage_guild or perms.administrator
    return app_commands.check(predicate)


@tree.command(name="modify-add", description="Admin: add N hours to a user's total.")
@is_guild_admin()
@app_commands.describe(member="User to modify", hours="Number of hours to add (can be fractional)")
async def modify_add(interaction: discord.Interaction, member: discord.Member, hours: app_commands.Range[float, 0.0, 10000.0]):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await add_seconds(interaction.guild.id, member.id, hours * 3600.0)
    new_total = await get_seconds(interaction.guild.id, member.id)
    await interaction.followup.send(
        f"Added **{hours}h** to **{member.display_name}**.\n"
        f"New total: **{fmt_duration(new_total)}**."
    )




@tree.command(name="reset_all", description="Reset all users' totals to 0 (Admin only).")
@is_guild_admin()
async def reset_all(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await clear_all(interaction.guild.id)
    await interaction.followup.send("All totals for this server have been reset to **0**.")

@tree.command(name="reset_user", description="Reset one user's total to 0 (Admin only).")
@is_guild_admin()
@app_commands.describe(member="The user to reset")
async def reset_user(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await clear_user(interaction.guild.id, member.id)
    await interaction.followup.send(f"Reset **{member.display_name}** to **0**.")

@tree.command(name="deduct_user", description="Deduct N hours from a user's total (Admin only).")
@is_guild_admin()
@app_commands.describe(member="The user to deduct from", hours="Number of hours to deduct (can be fractional)")
async def deduct_user(interaction: discord.Interaction, member: discord.Member, hours: app_commands.Range[float, 0.0, 10000.0]):
    await interaction.response.defer(thinking=True, ephemeral=True)
    current = await get_seconds(interaction.guild.id, member.id)
    new_value = max(0.0, current - (hours * 3600.0))
    await set_seconds(interaction.guild.id, member.id, new_value)
    await interaction.followup.send(
        f"Deducted **{hours}h** from **{member.display_name}**.\n"
        f"New total: **{fmt_duration(new_value)}**."
    )

# Optional: health command (ephemeral)
@tree.command(name="ping", description="Bot latency.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)} ms", ephemeral=True)

# --------- Run ---------
if __name__ == "__main__":
    bot.run(TOKEN)
