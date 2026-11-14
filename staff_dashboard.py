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
import io
import re
import time
from datetime import datetime, timezone, timedelta
import secrets
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
from aiohttp import web

AUTH_API_URL = (os.getenv("AUTH_API_URL") or "https://verification-server-gjqt.onrender.com").rstrip("/")
DASHBOARD_API_BUILD = "scnx-message-editor-2026-11-07"
STAFF_ROLE_NAMES = {"owner": "Owner", "co owner": "Co Owner", "admin": "Admin", "administrator": "Admin"}




async def _appeal_audit_entry(guild, action, member_id, timeout_only=False):
    me = guild.me
    if not me or not me.guild_permissions.view_audit_log:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=25)
    for attempt in range(5):
        try:
            async for entry in guild.audit_logs(limit=15, action=action):
                target = getattr(entry, "target", None)
                if not target or int(getattr(target, "id", 0)) != int(member_id):
                    continue
                if entry.created_at and entry.created_at < cutoff:
                    continue
                if timeout_only:
                    after_changes = getattr(getattr(entry, "changes", None), "after", None)
                    changed_until = getattr(after_changes, "communication_disabled_until", None)
                    if changed_until is None:
                        continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        if attempt < 4:
            await asyncio.sleep(0.8)
    return None


async def _issue_appeal_link(bot, guild, member, punishment, audit_entry):
    collection = getattr(bot, "appeal_tokens_collection", None)
    if collection is None or member.bot:
        return
    moderator = getattr(audit_entry, "user", None)
    if moderator is None:
        return
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    record = {
        "token": token,
        "guildId": str(guild.id),
        "guildName": guild.name,
        "memberId": str(member.id),
        "memberName": str(member),
        "punishment": punishment,
        "moderatorId": str(moderator.id),
        "moderatorName": str(moderator),
        "reason": str(getattr(audit_entry, "reason", None) or "No reason provided")[:500],
        "createdAt": now,
        "expiresAt": now + 30 * 24 * 60 * 60,
        "usedAt": None,
    }
    try:
        await collection.insert_one(record)
        base_url = (os.getenv("APPEALS_BASE_URL") or "https://cedar-cove-appeals.onrender.com").rstrip("/")
        link = f"{base_url}/{punishment}.html#token={token}"
        await member.send(
            f"You received a **{punishment}** action in **{guild.name}**. "
            f"You can submit one appeal using this personal link (valid for 30 days):\n{link}\n\n"
            "Your appeal will be sent to the Cedar Cove staff team for review. "
            "Keep your direct messages open for any follow-up."
        )
    except discord.Forbidden:
        await collection.delete_one({"token": token})
        logger = getattr(bot, "staff_dashboard_logger", None)
        if logger:
            logger.info("Could not DM %s their %s appeal link in guild %s.", member.id, punishment, guild.id)
    except Exception:
        await collection.delete_one({"token": token})
        logger = getattr(bot, "staff_dashboard_logger", None)
        if logger:
            logger.exception("Failed to issue %s appeal link for member %s.", punishment, member.id)


async def _appeal_member_ban(guild, user):
    entry = await _appeal_audit_entry(guild, discord.AuditLogAction.ban, user.id)
    if entry:
        await _issue_appeal_link(guild._state._get_client(), guild, user, "ban", entry)


async def _appeal_member_remove(member):
    entry = await _appeal_audit_entry(member.guild, discord.AuditLogAction.kick, member.id)
    if entry:
        await _issue_appeal_link(member.guild._state._get_client(), member.guild, member, "kick", entry)


async def _appeal_member_update(before, after):
    if getattr(before, "timed_out_until", None) == getattr(after, "timed_out_until", None):
        return
    if getattr(after, "timed_out_until", None) is None:
        return
    entry = await _appeal_audit_entry(after.guild, discord.AuditLogAction.member_update, after.id, timeout_only=True)
    if entry:
        await _issue_appeal_link(after.guild._state._get_client(), after.guild, after, "mute", entry)


async def public_appeal_validate(request):
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid appeal link."}, 400)
    if not isinstance(body, dict):
        return _json(request, {"error": "Invalid appeal link."}, 400)
    kind = str(body.get("kind") or "").lower()
    token = str(body.get("token") or "")
    if kind not in {"mute", "kick", "ban"} or not token or len(token) > 128:
        return _json(request, {"error": "This appeal link is invalid."}, 400)
    collection = request.app["staff_bot"].appeal_tokens_collection
    now = int(time.time())
    record = await collection.find_one({
        "token": token, "punishment": kind, "usedAt": None, "expiresAt": {"$gt": now}
    }, {"_id": 0, "memberId": 1, "punishment": 1})
    if not record:
        return _json(request, {"error": "This appeal link is invalid, expired, or has already been used."}, 410)
    return _json(request, {"ok": True, "memberId": record["memberId"], "punishment": record["punishment"]})


async def public_appeal_submit(request):
    try:
        body = await request.json()
    except Exception:
        return _json(request, {"error": "Invalid appeal submission."}, 400)
    if not isinstance(body, dict):
        return _json(request, {"error": "Invalid appeal submission."}, 400)
    kind = str(body.get("kind") or "").lower()
    token = str(body.get("token") or "")
    answers = body.get("answers") if isinstance(body, dict) else None
    if kind not in {"mute", "kick", "ban"} or not token or len(token) > 128:
        return _json(request, {"error": "This appeal link is invalid."}, 400)
    if not isinstance(answers, dict):
        return _json(request, {"error": "Your answers could not be read."}, 400)
    allowed = {
        "username": 80, "userId": 20, "reason": 2000,
        "appeal": 3000, "change": 2000
    }
    clean = {}
    for key, limit in allowed.items():
        value = answers.get(key, "")
        if not isinstance(value, str):
            return _json(request, {"error": "Invalid answer format."}, 400)
        clean[key] = value.strip()[:limit]
    if not clean["username"] or not clean["userId"].isdigit() or not clean["reason"] or not clean["appeal"]:
        return _json(request, {"error": "Please complete every required field."}, 400)
    if kind == "ban" and not clean["change"]:
        return _json(request, {"error": "Please explain what you would do differently."}, 400)

    bot = request.app["staff_bot"]
    collection = bot.appeal_tokens_collection
    now = int(time.time())
    claim = await collection.update_one(
        {"token": token, "punishment": kind, "memberId": clean["userId"],
         "usedAt": None, "expiresAt": {"$gt": now}},
        {"$set": {"usedAt": now, "submittedAt": now, "answers": clean}}
    )
    if not claim.matched_count:
        return _json(request, {"error": "This appeal link is invalid, expired, or has already been used."}, 410)

    record = await collection.find_one({"token": token}, {"_id": 0})
    guild = bot.get_guild(int(record["guildId"])) if record and str(record.get("guildId", "")).isdigit() else None
    delivered = False
    if guild:
        config = await bot.staff_dashboard_store.get(str(guild.id), {})
        logging_config = ((config.get("modules") or {}).get("logging") or {})
        events = logging_config.get("events") if isinstance(logging_config.get("events"), dict) else {}
        event_config = events.get("moderationActions") if isinstance(events.get("moderationActions"), dict) else {}
        channel_id = str(
            event_config.get("channelId")
            or logging_config.get("moderationActionsChannelId")
            or logging_config.get("channelId")
            or logging_config.get("logChannelId")
            or ""
        )
        channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        embed = discord.Embed(
            title=f"New {kind.title()} Appeal",
            description=f"**Member:** {record.get('memberName', clean['username'])} ({record['memberId']})\n"
                        f"**Moderator:** {record.get('moderatorName', 'Unknown')} ({record.get('moderatorId', 'Unknown')})\n"
                        f"**Original reason:** {record.get('reason', 'Not provided')}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        for key, title in (("username", "Discord username"), ("reason", "What happened"),
                           ("appeal", "Why should this be reconsidered?"), ("change", "What will change?")):
            if clean.get(key):
                embed.add_field(name=title, value=clean[key][:1024], inline=False)
        embed.set_footer(text=f"Appeal token: {token[:10]}…")
        try:
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                await channel.send(embed=embed)
                delivered = True
            elif guild.owner:
                await guild.owner.send(embed=embed)
                delivered = True
        except discord.HTTPException:
            pass
    return _json(request, {"ok": True, "delivered": delivered}, 201)


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
    app.router.add_route("OPTIONS", "/api/appeals/{tail:.*}", options_handler)
    app.router.add_post("/api/appeals/validate", public_appeal_validate)
    app.router.add_post("/api/appeals/submit", public_appeal_submit)
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
    app.router.add_post("/api/dashboard/verification/send", send_verification_message)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/audit", list_audit)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/honeypot-stats", get_honeypot_stats)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/role-nicknames", get_role_nicknames)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/role-nicknames", save_role_nicknames)
    app.router.add_get("/api/dashboard/guilds/{guild_id}/role-connections", get_role_connections)
    app.router.add_put("/api/dashboard/guilds/{guild_id}/role-connections", save_role_connections)


_configured_bot = None

# Wrap setup_staff above to keep route registration dependency-free for mega_bot.py.
_original_setup_staff = setup_staff
def setup_staff(bot, get_store, logger):
    global _configured_bot
    _configured_bot = bot
    _original_setup_staff(bot, get_store, logger)
    bot.staff_dashboard_store_for_panels = get_store("panels")
    bot.staff_management_store = get_store("staff_management_records")
    bot.member_moderation_store = get_store("member_moderation_records")