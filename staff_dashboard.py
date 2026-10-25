"""Session-authenticated staff dashboard API for Mega Bot.

Required environment variable:
  AUTH_API_URL=https://verification-server-gjqt.onrender.com

CORS uses wildcard origins without browser cookies; API calls require a valid
bearer session token verified by the verification service.
"""
import os
import asyncio
import re
import time
import secrets
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
from aiohttp import web

AUTH_API_URL = (os.getenv("AUTH_API_URL") or "https://verification-server-gjqt.onrender.com").rstrip("/")
DASHBOARD_API_BUILD = "staff-manager-2026-10-25-role-nickname-sync"
STAFF_ACCOUNT_DELETE_SECRET = os.getenv("STAFF_ACCOUNT_DELETE_SECRET", "").strip()
STAFF_ROLE_NAMES = {"owner": "Owner", "co owner": "Co Owner", "admin": "Admin", "administrator": "Admin"}


def setup_staff(bot, get_store, logger):
    bot.staff_dashboard_store = get_store("staff_dashboard_configs")
    bot.staff_dashboard_audit_store = get_store("staff_dashboard_audit")
    bot.role_nicknames_store = get_store("role_nicknames")
    if not getattr(bot, "_role_nickname_listener_added", False):
        bot.add_listener(_role_nickname_member_update, "on_member_update")
        bot._role_nickname_listener_added = True
    if not getattr(bot, "_role_nickname_sync_task", None):
        bot._role_nickname_sync_task = bot.loop.create_task(_role_nickname_sync_loop(bot))
    bot.staff_dashboard_logger = logger


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
        # Only retain expected top-level fields; no secrets are accepted.
        clean = {"guildId": gid, "timezone": str(config.get("timezone", ""))[:100], "modules": {}}
        for name, settings in config["modules"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", str(name)) or not isinstance(settings, dict):
                continue
            entry = {}
            for key, value in settings.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    entry[str(key)[:60]] = value if not isinstance(value, str) else value[:4000]
            clean["modules"][str(name)] = entry
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
    role_id = panel.get("supportRoleId")
    category = guild.get_channel(int(category_id)) if str(category_id or "").isdigit() else None
    role = guild.get_role(int(role_id)) if str(role_id or "").isdigit() else None
    if category_id and not isinstance(category, discord.CategoryChannel):
        return _json(request, {"error": "Choose a valid ticket category from this server."}, 400)
    if role_id and role is None:
        return _json(request, {"error": "Choose a valid support role from this server."}, 400)
    title = str(panel.get("title") or "How can we help?")[:256]
    description = str(panel.get("message") or "Choose an option below and our staff team will help you.")[:4000]
    types = [x.strip()[:80] for x in str(panel.get("types") or "General Support").splitlines() if x.strip()][:25]
    if not types:
        types = ["General Support"]
    options = [{"label": x, "description": None, "emoji": None, "ticket_message": None, "category_id": int(category.id) if category else None, "role_id": int(role.id) if role else None} for x in types]
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
        panel_record = {"title": title, "description": description, "channel_id": int(channel.id), "category_id": int(category.id) if category else None, "role_id": int(role.id) if role else None, "options": options, "message_id": msg.id, "guild_id": guild.id, "created_by_dashboard": True}
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


async def _apply_role_nickname(member, rules):
    if member.bot:
        return False
    current = member.nick or member.name
    base = current
    for rule in rules:
        prefix, suffix = str(rule.get("prefix") or ""), str(rule.get("suffix") or "")
        if prefix and base.startswith(prefix):
            base = base[len(prefix):]
        if suffix and base.endswith(suffix):
            base = base[:-len(suffix)]
    match = next((rule for rule in rules if str(rule.get("roleId", "")).isdigit() and any(str(role.id) == str(rule["roleId"]) for role in member.roles)), None)
    desired = ((str(match.get("prefix") or "") + base + str(match.get("suffix") or ""))[:32]) if match else base
    if desired == member.nick:
        return False
    try:
        await member.edit(nick=desired, reason="Cedar Cove role nickname rule")
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _role_nickname_member_update(before, after):
    if before.guild is None:
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
    store = request.app["role_nicknames_store"]
    previous_rules = await store.get(gid, [])
    if not isinstance(previous_rules, list):
        previous_rules = []
    await store.set(gid, rules)
    updated = 0
    for member in list(guild.members):
        if member.bot:
            continue
        current = member.nick or member.name
        base = current
        for old_rule in previous_rules:
            old_prefix = str(old_rule.get("prefix") or "")
            old_suffix = str(old_rule.get("suffix") or "")
            if old_prefix and base.startswith(old_prefix):
                base = base[len(old_prefix):]
            if old_suffix and base.endswith(old_suffix):
                base = base[:-len(old_suffix)]
        match = next((rule for rule in rules if any(str(role.id) == str(rule["roleId"]) for role in member.roles)), None)
        desired = ((str(match.get("prefix") or "") + base + str(match.get("suffix") or ""))[:32]) if match else base
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


async def _audit(request, gid, user, action, detail):
    store = request.app["audit_store"]
    records = await store.get(gid, [])
    if not isinstance(records, list):
        records = []
    records.append({"time": int(time.time()), "actor": str(user.get("username", "staff"))[:80], "action": action, "detail": detail[:500]})
    await store.set(gid, records[-500:])




def _staff_guild(request, user=None):
    bot = request.app["staff_bot"]
    configured = os.getenv("GUILD_ID", "").strip()
    if configured.isdigit():
        return bot.get_guild(int(configured))

    # Without GUILD_ID, prefer the server the signed-in Discord account owns
    # or can manage, rather than relying only on role names existing server-wide.
    discord_id = str((user or {}).get("discordId") or "").strip()
    if discord_id.isdigit():
        for guild in bot.guilds:
            if str(getattr(guild, "owner_id", "")) == discord_id:
                return guild
            member = guild.get_member(int(discord_id))
            if member and _member_can_manage_staff(member):
                return guild

    allowed = {"owner", "co owner", "coowner", "admin", "administrator"}
    return next(
        (
            guild for guild in bot.guilds
            if any(
                re.sub(r"[^a-z0-9]+", " ", role.name.casefold()).strip() in allowed
                for role in guild.roles
            )
        ),
        None,
    )


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

    guild = _staff_guild(request, user)
    if guild is None:
        return None, None, _json(request, {"error": "The bot could not find the configured Cedar Cove server. Set GUILD_ID in the bot service environment."}, 503)
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


def _member_staff_roles(member):
    return [r.name for r in sorted(member.roles, key=lambda role: role.position, reverse=True) if r.name.casefold() in {"owner", "co owner", "admin", "administrator", "mod", "moderator", "trial staff", "helper", "socials manager", "staff"}]


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


async def staff_list(request):
    user, guild, error = await _staff_manager(request)
    if error:
        return error
    records = await request.app["staff_management_store"].get(str(guild.id), {})
    if not isinstance(records, dict):
        records = {}
    items = []
    for member in sorted(guild.members, key=lambda m: m.display_name.casefold()):
        roles = _member_staff_roles(member)
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
    roles = _member_staff_roles(member)
    record = await request.app["staff_management_store"].get(str(guild.id), {})
    item = record.get(str(member.id), {}) if isinstance(record, dict) else {}
    if member.bot or (not roles and not item.get("suspended")):
        return _json(request, {"error": "Only current or suspended human staff members can be managed here."}, 403)
    return _json(request, {"member": {"id": str(member.id), "username": member.name, "displayName": member.display_name, "roles": _member_staff_roles(member)}, "record": item, "loaStatus": _loa_state(item)})


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
    existing_records = await request.app["staff_management_store"].get(str(guild.id), {})
    existing_record = existing_records.get(member_id, {}) if isinstance(existing_records, dict) else {}
    if member.bot or (not _member_staff_roles(member) and not existing_record.get("suspended")):
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
    if action in {"warn", "strike"}:
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
            return _json(request, {"error": f"The bot cannot find a Member role in the selected server '{guild.name}'. Roles containing 'member' visible to the bot: {', '.join(visible_roles) if visible_roles else 'none'}. Check GUILD_ID and make sure the bot is in the server that contains your Member role."}, 400)
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
        if not STAFF_ACCOUNT_DELETE_SECRET:
            return _json(request, {"error": "Dashboard access management is not configured. Set STAFF_ACCOUNT_DELETE_SECRET on both services."}, 503)
        allowed = action == "grant_dashboard_access"
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(AUTH_API_URL + "/api/internal/staff/dashboard-access", json={"username": target_username, "allowed": allowed}, headers={"X-Staff-Manager-Key": STAFF_ACCOUNT_DELETE_SECRET}) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status != 200:
                        return _json(request, {"error": payload.get("error", "Could not update dashboard access.")}, resp.status)
        except Exception:
            request.app["staff_logger"].exception("Dashboard access update failed")
            return _json(request, {"error": "Could not reach the verification service."}, 502)
        record.setdefault("accountActions", []).append({"action": action, "username": payload.get("username", target_username), "by": actor, "at": now})
    elif action == "delete_account":
        if not STAFF_ACCOUNT_DELETE_SECRET:
            return _json(request, {"error": "Account deletion is not configured yet. Set STAFF_ACCOUNT_DELETE_SECRET on both the bot and verification services."}, 503)
        if str(member_id) == str(user.get("discordId")):
            return _json(request, {"error": "You cannot delete your own dashboard account here."}, 400)
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(AUTH_API_URL + "/api/internal/staff/delete-account", json={"discordId": str(member.id)}, headers={"X-Staff-Manager-Key": STAFF_ACCOUNT_DELETE_SECRET}) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status != 200:
                        return _json(request, {"error": payload.get("error", "Account deletion failed.")}, resp.status)
        except Exception:
            request.app["staff_logger"].exception("Staff dashboard account deletion request failed")
            return _json(request, {"error": "Could not reach the verification service."}, 502)
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
    app.router.add_get("/api/dashboard/staff", staff_list)
    app.router.add_get("/api/dashboard/staff/{member_id}", staff_detail)
    app.router.add_post("/api/dashboard/staff/{member_id}/action", staff_action)
    app.router.add_get("/api/dashboard/status", api_status)
    app.router.add_get("/api/dashboard/guilds", list_guilds)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/channels", list_channels)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/roles", list_roles)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/config", get_config)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/config", save_config)
    # Compatibility aliases for dashboard versions that use /settings.
    app.router.add_get("/api/dashboard/guilds/{guild_id}/settings", get_config)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/settings", save_config)
    app.router.add_post("/api/dashboard/publish-panel", publish_panel)
    app.router.add_post("/api/dashboard/send-message", send_module_message)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/audit", list_audit)
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
