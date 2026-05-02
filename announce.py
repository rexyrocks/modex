import discord
import asyncio
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

ANNOUNCEMENT_CH = 1462897343102718214

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    ch = client.get_channel(ANNOUNCEMENT_CH)
    if ch is None:
        ch = await client.fetch_channel(ANNOUNCEMENT_CH)

    e = discord.Embed(
        color=0x8B6FFF,
        timestamp=datetime.now(timezone.utc),
    )
    e.set_author(
        name="Soul Server",
        icon_url=ch.guild.icon.url if ch.guild.icon else None,
    )
    e.add_field(name="", value=(
        "## ✦ Welcome to Soul Server!\n"
        "We've been working hard behind the scenes to make this place better for everyone. "
        "Here's everything that's new — read carefully.\n"
    ), inline=False)

    e.add_field(name="🤖  Meet Modex", value=(
        "> Modex is our custom-built bot — built from scratch for Soul Server.\n"
        "> It handles **staff onboarding**, **moderation logging**, and **server management** so everything runs smoother.\n"
        "> No more messy manual processes."
    ), inline=False)

    e.add_field(name="🛡️  Staff System", value=(
        "> We're rebuilding the staff team properly.\n"
        "> **Staff applications are opening very soon** — stay tuned for the announcement.\n"
        "> The process will be structured, fair, and handled entirely through Modex."
    ), inline=False)

    e.add_field(name="✅  Verification", value=(
        "> Verification has been fixed and is now fully working.\n"
        "> Head over to <#1462897343102718214> and verify yourself to get full access."
    ), inline=False)

    e.add_field(name="📋  What's Coming", value=(
        "```\n"
        "→  Staff applications\n"
        "→  Leveling system\n"
        "→  Verification upgrades\n"
        "→  More server events\n"
        "```"
    ), inline=False)

    e.set_footer(text="Soul Server  •  More updates coming soon")

    await ch.send(
        content="@everyone",
        embed=e,
        allowed_mentions=discord.AllowedMentions(everyone=True),
    )
    print("Announcement sent!")
    await client.close()

client.run(TOKEN)