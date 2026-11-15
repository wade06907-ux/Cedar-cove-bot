import asyncio
import datetime as dt
import logging
import uuid

import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger("community_events")


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _event_embed(event, going_count=None):
    starts = _as_utc(event["starts_at"])
    count = len(event.get("going", [])) if going_count is None else going_count
    embed = discord.Embed(
        title=f"🌲 {event['title']}",
        description=event.get("description") or "Come join us!",
        color=discord.Color.from_rgb(112, 137, 91),
    )
    embed.add_field(name="When", value=f"<t:{int(starts.timestamp())}:F>\n<t:{int(starts.timestamp())}:R>", inline=False)
    embed.add_field(name="Going", value=str(count), inline=True)
    embed.add_field(name="Hosted by", value=f"<@{event['created_by']}>", inline=True)
    embed.set_footer(text=f"Cedar Cove Events • Event ID: {event['_id']}")
    return embed


class EventRSVPView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I'm Going",
        emoji="🌲",
        style=discord.ButtonStyle.success,
        custom_id="cedarcove:event:rsvp",
    )
    async def rsvp(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.message is None:
            await interaction.response.send_message("This RSVP button only works in the event server.", ephemeral=True)
            return

        collection = interaction.client.community_events
        event = await collection.find_one({
            "guild_id": interaction.guild.id,
            "message_id": interaction.message.id,
            "status": "scheduled",
        })
        if not event:
            await interaction.response.send_message("This event is no longer accepting RSVPs.", ephemeral=True)
            return

        user_id = interaction.user.id
        already_going = user_id in event.get("going", [])
        if already_going:
            await collection.update_one({"_id": event["_id"]}, {"$pull": {"going": user_id}})
            message = "Your RSVP has been removed."
        else:
            await collection.update_one({"_id": event["_id"]}, {"$addToSet": {"going": user_id}})
            message = "You're on the attendee list. See you there! 🌲"

        updated = await collection.find_one({"_id": event["_id"]})
        try:
            await interaction.message.edit(embed=_event_embed(updated), view=self)
        except discord.HTTPException:
            log.exception("Could not refresh event RSVP message for event %s", event["_id"])
        await interaction.response.send_message(message, ephemeral=True)


event_group = app_commands.Group(name="event", description="Create and manage Cedar Cove community events")


@event_group.command(name="create", description="Create a community event with an RSVP button")
@app_commands.describe(
    title="Name of the event",
    starts_at="Date and time in ISO format with timezone, e.g. 2026-11-20T19:00-05:00",
    description="What members should know about the event",
    channel="Where the event announcement should be posted",
)
async def event_create(
    interaction: discord.Interaction,
    title: str,
    starts_at: str,
    description: str = "",
    channel: discord.TextChannel = None,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Events can only be created inside a server.", ephemeral=True)
        return
    if not (interaction.user.guild_permissions.manage_events or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("You need **Manage Events** permission to create events.", ephemeral=True)
        return
    if not title.strip() or len(title.strip()) > 100 or len(description) > 1000:
        await interaction.response.send_message("Keep the title between 1 and 100 characters and the description under 1,000 characters.", ephemeral=True)
        return

    try:
        parsed = dt.datetime.fromisoformat(starts_at.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("A timezone offset is required.")
        start_utc = parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        await interaction.response.send_message(
            "Use an ISO date/time with a timezone offset, such as `2026-11-20T19:00-05:00`.",
            ephemeral=True,
        )
        return

    if start_utc <= _utc_now() + dt.timedelta(minutes=2):
        await interaction.response.send_message("The event must start at least 2 minutes from now.", ephemeral=True)
        return

    destination = channel or interaction.channel
    if not isinstance(destination, discord.TextChannel):
        await interaction.response.send_message("Choose a regular text channel for the event announcement.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    event_id = uuid.uuid4().hex[:10].upper()
    event = {
        "_id": event_id,
        "guild_id": interaction.guild.id,
        "channel_id": destination.id,
        "message_id": None,
        "title": str(title).strip(),
        "description": str(description).strip(),
        "starts_at": start_utc,
        "created_by": interaction.user.id,
        "going": [],
        "status": "scheduled",
        "reminded_1h": False,
        "reminded_10m": False,
        "created_at": _utc_now(),
    }

    try:
        message = await destination.send(embed=_event_embed(event, 0), view=EventRSVPView())
        event["message_id"] = message.id
        await interaction.client.community_events.insert_one(event)
    except discord.Forbidden:
        await interaction.followup.send(f"I can't post in {destination.mention}. Give me **View Channel**, **Send Messages**, and **Embed Links** permissions.", ephemeral=True)
        return
    except discord.HTTPException:
        log.exception("Failed to publish community event in guild %s", interaction.guild.id)
        await interaction.followup.send("Discord couldn't publish the event. Please try again.", ephemeral=True)
        return

    await interaction.followup.send(
        f"✅ Event **{event['title']}** was posted in {destination.mention}.\nEvent ID: `{event_id}`",
        ephemeral=True,
    )


@event_group.command(name="list", description="Show upcoming community events")
async def event_list(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    cursor = interaction.client.community_events.find({
        "guild_id": interaction.guild.id,
        "status": "scheduled",
        "starts_at": {"$gt": _utc_now()},
    }).sort("starts_at", 1).limit(10)
    events = await cursor.to_list(length=10)
    if not events:
        await interaction.response.send_message("There are no upcoming community events right now.", ephemeral=True)
        return

    embed = discord.Embed(title="🌲 Upcoming Cedar Cove Events", color=discord.Color.from_rgb(112, 137, 91))
    for event in events:
        starts = _as_utc(event["starts_at"])
        embed.add_field(
            name=f"{event['title']} • {len(event.get('going', []))} going",
            value=f"<t:{int(starts.timestamp())}:F> (<t:{int(starts.timestamp())}:R>)\nID: `{event['_id']}` • <#{event['channel_id']}>",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@event_group.command(name="cancel", description="Cancel one of your server's upcoming events")
@app_commands.describe(event_id="The event ID shown by /event list or when it was created")
async def event_cancel(interaction: discord.Interaction, event_id: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not (interaction.user.guild_permissions.manage_events or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("You need **Manage Events** permission to cancel events.", ephemeral=True)
        return

    event = await interaction.client.community_events.find_one({
        "_id": event_id.strip().upper(),
        "guild_id": interaction.guild.id,
        "status": "scheduled",
    })
    if not event:
        await interaction.response.send_message("I couldn't find an active event with that ID in this server.", ephemeral=True)
        return

    await interaction.client.community_events.update_one({"_id": event["_id"]}, {"$set": {"status": "cancelled"}})
    channel = interaction.guild.get_channel(event["channel_id"])
    if isinstance(channel, discord.TextChannel):
        try:
            message = await channel.fetch_message(event["message_id"])
            embed = _event_embed(event)
            embed.title = f"❌ Cancelled: {event['title']}"
            embed.color = discord.Color.red()
            embed.add_field(name="Status", value="This event has been cancelled.", inline=False)
            view = EventRSVPView()
            for child in view.children:
                child.disabled = True
            await message.edit(embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Could not update cancelled event announcement %s", event["_id"])

    await interaction.response.send_message(f"✅ **{event['title']}** has been cancelled.", ephemeral=True)


async def event_reminder_loop(bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = _utc_now()
            cursor = bot.community_events.find({
                "status": "scheduled",
                "starts_at": {"$gt": now, "$lte": now + dt.timedelta(hours=1)},
            })
            events = await cursor.to_list(length=100)
            for event in events:
                starts = _as_utc(event["starts_at"])
                remaining = starts - now
                reminder_key = "reminded_10m" if remaining <= dt.timedelta(minutes=10) else "reminded_1h"
                if event.get(reminder_key):
                    continue
                result = await bot.community_events.update_one(
                    {"_id": event["_id"], "status": "scheduled", reminder_key: {"$ne": True}},
                    {"$set": {reminder_key: True}},
                )
                if not result.modified_count:
                    continue
                guild = bot.get_guild(event["guild_id"])
                channel = guild.get_channel(event["channel_id"]) if guild else None
                if not isinstance(channel, discord.TextChannel):
                    continue
                attendee_ids = event.get("going", [])
                mentions = " ".join(f"<@{uid}>" for uid in attendee_ids)
                label = "10 minutes" if reminder_key == "reminded_10m" else "1 hour"
                content = f"🔔 **{event['title']}** starts in {label}! {mentions}".strip()
                allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
                try:
                    await channel.send(content, allowed_mentions=allowed)
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("Could not send reminder for event %s", event["_id"])
        except Exception:
            log.exception("Community event reminder loop failed")
        await asyncio.sleep(30)


def setup_community_events(bot):
    bot.community_events = bot.community_events_collection
    bot.tree.add_command(event_group)
    bot.add_view(EventRSVPView())
    if not getattr(bot, "_community_event_reminder_task", None):
        bot._community_event_reminder_task = asyncio.create_task(event_reminder_loop(bot))
