import asyncio
import datetime as dt
import logging
import os
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

log = logging.getLogger("community_events")
EVENT_TIMEZONE_NAME = os.getenv("EVENT_TIMEZONE", "America/New_York")
try:
    EVENT_TIMEZONE = ZoneInfo(EVENT_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    log.error("Invalid EVENT_TIMEZONE %r; using America/New_York", EVENT_TIMEZONE_NAME)
    EVENT_TIMEZONE_NAME = "America/New_York"
    EVENT_TIMEZONE = ZoneInfo(EVENT_TIMEZONE_NAME)


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _event_embed(event):
    starts = _as_utc(event["starts_at"])
    ends = _as_utc(event["ends_at"])
    embed = discord.Embed(
        title=f"🌲 {event['title']}",
        description=event.get("description") or "Come join us!",
        color=discord.Color.from_rgb(112, 137, 91),
    )
    embed.add_field(
        name="When",
        value=f"<t:{int(starts.timestamp())}:F> – <t:{int(ends.timestamp())}:t>\n<t:{int(starts.timestamp())}:R>",
        inline=False,
    )
    embed.add_field(name="Going", value=str(len(event.get("going", []))), inline=True)
    embed.add_field(name="Hosted by", value=f"<@{event['created_by']}>", inline=True)
    if event.get("discord_event_url"):
        embed.add_field(name="Discord Event", value=f"[Open in Discord]({event['discord_event_url']})", inline=False)
    embed.set_footer(text=f"Cedar Cove Events • Event ID: {event['_id']}")
    return embed


class EventRSVPView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I'm Joining",
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
            await interaction.response.send_message("This event has ended or is no longer accepting RSVPs.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id in event.get("going", []):
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

    @discord.ui.button(
        label="End Event",
        emoji="🕯️",
        style=discord.ButtonStyle.danger,
        custom_id="cedarcove:event:end",
    )
    async def end_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.message is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This button only works inside the event server.", ephemeral=True)
            return
        if not (interaction.user.guild_permissions.manage_events or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("You need **Manage Events** permission to end an event.", ephemeral=True)
            return

        collection = interaction.client.community_events
        event = await collection.find_one({
            "guild_id": interaction.guild.id,
            "message_id": interaction.message.id,
            "status": "scheduled",
        })
        if not event:
            await interaction.response.send_message("This event has already ended.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            scheduled = await interaction.guild.fetch_scheduled_event(int(event["discord_event_id"]))
            if scheduled.status == discord.EventStatus.active:
                await scheduled.end(reason=f"Ended by {interaction.user}")
            elif scheduled.status == discord.EventStatus.scheduled:
                await scheduled.cancel(reason=f"Ended by {interaction.user}")
        except discord.NotFound:
            log.warning("Discord scheduled event %s was already missing", event.get("discord_event_id"))
        except discord.Forbidden:
            await interaction.followup.send("I can't end the Discord event. Please give me **Manage Events** permission.", ephemeral=True)
            return
        except discord.HTTPException:
            log.exception("Failed to end Discord event %s", event.get("discord_event_id"))
            await interaction.followup.send("Discord couldn't end the event. Please try again.", ephemeral=True)
            return

        await collection.update_one(
            {"_id": event["_id"], "status": "scheduled"},
            {"$set": {"status": "ended", "ended_at": _utc_now(), "ended_by": interaction.user.id}},
        )
        updated = dict(event)
        updated["status"] = "ended"
        embed = _event_embed(updated)
        embed.title = f"🕯️ Ended: {event['title']}"
        embed.color = discord.Color.dark_grey()
        embed.add_field(name="Status", value="This event has ended.", inline=False)
        try:
            await interaction.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            log.exception("Could not update ended event announcement %s", event["_id"])
        await interaction.followup.send(f"✅ **{event['title']}** has ended. The RSVP button remains on the announcement.", ephemeral=True)


event_group = app_commands.Group(name="event", description="Create and manage Cedar Cove community events")


@event_group.command(name="create", description="Create a real Discord Scheduled Event")
@app_commands.describe(
    title="Name of the event",
    start_date="Start date (YYYY-MM-DD)",
    start_time="Start time (24-hour HH:MM)",
    end_date="End date (YYYY-MM-DD)",
    end_time="End time (24-hour HH:MM)",
    repeats="Should this event repeat weekly?",
    description="Optional details about the event",
)
async def event_create(
    interaction: discord.Interaction,
    title: str,
    start_date: str,
    start_time: str,
    end_date: str,
    end_time: str,
    repeats: bool,
    description: str = "",
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
        start_local = dt.datetime.strptime(f"{start_date.strip()} {start_time.strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=EVENT_TIMEZONE)
        end_local = dt.datetime.strptime(f"{end_date.strip()} {end_time.strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=EVENT_TIMEZONE)
        start_utc = start_local.astimezone(dt.timezone.utc)
        end_utc = end_local.astimezone(dt.timezone.utc)
    except ValueError:
        await interaction.response.send_message(
            f"Use YYYY-MM-DD for dates and 24-hour HH:MM for times. Event timezone: {EVENT_TIMEZONE_NAME}.",
            ephemeral=True,
        )
        return

    now = _utc_now()
    if start_utc <= now + dt.timedelta(minutes=2):
        await interaction.response.send_message("The event must start at least 2 minutes from now.", ephemeral=True)
        return
    if end_utc <= start_utc:
        await interaction.response.send_message("The end date and time must be after the start date and time.", ephemeral=True)
        return
    if end_utc - start_utc > dt.timedelta(days=7):
        await interaction.response.send_message("A single event cannot be longer than 7 days.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    event_id = uuid.uuid4().hex[:10].upper()
    recurrence_rule = None
    if repeats:
        recurrence_rule = {
            "start": start_utc.isoformat(),
            "frequency": 2,
            "interval": 1,
            "by_weekday": [start_local.weekday()],
        }

    payload = {
        "name": title.strip(),
        "description": description.strip() or None,
        "scheduled_start_time": start_utc.isoformat(),
        "scheduled_end_time": end_utc.isoformat(),
        "privacy_level": 2,
        "entity_type": 3,
        "entity_metadata": {"location": "Cedar Cove Discord"},
    }
    if recurrence_rule:
        payload["recurrence_rule"] = recurrence_rule

    try:
        route = discord.http.Route(
            "POST",
            "/guilds/{guild_id}/scheduled-events",
            guild_id=interaction.guild.id,
        )
        created = await interaction.client.http.request(
            route,
            json=payload,
            reason=f"Cedar Cove community event created by {interaction.user}",
        )
        discord_event_id = str(created["id"])
        discord_event_url = f"https://discord.com/events/{interaction.guild.id}/{discord_event_id}"

        announcement_channel = interaction.channel
        if not isinstance(announcement_channel, discord.TextChannel):
            scheduled = await interaction.guild.fetch_scheduled_event(int(discord_event_id))
            await scheduled.delete(reason="Could not find a text channel for the event announcement")
            await interaction.followup.send("Run this command in a regular text channel so I can post the RSVP message.", ephemeral=True)
            return

        event = {
            "_id": event_id,
            "guild_id": interaction.guild.id,
            "channel_id": announcement_channel.id,
            "message_id": None,
            "discord_event_id": discord_event_id,
            "discord_event_url": discord_event_url,
            "title": title.strip(),
            "description": description.strip(),
            "starts_at": start_utc,
            "ends_at": end_utc,
            "timezone": EVENT_TIMEZONE_NAME,
            "repeats_weekly": repeats,
            "created_by": interaction.user.id,
            "going": [],
            "status": "scheduled",
            "reminded_1h": False,
            "reminded_10m": False,
            "created_at": now,
        }
        message = await announcement_channel.send(
            content=f"@here **A new Cedar Cove event has been scheduled!**\n{discord_event_url}",
            embed=_event_embed(event),
            view=EventRSVPView(),
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False),
        )
        event["message_id"] = message.id
        await interaction.client.community_events.insert_one(event)
    except discord.Forbidden:
        log.exception("Missing permissions while creating community event in guild %s", interaction.guild.id)
        await interaction.followup.send("I need **Manage Events**, plus **View Channel**, **Send Messages**, and **Embed Links** in this channel.", ephemeral=True)
        return
    except (discord.HTTPException, KeyError):
        log.exception("Failed to create community event in guild %s", interaction.guild.id)
        await interaction.followup.send("Discord couldn't create the event. Check that I have **Manage Events** permission and try again.", ephemeral=True)
        return
    except Exception:
        log.exception("Unexpected error creating community event in guild %s", interaction.guild.id)
        await interaction.followup.send("Something went wrong while saving the event. Check the bot logs.", ephemeral=True)
        return

    repeat_text = " It will repeat weekly." if repeats else ""
    await interaction.followup.send(
        f"✅ Created a real Discord event: **{event['title']}**. It is now in the server's Events tab.{repeat_text}\n[Open Discord Event]({discord_event_url})\nRSVP announcement: {announcement_channel.mention}",
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
        ends = _as_utc(event["ends_at"])
        repeat_text = " • Weekly" if event.get("repeats_weekly") else ""
        embed.add_field(
            name=f"{event['title']} • {len(event.get('going', []))} joining{repeat_text}",
            value=f"<t:{int(starts.timestamp())}:F> – <t:{int(ends.timestamp())}:t>\n[Discord Event]({event['discord_event_url']}) • ID: `{event['_id']}`",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@event_group.command(name="cancel", description="End one of your server's upcoming events")
@app_commands.describe(event_id="The event ID shown by /event list or when it was created")
async def event_cancel(interaction: discord.Interaction, event_id: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not (interaction.user.guild_permissions.manage_events or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("You need **Manage Events** permission to end events.", ephemeral=True)
        return
    event = await interaction.client.community_events.find_one({
        "_id": event_id.strip().upper(),
        "guild_id": interaction.guild.id,
        "status": "scheduled",
    })
    if not event:
        await interaction.response.send_message("I couldn't find an active event with that ID in this server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        scheduled = await interaction.guild.fetch_scheduled_event(int(event["discord_event_id"]))
        if scheduled.status == discord.EventStatus.active:
            await scheduled.end(reason=f"Ended by {interaction.user}")
        elif scheduled.status == discord.EventStatus.scheduled:
            await scheduled.cancel(reason=f"Ended by {interaction.user}")
    except discord.NotFound:
        pass
    except discord.Forbidden:
        await interaction.followup.send("I can't end the Discord event. Please give me **Manage Events** permission.", ephemeral=True)
        return
    except discord.HTTPException:
        log.exception("Failed to end Discord event %s", event.get("discord_event_id"))
        await interaction.followup.send("Discord couldn't end the event. Please try again.", ephemeral=True)
        return

    await interaction.client.community_events.update_one(
        {"_id": event["_id"], "status": "scheduled"},
        {"$set": {"status": "ended", "ended_at": _utc_now(), "ended_by": interaction.user.id}},
    )
    channel = interaction.guild.get_channel(event["channel_id"])
    if isinstance(channel, discord.TextChannel):
        try:
            message = await channel.fetch_message(event["message_id"])
            embed = _event_embed(event)
            embed.title = f"🕯️ Ended: {event['title']}"
            embed.color = discord.Color.dark_grey()
            embed.add_field(name="Status", value="This event has ended.", inline=False)
            await message.edit(embed=embed, view=EventRSVPView())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Could not update ended event announcement %s", event["_id"])
    await interaction.followup.send(f"✅ **{event['title']}** has ended. The RSVP button remains on the announcement.", ephemeral=True)


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
                mentions = " ".join(f"<@{uid}>" for uid in event.get("going", []))
                label = "10 minutes" if reminder_key == "reminded_10m" else "1 hour"
                try:
                    await channel.send(
                        f"🔔 **{event['title']}** starts in {label}! {mentions}".strip(),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
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
