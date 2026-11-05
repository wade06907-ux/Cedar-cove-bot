"""Session-authenticated staff dashboard API for Mega Bot.

Required environment variable:
  AUTH_API_URL=https://verification-server-gjqt.onrender.com

CORS uses wildcard origins without browser cookies; API calls require a valid
bearer session token verified by the verification service.
"""
import os
import asyncio
import base64
import html
import re
import time
from datetime import datetime, timezone
import secrets
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
from aiohttp import web

AUTH_API_URL = (os.getenv("AUTH_API_URL") or "https://verification-server-gjqt.onrender.com").rstrip("/")
DASHBOARD_API_BUILD = "staff-config-sync-2026-11-05"
STAFF_ROLE_NAMES = {"owner": "Owner", "co owner": "Co Owner", "admin": "Admin", "administrator": "Admin"}


def setup_staff(bot, get_store, logger):
    bot.staff_dashboard_store = get_store("staff_dashboard_configs")
    if not getattr(bot, "_staff_rank_commands_registered", False):
        async def _staff_rank_command(interaction, member, action, reason):
            if not interaction.guild or not interaction.user.guild_permissions.manage_roles:
                return await interaction.response.send_message("You need to run this in a server and have Manage Roles permission.", ephemeral=True)
            guild = interaction.guild
            if member.bot or member.id == interaction.user.id:
                return await interaction.response.send_message("Choose another human staff member.", ephemeral=True)
            config = await bot.staff_dashboard_store.get(str(guild.id), {})
            ladder = (((config.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
            ladder_roles = []
            for item in ladder:
                if isinstance(item, dict) and str(item.get("roleId") or "").isdigit():
                    role = guild.get_role(int(item["roleId"]))
                    if role:
                        ladder_roles.append((item, role))
            if not ladder_roles:
                return await interaction.response.send_message("Set up the staff ladder in the dashboard first.", ephemeral=True)
            me = guild.me
            if not me or not me.guild_permissions.manage_roles or any(role >= me.top_role for _, role in ladder_roles):
                return await interaction.response.send_message("I need Manage Roles and my highest role must be above all staff ladder roles.", ephemeral=True)
            if member.top_role >= me.top_role:
                return await interaction.response.send_message("I cannot manage this member because their highest role is above or equal to mine.", ephemeral=True)
            indexes = [i for i, (_, role) in enumerate(ladder_roles) if role in member.roles]
            current = max(indexes) if indexes else -1
            target_index = current - 1 if action == "promote" else current + 1
            if action == "demote" and current < 0:
                return await interaction.response.send_message("That member does not currently hold a staff ladder role.", ephemeral=True)
            if target_index < 0 or target_index >= len(ladder_roles):
                edge = "highest" if action == "promote" else "lowest"
                return await interaction.response.send_message(f"That member is already at the {edge} rank.", ephemeral=True)
            old_rank = ladder_roles[current][0].get("name", ladder_roles[current][1].name) if current >= 0 else "Not on ladder"
            target_item, target_role = ladder_roles[target_index]
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                remove = [role for _, role in ladder_roles if role in member.roles and role != target_role]
                if remove:
                    await member.remove_roles(*remove, reason=f"Staff {action} by {interaction.user}: {reason}")
                if target_role not in member.roles:
                    await member.add_roles(target_role, reason=f"Staff {action} by {interaction.user}: {reason}")
            except (discord.Forbidden, discord.HTTPException):
                return await interaction.followup.send("Discord could not update the member roles. Check my permissions and role hierarchy.", ephemeral=True)
            word = "promoted" if action == "promote" else "demoted"
            new_rank = target_item.get("name", target_role.name)
            await _update_staff_ladder_post(guild, f"{member.display_name} was {word}: {old_rank} -> {new_rank}")
            await interaction.followup.send(f"{member.mention} was {word} from {old_rank} to {new_rank}.", ephemeral=True)

        @bot.tree.command(name="promote", description="Promote a staff member to the next staff rank.")
        @discord.app_commands.describe(member="Staff member", reason="Reason for the promotion")
        async def promote_staff(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
            await _staff_rank_command(interaction, member, "promote", reason[:500])

        @bot.tree.command(name="demote", description="Demote a staff member to the next lower staff rank.")
        @discord.app_commands.describe(member="Staff member", reason="Reason for the demotion")
        async def demote_staff(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
            await _staff_rank_command(interaction, member, "demote", reason[:500])

        bot._staff_rank_commands_registered = True

    bot.staff_dashboard_audit_store = get_store("staff_dashboard_audit")
    bot.role_nicknames_store = get_store("role_nicknames")
    bot.audit_log_cursor_store = get_store("audit_log_cursors")
    if not getattr(bot, "_role_nickname_listener_added", False):
        bot.add_listener(_role_nickname_member_update, "on_member_update")
        bot._role_nickname_listener_added = True
    if not getattr(bot, "_welcome_leave_listeners_added", False):
        bot.add_listener(_welcome_leave_member_join, "on_member_join")
        bot.add_listener(_welcome_leave_member_remove, "on_member_remove")
        bot._welcome_leave_listeners_added = True
    bot.staff_dashboard_logger = logger
    listeners = [
        (_dashboard_log_raw_message_delete, 'on_raw_message_delete'), (_dashboard_log_message_edit, 'on_message_edit'),
        (_dashboard_log_channel_create, 'on_guild_channel_create'), (_dashboard_log_channel_delete, 'on_guild_channel_delete'),
        (_dashboard_log_channel_update, 'on_guild_channel_update'), (_dashboard_log_role_create, 'on_guild_role_create'),
        (_dashboard_log_role_delete, 'on_guild_role_delete'), (_dashboard_log_role_update, 'on_guild_role_update'),
        (_dashboard_log_member_join, 'on_member_join'), (_dashboard_log_member_leave, 'on_member_remove'),
        (_dashboard_log_member_ban, 'on_member_ban'), (_dashboard_log_member_unban, 'on_member_unban'),
        (_dashboard_log_member_update, 'on_member_update'), (_dashboard_log_voice_state_update, 'on_voice_state_update'),
        (_dashboard_honeypot_message, 'on_message'),
    ]
    for listener, event_name in listeners:
        marker = '_dashboard_listener_' + event_name
        if not getattr(bot, marker, False):
            bot.add_listener(listener, event_name)
            setattr(bot, marker, True)
    if not getattr(bot, '_audit_log_reader_listener_added', False):
        async def _start_audit_log_reader():
            if not getattr(bot, '_audit_log_reader_task_started', False):
                bot._audit_log_reader_task = asyncio.create_task(_audit_log_reader_loop(bot))
                bot._audit_log_reader_task_started = True
        bot.add_listener(_start_audit_log_reader, 'on_ready')
        bot._audit_log_reader_listener_added = True
    if not getattr(bot, '_verification_refresh_listener_added', False):
        async def _start_verification_refresh():
            if not getattr(bot, '_verification_refresh_task_started', False):
                bot._verification_refresh_task = asyncio.create_task(_verification_refresh_loop(bot))
                bot._verification_refresh_task_started = True
        bot.add_listener(_start_verification_refresh, 'on_ready')
        bot._verification_refresh_listener_added = True



async def _audit_log_reader_loop(bot):
    """Stream fresh Discord audit entries quickly with SCNX-inspired embeds."""
    await bot.wait_until_ready()
    cursors = getattr(bot, "audit_log_cursor_store", None)
    logger = getattr(bot, "staff_dashboard_logger", None) or __import__("logging").getLogger("mega_bot.audit")
    action_styles = {
        "ban": ("🔨", discord.Color.red()),
        "kick": ("👢", discord.Color.red()),
        "unban": ("🔓", discord.Color.green()),
        "member_update": ("👤", discord.Color.orange()),
        "member_role_update": ("🎭", discord.Color.blurple()),
        "role_create": ("🆕", discord.Color.green()),
        "role_delete": ("🗑️", discord.Color.red()),
        "role_update": ("📝", discord.Color.orange()),
        "channel_create": ("📁", discord.Color.green()),
        "channel_delete": ("🗑️", discord.Color.red()),
        "channel_update": ("📝", discord.Color.orange()),
        "message_delete": ("🗑️", discord.Color.red()),
        "message_bulk_delete": ("🧹", discord.Color.red()),
        "overwrite_create": ("🔐", discord.Color.green()),
        "overwrite_update": ("🔐", discord.Color.orange()),
        "overwrite_delete": ("🔐", discord.Color.red()),
        "webhook_create": ("🔗", discord.Color.green()),
        "webhook_delete": ("🔗", discord.Color.red()),
        "webhook_update": ("🔗", discord.Color.orange()),
        "invite_create": ("✉️", discord.Color.green()),
        "invite_delete": ("✉️", discord.Color.red()),
    }
    change_labels = {
        "name": "Name", "nick": "Nickname", "topic": "Topic", "bitrate": "Bitrate",
        "slowmode_delay": "Slowmode", "rtc_region": "Region", "nsfw": "Age restricted",
        "hoist": "Displayed separately", "mentionable": "Mentionable", "permissions": "Permissions",
        "color": "Color", "position": "Position", "communication_disabled_until": "Timeout",
        "deaf": "Server deafened", "mute": "Server muted",
    }
    while not bot.is_closed():
        for guild in list(bot.guilds):
            try:
                settings = await _dashboard_module(bot, guild.id, "logging")
                me = guild.me
                if not me or not me.guild_permissions.view_audit_log:
                    continue

                cursor_key = str(guild.id)
                cursor = await cursors.get(cursor_key, None) if cursors else None
                cursor_text = str(cursor).strip() if cursor is not None else ""
                if not cursor_text.isdigit():
                    newest = None
                    async for entry in guild.audit_logs(limit=1):
                        newest = entry
                        break
                    if newest is not None and cursors:
                        await cursors.set(cursor_key, str(newest.id))
                        logger.info("Audit cursor initialized (no history replay): guild=%s cursor=%s", guild.id, newest.id)
                    elif cursor is not None:
                        logger.warning("Invalid audit cursor reset: guild=%s value=%r", guild.id, cursor)
                        if cursors:
                            await cursors.set(cursor_key, "")
                    continue

                cursor_id = int(cursor_text)
                entries = []
                async for entry in guild.audit_logs(limit=100):
                    if entry.id <= cursor_id:
                        break
                    entries.append(entry)
                entries.reverse()

                for entry in entries:
                    action_key = getattr(entry.action, "name", str(entry.action)).lower().replace(" ", "_")
                    event_key = {
                        "ban": "memberBan", "unban": "memberUnban",
                        "kick": "moderationActions", "member_update": "moderationActions",
                        "member_role_update": "memberRoleUpdate",
                        "role_create": "roleCreate", "role_delete": "roleDelete", "role_update": "roleUpdate",
                        "channel_create": "channelCreate", "channel_delete": "channelDelete", "channel_update": "channelUpdate",
                        "message_delete": "messageDelete",
                    }.get(action_key)
                    events = settings.get("events") if isinstance(settings.get("events"), dict) else {}
                    event_settings = events.get(event_key) if event_key and isinstance(events.get(event_key), dict) else {}
                    if event_key and not event_settings.get("enabled", settings.get(event_key + "Enabled", True)):
                        continue
                    channel_id = str(
                        event_settings.get("channelId")
                        or (settings.get(event_key + "ChannelId") if event_key else "")
                        or settings.get("channelId")
                        or settings.get("logChannelId")
                        or ""
                    )
                    channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
                    if not isinstance(channel, discord.TextChannel):
                        continue
                    perms = channel.permissions_for(me)
                    if not perms or not perms.view_channel or not perms.send_messages or not perms.embed_links:
                        continue
                    emoji, color = action_styles.get(action_key, ("📋", discord.Color.blurple()))
                    action = str(entry.action).replace("_", " ").title()
                    actor = entry.user
                    target = entry.target
                    embed = discord.Embed(
                        title=f"{emoji} {action}",
                        color=color,
                        timestamp=entry.created_at,
                    )
                    embed.set_author(
                        name=str(actor) if actor else "Unknown moderator",
                        icon_url=actor.display_avatar.url if actor else (guild.icon.url if guild.icon else None),
                    )
                    if target is not None:
                        embed.add_field(name="Target", value=f"{target}\\n`{getattr(target, 'id', 'unknown')}`"[:1024], inline=True)
                    if entry.user_id:
                        embed.add_field(name="Moderator ID", value=f"`{entry.user_id}`", inline=True)
                    if entry.reason:
                        embed.add_field(name="Reason", value=str(entry.reason)[:1024], inline=False)

                    details = []
                    changes = getattr(entry, "changes", None)
                    # discord.py exposes AuditLogChanges as an object, not an iterable.
                    # Read its known before/after attributes safely.
                    change_keys = (
                        "name", "nick", "topic", "bitrate", "slowmode_delay", "rtc_region",
                        "nsfw", "hoist", "mentionable", "permissions", "color", "position",
                        "communication_disabled_until", "deaf", "mute", "roles", "channel",
                        "overwrites", "type", "icon", "owner_id", "afk_timeout",
                        "verification_level", "default_notifications", "content_filter",
                    )
                    if changes is not None:
                        for key in change_keys:
                            before = getattr(getattr(changes, "before", None), key, None)
                            after = getattr(getattr(changes, "after", None), key, None)
                            if before is None and after is None:
                                continue
                            label = change_labels.get(key, key.replace("_", " ").title() or "Updated")
                            details.append(f"**{label}:** {str(before)[:350] if before is not None else '—'} → {str(after)[:350] if after is not None else '—'}")
                        role_changes = getattr(changes, "roles", None)
                        if role_changes:
                            details.append("**Roles:** " + str(role_changes)[:700])
                    if details:
                        embed.add_field(name="Changes", value="\\n".join(details)[:1024], inline=False)
                    if guild.icon:
                        embed.set_thumbnail(url=guild.icon.url)
                    embed.set_footer(text=f"{guild.name} • Cedar Cove Logs", icon_url=None)
                    try:
                        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.warning("Audit delivery failed: guild=%s channel=%s entry=%s error=%s", guild.id, channel.id, entry.id, exc)
                        break
                    if cursors:
                        await cursors.set(cursor_key, str(entry.id))
            except discord.Forbidden:
                logger.warning("Cannot read Discord audit log: guild=%s; grant View Audit Log", guild.id)
            except (discord.HTTPException, ValueError, TypeError) as exc:
                logger.warning("Audit reader failed for guild=%s: %s", guild.id, exc)
            except Exception:
                logger.exception("Unexpected audit reader error for guild=%s", guild.id)
        await asyncio.sleep(2)


async def _dashboard_module(bot, guild_id, name):
    store = getattr(bot, 'staff_dashboard_store', None)
    if store is None:
        return {}
    config = await store.get(str(guild_id), {})
    module = ((config.get('modules') or {}).get(name) or {})
    if name == 'logging':
        module = dict(module)
        events = module.get('events') or {}
        if isinstance(events, dict):
            for event_key, event_settings in events.items():
                if not isinstance(event_settings, dict):
                    continue
                if 'enabled' in event_settings:
                    module[event_key + 'Enabled'] = bool(event_settings.get('enabled'))
                if event_settings.get('channelId') is not None:
                    module[event_key + 'ChannelId'] = str(event_settings.get('channelId') or '')
    return module

async def _log_event(guild, event_key, title, description):
    """Send a single polished SCNX-style event embed to the configured log channel."""
    bot = guild._state._get_client()
    settings = await _dashboard_module(bot, guild.id, 'logging')
    logger = getattr(bot, 'staff_dashboard_logger', None) or __import__('logging').getLogger('mega_bot.audit')
    events = settings.get('events') if isinstance(settings.get('events'), dict) else {}
    event_settings = events.get(event_key) if isinstance(events.get(event_key), dict) else {}
    enabled = event_settings.get('enabled', settings.get(event_key + 'Enabled', True))
    if not enabled:
        return
    channel_id = str(
        event_settings.get('channelId')
        or settings.get(event_key + 'ChannelId')
        or settings.get('channelId')
        or settings.get('logChannelId')
        or ''
    )
    channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if not isinstance(channel, discord.TextChannel):
        logger.info('Audit event skipped: guild=%s event=%s; no valid log channel configured', guild.id, event_key)
        return
    me = guild.me
    perms = channel.permissions_for(me) if me else None
    if not perms or not perms.send_messages or not perms.embed_links:
        logger.warning('Audit destination permission issue: guild=%s event=%s channel=%s send_messages=%s embed_links=%s', guild.id, event_key, channel.id, bool(perms and perms.send_messages), bool(perms and perms.embed_links))
        return

    event_styles = {
        'messageDelete': ('🗑️', discord.Color.red()),
        'messageEdit': ('✏️', discord.Color.orange()),
        'channelCreate': ('📁', discord.Color.green()),
        'channelDelete': ('🗑️', discord.Color.red()),
        'channelUpdate': ('📝', discord.Color.orange()),
        'roleCreate': ('🆕', discord.Color.green()),
        'roleDelete': ('🗑️', discord.Color.red()),
        'roleUpdate': ('📝', discord.Color.orange()),
        'memberJoin': ('📥', discord.Color.green()),
        'memberLeave': ('📤', discord.Color.orange()),
        'memberBan': ('🔨', discord.Color.red()),
        'memberUnban': ('🔓', discord.Color.green()),
        'memberUpdate': ('👤', discord.Color.blurple()),
        'nicknameUpdate': ('🏷️', discord.Color.orange()),
        'memberRoleUpdate': ('🎭', discord.Color.blurple()),
        'voiceUpdate': ('🔊', discord.Color.blurple()),
        'moderationActions': ('⚖️', discord.Color.orange()),
    }
    emoji, color = event_styles.get(event_key, ('📋', discord.Color.blurple()))
    embed = discord.Embed(
        title=f'{emoji}  {title}',
        description=str(description)[:4000],
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    if guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)
    else:
        embed.set_author(name=guild.name)
    embed.set_footer(text='Cedar Cove • Server Logs')
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        logger.info('Audit event delivered: guild=%s event=%s channel=%s', guild.id, event_key, channel.id)
    except discord.Forbidden as exc:
        logger.error('Audit event forbidden: guild=%s event=%s channel=%s error=%s', guild.id, event_key, channel.id, exc)
    except discord.HTTPException as exc:
        logger.error('Audit event failed: guild=%s event=%s channel=%s status=%s error=%s', guild.id, event_key, channel.id, exc.status, exc)

async def _dashboard_honeypot_message(message):
    if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member): return
    bot = message.guild._state._get_client()
    module = await _dashboard_module(bot, message.guild.id, 'honeypot')
    if not module.get('enabled') or str(message.channel.id) != str(module.get('channelId') or ''): return
    if message.author.id == message.guild.owner_id:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await message.channel.send(
                f"❌ {message.author.mention}, the server owner cannot be banned by the honeypot.",
                delete_after=10,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        return
    if message.author.guild_permissions.administrator: return
    reason = 'Cedar Cove honeypot: message sent in the configured trap channel'
    punishment = str(module.get('punishment') or 'ban').lower()
    try:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log = getattr(bot, 'staff_dashboard_logger', None)
            if log:
                log.warning('Honeypot could not delete trap message %s: %s', message.id, exc)
        if punishment == 'kick':
            await message.author.kick(reason=reason); action = 'kick'
        elif punishment == 'softban':
            await message.guild.ban(message.author, reason=reason, delete_message_seconds=0)
            await message.guild.unban(discord.Object(id=message.author.id), reason='Honeypot softban complete'); action = 'softban'
        elif punishment == 'mute':
            from datetime import timedelta
            duration = max(1, min(40320, int(module.get('durationMinutes') or 60)))
            await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=duration), reason=reason); action = 'timeout'
        else:
            await message.author.ban(reason=reason, delete_message_seconds=0); action = 'ban'
        store = getattr(bot, 'staff_dashboard_store', None)
        if store is not None:
            config = await store.get(str(message.guild.id), {})
            hp = config.setdefault('modules', {}).setdefault('honeypot', {})
            hp['banCount'] = int(hp.get('banCount') or 0) + (1 if action in {'ban', 'softban'} else 0)
            hp['actionCount'] = int(hp.get('actionCount') or 0) + 1
            await store.set(str(message.guild.id), config)
    except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
        log = getattr(bot, 'staff_dashboard_logger', None)
        if log: log.warning('Honeypot punishment failed: %s', exc)

async def _dashboard_log_raw_message_delete(payload):
    cached = getattr(payload, 'cached_message', None)
    state = getattr(cached, '_state', None) or getattr(payload, '_state', None)
    bot = state._get_client() if state and hasattr(state, '_get_client') else None
    guild_id = getattr(payload, 'guild_id', None)
    guild = bot.get_guild(guild_id) if bot and guild_id else None
    logger = getattr(bot, 'staff_dashboard_logger', None) if bot else None
    logger = logger or __import__('logging').getLogger('mega_bot.audit')

    if guild is None:
        logger.warning(
            'Message delete received but guild/client could not be resolved: guild=%s channel=%s message=%s',
            guild_id, getattr(payload, 'channel_id', None), getattr(payload, 'message_id', None)
        )
        return

    if cached is not None and getattr(getattr(cached, 'author', None), 'bot', False):
        return

    channel = guild.get_channel(getattr(payload, 'channel_id', 0))
    channel_name = channel.mention if channel else f"channel {getattr(payload, 'channel_id', 'unknown')}"
    message_id = getattr(payload, 'message_id', 'unknown')
    if cached is not None:
        author = getattr(cached, 'author', None)
        author_text = f"{author} ({getattr(author, 'id', 'unknown')})" if author else "Unknown author"
        content = getattr(cached, 'content', '') or '[no text]'
        description = (
            f"Author: {author_text}\\nChannel: {channel_name}\\n"
            f"Message ID: {message_id}\\nContent: {content[:2500]}"
        )
    else:
        description = (
            f"Message deleted\\nChannel: {channel_name}\\nMessage ID: {message_id}\\n"
            "Content was not cached; Discord does not provide the deleted message body."
        )

    logger.info(
        'MESSAGE DELETE EVENT RECEIVED | guild=%s | channel=%s | message=%s | cached=%s',
        guild.id, getattr(payload, 'channel_id', None), message_id, cached is not None
    )
    try:
        await _log_event(guild, 'messageDelete', 'Message deleted', description)
    except Exception:
        logger.exception('Message-delete audit delivery crashed: guild=%s message=%s', guild.id, message_id)

async def _dashboard_log_message_edit(before, after):
    if after.guild and not after.author.bot and before.content != after.content: await _log_event(after.guild, 'messageEdit', 'Message edited', 'Author: {} ({})\nChannel: {}\nBefore: {}\nAfter: {}'.format(after.author, after.author.id, after.channel.mention, (before.content or '[empty]')[:1200], (after.content or '[empty]')[:1200]))
async def _dashboard_log_channel_create(channel):
    if channel.guild: await _log_event(channel.guild, 'channelCreate', 'Channel created', '{} ({})'.format(channel.name, channel.id))
async def _dashboard_log_channel_delete(channel):
    if channel.guild: await _log_event(channel.guild, 'channelDelete', 'Channel deleted', '{} ({})'.format(channel.name, channel.id))
async def _dashboard_log_channel_update(before, after):
    if after.guild and (before.name != after.name or before.category_id != after.category_id or getattr(before, 'topic', None) != getattr(after, 'topic', None) or getattr(before, 'slowmode_delay', None) != getattr(after, 'slowmode_delay', None) or getattr(before, 'nsfw', None) != getattr(after, 'nsfw', None)): await _log_event(after.guild, 'channelUpdate', 'Channel updated', 'Name: {} -> {}\\nCategory: {} -> {}\\nTopic: {} -> {}\\nSlowmode: {} -> {}\\nAge restricted: {} -> {}\\nChannel ID: {}'.format(before.name, after.name, getattr(before.category, 'name', 'None'), getattr(after.category, 'name', 'None'), getattr(before.topic, None) or '[none]', getattr(after.topic, None) or '[none]', getattr(before.slowmode_delay, 0), getattr(after.slowmode_delay, 0), getattr(before.nsfw, False), getattr(after.nsfw, False), after.id))
async def _dashboard_log_role_create(role):
    await _log_event(role.guild, 'roleCreate', 'Role created', '{} ({})'.format(role.name, role.id))
async def _dashboard_log_role_delete(role):
    await _log_event(role.guild, 'roleDelete', 'Role deleted', '{} ({})'.format(role.name, role.id))
async def _dashboard_log_role_update(before, after):
    if before.name != after.name or before.permissions != after.permissions or before.color != after.color or before.hoist != after.hoist or before.mentionable != after.mentionable or before.position != after.position: await _log_event(after.guild, 'roleUpdate', 'Role updated', 'Name: {} -> {}\\nPermissions changed: {}\\nColor: {} -> {}\\nDisplayed separately: {} -> {}\\nMentionable: {} -> {}\\nPosition: {} -> {}\\nRole ID: {}'.format(before.name, after.name, before.permissions != after.permissions, before.color, after.color, before.hoist, after.hoist, before.mentionable, after.mentionable, before.position, after.position, after.id))
async def _dashboard_log_voice_state_update(member, before, after):
    if member.bot:
        return
    before_channel = getattr(before, "channel", None)
    after_channel = getattr(after, "channel", None)
    if before_channel == after_channel and before.self_mute == after.self_mute and before.self_deaf == after.self_deaf:
        return
    before_name = before_channel.mention if before_channel else "Not connected"
    after_name = after_channel.mention if after_channel else "Not connected"
    description = (
        f"Member: {member} ({member.id})\\n"
        f"Channel: {before_name} → {after_name}\\n"
        f"Self mute: {before.self_mute} → {after.self_mute}\\n"
        f"Self deaf: {before.self_deaf} → {after.self_deaf}"
    )
    await _log_event(member.guild, "voiceUpdate", "Voice state updated", description)


async def _dashboard_log_member_join(member):
    if not member.bot: await _log_event(member.guild, 'memberJoin', 'Member joined', '{} ({})'.format(member, member.id))
async def _dashboard_log_member_leave(member):
    if not member.bot: await _log_event(member.guild, 'memberLeave', 'Member left', '{} ({})'.format(member, member.id))
async def _dashboard_log_member_ban(guild, user):
    await _log_event(guild, 'memberBan', 'Member banned', '{} ({})'.format(user, user.id))
async def _dashboard_log_member_unban(guild, user):
    await _log_event(guild, 'memberUnban', 'Member unbanned', '{} ({})'.format(user, user.id))
async def _update_staff_ladder_post(guild, event_text="Staff roster updated"):
    bot = guild._state._get_client()
    store = getattr(bot, "staff_dashboard_store", None)
    if store is None:
        return
    config = await store.get(str(guild.id), {})
    modules = config.get("modules") or {}
    ladder = ((modules.get("staff_ladder") or {}).get("items") or [])
    settings = modules.get("staff_actions") or {}
    channel_id = str(settings.get("staffLadderAnnouncementChannelId") or "")
    channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if not isinstance(channel, discord.TextChannel):
        return

    ranks = []
    role_tokens = {}
    for item in ladder:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("roleId") or "")
        role = guild.get_role(int(rid)) if rid.isdigit() else None
        if role:
            rank_name = str(item.get("name") or role.name)
            ranks.append((rank_name, role))
            slug = re.sub(r"[^a-z0-9]+", "_", rank_name.casefold()).strip("_")
            role_tokens["role_" + slug] = rank_name

    lines = ["# 🌲 Cedar Cove Staff Roster", ""]
    if event_text and event_text not in {"Staff roster updated", "Staff ladder updated"}:
        lines.extend([f"> {event_text}", ""])

    name_style = str(settings.get("staffRosterNameStyle") or "prefix").lower()
    prefix_template = str(settings.get("staffRosterPrefix") or "[role_{role_name}]")
    for rank_name, role in ranks:
        normalized = re.sub(r"[^a-z0-9]+", " ", rank_name.casefold()).strip()
        if normalized == "owner":
            heading = "👑 Owner"
        elif normalized in {"co owner", "coowner"}:
            heading = "⭐ Co-Owner"
        elif normalized in {"admin", "administrator", "administrators"}:
            heading = "🛡️ Administrators"
        elif normalized in {"mod", "moderator", "moderators"}:
            heading = "🔨 Moderators"
        elif normalized in {"trial staff", "trial", "trainee"}:
            heading = "🌱 Trial Staff"
        else:
            heading = "⭐ " + rank_name

        members = [member for member in guild.members if not member.bot and role in member.roles]
        members.sort(key=lambda member: member.display_name.casefold())
        lines.extend(["", f"## {heading}", ""])
        if not members:
            lines.append("* *Open*")
            continue

        slug = re.sub(r"[^a-z0-9]+", "_", rank_name.casefold()).strip("_")
        token_map = dict(role_tokens)
        token_map["role_name"] = rank_name
        token_map["role_" + slug] = rank_name
        for member in members:
            if name_style == "display_name":
                rendered = member.display_name
            else:
                base_name = member.display_name
                for configured_rank, _ in ranks:
                    base_name = re.sub(r"^\s*\[" + re.escape(configured_rank) + r"\]\s*", "", base_name, count=1, flags=re.IGNORECASE)
                prefix = prefix_template.replace("{role_name}", rank_name).replace("{display_name}", base_name)
                for token, value in token_map.items():
                    prefix = re.sub(r"(?<![a-zA-Z0-9_])" + re.escape(token) + r"(?![a-zA-Z0-9_])", value, prefix, flags=re.IGNORECASE)
                rendered = prefix + " " + base_name
            lines.append(f"* **{rendered}**")

    content = "\n".join(lines)
    if len(content) > 1990:
        content = content[:1987] + "..."

    old_mid = str(settings.get("staffLadderAnnouncementMessageId") or "")
    old_cid = str(settings.get("staffLadderAnnouncementPreviousChannelId") or channel.id)
    if old_mid.isdigit() and old_cid.isdigit():
        old_channel = guild.get_channel(int(old_cid))
        if isinstance(old_channel, discord.TextChannel):
            try:
                old_msg = await old_channel.fetch_message(int(old_mid))
                if old_channel.id == channel.id:
                    await old_msg.edit(content=content, embeds=[], allowed_mentions=discord.AllowedMentions.none())
                    settings["staffLadderAnnouncementPreviousChannelId"] = str(channel.id)
                    modules["staff_actions"] = settings
                    config["modules"] = modules
                    await store.set(str(guild.id), config)
                    return
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    try:
        sent = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        settings["staffLadderAnnouncementMessageId"] = str(sent.id)
        settings["staffLadderAnnouncementPreviousChannelId"] = str(channel.id)
        modules["staff_actions"] = settings
        config["modules"] = modules
        await store.set(str(guild.id), config)
    except (discord.Forbidden, discord.HTTPException):
        pass

async def _dashboard_log_member_update(before, after):
    if before.guild and before.nick != after.nick: await _log_event(after.guild, 'nicknameUpdate', 'Nickname changed', '{}: {} -> {}'.format(after, before.nick or before.name, after.nick or after.name))
    if before.guild and {r.id for r in before.roles} != {r.id for r in after.roles}:
        await _log_event(after.guild, 'memberRoleUpdate', 'Member roles changed', '{} ({})'.format(after, after.id))
        cfg = await after.guild._state._get_client().staff_dashboard_store.get(str(after.guild.id), {})
        ladder = (((cfg.get('modules') or {}).get('staff_ladder') or {}).get('items') or [])
        role_ids = {int(x.get("roleId")) for x in ladder if isinstance(x, dict) and str(x.get("roleId") or "").isdigit()}
        if any(r.id in role_ids for r in set(after.roles) - set(before.roles)) or any(r.id in role_ids for r in set(before.roles) - set(after.roles)):
            await _update_staff_ladder_post(after.guild, "Staff ladder updated")

async def _verification_refresh_loop(bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in list(bot.guilds):
            try:
                module = await _dashboard_module(bot, guild.id, 'verification')
                if not module.get('enabled') or not module.get('refresh'): continue
                channel_id = str(module.get('channelId') or '')
                channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
                if not isinstance(channel, discord.TextChannel): continue
                content = str(module.get('message') or 'Read the rules and complete verification to access Cedar Cove.')[:1800]
                message_id = str(module.get('messageId') or '')
                try:
                    if message_id.isdigit():
                        old = await channel.fetch_message(int(message_id))
                        if old.author.id == bot.user.id:
                            if old.content != content: await old.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                            continue
                    sent = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                    module['messageId'] = str(sent.id)
                    store = getattr(bot, 'staff_dashboard_store', None)
                    if store is not None:
                        config = await store.get(str(guild.id), {})
                        config.setdefault('modules', {})['verification'] = module
                        await store.set(str(guild.id), config)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException): pass
            except Exception as exc:
                log = getattr(bot, 'staff_dashboard_logger', None)
                if log: log.warning('Verification refresh failed for guild %s: %s', guild.id, exc)
        await asyncio.sleep(300)

async def get_honeypot_stats(request):
    user, error = await _authorized(request)
    if error: return error
    gid = request.match_info['guild_id']
    if not gid.isdigit() or request.app['staff_bot'].get_guild(int(gid)) is None: return _json(request, {'error': 'The bot is not in that server.'}, 404)
    config = await request.app['staff_store'].get(gid, {})
    module = ((config.get('modules') or {}).get('honeypot') or {})
    return _json(request, {'enabled': bool(module.get('enabled')), 'banCount': int(module.get('banCount') or 0), 'actionCount': int(module.get('actionCount') or 0), 'channelId': str(module.get('channelId') or '')})


def _origin(request: web.Request) -> str:
    return (request.headers.get("Origin") or "").rstrip("/")


def _headers(request: web.Request) -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
        "Vary": "Origin",
        "Cache-Control": "no-store",
    }


def _json(request, data, status=200):
    return web.json_response(data, status=status, headers=_headers(request))


async def _authorized(request: web.Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not auth[7:].strip():
        return None, _json(request, {"error": "Sign in again to continue."}, 401)
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(AUTH_API_URL + "/api/auth/me", headers={"Authorization": auth}) as resp:
                if resp.status != 200:
                    return None, _json(request, {"error": "Your website session is invalid or expired."}, 401)
                user = await resp.json(content_type=None)
                if not bool(user.get("whitelisted", False)):
                    return None, _json(request, {"error": "Your website account has not been approved for dashboard access."}, 403)
    except Exception as exc:
        request.app["staff_logger"].warning("Dashboard auth service unavailable: %s", exc)
        return None, _json(request, {"error": "Could not verify your login session."}, 502)
    return user, None


async def _guild(request, user):
    gid = request.match_info.get("guild_id") or str((await request.json()).get("guildId", ""))
    if not gid.isdigit() or request.app["staff_bot"].get_guild(int(gid)) is None:
        return None, _json(request, {"error": "This server is not allowed for dashboard management."}, 403)
    guild = request.app["staff_bot"].get_guild(int(gid))
    if guild is None:
        return None, _json(request, {"error": "The bot is not in that Discord server."}, 404)
    return guild, None


async def options_handler(request):
    return web.Response(status=204, headers=_headers(request))


async def api_build(request):
    # Public diagnostic endpoint: confirms which dashboard API code Render is serving.
    return _json(request, {"service": "cedar-cove-bot-dashboard-api", "build": DASHBOARD_API_BUILD, "cors": "wildcard", "originAllowlistRequired": False})


async def api_status(request):
    user, error = await _authorized(request)
    if error:
        return error
    return _json(request, {"status": "ok", "user": user.get("username"), "guilds": len(request.app["staff_bot"].guilds)})


async def list_guilds(request):
    user, error = await _authorized(request)
    if error:
        return error
    bot = request.app["staff_bot"]
    items = [
        {"id": str(guild.id), "name": guild.name, "icon": guild.icon.url if guild.icon else None}
        for guild in sorted(bot.guilds, key=lambda item: item.name.casefold())
    ]
    return _json(request, {"guilds": items})


async def list_channels(request):
    user, error = await _authorized(request)
    if error:
        return error
    guild, error = await _guild(request, user)
    if error:
        return error
    channels = []
    for channel in sorted(guild.channels, key=lambda c: (getattr(c, "position", 0), c.name.casefold())):
        if isinstance(channel, discord.CategoryChannel):
            kind = "category"
        elif isinstance(channel, discord.TextChannel) or isinstance(channel, discord.ForumChannel):
            kind = "text"
        elif isinstance(channel, discord.VoiceChannel):
            kind = "voice"
        else:
            continue
        channels.append({"id": str(channel.id), "name": channel.name, "type": kind, "categoryId": str(channel.category_id) if getattr(channel, "category_id", None) else None})
    roles = [{"id": str(r.id), "name": r.name} for r in sorted(guild.roles, key=lambda r: r.position, reverse=True) if not r.is_default()]
    return _json(request, {"channels": channels, "roles": roles})


async def list_roles(request: web.Request):
    user, error = await _authorized(request)
    if error:
        return error
    guild, error = await _guild(request, user)
    if error:
        return error
    roles = [
        {"id": str(role.id), "name": role.name}
        for role in sorted(guild.roles, key=lambda role: role.position, reverse=True)
        if not role.is_default()
    ]
    return _json(request, {"roles": roles})


async def get_staff_ladder(request):
    user, error = await _authorized(request)
    if error:
        return error
    guild, error = await _guild(request, user)
    if error:
        return error
    config = await request.app["staff_store"].get(str(guild.id), {})
    ladder = ((config.get("modules") or {}).get("staff_ladder") or {}).get("items", [])
    return _json(request, {"guildId": str(guild.id), "items": ladder if isinstance(ladder, list) else []})


async def save_staff_ladder(request):
    user, error = await _authorized(request)
    if error:
        return error
    guild, error = await _guild(request, user)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid JSON body."}, 400)
    items = body.get("items")
    if not isinstance(items, list) or len(items) > 30:
        return _json(request, {"error": "Provide up to 30 ladder ranks."}, 400)
    cleaned = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:60]
        description = str(item.get("description") or "").strip()[:240]
        role_id = str(item.get("roleId") or "").strip()
        if not name or not role_id.isdigit() or role_id in seen:
            return _json(request, {"error": "Each rank needs a name and a unique Discord role."}, 400)
        role = guild.get_role(int(role_id))
        if role is None or role.is_default():
            return _json(request, {"error": "A selected Discord role no longer exists."}, 400)
        seen.add(role_id)
        cleaned.append({"id": str(item.get("id") or secrets.token_hex(6)), "name": name, "description": description, "roleId": role_id})
    config = await request.app["staff_store"].get(str(guild.id), {})
    modules = config.setdefault("modules", {})
    modules["staff_ladder"] = {"items": cleaned, "updatedAt": int(time.time()), "updatedBy": str(user.get("username") or "")}
    await request.app["staff_store"].set(str(guild.id), config)
    return _json(request, {"ok": True, "items": cleaned})


async def get_config(request):
    user, error = await _authorized(request)
    if error:
        return error
    gid = request.match_info["guild_id"]
    if not gid.isdigit() or request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "This server is not allowed."}, 403)
    config = await request.app["staff_store"].get(gid, {})
    if not isinstance(config, dict):
        config = {}
    if not isinstance(config.get("modules"), dict):
        config["modules"] = {}
    return _json(request, {"guildId": gid, "config": config})


async def save_config(request):
    user, error = await _authorized(request)
    if error:
        return error
    gid = request.match_info["guild_id"]
    if not gid.isdigit() or request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "This server is not allowed."}, 403)
    if request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "The bot is not in that server."}, 404)
    try:
        body = await request.json()
        config = body.get("config")
        if not isinstance(config, dict) or not isinstance(config.get("modules"), dict):
            return _json(request, {"error": "Invalid configuration format."}, 400)
        raw = str(config.get("guildId", gid))
        if raw != gid:
            return _json(request, {"error": "Configuration server ID does not match the request."}, 400)
        if len(str(config)) > 100_000:
            return _json(request, {"error": "Configuration is too large."}, 413)
        # Merge submitted settings into the existing guild config. Some dashboard
        # panels submit only their own module; replacing the whole document here
        # silently erased every other saved module (including logging/welcome).
        existing = await request.app["staff_store"].get(gid, {})
        existing_modules = existing.get("modules") if isinstance(existing, dict) else {}
        if not isinstance(existing_modules, dict):
            existing_modules = {}
        clean = {
            "guildId": gid,
            "timezone": str(config.get("timezone", existing.get("timezone", "")))[:100],
            "modules": {str(k): dict(v) for k, v in existing_modules.items()
                        if re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", str(k)) and isinstance(v, dict)},
        }
        for name, settings in config["modules"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", str(name)) or not isinstance(settings, dict):
                continue
            entry = dict(clean["modules"].get(str(name), {}))
            for key, value in settings.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    entry[str(key)[:60]] = value if not isinstance(value, str) else value[:4000]
                elif name == "logging" and key == "events" and isinstance(value, dict):
                    events = entry.setdefault("events", {})
                    if not isinstance(events, dict):
                        events = {}
                    for event_key, event_settings in value.items():
                        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", str(event_key)) or not isinstance(event_settings, dict):
                            continue
                        saved_event = dict(events.get(str(event_key), {}))
                        if "enabled" in event_settings:
                            saved_event["enabled"] = bool(event_settings["enabled"])
                        if event_settings.get("channelId") is not None:
                            saved_event["channelId"] = str(event_settings.get("channelId") or "")[:30]
                        events[str(event_key)] = saved_event
                    entry["events"] = events
            clean["modules"][str(name)] = entry

        # Respect per-event switches from the dashboard instead of forcing them on.
        # Normalize nested event settings to legacy flat keys used by listeners.
        logging_settings = clean["modules"].setdefault("logging", {})
        events = logging_settings.get("events")
        if isinstance(events, dict):
            for event_key, event_settings in events.items():
                if not isinstance(event_settings, dict):
                    continue
                if "enabled" in event_settings:
                    logging_settings[event_key + "Enabled"] = bool(event_settings["enabled"])
                if "channelId" in event_settings:
                    logging_settings[event_key + "ChannelId"] = str(event_settings.get("channelId") or "")

        await request.app["staff_store"].set(gid, clean)
        await _audit(request, gid, user, "save_config", "Dashboard configuration saved")
        return _json(request, {"ok": True, "config": clean})
    except (ValueError, TypeError):
        return _json(request, {"error": "Invalid JSON body."}, 400)


async def publish_panel(request):
    user, error = await _authorized(request)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid JSON body."}, 400)
    gid = str(body.get("guildId", ""))
    if not gid.isdigit() or request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "This server is not allowed."}, 403)
    guild = request.app["staff_bot"].get_guild(int(gid))
    if guild is None:
        return _json(request, {"error": "The bot is not in that server."}, 404)
    modules = body.get("config", {}).get("modules", {})
    panel = modules.get("tickets", {}) if isinstance(modules, dict) else {}
    channel_id = panel.get("panelChannelId")
    if not str(channel_id or "").isdigit():
        return _json(request, {"error": "Choose a panel destination channel first."}, 400)
    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return _json(request, {"error": "The selected panel destination is not a text channel in this server."}, 400)
    me = guild.me
    perms = channel.permissions_for(me) if me else None
    if not perms or not (perms.send_messages and perms.embed_links):
        return _json(request, {"error": "The bot needs Send Messages and Embed Links in the selected channel."}, 403)
    category_id = panel.get("categoryId")
    role_ids = panel.get("supportRoleIds") or ([panel.get("supportRoleId")] if panel.get("supportRoleId") else [])
    role_ids = [int(value) for value in role_ids if str(value or "").isdigit()]
    category = guild.get_channel(int(category_id)) if str(category_id or "").isdigit() else None
    roles = [guild.get_role(role_id) for role_id in role_ids]
    roles = [role for role in roles if role is not None]
    if category_id and not isinstance(category, discord.CategoryChannel):
        return _json(request, {"error": "Choose a valid ticket category from this server."}, 400)
    if len(roles) != len(role_ids):
        return _json(request, {"error": "One or more selected help roles are invalid for this server."}, 400)
    title = str(panel.get("title") or "How can we help?")[:256]
    description = str(panel.get("message") or "Choose an option below and our staff team will help you.")[:4000]
    panel_content = str(panel.get("panelMessage") or "")[:2000]
    ticket_welcome = str(panel.get("welcomeMessage") or "Thank you for contacting support.\\nPlease describe your issue and wait for a response.")[:2000]
    channel_name_format = str(panel.get("channelNameFormat") or "{username}-{number}")[:100]
    definition = body.get("panelDefinition") if isinstance(body.get("panelDefinition"), dict) else {}
    children = body.get("childPanels") if isinstance(body.get("childPanels"), list) else []
    if definition.get("isMulti") and not children:
        ticket_module = (body.get("config") or {}).get("modules", {}).get("tickets", {})
        raw_definitions = ticket_module.get("panelDefinitionsJson", [])
        try:
            saved_definitions = json.loads(raw_definitions) if isinstance(raw_definitions, str) else raw_definitions
        except (TypeError, ValueError):
            saved_definitions = []
        if isinstance(saved_definitions, list):
            selected_ids = {str(value) for value in (definition.get("singlePanelIds") or [])}
            children = [item for item in saved_definitions if isinstance(item, dict) and not item.get("isMulti") and (str(item.get("id")) in selected_ids or str(item.get("parentId") or "") == str(definition.get("id")))]
    if definition.get("isMulti"):
        options = []
        for child in children[:25]:
            child_label = str(child.get("buttonLabel") or (child.get("types") or ["Ticket"])[0])[:80]
            child_welcome = str(child.get("welcomeMessage") or ticket_welcome)[:2000]
            child_format = str(child.get("channelNameFormat") or "{username}-{number}")[:100]
            child_types = child.get("types") if isinstance(child.get("types"), list) else []
            child_type = str(child_types[0] if child_types else child_label)[:80]
            options.append({"label": child_label, "description": None, "emoji": None, "ticket_message": child_welcome, "channel_name_format": child_format, "category_id": int(category.id) if category else None, "role_id": int(roles[0].id) if roles else None, "role_ids": [int(role.id) for role in roles], "ticket_type": child_type})
        if not options:
            return _json(request, {"error": "Add at least one single panel to this multi-panel."}, 400)
    else:
        types = [x.strip()[:80] for x in str(panel.get("types") or "General Support").splitlines() if x.strip()][:25]
        if not types:
            types = ["General Support"]
        options = [{"label": x, "description": None, "emoji": None, "ticket_message": ticket_welcome, "channel_name_format": channel_name_format, "category_id": int(category.id) if category else None, "role_id": int(roles[0].id) if roles else None, "role_ids": [int(role.id) for role in roles]} for x in types]
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    try:
        staff_bot = request.app["staff_bot"]
        if len(options) == 1:
            button_label = str(panel.get("buttonLabel") or "Open Ticket")[:80]
            view = staff_bot.ticket_open_view_cls(label=button_label)
        else:
            view = staff_bot.ticket_select_view_cls(options)
        msg = await channel.send(content=panel_content or None, embed=embed, view=view)
        panel_record = {"title": title, "description": description, "channel_id": int(channel.id), "category_id": int(category.id) if category else None, "role_id": int(roles[0].id) if roles else None, "options": options, "message_id": msg.id, "guild_id": guild.id, "channel_name_format": channel_name_format, "ticket_message": ticket_welcome, "created_by_dashboard": True}
        await request.app["panels_store"].set(msg.id, panel_record)
        await _audit(request, gid, user, "publish_panel", f"Ticket panel posted in #{channel.name}")
        return _json(request, {"ok": True, "channelId": str(channel.id), "channelName": channel.name, "messageId": str(msg.id)})
    except discord.Forbidden:
        return _json(request, {"error": "Discord denied the request. Check the bot's channel permissions."}, 403)
    except discord.HTTPException as exc:
        return _json(request, {"error": f"Discord could not post the panel (HTTP {exc.status})."}, 502)


async def send_module_message(request):
    user, error = await _authorized(request)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid JSON body."}, 400)
    gid, module_name = str(body.get("guildId", "")), str(body.get("module", ""))
    if not gid.isdigit() or request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "This server is not allowed."}, 403)
    guild = request.app["staff_bot"].get_guild(int(gid))
    if guild is None:
        return _json(request, {"error": "The bot is not in that server."}, 404)
    config = await request.app["staff_store"].get(gid, {})
    module = config.get("modules", {}).get(module_name, {})
    if module_name in {"custom_message", "honeypot"}:
        channel_id = str(body.get("channelId") or (module.get("channelId") if module_name == "honeypot" else "") or "")
        module = {"enabled": True}
    else:
        if not module or not module.get("enabled"):
            return _json(request, {"error": "Enable and save this module before sending a message."}, 400)
        channel_id = module.get("channelId") or module.get("logChannelId")
    if not str(channel_id or "").isdigit():
        return _json(request, {"error": "Choose and save a destination channel first."}, 400)
    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return _json(request, {"error": "The configured destination is not a text channel in this server."}, 400)
    template = str(module.get("template") or f"{module_name.title()} notification")[:1800]
    content = str(body.get("message") or template)[:1900]
    if not content.strip():
        return _json(request, {"error": "Message cannot be empty."}, 400)
    try:
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        await _audit(request, gid, user, "send_message", f"{module_name} message sent to #{channel.name}")
        return _json(request, {"ok": True, "channelId": str(channel.id), "channelName": channel.name})
    except discord.Forbidden:
        return _json(request, {"error": "The bot cannot send messages in that channel."}, 403)
    except discord.HTTPException as exc:
        return _json(request, {"error": f"Discord could not send the message (HTTP {exc.status})."}, 502)


def _profile_display_name(member):
    return (getattr(member, "global_name", None) or member.name).strip()


def _role_nickname_value(member, rule):
    base = _profile_display_name(member)
    prefix = str(rule.get("prefix") or "")
    suffix = str(rule.get("suffix") or "")
    if prefix and base.casefold().startswith(prefix.casefold()):
        prefix = ""
    if suffix and base.casefold().endswith(suffix.casefold()):
        suffix = ""
    available = max(0, 32 - len(prefix) - len(suffix))
    return (prefix + base[:available] + suffix)[:32]


async def _apply_role_nickname(member, rules):
    if member.bot:
        return False
    match = next((rule for rule in rules if str(rule.get("roleId", "")).isdigit() and any(str(role.id) == str(rule["roleId"]) for role in member.roles)), None)
    desired = _role_nickname_value(member, match) if match else _profile_display_name(member)[:32]
    if desired == member.nick:
        return False
    try:
        await member.edit(nick=desired, reason="Cedar Cove role nickname rule")
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _welcome_leave_member_join(member):
    if member.bot:
        return
    bot = member.guild._state._get_client()
    store = getattr(bot, "staff_dashboard_store", None)
    if store is None:
        return
    config = await store.get(str(member.guild.id), {})
    module = (config.get("modules") or {}).get("welcome") or {}
    channel_id = str(module.get("channelId") or "")
    if not module.get("enabled") or not channel_id.isdigit():
        return
    channel = member.guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return
    template = str(module.get("message") or "Welcome {user} to {server}!").strip()[:1800]
    message = template.replace("{user}", member.mention).replace("{username}", member.name).replace("{server}", member.guild.name).replace("{member_count}", str(member.guild.member_count or len(member.guild.members)))
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _welcome_leave_member_remove(member):
    if member.bot:
        return
    bot = member.guild._state._get_client()
    store = getattr(bot, "staff_dashboard_store", None)
    if store is None:
        return
    config = await store.get(str(member.guild.id), {})
    module = (config.get("modules") or {}).get("goodbye") or {}
    channel_id = str(module.get("channelId") or "")
    if not module.get("enabled") or not channel_id.isdigit():
        return
    channel = member.guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return
    template = str(module.get("message") or "{username} has left {server}.").strip()[:1800]
    message = template.replace("{user}", member.mention).replace("{username}", member.name).replace("{server}", member.guild.name).replace("{member_count}", str(max(0, (member.guild.member_count or len(member.guild.members)) - 1)))
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _role_nickname_member_update(before, after):
    if before.guild is None:
        return
    if before.roles == after.roles:
        return
    store = getattr(_configured_bot, "role_nicknames_store", None)
    if store is None:
        return
    rules = await store.get(str(after.guild.id), [])
    if isinstance(rules, list):
        await _apply_role_nickname(after, rules)


async def _role_nickname_sync_loop(bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        store = getattr(bot, "role_nicknames_store", None)
        if store is not None:
            for guild in list(bot.guilds):
                try:
                    rules = await store.get(str(guild.id), [])
                    if not isinstance(rules, list) or not rules:
                        continue
                    for member in list(guild.members):
                        await _apply_role_nickname(member, rules)
                except Exception as exc:
                    logger = getattr(bot, "staff_dashboard_logger", None)
                    if logger:
                        logger.warning("Role nickname sync failed for guild %s: %s", guild.id, exc)
        await asyncio.sleep(300)


async def get_role_nicknames(request):
    user, error = await _authorized(request)
    if error:
        return error
    gid = request.match_info["guild_id"]
    if not gid.isdigit() or request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "The bot is not in that Discord server."}, 404)
    rules = await request.app["role_nicknames_store"].get(gid, [])
    return _json(request, {"guildId": gid, "rules": rules if isinstance(rules, list) else []})


async def save_role_nicknames(request):
    user, error = await _authorized(request)
    if error:
        return error
    gid = request.match_info["guild_id"]
    guild = request.app["staff_bot"].get_guild(int(gid)) if gid.isdigit() else None
    if guild is None:
        return _json(request, {"error": "The bot is not in that Discord server."}, 404)
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid JSON body."}, 400)
    incoming = body.get("rules")
    if not isinstance(incoming, list) or len(incoming) > 100:
        return _json(request, {"error": "Rules must be a list of up to 100 items."}, 400)
    rules, seen = [], set()
    for item in incoming:
        if not isinstance(item, dict):
            continue
        role_id = str(item.get("roleId") or "")
        role = guild.get_role(int(role_id)) if role_id.isdigit() else None
        if role is None or role.is_default():
            return _json(request, {"error": "Choose a valid role from this server."}, 400)
        if role_id in seen:
            return _json(request, {"error": "Each role can have only one nickname rule."}, 400)
        seen.add(role_id)
        prefix, suffix = str(item.get("prefix") or "")[:20], str(item.get("suffix") or "")[:20]
        if not prefix and not suffix:
            return _json(request, {"error": "Each rule needs a prefix, suffix, or both."}, 400)
        rules.append({"roleId": role_id, "roleName": role.name, "prefix": prefix, "suffix": suffix})
    if not guild.me.guild_permissions.manage_nicknames:
        return _json(request, {"error": "The bot needs Manage Nicknames permission to apply role nickname rules."}, 403)
    store = request.app["role_nicknames_store"]
    previous_rules = await store.get(gid, [])
    if not isinstance(previous_rules, list):
        previous_rules = []
    await store.set(gid, rules)
    updated = 0
    try:
        members = [member async for member in guild.fetch_members(limit=None)]
    except discord.HTTPException:
        members = list(guild.members)
    for member in members:
        if member.bot:
            continue
        match = next((rule for rule in rules if any(str(role.id) == str(rule["roleId"]) for role in member.roles)), None)
        desired = _role_nickname_value(member, match) if match else _profile_display_name(member)[:32]
        if desired != member.nick:
            try:
                await member.edit(nick=desired, reason="Cedar Cove role nickname rule sync")
                updated += 1
            except (discord.Forbidden, discord.HTTPException):
                pass
    await _audit(request, gid, user, "save_role_nicknames", f"Saved {len(rules)} rules; updated {updated} members")
    return _json(request, {"ok": True, "rules": rules, "updatedMembers": updated})


async def list_audit(request):
    user, error = await _authorized(request)
    if error:
        return error
    gid = request.match_info["guild_id"]
    if not gid.isdigit() or request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "This server is not allowed."}, 403)
    records = await request.app["audit_store"].get(gid, [])
    return _json(request, {"items": list(reversed(records[-100:]))})


async def _set_dashboard_access(request, *, allowed, discord_id=None, username=None):
    auth_db = request.app["staff_bot"].auth_db
    users = auth_db["users"]
    sessions = auth_db["sessions"]

    if discord_id:
        if not str(discord_id).isdigit():
            return {"found": False, "error": "Invalid Discord account ID."}
        query = {"discordId": str(discord_id)}
    elif username:
        username = str(username).strip()
        if not username or len(username) > 100:
            return {"found": False, "error": "Provide a valid website username."}
        query = {"usernameLower": username.casefold()}
    else:
        return {"found": False, "error": "Provide a website username or Discord account ID."}

    user = await users.find_one(query, {"_id": 1, "username": 1, "discordId": 1})
    if user is None and username:
        user = await users.find_one(
            {"username": {"$regex": "^" + re.escape(username) + "$", "$options": "i"}},
            {"_id": 1, "username": 1, "discordId": 1},
        )
    if user is None:
        return {"found": False}

    await users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "whitelisted": bool(allowed),
            "dashboardAccessUpdatedAt": datetime.now(timezone.utc),
        }},
    )
    if not allowed:
        await sessions.delete_many({"userId": user["_id"]})

    return {
        "found": True,
        "username": user.get("username", ""),
        "discordId": user.get("discordId", str(discord_id or "")),
        "whitelisted": bool(allowed),
    }


async def _delete_linked_dashboard_account(request, discord_id):
    if not str(discord_id).isdigit():
        return {"deleted": False, "error": "Invalid Discord account ID."}

    auth_db = request.app["staff_bot"].auth_db
    users = auth_db["users"]
    sessions = auth_db["sessions"]
    user = await users.find_one({"discordId": str(discord_id)}, {"_id": 1})
    if user is None:
        return {"deleted": False, "found": False}

    await sessions.delete_many({"userId": user["_id"]})
    await users.delete_one({"_id": user["_id"]})
    return {"deleted": True, "found": True}


async def _audit(request, gid, user, action, detail):
    store = request.app["audit_store"]
    records = await store.get(gid, [])
    if not isinstance(records, list):
        records = []
    records.append({"time": int(time.time()), "actor": str(user.get("username", "staff"))[:80], "action": action, "detail": detail[:500]})
    await store.set(gid, records[-500:])




async def _staff_guild(request, user=None):
    bot = request.app["staff_bot"]
    discord_id = str((user or {}).get("discordId") or "").strip()

    if discord_id.isdigit():
        for guild in bot.guilds:
            if str(getattr(guild, "owner_id", "")) == discord_id:
                return guild

            member = guild.get_member(int(discord_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(discord_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue

            if _member_can_manage_staff(member):
                return guild

    # The primary website owner can use the only server connected to this bot.
    # If the bot is in multiple servers, never guess which one they intended.
    if str((user or {}).get("username", "")).casefold() == "glitchstorm7320" and len(bot.guilds) == 1:
        return bot.guilds[0]

    return None


def _member_can_manage_staff(member):
    allowed = {"owner", "co owner", "coowner", "admin", "administrator"}
    return any(
        re.sub(r"[^a-z0-9]+", " ", role.name.casefold()).strip() in allowed
        for role in member.roles
    )


async def _staff_manager(request):
    user, error = await _authorized(request)
    if error:
        return None, None, error

    # Dashboard access is approved in the verification server's users.whitelisted field.
    if not bool(user.get("whitelisted", False)):
        return None, None, _json(request, {
            "error": "Your website account has not been approved for dashboard access."
        }, 403)

    guild = await _staff_guild(request, user)
    if guild is None:
        return None, None, _json(request, {"error": "The bot could not identify a Cedar Cove server for your linked Discord account. Make sure your Discord account is linked and the bot is in the server."}, 503)
    if not _can_manage_staff(user, guild):
        return None, None, _json(request, {"error": "Staff Management is available only to Owner, Co Owner, and Admin roles."}, 403)
    return user, guild, None


def _can_manage_staff(user, guild):
    # The designated website owner account has staff-management access by default.
    if str(user.get("username", "")).casefold() == "glitchstorm7320":
        return True

    discord_id = str(user.get("discordId") or "").strip()
    if not discord_id.isdigit():
        return False

    # The actual Discord server owner must always qualify, even without an
    # explicitly assigned role named "Owner".
    if str(getattr(guild, "owner_id", "")) == discord_id:
        return True

    member = guild.get_member(int(discord_id))
    if member is None:
        return False

    # Normalize emoji and decorative characters so roles such as "👑 Owner"
    # and "Co-Owner" still match the intended permission names.
    allowed = {"owner", "co owner", "coowner", "admin", "administrator"}
    for role in member.roles:
        normalized = re.sub(r"[^a-z0-9]+", " ", role.name.casefold()).strip()
        if normalized in allowed:
            return True
    return False


async def dashboard_access(request):
    user, error = await _authorized(request)
    if error:
        return error
    if not bool(user.get("whitelisted", False)):
        return _json(request, {"staffManagement": False, "dashboardAccess": False})
    guild = await _staff_guild(request)
    can_manage = bool(guild and _can_manage_staff(user, guild))
    return _json(request, {"staffManagement": can_manage, "dashboardAccess": True})


def _member_staff_roles(member, configured_role_ids=None):
    configured_role_ids = {str(value) for value in (configured_role_ids or [])}
    recognized = {"owner", "co owner", "coowner", "admin", "administrator", "mod", "moderator", "trial staff", "helper", "socials manager", "staff"}
    return [role.name for role in sorted(member.roles, key=lambda role: role.position, reverse=True)
            if re.sub(r"[^a-z0-9]+", " ", role.name.casefold()).strip() in recognized or str(role.id) in configured_role_ids]


def _loa_state(record, now=None):
    now = int(now or time.time())
    loa = record.get("loa")
    if not isinstance(loa, dict) or not loa.get("active"):
        return "none"
    end = int(loa.get("endAt") or 0)
    if end and end <= now:
        return "expired"
    if end and end - now <= 3 * 86400:
        return "near_expiry"
    return "on_leave"



async def public_staff_list(request):
    bot = request.app["staff_bot"]
    named_guilds = [guild for guild in bot.guilds if "cedar cove" in guild.name.casefold()]
    if named_guilds:
        guild = sorted(named_guilds, key=lambda item: (item.name.casefold() != "cedar cove", item.name.casefold()))[0]
    elif len(bot.guilds) == 1:
        guild = bot.guilds[0]
    else:
        return _json(request, {"error": "Cedar Cove server could not be identified.", "items": []}, 503)

    staff_config = await request.app["staff_store"].get(str(guild.id), {})
    ladder_items = (((staff_config.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
    ladder_role_ids = [item.get("roleId") for item in ladder_items if isinstance(item, dict)]

    items = []
    for member in guild.members:
        if member.bot:
            continue
        roles = _member_staff_roles(member, ladder_role_ids)
        if not roles:
            continue
        items.append({
            "id": str(member.id),
            "displayName": member.display_name,
            "role": roles[0],
            "avatar": member.display_avatar.with_size(256).url,
        })

    items.sort(key=lambda item: item["displayName"].casefold())
    return _json(request, {"items": items})


async def staff_list(request):
    user, guild, error = await _staff_manager(request)
    if error:
        return error
    records = await request.app["staff_management_store"].get(str(guild.id), {})
    if not isinstance(records, dict):
        records = {}
    staff_config = await request.app["staff_store"].get(str(guild.id), {})
    ladder_items = (((staff_config.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
    ladder_role_ids = [item.get("roleId") for item in ladder_items if isinstance(item, dict)]
    items = []
    for member in sorted(guild.members, key=lambda m: m.display_name.casefold()):
        roles = _member_staff_roles(member, ladder_role_ids)
        record = records.get(str(member.id), {})
        suspended = bool(record.get("suspended"))
        if (not roles and not suspended) or member.bot:
            continue
        loa_status = _loa_state(record)
        items.append({"id": str(member.id), "username": member.name, "displayName": member.display_name, "avatar": member.display_avatar.url, "roles": roles or ["Member (suspended)"], "warnings": len(record.get("warnings", [])), "strikes": len(record.get("strikes", [])), "suspended": suspended, "loaStatus": loa_status})
    return _json(request, {"guild": guild.name, "items": items, "viewerRoles": ["Dashboard Authorized"]})


async def staff_detail(request):
    user, guild, error = await _staff_manager(request)
    if error:
        return error
    member_id = request.match_info["member_id"]
    if not member_id.isdigit():
        return _json(request, {"error": "Invalid Discord member ID."}, 400)
    try:
        member = guild.get_member(int(member_id)) or await guild.fetch_member(int(member_id))
    except discord.NotFound:
        return _json(request, {"error": "Staff member is no longer in the server."}, 404)
    except discord.HTTPException:
        return _json(request, {"error": "Discord could not load this member. Try again."}, 502)
    staff_config = await request.app["staff_store"].get(str(guild.id), {})
    ladder_items = (((staff_config.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
    ladder_role_ids = [entry.get("roleId") for entry in ladder_items if isinstance(entry, dict)]
    roles = _member_staff_roles(member, ladder_role_ids)
    record = await request.app["staff_management_store"].get(str(guild.id), {})
    item = record.get(str(member.id), {}) if isinstance(record, dict) else {}
    if member.bot or (not roles and not item.get("suspended")):
        return _json(request, {"error": "Only current or suspended human staff members can be managed here."}, 403)
    return _json(request, {"member": {"id": str(member.id), "username": member.name, "displayName": member.display_name, "roles": _member_staff_roles(member, ladder_role_ids)}, "record": item, "loaStatus": _loa_state(item)})


async def staff_action_settings(request):
    user, guild, error = await _staff_manager(request)
    if error:
        return error
    config = await request.app["staff_store"].get(str(guild.id), {})
    settings = ((config.get("modules") or {}).get("staff_actions") or {})
    if request.method == "GET":
        return _json(request, {"settings": settings})
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid JSON body."}, 400)
    channels = {str(c.id) for c in guild.text_channels}
    roles = {str(r.id) for r in guild.roles}
    for key in ("promotionChannelId", "retirementChannelId", "firedChannelId", "staffLadderAnnouncementChannelId"):
        value = str(body.get(key) or "")
        if value and value not in channels:
            return _json(request, {"error": "Choose a valid text channel for " + key + "."}, 400)
    for key in ("memberRoleId", "retiredRoleId"):
        value = str(body.get(key) or "")
        if not value or value not in roles:
            return _json(request, {"error": "Choose a valid Discord role for " + key + "."}, 400)
    roster_style = str(body.get("staffRosterNameStyle") or "prefix").lower()
    if roster_style not in {"prefix", "display_name"}:
        return _json(request, {"error": "Choose a valid staff roster name style."}, 400)
    roster_prefix = str(body.get("staffRosterPrefix") or "[role_{role_name}]").strip()[:80]
    settings = {
        "promotionChannelId": str(body.get("promotionChannelId") or ""),
        "promotionMessage": str(body.get("promotionMessage") or "")[:1800],
        "retirementChannelId": str(body.get("retirementChannelId") or ""),
        "retirementMessage": str(body.get("retirementMessage") or "")[:1800],
        "firedChannelId": str(body.get("firedChannelId") or ""),
        "firedMessage": str(body.get("firedMessage") or "")[:1800],
        "staffLadderAnnouncementChannelId": str(body.get("staffLadderAnnouncementChannelId") or ""),
        "staffRosterNameStyle": roster_style,
        "staffRosterPrefix": roster_prefix,
        "memberRoleId": str(body.get("memberRoleId")),
        "retiredRoleId": str(body.get("retiredRoleId")),
    }
    config.setdefault("modules", {})["staff_actions"] = settings
    await request.app["staff_store"].set(str(guild.id), config)
    await _update_staff_ladder_post(guild)
    return _json(request, {"ok": True, "settings": settings})


async def _send_staff_announcement(guild, channel_id, template, member, actor, reason, action, old_rank="", new_rank=""):
    channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
    if not isinstance(channel, discord.TextChannel) or not template:
        return
    message = template.replace("{member}", member.mention).replace("{username}", member.display_name)
    message = message.replace("{actor}", str(actor.get("username") or "Staff")).replace("{reason}", reason or "No reason provided")
    message = message.replace("{action}", action).replace("{old_rank}", old_rank).replace("{new_rank}", new_rank)
    try:
        await channel.send(message[:2000], allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except (discord.Forbidden, discord.HTTPException):
        # Role changes remain successful even if the announcement channel is unavailable.
        return


async def staff_action(request):
    user, guild, error = await _staff_manager(request)
    if error:
        return error
    member_id = request.match_info["member_id"]
    if not member_id.isdigit():
        return _json(request, {"error": "Invalid Discord member ID."}, 400)
    try:
        member = guild.get_member(int(member_id)) or await guild.fetch_member(int(member_id))
    except discord.NotFound:
        return _json(request, {"error": "Staff member is no longer in the server."}, 404)
    except discord.HTTPException:
        return _json(request, {"error": "Discord could not load this member. Try again."}, 502)
    staff_config_for_check = await request.app["staff_store"].get(str(guild.id), {})
    ladder_for_check = (((staff_config_for_check.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
    ladder_role_ids_for_check = [entry.get("roleId") for entry in ladder_for_check if isinstance(entry, dict)]
    existing_records = await request.app["staff_management_store"].get(str(guild.id), {})
    existing_record = existing_records.get(member_id, {}) if isinstance(existing_records, dict) else {}
    if member.bot or (not _member_staff_roles(member, ladder_role_ids_for_check) and not existing_record.get("suspended")):
        return _json(request, {"error": "Only current or suspended human staff members can be managed here."}, 403)
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid JSON body."}, 400)
    action = str(body.get("action") or "").strip().lower()
    reason = str(body.get("reason") or "").strip()[:1000]
    now = int(time.time())
    store = request.app["staff_management_store"]
    records = await store.get(str(guild.id), {})
    if not isinstance(records, dict):
        records = {}
    record = records.setdefault(member_id, {"warnings": [], "strikes": [], "loa": None, "suspended": False})
    actor = {"id": str(user.get("discordId")), "username": str(user.get("discordUsername") or user.get("username") or "Staff manager")[:100]}
    if action in {"retire", "fire", "fired"}:
        is_fired = action in {"fire", "fired"}
        if is_fired and not reason:
            return _json(request, {"error": "A reason is required to fire a staff member."}, 400)
        if str(member.id) == str(user.get("discordId")) and is_fired:
            return _json(request, {"error": "You cannot fire yourself through staff management."}, 400)
        config = await request.app["staff_store"].get(str(guild.id), {})
        settings = ((config.get("modules") or {}).get("staff_actions") or {})
        member_role = guild.get_role(int(settings.get("memberRoleId") or 0)) if str(settings.get("memberRoleId") or "").isdigit() else None
        retired_role = guild.get_role(int(settings.get("retiredRoleId") or 0)) if str(settings.get("retiredRoleId") or "").isdigit() else None
        if not member_role or not retired_role:
            return _json(request, {"error": "Configure the Member and Retired from Staff roles in Staff Management settings first."}, 400)
        ladder = (((config.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
        staff_roles = {role for role in member.roles if re.sub(r"[^a-z0-9]+", " ", role.name.casefold()).strip() in {"owner", "co owner", "coowner", "admin", "administrator", "mod", "moderator", "trial staff", "helper", "socials manager", "staff"}}
        for item in ladder:
            rid = str(item.get("roleId") or "")
            role = guild.get_role(int(rid)) if rid.isdigit() else None
            if role and role in member.roles:
                staff_roles.add(role)
        if not staff_roles:
            return _json(request, {"error": "This member has no recognized staff role to retire."}, 400)
        if not guild.me.guild_permissions.manage_roles:
            return _json(request, {"error": "The bot needs Manage Roles permission."}, 403)
        if any(role >= guild.me.top_role for role in [member_role, retired_role, *staff_roles]):
            return _json(request, {"error": "Move the bot's highest role above the Member, Retired from Staff, and staff roles."}, 403)

        # Revoke dashboard access and invalidate active website sessions,
        # but preserve the person's website account and its existing data.
        try:
            access_result = await _set_dashboard_access(
                request, discord_id=str(member.id), allowed=False
            )
            access_revoked = bool(access_result.get("found") and access_result.get("whitelisted") is False)
        except Exception:
            request.app["staff_logger"].exception("Staff dashboard access revocation failed")
            return _json(request, {"error": "Could not update the linked website account. No Discord roles were changed."}, 502)

        try:
            await member.remove_roles(*staff_roles, reason=f"Staff {'fired' if is_fired else 'retired'} by {actor['username']}: {reason or 'No reason provided'}")
            add_roles = [role for role in (member_role, retired_role) if role not in member.roles]
            if add_roles:
                await member.add_roles(*add_roles, reason=f"Staff {'fired' if is_fired else 'retired'} by {actor['username']}")
        except discord.Forbidden:
            return _json(request, {"error": "Dashboard access was revoked, but Discord denied the role change. Check Manage Roles and role hierarchy."}, 403)
        except discord.HTTPException:
            return _json(request, {"error": "Dashboard access was revoked, but Discord could not update this member's roles."}, 502)

        final_action = "fire" if is_fired else "retire"
        record.setdefault("history", []).append({"action": final_action, "by": actor, "at": now, "reason": reason})
        records.pop(member_id, None)
        await store.set(str(guild.id), records)
        if is_fired:
            await _update_staff_ladder_post(guild, f"{member.display_name} was fired")
            await _send_staff_announcement(guild, settings.get("firedChannelId"), settings.get("firedMessage"), member, actor, reason, "fired")
            await _audit(request, str(guild.id), user, "staff_fired", f"Fired {member_id}: {reason}")
            return _json(request, {"ok": True, "message": ("Staff member fired; dashboard access revoked and website account preserved." if access_revoked else "Staff member fired; no linked website account was found to revoke."), "dashboardAccessRevoked": access_revoked})
        await _update_staff_ladder_post(guild, f"{member.display_name} retired from staff")
        await _send_staff_announcement(guild, settings.get("retirementChannelId"), settings.get("retirementMessage"), member, actor, reason, "retired")
        await _audit(request, str(guild.id), user, "staff_retired", f"Retired {member_id}: {reason or 'No reason provided'}")
        return _json(request, {"ok": True, "message": ("Staff member retired; dashboard access revoked and website account preserved." if access_revoked else "Staff member retired; no linked website account was found to revoke."), "dashboardAccessRevoked": access_revoked})
    elif action in {"promote", "demote"}:
        config = await request.app["staff_store"].get(str(guild.id), {})
        ladder = (((config.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
        if not ladder:
            return _json(request, {"error": "Set up and save the staff ladder first."}, 400)
        ladder_roles = []
        for item in ladder:
            role = guild.get_role(int(item.get("roleId", 0))) if str(item.get("roleId", "")).isdigit() else None
            if role:
                ladder_roles.append((item, role))
        if not ladder_roles:
            return _json(request, {"error": "None of the saved staff ladder roles still exist in Discord. Re-select the roles and save the ladder."}, 400)
        current_indexes = [i for i, (_, role) in enumerate(ladder_roles) if role in member.roles]
        # First saved rank is highest; last saved rank is lowest.
        current_index = max(current_indexes) if current_indexes else -1
        next_index = current_index - 1 if action == "promote" else current_index + 1
        if action == "demote" and current_index < 0:
            return _json(request, {"error": "This member does not currently hold a staff ladder role."}, 400)
        if next_index < 0 or next_index >= len(ladder_roles):
            edge = "highest" if action == "promote" else "lowest"
            return _json(request, {"error": f"This staff member is already at the {edge} rank."}, 400)
        old_rank = ladder_roles[current_index][0].get("name", ladder_roles[current_index][1].name) if current_index >= 0 else "Not on ladder"
        target_item, target_role = ladder_roles[next_index]
        if not guild.me.guild_permissions.manage_roles:
            return _json(request, {"error": "The bot needs Manage Roles permission to change staff ranks."}, 403)
        if target_role >= guild.me.top_role or any(role >= guild.me.top_role for _, role in ladder_roles if role in member.roles):
            return _json(request, {"error": "Move the bot's highest role above all staff ladder roles."}, 403)
        try:
            remove_roles = [role for _, role in ladder_roles if role in member.roles and role != target_role]
            if remove_roles:
                await member.remove_roles(*remove_roles, reason=f"Staff {action} by {actor['username']}: {reason or 'Staff ladder change'}")
            if target_role not in member.roles:
                await member.add_roles(target_role, reason=f"Staff {action} by {actor['username']}: {reason or 'Staff ladder change'}")
        except discord.Forbidden:
            return _json(request, {"error": "Discord denied the role change. Check Manage Roles and role hierarchy."}, 403)
        except discord.HTTPException:
            return _json(request, {"error": "Discord could not update this staff member's role."}, 502)
        record.setdefault("history", []).append({"action": action, "rank": target_item.get("name", target_role.name), "by": actor, "at": now, "reason": reason})
        settings = (((config.get("modules") or {}).get("staff_actions") or {}))
        await _send_staff_announcement(guild, settings.get("promotionChannelId"), settings.get("promotionMessage"), member, actor, reason, action, old_rank=old_rank, new_rank=target_item.get("name", target_role.name))
        action_word = "promoted" if action == "promote" else "demoted"
        await _update_staff_ladder_post(guild, f"{member.display_name} was {action_word}: {old_rank} -> {target_item.get('name', target_role.name)}")
    elif action in {"warn", "strike"}:
        if not reason:
            return _json(request, {"error": "Enter a reason for this action."}, 400)
        key = "warnings" if action == "warn" else "strikes"
        record.setdefault(key, []).append({"reason": reason, "by": actor, "at": now})
    elif action == "loa_start":
        days = body.get("days", 7)
        try:
            days = max(1, min(90, int(days)))
        except (TypeError, ValueError):
            days = 7
        if not reason:
            return _json(request, {"error": "Enter a reason for the leave of absence."}, 400)
        record["loa"] = {"active": True, "reason": reason, "startAt": now, "endAt": now + days * 86400, "by": actor, "history": (record.get("loa") or {}).get("history", [])}
        record["loa"].setdefault("history", []).append({"action": "started", "by": actor, "at": now, "endAt": now + days * 86400})
    elif action == "loa_extend":
        loa = record.get("loa")
        if not isinstance(loa, dict) or not loa.get("active"):
            return _json(request, {"error": "This staff member has no active leave of absence."}, 400)
        try:
            days = max(1, min(90, int(body.get("days", 7))))
        except (TypeError, ValueError):
            days = 7
        loa["endAt"] = max(now, int(loa.get("endAt") or now)) + days * 86400
        loa.setdefault("history", []).append({"action": "extended", "by": actor, "at": now, "endAt": loa["endAt"], "reason": reason})
    elif action == "loa_end":
        loa = record.get("loa")
        if not isinstance(loa, dict) or not loa.get("active"):
            return _json(request, {"error": "This staff member has no active leave of absence."}, 400)
        loa["active"] = False
        loa["endedAt"] = now
        loa.setdefault("history", []).append({"action": "ended", "by": actor, "at": now, "reason": reason})
    elif action == "suspend":
        if record.get("suspended"):
            return _json(request, {"error": "This staff member is already suspended."}, 400)
        normalize_role = lambda value: re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        staff_role_ids = [str(role.id) for role in member.roles if normalize_role(role.name) in {"owner", "co owner", "coowner", "admin", "administrator", "mod", "moderator", "trial staff", "helper", "socials manager", "staff"}]
        normalized_roles = [(role, normalize_role(role.name)) for role in guild.roles]
        member_role = next((role for role, name in normalized_roles if name == "member"), None)
        if member_role is None:
            member_role = next((role for role, name in normalized_roles if name in {"members", "server member", "server members"} or name.startswith("member ")), None)
        if not staff_role_ids:
            return _json(request, {"error": "No recognized staff roles were found to remove."}, 400)
        if member_role is None:
            visible_roles = [role.name for role in guild.roles if "member" in normalize_role(role.name)]
            return _json(request, {"error": f"The bot cannot find a Member role in the selected server '{guild.name}'. Roles containing 'member' visible to the bot: {', '.join(visible_roles) if visible_roles else 'none'}. Make sure the bot is in the correct server and can see its roles."}, 400)
        if not guild.me.guild_permissions.manage_roles:
            return _json(request, {"error": "The bot needs the Manage Roles permission."}, 403)
        if member_role >= guild.me.top_role or any((guild.get_role(int(role_id)) and guild.get_role(int(role_id)) >= guild.me.top_role) for role_id in staff_role_ids):
            return _json(request, {"error": "The bot's highest role must be above the Member role and every staff role it needs to change."}, 403)
        try:
            await member.remove_roles(*(guild.get_role(int(role_id)) for role_id in staff_role_ids if guild.get_role(int(role_id))), reason=f"Staff suspended by {actor['username']}: {reason or 'No reason provided'}")
            added_member_role = member_role not in member.roles
            if added_member_role:
                await member.add_roles(member_role, reason=f"Staff suspended by {actor['username']}")
        except discord.Forbidden:
            return _json(request, {"error": "The bot cannot change one or more roles. Check Manage Roles and role hierarchy."}, 403)
        except discord.HTTPException:
            return _json(request, {"error": "Discord could not update this member's roles. Try again."}, 502)
        record["suspended"] = True
        record["suspensionAddedMemberRole"] = added_member_role
        record["suspension"] = {"reason": reason, "by": actor, "at": now, "removedRoleIds": staff_role_ids, "memberRoleId": str(member_role.id)}
    elif action == "unsuspend":
        if not record.get("suspended"):
            return _json(request, {"error": "This staff member is not suspended."}, 400)
        suspension = record.get("suspension") or {}
        restore_roles = [guild.get_role(int(role_id)) for role_id in suspension.get("removedRoleIds", []) if str(role_id).isdigit() and guild.get_role(int(role_id))]
        member_role_id = str(suspension.get("memberRoleId") or "")
        member_role = guild.get_role(int(member_role_id)) if member_role_id.isdigit() else None
        if not guild.me.guild_permissions.manage_roles:
            return _json(request, {"error": "The bot needs the Manage Roles permission."}, 403)
        try:
            if record.get("suspensionAddedMemberRole") and member_role and member_role in member.roles:
                await member.remove_roles(member_role, reason=f"Staff suspension ended by {actor['username']}")
            roles_to_restore = [role for role in restore_roles if role not in member.roles]
            if roles_to_restore:
                await member.add_roles(*roles_to_restore, reason=f"Staff suspension ended by {actor['username']}")
        except discord.Forbidden:
            return _json(request, {"error": "The bot cannot restore one or more roles. Check Manage Roles and role hierarchy."}, 403)
        except discord.HTTPException:
            return _json(request, {"error": "Discord could not restore this member's roles. Try again."}, 502)
        record["suspended"] = False
        record["suspensionEndedAt"] = now
    elif action in {"grant_dashboard_access", "revoke_dashboard_access"}:
        target_username = str(body.get("username") or "").strip()
        if not target_username:
            return _json(request, {"error": "Enter the website username to update."}, 400)
        allowed = action == "grant_dashboard_access"
        try:
            payload = await _set_dashboard_access(
                request, username=target_username, allowed=allowed
            )
            if not payload.get("found"):
                return _json(request, {"error": payload.get("error", "No linked website account was found.")}, 404)
        except Exception:
            request.app["staff_logger"].exception("Dashboard access update failed")
            return _json(request, {"error": "Could not update the linked website account in MongoDB."}, 502)
        record.setdefault("accountActions", []).append({"action": action, "username": payload.get("username", target_username), "by": actor, "at": now})
    elif action == "delete_account":
        if str(member_id) == str(user.get("discordId")):
            return _json(request, {"error": "You cannot delete your own dashboard account here."}, 400)
        try:
            payload = await _delete_linked_dashboard_account(request, str(member.id))
            if not payload.get("deleted"):
                return _json(request, {"error": payload.get("error", "No linked website account was found.")}, 404)
        except Exception:
            request.app["staff_logger"].exception("Staff dashboard account deletion failed")
            return _json(request, {"error": "Could not delete the linked website account from MongoDB."}, 502)
        record.setdefault("accountActions", []).append({"action": "dashboard_account_deleted", "by": actor, "at": now})
    else:
        return _json(request, {"error": "Unknown staff action."}, 400)
    record.setdefault("history", []).append({"action": action, "by": actor, "at": now, "reason": reason})
    records[member_id] = record
    await store.set(str(guild.id), records)
    await _audit(request, str(guild.id), user, "staff_" + action, f"Staff member {member_id}: {reason or action}")
    return _json(request, {"ok": True, "message": "Staff record updated.", "record": record})



def register_staff_routes(app: web.Application):
    app["staff_logger"] = __import__("logging").getLogger("mega_bot.staff_dashboard")
    app["staff_logger"].info("Staff dashboard API build=%s; wildcard CORS enabled; DASHBOARD_ALLOWED_ORIGINS is not required.", DASHBOARD_API_BUILD)
    # Routes are attached by start_health_server after setup_staff has been called.
    async def bind_app_context(request):
        return None

    # The bot and stores are passed in through module globals by setup_staff.
    # Capture the configured values from setup_staff's bot reference.
    # They are resolved lazily so the health server can be constructed safely.
    import sys
    module = sys.modules[__name__]
    bot = getattr(module, "_bot", None)
    if bot is None:
        # setup_staff stores the bot in a module global below via wrapper patch.
        bot = getattr(app, "_staff_bot", None)
    if bot is None:
        # register_staff_routes is invoked after setup_staff in mega_bot.py.
        bot = _configured_bot
    app["staff_bot"] = bot
    app["staff_store"] = bot.staff_dashboard_store
    app["audit_store"] = bot.staff_dashboard_audit_store
    app["panels_store"] = bot.staff_dashboard_store_for_panels
    app["staff_management_store"] = bot.staff_management_store
    app["role_nicknames_store"] = bot.role_nicknames_store
    app.router.add_route("OPTIONS", "/api/dashboard/{tail:.*}", options_handler)
    app.router.add_get("/api/dashboard/build", api_build)
    app.router.add_get("/api/dashboard/access", dashboard_access)
    app.router.add_get("/api/public/staff", public_staff_list)
    app.router.add_get("/api/dashboard/staff", staff_list)
    app.router.add_get("/api/dashboard/staff-actions-settings", staff_action_settings)
    app.router.add_put("/api/dashboard/staff-actions-settings", staff_action_settings)
    app.router.add_get("/api/dashboard/staff/{member_id}", staff_detail)
    app.router.add_post("/api/dashboard/staff/{member_id}/action", staff_action)
    app.router.add_get("/api/dashboard/status", api_status)
    app.router.add_get("/api/dashboard/guilds", list_guilds)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/channels", list_channels)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/roles", list_roles)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/staff-ladder", get_staff_ladder)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/staff-ladder", save_staff_ladder)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/config", get_config)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/config", save_config)
    # Compatibility aliases for dashboard versions that use /settings.
    app.router.add_get("/api/dashboard/guilds/{guild_id}/settings", get_config)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/settings", save_config)
    app.router.add_post("/api/dashboard/publish-panel", publish_panel)
    app.router.add_post("/api/dashboard/send-message", send_module_message)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/audit", list_audit)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/honeypot-stats", get_honeypot_stats)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/role-nicknames", get_role_nicknames)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/role-nicknames", save_role_nicknames)


_configured_bot = None

# Wrap setup_staff above to keep route registration dependency-free for mega_bot.py.
_original_setup_staff = setup_staff
def setup_staff(bot, get_store, logger):
    global _configured_bot
    _configured_bot = bot
    _original_setup_staff(bot, get_store, logger)
    bot.staff_dashboard_store_for_panels = get_store("panels")
    bot.staff_management_store = get_store("staff_management_records")
