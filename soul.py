import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime, timezone, timedelta
import os
import re
import json
import logging
import tempfile
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask
from threading import Thread
from openai import AsyncOpenAI

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LOGGING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SoulBot")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  KEEP ALIVE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Soul Bot is alive!"

def keep_alive():
    port = int(os.getenv("PORT", 8080))
    Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port),
        daemon=True,
    ).start()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ENV
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
load_dotenv()

def require_env(key: str, cast=str):
    val = os.getenv(key)
    if val is None:
        raise RuntimeError(f"Missing env var: {key}")
    val = val.strip()
    if cast is int:
        try:
            return int(val)
        except ValueError:
            raise RuntimeError(f"Env var {key} must be int, got: {val!r}")
    return val

def optional_int_env(key: str):
    val = os.getenv(key, "").strip()
    return int(val) if val else None

TOKEN           = require_env("BOT_TOKEN")
SOUL_STAFF_ROLE = require_env("SOUL_STAFF_ROLE", int)
TICKET_LOG_CH   = require_env("TICKET_LOG_CH", int)
CONVO_LOG_CH    = require_env("CONVO_LOG_CH", int)
MODLOG_CH       = require_env("MODLOG_CH", int)
GROQ_API_KEY    = require_env("GROK_API_KEY")   # env key unchanged

_raw_leadership  = require_env("LEADERSHIP_ROLES")
LEADERSHIP_ROLES = [int(x.strip()) for x in _raw_leadership.split(",") if x.strip()]
if not LEADERSHIP_ROLES:
    raise RuntimeError("LEADERSHIP_ROLES must have at least one role ID")

# Onboarding channels go inside this category
ONBOARDING_CATEGORY_ID = optional_int_env("ONBOARDING_CATEGORY_ID")

COOLDOWN_DAYS = 7

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GROQ CLIENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ai = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)
AI_MODEL = "llama-3.3-70b-versatile"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STRUCTURED QUESTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUESTIONS = [
    ("🌍 Timezone & Availability",  "What's your timezone, and how many hours per week can you actively be online?"),
    ("🛡️ Moderation Experience",    "Do you have any previous moderation or community management experience? Tell us about it."),
    ("💪 Biggest Strength",         "What do you think your biggest strength would be as a Soul Staff member?"),
    ("⚠️ Handling Rule Breakers",   "How would you handle a member who is repeatedly breaking the rules but claims they didn't know about them?"),
    ("📋 Weekly Minimum",           "Our staff minimum is 30 messages/week. Missing it 2 weeks in a row triggers a demotion review. Are you okay with that, and how will you stay consistent?"),
    ("🤝 Conflict Resolution",      "If you disagreed with a decision made by a Supreme or Owner, what would you do?"),
    ("📝 Anything Else",            "Is there anything else the leadership team should know about you before you officially start?"),
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PROMPTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVALUATOR_SYSTEM = """You are a strict answer quality evaluator for a Discord staff interview. 
Your job is to decide if a candidate's answer to a question is valid or not.

Reply with ONLY a JSON object in this exact format — nothing else:
{"valid": true/false, "reason": "short reason if invalid, empty string if valid"}

An answer is INVALID if it:
- Is random gibberish or keyboard spam (e.g. "asdfgh", "zxcvbn", "aaaaaa")
- Contains excessive profanity or is hostile/abusive
- Is completely off-topic or nonsensical (e.g. answering "what is your timezone" with "i like pizza")
- Is a single character or clearly meaningless (e.g. ".", "k", "?")

An answer is VALID if it:
- Makes a genuine attempt to answer, even if short (e.g. "IST", "6 hours", "no experience")
- Is casual or uses slang but still answers the question
- Contains mild swearing in an otherwise genuine answer
- Skips using the word "skip"

Be lenient. Only flag truly bad-faith responses."""

QUESTION_PROMPT_SYSTEM = """You are Aria, the professional staff interviewer for Soul Server, a Discord community.
You are asking structured onboarding questions one at a time. 
Your tone is friendly, warm, and professional — like a senior staff member onboarding a new teammate.
Keep your messages short and natural. Do NOT ask multiple questions at once.
After acknowledging an answer, smoothly transition to the next question you are given."""

SUMMARY_SYSTEM = "You are a staff onboarding assistant for Soul Server. Be concise and honest."

def build_question_prompt(prev_answer: str | None, next_question: str, candidate: str) -> list[dict]:
    if prev_answer is None:
        user_msg = (
            f"The candidate's name is **{candidate}**. "
            f"Start with a brief friendly welcome (1-2 sentences), then ask this question naturally:\n\n"
            f"**Question:** {next_question}"
        )
    else:
        user_msg = (
            f"The candidate just answered: \"{prev_answer}\"\n\n"
            f"Acknowledge their answer briefly (1 sentence), then naturally ask the next question:\n\n"
            f"**Next question:** {next_question}"
        )
    return [
        {"role": "system", "content": QUESTION_PROMPT_SYSTEM},
        {"role": "user",   "content": user_msg},
    ]

def build_summary_prompt(name: str, transcript: str) -> str:
    return (
        f"Here is the interview transcript for candidate **{name}**:\n\n"
        f"{transcript}\n\n"
        "Write a structured summary for the Soul Server leadership team. Use this exact format:\n\n"
        f"**👤 Candidate:** {name}\n"
        "**🕐 Timezone / Availability:** [what they said]\n"
        "**🛡️ Experience:** [moderation/community background]\n"
        "**💪 Strengths:** [what stood out positively]\n"
        "**⚠️ Concerns / Red Flags:** [anything suspicious or worrying, or 'None']\n"
        "**📋 Rules Awareness:** [do they understand the weekly minimum and consequences?]\n"
        "**🤝 Conflict Handling:** [how they'd handle disagreements with leadership]\n"
        "**🗒️ Other Notes:** [anything else worth knowing]\n\n"
        "Be concise and honest. Leadership will make the final call."
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  COOLDOWN STORAGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COOLDOWN_FILE = Path("cooldowns.json")

def load_cooldowns() -> dict:
    if COOLDOWN_FILE.exists():
        try:
            return json.loads(COOLDOWN_FILE.read_text())
        except Exception as e:
            log.warning("cooldowns.json read failed: %s", e)
    return {}

def save_cooldowns(data: dict):
    fd, tmp = tempfile.mkstemp(dir=COOLDOWN_FILE.parent or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        Path(tmp).replace(COOLDOWN_FILE)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise

def set_cooldown(user_id: int):
    data = load_cooldowns()
    data[str(user_id)] = datetime.now(timezone.utc).isoformat()
    save_cooldowns(data)

def get_cooldown_remaining(user_id: int) -> timedelta | None:
    data = load_cooldowns()
    entry = data.get(str(user_id))
    if not entry:
        return None
    try:
        last = datetime.fromisoformat(entry)
    except ValueError:
        return None
    expires = last + timedelta(days=COOLDOWN_DAYS)
    now = datetime.now(timezone.utc)
    return (expires - now) if now < expires else None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BOT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="s!", intents=intents)
active_sessions: set[int] = set()
_session_lock = asyncio.Lock()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def ts(dt=None) -> str:
    return f"<t:{int((dt or datetime.now(timezone.utc)).timestamp())}:F>"

def short_ts(dt=None) -> str:
    return f"<t:{int((dt or datetime.now(timezone.utc)).timestamp())}:R>"

def fmt_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    m, s = divmod(total, 60)
    return f"{m}m {s}s" if m else f"{s}s"

def has_leadership(member: discord.Member) -> bool:
    return any(r.id in LEADERSHIP_ROLES for r in member.roles)

async def resolve_ch(guild: discord.Guild, channel_id: int):
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            log.error("Cannot find channel %d", channel_id)
    return ch

async def send_modlog(guild: discord.Guild, embed: discord.Embed):
    ch = await resolve_ch(guild, MODLOG_CH)
    if ch:
        try:
            await ch.send(embed=embed)
        except Exception as e:
            log.error("Modlog failed: %s", e)

async def send_to(guild: discord.Guild, channel_id: int, **kwargs):
    ch = await resolve_ch(guild, channel_id)
    if ch:
        try:
            await ch.send(**kwargs)
        except Exception as e:
            log.error("send_to %d failed: %s", channel_id, e)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AI CALLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def ai_chat(messages: list[dict], max_tokens: int = 300) -> str:
    try:
        resp = await ai.chat.completions.create(
            model=AI_MODEL,
            max_tokens=max_tokens,
            messages=messages,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("AI error: %s", e)
        return "*(AI error — please contact leadership)*"

async def ai_evaluate(question: str, answer: str) -> tuple[bool, str]:
    """Returns (is_valid, reason). Uses Groq to evaluate answer quality."""
    prompt = f"Question asked: {question}\nCandidate's answer: {answer}"
    try:
        resp = await ai.chat.completions.create(
            model=AI_MODEL,
            max_tokens=80,
            temperature=0,
            messages=[
                {"role": "system", "content": EVALUATOR_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        return bool(data.get("valid", True)), data.get("reason", "")
    except Exception as e:
        log.warning("ai_evaluate failed (%s) — defaulting to valid", e)
        return True, ""  # fail open — don't punish for AI errors

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CREATE ONBOARDING CHANNEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def create_onboarding_channel(guild: discord.Guild, member: discord.Member) -> discord.TextChannel | None:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True,
            manage_messages=True, read_message_history=True, embed_links=True,
        ),
    }
    for role_id in LEADERSHIP_ROLES:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

    category = None
    if ONBOARDING_CATEGORY_ID:
        category = guild.get_channel(ONBOARDING_CATEGORY_ID)
        if category is None:
            log.warning("ONBOARDING_CATEGORY_ID %d not found — no category will be set", ONBOARDING_CATEGORY_ID)

    safe = re.sub(r"[^a-z0-9-]", "", member.name.lower().replace(" ", "-"))[:20] or "staff"

    try:
        return await guild.create_text_channel(
            name=f"onboarding-{safe}",
            overwrites=overwrites,
            category=category,
            topic=f"Staff onboarding for {member.display_name} ({member.id})",
            reason="Soul Staff onboarding",
        )
    except Exception as e:
        log.error("Failed to create onboarding channel for %s: %s", member.name, e)
        return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BROCHURE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_brochure(member: discord.Member) -> discord.Embed:
    e = discord.Embed(color=0x8B6FFF, timestamp=datetime.now(timezone.utc))
    e.set_author(
        name="Soul Server — Staff Onboarding",
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="", value=(
        f"## ✦ Welcome, {member.display_name}!\n"
        "You've been assigned **Soul Staff**. Read this carefully before you begin.\n"
        f"> *Assigned {short_ts()}*"
    ), inline=False)
    e.add_field(name="🏛️  Role Hierarchy", value=(
        "```\n"
        "👑  Owner      →  Final authority\n"
        "💜  Supreme    →  Senior leadership\n"
        "⭐  Soul Staff →  You are here\n"
        "🔧  Mod        →  Chat moderation\n"
        "💎  Sapphire   →  Trusted members\n"
        "```"
    ), inline=False)
    e.add_field(name="📋  Your Duties", value=(
        "**🛡️ Moderation** — monitor chat, mute rule breakers, log incidents\n"
        "**🎉 Events** — host bi-weekly events, manage event channels\n"
        "**🤝 Welcoming** — greet new members daily\n"
        "**📢 Announcements** — post event reminders & updates"
    ), inline=False)
    e.add_field(
        name="💬  Weekly Minimum",
        value="```\n30 messages / week\nMiss 2 weeks in a row → demotion review\n```",
        inline=True,
    )
    e.add_field(
        name="📈  Promotions",
        value="Consistency → Initiative → Internal vote → Private notice\n*No public campaigning.*",
        inline=True,
    )
    e.add_field(name="📌  Staff Rules", value=(
        "> Never abuse permissions — log everything\n"
        "> Keep staff matters in staff channels\n"
        "> Respect every member regardless of rank\n"
        "> Going inactive? Notify the team first\n"
        "> Disagreements go through private channels"
    ), inline=False)
    e.set_footer(text="Press 'Start Onboarding' below when you're ready • Soul Server")
    return e

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ONBOARDING VIEW
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class OnboardingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start Onboarding", style=discord.ButtonStyle.primary,
        emoji="📋", custom_id="soul_start_onboarding",
    )
    async def start_onboarding(self, interaction: discord.Interaction, button: discord.ui.Button):
        member  = interaction.user
        channel = interaction.channel
        guild   = interaction.guild

        await interaction.response.defer(ephemeral=True)

        remaining = get_cooldown_remaining(member.id)
        if remaining:
            await interaction.followup.send(embed=discord.Embed(
                title="⏳ Cooldown Active",
                description=f"You can start again in **{remaining.days}d {remaining.seconds // 3600}h**.",
                color=0xFFA500,
            ), ephemeral=True)
            return

        async with _session_lock:
            if member.id in active_sessions:
                await interaction.followup.send(embed=discord.Embed(
                    description="⚠️ You already have an active session!",
                    color=0xFFA500,
                ), ephemeral=True)
                return
            active_sessions.add(member.id)

        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        await interaction.followup.send(embed=discord.Embed(
            description="✅ Interview starting — answer naturally, no rush!",
            color=0x57F287,
        ), ephemeral=True)

        asyncio.create_task(run_interview(member, guild, channel))

    @discord.ui.button(
        label="I Need Help", style=discord.ButtonStyle.secondary,
        emoji="🆘", custom_id="soul_need_help",
    )
    async def need_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        pings = " ".join(
            interaction.guild.get_role(r).mention
            for r in LEADERSHIP_ROLES
            if interaction.guild.get_role(r)
        )
        try:
            await interaction.channel.send(f"🆘 {interaction.user.mention} needs help with onboarding! {pings}")
        except Exception:
            pass
        await interaction.followup.send(embed=discord.Embed(
            description="✅ Leadership pinged — they'll be here soon!",
            color=0x57F287,
        ), ephemeral=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INTERVIEW FLOW
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def run_interview(member: discord.Member, guild: discord.Guild, channel: discord.TextChannel):
    start_time = datetime.now(timezone.utc)
    convo_log  = []
    flagged    = False
    answers    = {}
    total      = len(QUESTIONS)

    try:
        e = discord.Embed(title="📋 Interview Started", color=0x5865F2, timestamp=start_time)
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name="Member",  value=f"{member.mention} `{member.id}`")
        e.add_field(name="Channel", value=channel.mention)
        e.add_field(name="Status",  value="🟡 In Progress")
        await send_modlog(guild, e)

        prev_answer = None

        for q_idx, (label, question) in enumerate(QUESTIONS, 1):
            async with channel.typing():
                aria_msg = await ai_chat(
                    build_question_prompt(prev_answer, question, member.display_name),
                    max_tokens=200,
                )
            await channel.send(aria_msg)
            convo_log.append(f"**[Aria — Q{q_idx}/{total} — {label}]** {aria_msg}")

            strikes      = 0
            accepted     = False
            final_answer = "*(no answer)*"

            while not accepted:
                def check(m, _ch=channel, _mb=member):
                    return m.author.id == _mb.id and m.channel.id == _ch.id

                try:
                    msg = await bot.wait_for("message", timeout=300.0, check=check)
                except asyncio.TimeoutError:
                    convo_log.append(f"**[{member.display_name} — A{q_idx}]** *(timed out)*")
                    await channel.send(embed=discord.Embed(
                        description="⏱️ No response for 5 minutes — skipping this question.",
                        color=0xFEE75C,
                    ))
                    final_answer = "*(timed out)*"
                    accepted = True
                    continue

                raw = msg.content.strip()
                convo_log.append(f"**[{member.display_name} — A{q_idx}]** {raw}")

                if raw.lower() == "skip":
                    await msg.add_reaction("⏭️")
                    final_answer = "*(skipped)*"
                    accepted = True
                    continue

                valid, reason = await ai_evaluate(question, raw)

                if not valid:
                    strikes += 1
                    remaining = 3 - strikes
                    convo_log.append(f"**[System]** Strike {strikes}/3 — {reason}")
                    if strikes >= 3:
                        flagged = True
                        final_answer = f"*(flagged — 3 bad answers — last: `{raw[:100]}`)*"
                        convo_log.append("**[System]** 3 strikes — session flagged.")
                        await channel.send(embed=discord.Embed(
                            title="🚫 Session Flagged",
                            description=(
                                "You've given **3 invalid responses**.\n"
                                "This session has been flagged and sent to leadership.\n\n"
                                "If this was genuine, reach out to an **Owner or Supreme** directly."
                            ),
                            color=0xED4245,
                        ))
                        accepted = True
                    else:
                        await channel.send(embed=discord.Embed(
                            title="⚠️ Invalid Response",
                            description=(
                                f"**Reason:** {reason}\n"
                                f"Please give a proper answer.\n\n"
                                f"**{remaining} warning{'s' if remaining > 1 else ''} remaining** "
                                "before this session gets flagged."
                            ),
                            color=0xFFA500,
                        ))
                else:
                    await msg.add_reaction("✅")
                    final_answer = raw
                    prev_answer  = raw
                    accepted     = True

            answers[label] = final_answer
            if flagged:
                for remaining_label, _ in QUESTIONS[q_idx:]:
                    answers[remaining_label] = "*(session flagged — not reached)*"
                break

        end_time = datetime.now(timezone.utc)
        duration = end_time - start_time
        summary  = ""

        if not flagged:
            set_cooldown(member.id)
            transcript = "\n".join(convo_log)
            async with channel.typing():
                summary = await ai_chat([
                    {"role": "system", "content": SUMMARY_SYSTEM},
                    {"role": "user",   "content": build_summary_prompt(member.display_name, transcript)},
                ], max_tokens=700)
            async with channel.typing():
                signoff = await ai_chat([
                    {"role": "system", "content": QUESTION_PROMPT_SYSTEM},
                    {"role": "user", "content": (
                        "The interview is now complete. Write a short warm sign-off "
                        "(2-3 sentences max). Tell the candidate their answers have been sent "
                        "to leadership and welcome them to the team."
                    )},
                ], max_tokens=120)
            await channel.send(signoff)
            convo_log.append(f"**[Aria — Sign Off]** {signoff}")
            e2 = discord.Embed(title="✅ Interview Completed", color=0x57F287, timestamp=end_time)
            e2.set_author(name=str(member), icon_url=member.display_avatar.url)
            e2.add_field(name="Member",   value=f"{member.mention} `{member.id}`")
            e2.add_field(name="Status",   value="🟢 Completed")
            e2.add_field(name="Duration", value=fmt_duration(duration))
            await send_modlog(guild, e2)
        else:
            e2 = discord.Embed(title="🚩 Interview Flagged", color=0xED4245, timestamp=end_time)
            e2.set_author(name=str(member), icon_url=member.display_avatar.url)
            e2.add_field(name="Member",   value=f"{member.mention} `{member.id}`")
            e2.add_field(name="Status",   value="🔴 Flagged")
            e2.add_field(name="Duration", value=fmt_duration(duration))
            await send_modlog(guild, e2)

        await post_logs(guild, member, convo_log, answers, summary, flagged, duration)

        cd_msg = await channel.send(embed=discord.Embed(
            description="🗑️ This channel will be deleted in **30s**.",
            color=0x2F3136,
        ))
        for secs in [25, 20, 15, 10, 5]:
            await asyncio.sleep(5)
            try:
                await cd_msg.edit(embed=discord.Embed(
                    description=f"🗑️ This channel will be deleted in **{secs}s**.",
                    color=0x2F3136,
                ))
            except Exception:
                pass
        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Onboarding complete")
        except Exception as ex:
            log.error("Channel delete failed: %s", ex)

    except Exception as ex:
        log.exception("Interview error for %s: %s", member.name, ex)
    finally:
        async with _session_lock:
            active_sessions.discard(member.id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST LOGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def post_logs(guild, member, convo_log, answers, summary, flagged, duration):
    color = 0xED4245 if flagged else 0x8B6FFF

    # Summary embed → TICKET_LOG_CH
    se = discord.Embed(
        title="🚩 Flagged Interview" if flagged else "📋 Staff Interview — Summary",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    se.set_author(name=f"{member.display_name} • {member.name}", icon_url=member.display_avatar.url)
    se.set_thumbnail(url=member.display_avatar.url)
    se.add_field(name="Member Info", value=(
        f"**Mention:** {member.mention}\n"
        f"**ID:** `{member.id}`\n"
        f"**Joined:** {short_ts(member.joined_at)}\n"
        f"**Account age:** {short_ts(member.created_at)}\n"
        f"**Duration:** {fmt_duration(duration)}"
    ), inline=False)
    if flagged:
        se.add_field(name="⚠️ Flagged", value="Session flagged — timeout or spam.", inline=False)
    # Per-question answers
    for label, answer in answers.items():
        se.add_field(name=label, value=(answer[:1020] if answer else "*(empty)*"), inline=False)
    se.add_field(
        name="🤖 AI Summary",
        value=summary[:1024] if summary else "*(not generated — session was flagged)*",
        inline=False,
    )
    se.set_footer(text=f"Soul Staff Onboarding • ID: {member.id}")
    await send_to(guild, TICKET_LOG_CH, embed=se)

    # Full transcript → CONVO_LOG_CH
    full = "\n".join(convo_log)
    if not full:
        return
    chunks = [full[i:i+3900] for i in range(0, len(full), 3900)]
    for idx, chunk in enumerate(chunks):
        te = discord.Embed(
            title=(
                f"📜 Interview Transcript — {member.display_name}"
                + (f" ({idx+1}/{len(chunks)})" if len(chunks) > 1 else "")
            ),
            description=chunk,
            color=0xED4245 if flagged else 0x36393F,
            timestamp=datetime.now(timezone.utc),
        )
        if idx == 0:
            te.set_author(name=f"{member.name} • ID: {member.id}", icon_url=member.display_avatar.url)
        te.set_footer(text=("⚠️ Flagged" if flagged else "✅ Completed") + f" • {ts()}")
        await send_to(guild, CONVO_LOG_CH, embed=te)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ROLE LISTENER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_ids = {r.id for r in before.roles}
    after_ids  = {r.id for r in after.roles}

    if SOUL_STAFF_ROLE not in before_ids and SOUL_STAFF_ROLE in after_ids:
        channel = await create_onboarding_channel(after.guild, after)
        if channel:
            await channel.send(
                content=f"👋 Welcome {after.mention}! Read the brochure below and click **Start Onboarding** when ready.",
                embed=build_brochure(after),
                view=OnboardingView(),
            )
        e = discord.Embed(title="⭐ Soul Staff Assigned", color=0x8B6FFF, timestamp=datetime.now(timezone.utc))
        e.set_author(name=str(after), icon_url=after.display_avatar.url)
        e.add_field(name="Member",  value=f"{after.mention} `{after.name}`")
        e.add_field(name="Channel", value=channel.mention if channel else "*(failed)*")
        e.add_field(name="Time",    value=ts())
        e.set_footer(text=f"ID: {after.id}")
        await send_modlog(after.guild, e)

    for role_id in after_ids - before_ids - {SOUL_STAFF_ROLE}:
        role = after.guild.get_role(role_id)
        if not role:
            continue
        e = discord.Embed(title="✅ Role Added", color=0x57F287, timestamp=datetime.now(timezone.utc))
        e.set_author(name=str(after), icon_url=after.display_avatar.url)
        e.add_field(name="Member", value=after.mention)
        e.add_field(name="Role",   value=role.mention)
        e.add_field(name="Time",   value=ts())
        e.set_footer(text=f"ID: {after.id}")
        await send_modlog(after.guild, e)

    for role_id in before_ids - after_ids:
        role = after.guild.get_role(role_id)
        if not role:
            continue
        e = discord.Embed(title="❌ Role Removed", color=0xED4245, timestamp=datetime.now(timezone.utc))
        e.set_author(name=str(after), icon_url=after.display_avatar.url)
        e.add_field(name="Member", value=after.mention)
        e.add_field(name="Role",   value=role.name)
        e.add_field(name="Time",   value=ts())
        e.set_footer(text=f"ID: {after.id}")
        await send_modlog(after.guild, e)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MODLOG EVENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.event
async def on_member_join(member: discord.Member):
    e = discord.Embed(title="📥 Member Joined", color=0x57F287, timestamp=datetime.now(timezone.utc))
    e.set_author(name=str(member), icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Member",          value=f"{member.mention} `{member.name}`")
    e.add_field(name="Account Created", value=short_ts(member.created_at))
    e.add_field(name="Joined",          value=ts())
    e.set_footer(text=f"ID: {member.id}")
    await send_modlog(member.guild, e)

@bot.event
async def on_member_remove(member: discord.Member):
    action, mod = "Left the server", None
    try:
        async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
            if entry.target.id == member.id:
                action = f"Kicked — _{entry.reason or 'no reason'}_"
                mod = entry.user
                break
    except Exception:
        pass
    e = discord.Embed(title="📤 Member Left / Kicked", color=0xFEE75C, timestamp=datetime.now(timezone.utc))
    e.set_author(name=str(member), icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Member", value=f"`{member.name}`")
    e.add_field(name="Action", value=action)
    if mod:
        e.add_field(name="Kicked By", value=mod.mention)
    e.add_field(name="Time", value=ts())
    e.set_footer(text=f"ID: {member.id}")
    await send_modlog(member.guild, e)

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    reason, mod = "Unknown", None
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                reason = entry.reason or "No reason given"
                mod = entry.user
                break
    except Exception:
        pass
    e = discord.Embed(title="🔨 Member Banned", color=0xED4245, timestamp=datetime.now(timezone.utc))
    e.set_author(name=str(user), icon_url=user.display_avatar.url)
    e.set_thumbnail(url=user.display_avatar.url)
    e.add_field(name="User",   value=f"{user.mention} `{user.name}`")
    e.add_field(name="Reason", value=reason)
    if mod:
        e.add_field(name="Banned By", value=mod.mention)
    e.add_field(name="Time", value=ts())
    e.set_footer(text=f"ID: {user.id}")
    await send_modlog(guild, e)

@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    e = discord.Embed(title="✅ Member Unbanned", color=0x57F287, timestamp=datetime.now(timezone.utc))
    e.set_author(name=str(user), icon_url=user.display_avatar.url)
    e.add_field(name="User", value=f"`{user.name}`")
    e.add_field(name="Time", value=ts())
    e.set_footer(text=f"ID: {user.id}")
    await send_modlog(guild, e)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    e = discord.Embed(title="🗑️ Message Deleted", color=0xFF6B6B, timestamp=datetime.now(timezone.utc))
    e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    e.add_field(name="Author",  value=f"{message.author.mention} `{message.author.name}`", inline=True)
    e.add_field(name="Channel", value=message.channel.mention, inline=True)
    e.add_field(name="Content", value=message.content[:1000] if message.content else "*(no text)*", inline=False)
    e.set_footer(text=f"Author ID: {message.author.id}")
    await send_modlog(message.guild, e)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    e = discord.Embed(title="✏️ Message Edited", color=0xFFA500, timestamp=datetime.now(timezone.utc))
    e.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
    e.add_field(name="Author",  value=before.author.mention, inline=True)
    e.add_field(name="Channel", value=before.channel.mention, inline=True)
    e.add_field(name="Before",  value=before.content[:500] or "*(empty)*", inline=False)
    e.add_field(name="After",   value=after.content[:500] or "*(empty)*", inline=False)
    e.add_field(name="Jump",    value=f"[Go to message]({after.jump_url})", inline=False)
    e.set_footer(text=f"Author ID: {before.author.id}")
    await send_modlog(before.guild, e)

@bot.event
async def on_guild_channel_create(channel):
    if channel.name.startswith("onboarding-"):
        return
    e = discord.Embed(title="📁 Channel Created", color=0x57F287, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Channel", value=f"{channel.mention} `{channel.name}`")
    e.add_field(name="Type",    value=str(channel.type))
    e.add_field(name="Time",    value=ts())
    await send_modlog(channel.guild, e)

@bot.event
async def on_guild_channel_delete(channel):
    if channel.name.startswith("onboarding-"):
        return
    e = discord.Embed(title="🗂️ Channel Deleted", color=0xED4245, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Channel", value=f"`{channel.name}`")
    e.add_field(name="Type",    value=str(channel.type))
    e.add_field(name="Time",    value=ts())
    await send_modlog(channel.guild, e)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if before.channel == after.channel:
        return
    if after.channel:
        e = discord.Embed(title="🔊 Joined Voice", color=0x57F287, timestamp=datetime.now(timezone.utc))
        e.add_field(name="Member",  value=member.mention)
        e.add_field(name="Channel", value=f"**{after.channel.name}**")
    else:
        e = discord.Embed(title="🔇 Left Voice", color=0xED4245, timestamp=datetime.now(timezone.utc))
        e.add_field(name="Member",  value=member.mention)
        e.add_field(name="Channel", value=f"**{before.channel.name}**")
    e.set_author(name=str(member), icon_url=member.display_avatar.url)
    e.set_footer(text=short_ts())
    await send_modlog(member.guild, e)

@bot.event
async def on_guild_role_create(role: discord.Role):
    e = discord.Embed(title="🆕 Role Created", color=0x57F287, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Role", value=f"{role.mention} `{role.name}`")
    e.add_field(name="Time", value=ts())
    await send_modlog(role.guild, e)

@bot.event
async def on_guild_role_delete(role: discord.Role):
    e = discord.Embed(title="🗑️ Role Deleted", color=0xED4245, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Role", value=f"`{role.name}`")
    e.add_field(name="Time", value=ts())
    await send_modlog(role.guild, e)

@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    if before.name != after.name:
        e = discord.Embed(title="🏠 Server Name Changed", color=0xFFA500, timestamp=datetime.now(timezone.utc))
        e.add_field(name="Before", value=before.name)
        e.add_field(name="After",  value=after.name)
        await send_modlog(after, e)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SLASH COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.tree.command(name="onboard", description="Manually create an onboarding channel for a member")
@app_commands.describe(member="Member to onboard")
async def onboard_cmd(interaction: discord.Interaction, member: discord.Member):
    if not has_leadership(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    channel = await create_onboarding_channel(interaction.guild, member)
    if channel:
        await channel.send(
            content=f"👋 Welcome {member.mention}! Read the brochure below and click **Start Onboarding** when ready.",
            embed=build_brochure(member),
            view=OnboardingView(),
        )
        await interaction.followup.send(f"✅ Created: {channel.mention}", ephemeral=True)
    else:
        await interaction.followup.send("❌ Failed — check bot permissions.", ephemeral=True)

@bot.tree.command(name="cooldown", description="Check a member's onboarding cooldown")
@app_commands.describe(member="Member to check")
async def cooldown_cmd(interaction: discord.Interaction, member: discord.Member):
    if not has_leadership(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    remaining = get_cooldown_remaining(member.id)
    if remaining:
        await interaction.response.send_message(embed=discord.Embed(
            description=f"⏳ **{member.display_name}** has **{remaining.days}d {remaining.seconds // 3600}h** left.",
            color=0xFFA500,
        ), ephemeral=True)
    else:
        await interaction.response.send_message(embed=discord.Embed(
            description=f"✅ **{member.display_name}** has no active cooldown.",
            color=0x57F287,
        ), ephemeral=True)

@bot.tree.command(name="resetcooldown", description="Reset a member's onboarding cooldown")
@app_commands.describe(member="Member to reset")
async def reset_cooldown(interaction: discord.Interaction, member: discord.Member):
    if not has_leadership(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    data = load_cooldowns()
    data.pop(str(member.id), None)
    save_cooldowns(data)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"✅ Cooldown reset for **{member.display_name}**.",
        color=0x57F287,
    ), ephemeral=True)

@bot.tree.command(name="staffinfo", description="Show Soul Staff requirements")
async def staffinfo(interaction: discord.Interaction):
    e = discord.Embed(title="⭐ Soul Staff — Quick Reference", color=0x8B6FFF, timestamp=datetime.now(timezone.utc))
    e.add_field(name="💬 Min Messages / Week", value="30",                    inline=True)
    e.add_field(name="🎉 Event Hosting",       value="Bi-weekly",             inline=True)
    e.add_field(name="⚠️ Demotion Trigger",    value="2 weeks below minimum", inline=True)
    e.add_field(name="⏳ Onboarding Cooldown", value=f"{COOLDOWN_DAYS} days", inline=True)
    await interaction.response.send_message(embed=e)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  READY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.event
async def on_ready():
    bot.add_view(OnboardingView())
    await bot.tree.sync()
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching, name="you 👁️"),
    )
    log.info("Online — %s | Guilds: %d", bot.user, len(bot.guilds))

keep_alive()
bot.run(TOKEN)
