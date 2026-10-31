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
DASHBOARD_API_BUILD = "staff-manager-2026-10-30-public-staff-roster"
STAFF_ROLE_NAMES = {"owner": "Owner", "co owner": "Co Owner", "admin": "Admin", "administrator": "Admin"}


def setup_staff(bot, get_store, logger):
    bot.staff_dashboard_store = get_store("staff_dashboard_configs")
    bot.staff_dashboard_audit_store = get_store("staff_dashboard_audit")
    bot.role_nicknames_store = get_store("role_nicknames")
    if not getattr(bot, "_github_staff_sync_listeners_added", False):
        bot.add_listener(_github_staff_member_update, "on_member_update")
        bot.add_listener(_github_staff_member_join, "on_member_join")
        bot.add_listener(_github_staff_member_remove, "on_member_remove")
        bot.add_listener(_github_staff_sync_on_ready, "on_ready")
        bot._github_staff_sync_listeners_added = True
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
        (_dashboard_log_member_update, 'on_member_update'), (_dashboard_honeypot_message, 'on_message'),
    ]
    for listener, event_name in listeners:
        marker = '_dashboard_listener_' + event_name
        if not getattr(bot, marker, False):
            bot.add_listener(listener, event_name)
            setattr(bot, marker, True)
    if not getattr(bot, '_verification_refresh_listener_added', False):
        async def _start_verification_refresh():
            if not getattr(bot, '_verification_refresh_task_started', False):
                bot._verification_refresh_task = asyncio.create_task(_verification_refresh_loop(bot))
                bot._verification_refresh_task_started = True
        bot.add_listener(_start_verification_refresh, 'on_ready')
        bot._verification_refresh_listener_added = True



async def _dashboard_module(bot, guild_id, name):
    store = getattr(bot, 'staff_dashboard_store', None)
    if store is None: return {}
    config = await store.get(str(guild_id), {})
    return ((config.get('modules') or {}).get(name) or {})

async def _log_event(guild, event_key, title, description):
    bot = guild._state._get_client()
    settings = await _dashboard_module(bot, guild.id, 'logging')
    if not settings.get(event_key + 'Enabled'): return
    channel_id = str(settings.get(event_key + 'ChannelId') or '')
    channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if not isinstance(channel, discord.TextChannel): return
    embed = discord.Embed(title=title, description=str(description)[:4000], color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    try: await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException): pass

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
    state = getattr(payload, '_state', None)
    bot = state._get_client() if state else None
    guild = bot.get_guild(payload.guild_id) if bot and payload.guild_id else None
    if guild is None:
        return
    cached = getattr(payload, 'cached_message', None)
    if cached is not None and cached.author.bot:
        return
    channel = guild.get_channel(payload.channel_id)
    channel_name = channel.mention if channel else 'channel ' + str(payload.channel_id)
    if cached is not None:
        description = 'Author: {} ({})\\nChannel: {}\\nMessage ID: {}\\nContent: {}'.format(cached.author, cached.author.id, channel_name, payload.message_id, (cached.content or '[no text]')[:2500])
    else:
        description = 'Channel: {}\\nMessage ID: {}\\nContent unavailable (message was not cached by Discord.py).'.format(channel_name, payload.message_id)
    await _log_event(guild, 'messageDelete', 'Message deleted', description)

async def _dashboard_log_message_edit(before, after):
    if after.guild and not after.author.bot and before.content != after.content: await _log_event(after.guild, 'messageEdit', 'Message edited', 'Author: {} ({})\nChannel: {}\nBefore: {}\nAfter: {}'.format(after.author, after.author.id, after.channel.mention, (before.content or '[empty]')[:1200], (after.content or '[empty]')[:1200]))
async def _dashboard_log_channel_create(channel):
    if channel.guild: await _log_event(channel.guild, 'channelCreate', 'Channel created', '{} ({})'.format(channel.name, channel.id))
async def _dashboard_log_channel_delete(channel):
    if channel.guild: await _log_event(channel.guild, 'channelDelete', 'Channel deleted', '{} ({})'.format(channel.name, channel.id))
async def _dashboard_log_channel_update(before, after):
    if after.guild and (before.name != after.name or before.category_id != after.category_id): await _log_event(after.guild, 'channelUpdate', 'Channel updated', '{} -> {} ({})'.format(before.name, after.name, after.id))
async def _dashboard_log_role_create(role):
    await _log_event(role.guild, 'roleCreate', 'Role created', '{} ({})'.format(role.name, role.id))
async def _dashboard_log_role_delete(role):
    await _log_event(role.guild, 'roleDelete', 'Role deleted', '{} ({})'.format(role.name, role.id))
async def _dashboard_log_role_update(before, after):
    if before.name != after.name or before.permissions != after.permissions: await _log_event(after.guild, 'roleUpdate', 'Role updated', '{} -> {} ({})'.format(before.name, after.name, after.id))
async def _dashboard_log_member_join(member):
    if not member.bot: await _log_event(member.guild, 'memberJoin', 'Member joined', '{} ({})'.format(member, member.id))
async def _dashboard_log_member_leave(member):
    if not member.bot: await _log_event(member.guild, 'memberLeave', 'Member left', '{} ({})'.format(member, member.id))
async def _dashboard_log_member_ban(guild, user):
    await _log_event(guild, 'memberBan', 'Member banned', '{} ({})'.format(user, user.id))
async def _dashboard_log_member_unban(guild, user):
    await _log_event(guild, 'memberUnban', 'Member unbanned', '{} ({})'.format(user, user.id))
async def _dashboard_log_member_update(before, after):
    if before.guild and before.nick != after.nick: await _log_event(after.guild, 'nicknameUpdate', 'Nickname changed', '{}: {} -> {}'.format(after, before.nick or before.name, after.nick or after.name))
    if before.guild and {r.id for r in before.roles} != {r.id for r in after.roles}: await _log_event(after.guild, 'memberRoleUpdate', 'Member roles changed', '{} ({})'.format(after, after.id))

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
    if request.app["staff_bot"].get_guild(int(gid)) is None:
        return _json(request, {"error": "The bot is not in that server."}, 404)
    config = await request.app["staff_store"].get(gid, {})
    if not isinstance(config, dict):
        config = {}
    modules = config.setdefault("modules", {})
    logging_settings = modules.setdefault("logging", {})
    for event_key in (
        "messageDelete", "messageEdit", "channelCreate", "channelDelete",
        "channelUpdate", "roleCreate", "roleDelete", "roleUpdate",
        "memberJoin", "memberLeave", "memberBan", "memberUnban",
        "nicknameUpdate", "memberRoleUpdate",
    ):
        logging_settings[event_key + "Enabled"] = True
    await request.app["staff_store"].set(gid, config)
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
            clean["modules"][str(name)] = entry

        # The logging panel should never silently turn its audit events off.
        # Keep every event enabled in storage, even when an older dashboard
        # payload omits a toggle or sends a stale false value.
        logging_settings = clean["modules"].setdefault("logging", {})
        for event_key in (
            "messageDelete", "messageEdit", "channelCreate", "channelDelete",
            "channelUpdate", "roleCreate", "roleDelete", "roleUpdate",
            "memberJoin", "memberLeave", "memberBan", "memberUnban",
            "nicknameUpdate", "memberRoleUpdate",
        ):
            logging_settings[event_key + "Enabled"] = True

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
    types = [x.strip()[:80] for x in str(panel.get("types") or "General Support").splitlines() if x.strip()][:25]
    if not types:
        types = ["General Support"]
    options = [{"label": x, "description": None, "emoji": None, "ticket_message": None, "category_id": int(category.id) if category else None, "role_id": int(roles[0].id) if roles else None, "role_ids": [int(role.id) for role in roles]} for x in types]
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    try:
        staff_bot = request.app["staff_bot"]
        if len(options) == 1:
            button_label = str(panel.get("buttonLabel") or "Open Ticket")[:80]
            view = staff_bot.ticket_open_view_cls(label=button_label)
        else:
            view = staff_bot.ticket_select_view_cls(options)
        msg = await channel.send(embed=embed, view=view)
        panel_record = {"title": title, "description": description, "channel_id": int(channel.id), "category_id": int(category.id) if category else None, "role_id": int(roles[0].id) if roles else None, "options": options, "message_id": msg.id, "guild_id": guild.id, "created_by_dashboard": True}
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
    guild = _staff_guild(request)
    can_manage = bool(guild and _can_manage_staff(user, guild))
    return _json(request, {"staffManagement": can_manage, "dashboardAccess": True})


def _member_staff_roles(member, configured_role_ids=None):
    configured_role_ids = {str(value) for value in (configured_role_ids or [])}
    recognized = {"owner", "co owner", "coowner", "admin", "administrator", "mod", "moderator", "trial staff", "helper", "socials manager", "staff"}
    return [role.name for role in sorted(member.roles, key=lambda role: role.position, reverse=True)
            if re.sub(r"[^a-z0-9]+", " ", role.name.casefold()).strip() in recognized or str(role.id) in configured_role_ids]


GITHUB_API_BASE = "https://api.github.com"
GITHUB_STAFF_REPOSITORY = (os.getenv("GITHUB_STAFF_REPOSITORY") or "wade06907-ux/Cedarcoveweb").strip()
GITHUB_STAFF_BRANCH = (os.getenv("GITHUB_STAFF_BRANCH") or "main").strip()
GITHUB_STAFF_HTML_FILE = (os.getenv("GITHUB_STAFF_HTML_FILE") or "index.html").strip().lstrip("/")
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or "").strip()
_GITHUB_STAFF_SYNC_LOCK = asyncio.Lock()


def _github_staff_guild(bot):
    matches = [guild for guild in bot.guilds if "cedar cove" in guild.name.casefold()]
    if matches:
        return sorted(matches, key=lambda item: (item.name.casefold() != "cedar cove", item.name.casefold()))[0]
    return bot.guilds[0] if len(bot.guilds) == 1 else None


async def _github_staff_items(bot, guild):
    config = await bot.staff_dashboard_store.get(str(guild.id), {})
    ladder = (((config.get("modules") or {}).get("staff_ladder") or {}).get("items") or [])
    ladder_ids = [item.get("roleId") for item in ladder if isinstance(item, dict)]
    items = []
    for member in guild.members:
        if member.bot:
            continue
        roles = _member_staff_roles(member, ladder_ids)
        if not roles:
            continue
        items.append({
            "id": str(member.id),
            "name": member.display_name,
            "role": roles[0],
            "avatar": str(member.display_avatar.with_size(256).url),
        })
    items.sort(key=lambda item: item["name"].casefold())
    return items


def _github_staff_cards(items):
    if not items:
        return '<p class="staff-note">Our staff team is getting ready. Check back soon!</p>'
    cards = []
    for item in items:
        name = html.escape(str(item["name"]), quote=True)
        role = html.escape(str(item["role"]), quote=True)
        avatar = html.escape(str(item["avatar"]), quote=True)
        cards.append(
            '      <div class="staff-card">\n'
            f'        <div class="staff-avatar"><img src="{avatar}" alt="{name} avatar"></div>\n'
            f'        <h3>{name}</h3>\n'
            f'        <span class="staff-role">{role}</span>\n'
            '      </div>'
        )
    return "\n".join(cards)


async def sync_github_staff_website(bot, reason="staff roster update"):
    if not GITHUB_TOKEN:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", GITHUB_STAFF_REPOSITORY):
        bot.staff_dashboard_logger.error("GitHub staff sync skipped: GITHUB_STAFF_REPOSITORY must be owner/repository.")
        return False

    guild = _github_staff_guild(bot)
    if guild is None:
        bot.staff_dashboard_logger.warning("GitHub staff sync skipped: Cedar Cove Discord server could not be identified.")
        return False

    async with _GITHUB_STAFF_SYNC_LOCK:
        try:
            items = await _github_staff_items(bot, guild)
            cards = _github_staff_cards(items)
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            encoded_path = __import__("urllib.parse", fromlist=["quote"]).quote(GITHUB_STAFF_HTML_FILE, safe="/")
            url = f"{GITHUB_API_BASE}/repos/{GITHUB_STAFF_REPOSITORY}/contents/{encoded_path}"
            timeout = aiohttp.ClientTimeout(total=35)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params={"ref": GITHUB_STAFF_BRANCH}) as response:
                    payload = await response.json(content_type=None)
                    if response.status != 200:
                        bot.staff_dashboard_logger.error(
                            "GitHub staff sync could not read %s/%s (%s): %s",
                            GITHUB_STAFF_REPOSITORY, GITHUB_STAFF_HTML_FILE,
                            response.status, str(payload)[:500],
                        )
                        return False

                try:
                    current_html = base64.b64decode(payload["content"]).decode("utf-8")
                except Exception:
                    bot.staff_dashboard_logger.exception("GitHub staff sync received unreadable HTML content.")
                    return False

                grid_match = re.search(r'<div\s+class="staff-grid"[^>]*>', current_html, re.IGNORECASE)
                if not grid_match:
                    bot.staff_dashboard_logger.error(
                        "GitHub staff sync stopped: no staff-grid was found in %s.",
                        GITHUB_STAFF_HTML_FILE,
                    )
                    return False

                start = grid_match.end()
                depth = 1
                end = None
                for tag in re.finditer(r'</?div\b[^>]*>', current_html[start:], re.IGNORECASE):
                    if tag.group(0).startswith("</"):
                        depth -= 1
                    elif not tag.group(0).rstrip().endswith("/>"):
                        depth += 1
                    if depth == 0:
                        end = start + tag.start()
                        break

                if end is None:
                    bot.staff_dashboard_logger.error("GitHub staff sync stopped: staff-grid HTML is incomplete.")
                    return False

                updated_html = current_html[:start] + "\n" + cards + "\n    " + current_html[end:]

                body = {
                    "message": f"Update Cedar Cove staff cards ({reason})",
                    "content": base64.b64encode(updated_html.encode("utf-8")).decode("ascii"),
                    "sha": payload["sha"],
                    "branch": GITHUB_STAFF_BRANCH,
                }
                async with session.put(url, headers=headers, json=body) as response:
                    result = await response.json(content_type=None)
                    if response.status not in (200, 201):
                        bot.staff_dashboard_logger.error(
                            "GitHub staff sync could not commit website update (%s): %s",
                            response.status, str(result)[:500],
                        )
                        return False

            bot.staff_dashboard_logger.info(
                "Updated %d Cedar Cove staff cards in %s/%s:%s (%s).",
                len(items), GITHUB_STAFF_REPOSITORY, GITHUB_STAFF_BRANCH,
                GITHUB_STAFF_HTML_FILE, result.get("commit", {}).get("sha", "commit created"),
            )
            return True
        except Exception:
            bot.staff_dashboard_logger.exception("GitHub staff website update failed.")
            return False


async def _github_staff_sync_on_ready():
    bot = _configured_bot
    if bot is None or getattr(bot, "_github_staff_initial_sync_started", False):
        return
    bot._github_staff_initial_sync_started = True
    await bot.wait_until_ready()
    await sync_github_staff_website(bot, "bot startup")


async def _github_staff_member_update(before, after):
    if before.bot:
        return
    changed = (
        before.roles != after.roles
        or before.display_name != after.display_name
        or str(before.display_avatar.url) != str(after.display_avatar.url)
    )
    if changed:
        await sync_github_staff_website(after._state._get_client(), "member profile or staff role changed")


async def _github_staff_member_join(member):
    if not member.bot:
        await sync_github_staff_website(member._state._get_client(), "member joined")


async def _github_staff_member_remove(member):
    if not member.bot:
        await sync_github_staff_website(member._state._get_client(), "member left")


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
    for key in ("promotionChannelId", "retirementChannelId", "firedChannelId"):
        value = str(body.get(key) or "")
        if value and value not in channels:
            return _json(request, {"error": "Choose a valid text channel for " + key + "."}, 400)
    for key in ("memberRoleId", "retiredRoleId"):
        value = str(body.get(key) or "")
        if not value or value not in roles:
            return _json(request, {"error": "Choose a valid Discord role for " + key + "."}, 400)
    settings = {
        "promotionChannelId": str(body.get("promotionChannelId") or ""),
        "promotionMessage": str(body.get("promotionMessage") or "")[:1800],
        "retirementChannelId": str(body.get("retirementChannelId") or ""),
        "retirementMessage": str(body.get("retirementMessage") or "")[:1800],
        "firedChannelId": str(body.get("firedChannelId") or ""),
        "firedMessage": str(body.get("firedMessage") or "")[:1800],
        "memberRoleId": str(body.get("memberRoleId")),
        "retiredRoleId": str(body.get("retiredRoleId")),
    }
    config.setdefault("modules", {})["staff_actions"] = settings
    await request.app["staff_store"].set(str(guild.id), config)
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
            await _send_staff_announcement(guild, settings.get("firedChannelId"), settings.get("firedMessage"), member, actor, reason, "fired")
            await _audit(request, str(guild.id), user, "staff_fired", f"Fired {member_id}: {reason}")
            return _json(request, {"ok": True, "message": ("Staff member fired; dashboard access revoked and website account preserved." if access_revoked else "Staff member fired; no linked website account was found to revoke."), "dashboardAccessRevoked": access_revoked})
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
        # First saved rank is lowest; last saved rank is highest.
        current_index = max(current_indexes) if current_indexes else -1
        next_index = current_index + 1 if action == "promote" else current_index - 1
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
