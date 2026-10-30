"""
Mega Bot — combines the Role Connections bot and the Ticket bot into a
single Discord application, sharing one bot process, one health-check
server, and one .env configuration.
"""

import os
import re
import json
import time
import uuid
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

try:
    import motor.motor_asyncio
    MOTOR_AVAILABLE = True
except ImportError:
    MOTOR_AVAILABLE = False

# The Voice (Cedar Cove) subsystem has a hard dependency on motor — unlike
# the other two subsystems, it has no JSON fallback.
from motor.motor_asyncio import AsyncIOMotorClient


load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Accepts either DISCORD_BOT_TOKEN or DISCORD_TOKEN so existing .env files
# from either original bot keep working without edits.
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")

MONGODB_URI = os.getenv("MONGODB_URI")
# Single shared database name for both subsystems (accepts either old name).
MONGODB_DB_NAME = (os.getenv("MONGODB_DATABASE") or "cedar_cove").strip() or "cedar_cove"
MONGODB_DB = MONGODB_DB_NAME  # all bot modules share the Cedar Cove database

GUILD_ID = os.getenv("GUILD_ID")
PORT = int(os.getenv("PORT", "8080"))

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

ROLE_CONNECTIONS_JSON_PATH = os.path.join(DATA_DIR, "role_connections.json")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mega_bot")
ROLE_SYNC_LOG_LEVEL = logging.DEBUG

START_TIME = time.time()
# ---------------------------------------------------------------------------
# Storage — Role Connections subsystem
# ---------------------------------------------------------------------------
# Uses MongoDB automatically if MONGODB_URI is set and reachable, otherwise
# falls back to a local JSON file.

class RoleConnectionStorage:
    def __init__(self, mongo_uri: Optional[str], db_name: str, json_path: str):
        self._mongo_uri = mongo_uri
        self._db_name = db_name
        self._json_path = json_path
        self._use_mongo = False
        self._client = None
        self._db = None
        self._lock = asyncio.Lock()

    async def connect(self):
        if MOTOR_AVAILABLE and self._mongo_uri:
            try:
                self._client = motor.motor_asyncio.AsyncIOMotorClient(
                    self._mongo_uri, serverSelectionTimeoutMS=5000
                )
                await self._client.admin.command("ping")
                self._db = self._client[self._db_name]
                self._use_mongo = True
                logger.info("Connected to MongoDB successfully. Using MongoDB storage.")
                return
            except Exception as exc:
                logger.warning("Could not connect to MongoDB (%s). Falling back to JSON storage.", exc)
                self._use_mongo = False
                self._client = None
                self._db = None
        else:
            logger.info("MongoDB not configured or motor not installed. Using JSON storage.")

        if not self._use_mongo:
            self._ensure_json_file()

    def _ensure_json_file(self):
        if not os.path.exists(self._json_path):
            with open(self._json_path, "w", encoding="utf-8") as f:
                json.dump({"guilds": {}}, f, indent=2)

    def _read_json(self) -> dict:
        self._ensure_json_file()
        with open(self._json_path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {"guilds": {}}

    def _write_json(self, data: dict):
        with open(self._json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _get_guild_block(self, data: dict, guild_id: int) -> dict:
        gid = str(guild_id)
        if gid not in data["guilds"]:
            data["guilds"][gid] = {"connections": [], "settings": {"enabled": True}}
        return data["guilds"][gid]

    async def get_connections(self, guild_id: int) -> list:
        if self._use_mongo:
            cursor = self._db.connections.find({"guild_id": guild_id})
            docs = await cursor.to_list(length=None)
            for d in docs:
                d.pop("_id", None)
            return docs
        async with self._lock:
            data = self._read_json()
            block = self._get_guild_block(data, guild_id)
            return list(block["connections"])

    async def get_connection(self, guild_id: int, connection_id: str) -> Optional[dict]:
        conns = await self.get_connections(guild_id)
        for c in conns:
            if c["id"] == connection_id:
                return c
        return None

    async def create_connection(self, guild_id: int, name: str, main_role_id: int,
                                 connection_roles: list, combine_mode: str) -> str:
        connection_id = uuid.uuid4().hex[:8]
        doc = {
            "id": connection_id,
            "guild_id": guild_id,
            "name": name,
            "enabled": True,
            "main_role_id": main_role_id,
            "connection_roles": connection_roles,
            "combine_mode": combine_mode,
        }
        if self._use_mongo:
            await self._db.connections.insert_one(dict(doc))
        else:
            async with self._lock:
                data = self._read_json()
                block = self._get_guild_block(data, guild_id)
                block["connections"].append(doc)
                self._write_json(data)
        return connection_id

    async def update_connection(self, guild_id: int, connection_id: str, updates: dict) -> bool:
        if self._use_mongo:
            result = await self._db.connections.update_one(
                {"guild_id": guild_id, "id": connection_id}, {"$set": updates}
            )
            return result.matched_count > 0
        async with self._lock:
            data = self._read_json()
            block = self._get_guild_block(data, guild_id)
            for c in block["connections"]:
                if c["id"] == connection_id:
                    c.update(updates)
                    self._write_json(data)
                    return True
            return False

    async def delete_connection(self, guild_id: int, connection_id: str) -> bool:
        if self._use_mongo:
            result = await self._db.connections.delete_one(
                {"guild_id": guild_id, "id": connection_id}
            )
            return result.deleted_count > 0
        async with self._lock:
            data = self._read_json()
            block = self._get_guild_block(data, guild_id)
            before = len(block["connections"])
            block["connections"] = [c for c in block["connections"] if c["id"] != connection_id]
            self._write_json(data)
            return len(block["connections"]) < before

    async def get_settings(self, guild_id: int) -> dict:
        if self._use_mongo:
            doc = await self._db.settings.find_one({"guild_id": guild_id})
            if doc:
                doc.pop("_id", None)
                return doc
            return {"guild_id": guild_id, "enabled": True}
        async with self._lock:
            data = self._read_json()
            block = self._get_guild_block(data, guild_id)
            return dict(block["settings"])

    async def update_settings(self, guild_id: int, updates: dict):
        if self._use_mongo:
            await self._db.settings.update_one(
                {"guild_id": guild_id}, {"$set": updates}, upsert=True
            )
        else:
            async with self._lock:
                data = self._read_json()
                block = self._get_guild_block(data, guild_id)
                block["settings"].update(updates)
                self._write_json(data)

    async def get_guild_links(self, guild_id: int) -> list:
        if self._use_mongo:
            cursor = self._db.guild_links.find(
                {"$or": [{"guild_a_id": guild_id}, {"guild_b_id": guild_id}]}
            )
            docs = await cursor.to_list(length=None)
            for d in docs:
                d.pop("_id", None)
            return docs
        async with self._lock:
            data = self._read_json()
            links = data.get("guild_links", [])
            return [l for l in links if l["guild_a_id"] == guild_id or l["guild_b_id"] == guild_id]

    async def get_all_guild_links(self) -> list:
        if self._use_mongo:
            cursor = self._db.guild_links.find({})
            docs = await cursor.to_list(length=None)
            for d in docs:
                d.pop("_id", None)
            return docs
        async with self._lock:
            data = self._read_json()
            return list(data.get("guild_links", []))

    async def create_guild_link(self, guild_a_id: int, guild_b_id: int) -> str:
        link_id = uuid.uuid4().hex[:8]
        doc = {
            "id": link_id,
            "guild_a_id": guild_a_id,
            "guild_b_id": guild_b_id,
            "enabled": True,
        }
        if self._use_mongo:
            await self._db.guild_links.insert_one(dict(doc))
        else:
            async with self._lock:
                data = self._read_json()
                data.setdefault("guild_links", []).append(doc)
                self._write_json(data)
        return link_id

    async def delete_guild_link(self, link_id: str) -> bool:
        if self._use_mongo:
            result = await self._db.guild_links.delete_one({"id": link_id})
            return result.deleted_count > 0
        async with self._lock:
            data = self._read_json()
            links = data.get("guild_links", [])
            before = len(links)
            data["guild_links"] = [l for l in links if l["id"] != link_id]
            self._write_json(data)
            return len(data["guild_links"]) < before


role_connections_storage = RoleConnectionStorage(MONGODB_URI, MONGODB_DB_NAME, ROLE_CONNECTIONS_JSON_PATH)


# ---------------------------------------------------------------------------
# Storage — Ticket subsystem
# ---------------------------------------------------------------------------
# JSON files by default, MongoDB automatically if MONGODB_URI is set. Shares
# the same MongoDB database as the Role Connections subsystem (different
# collections), and stores its JSON files under the same data/ directory.

class JSONStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump({}, f)

    def _read(self) -> dict:
        with open(self.path, "r") as f:
            return json.load(f)

    def _write(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    async def get_all(self) -> dict:
        async with self._lock:
            return self._read()

    async def get(self, key, default=None):
        async with self._lock:
            return self._read().get(str(key), default)

    async def set(self, key, value):
        async with self._lock:
            data = self._read()
            data[str(key)] = value
            self._write(data)

    async def delete(self, key):
        async with self._lock:
            data = self._read()
            data.pop(str(key), None)
            self._write(data)

    async def find_one(self, predicate):
        async with self._lock:
            data = self._read()
        for k, v in data.items():
            if predicate(k, v):
                return k, v
        return None, None


class MongoStore:
    def __init__(self, collection):
        self._col = collection

    @staticmethod
    def _wrap(value):
        return dict(value) if isinstance(value, dict) else {"value": value}

    @staticmethod
    def _unwrap(doc):
        if doc is None:
            return None
        doc = dict(doc)
        doc.pop("_id", None)
        if set(doc.keys()) == {"value"}:
            return doc["value"]
        return doc

    async def get_all(self) -> dict:
        result = {}
        async for doc in self._col.find({}):
            result[doc["_id"]] = self._unwrap(doc)
        return result

    async def get(self, key, default=None):
        doc = await self._col.find_one({"_id": str(key)})
        return self._unwrap(doc) if doc is not None else default

    async def set(self, key, value):
        payload = self._wrap(value)
        payload["_id"] = str(key)
        await self._col.replace_one({"_id": str(key)}, payload, upsert=True)

    async def delete(self, key):
        await self._col.delete_one({"_id": str(key)})

    async def find_one(self, predicate):
        async for doc in self._col.find({}):
            key = doc["_id"]
            value = self._unwrap(doc)
            if predicate(key, value):
                return key, value
        return None, None


_mongo_db = None


def _get_mongo_db():
    global _mongo_db
    if _mongo_db is None:
        from motor.motor_asyncio import AsyncIOMotorClient
        _mongo_db = AsyncIOMotorClient(MONGODB_URI)[MONGODB_DB]
    return _mongo_db


def get_store(name: str):
    if MONGODB_URI:
        return MongoStore(_get_mongo_db()[name])
    return JSONStore(os.path.join(DATA_DIR, f"{name}.json"))


panels_store = get_store("panels")       # message_id -> panel config
tickets_store = get_store("tickets")     # channel_id -> ticket record
counters_store = get_store("counters")   # guild_id -> last ticket number

# In-memory scratch space for panels currently being built via /setup.
active_builders: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Storage — Voice (Cedar Cove) subsystem
# ---------------------------------------------------------------------------
# Unlike the other two subsystems, this one requires MongoDB — there's no
# JSON fallback. If MONGODB_URI isn't set, the bot will still start (the
# other subsystems work fine), but joining the lobby/creator channels will
# fail until a connection string is provided.

cedar_cove_mongo = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
cedar_cove_db = cedar_cove_mongo[MONGODB_DB_NAME]

vc_guild_configs = cedar_cove_db["guilds"]
temp_channels = cedar_cove_db["temp_channels"]


async def check_cedar_cove_mongodb():
    try:
        await cedar_cove_mongo.server_info()
        logger.info("Cedar Cove: MongoDB connected.")
    except Exception as exc:
        logger.warning(
            "Cedar Cove: MongoDB connection failed (%s). Voice channel "
            "features (Join to Create / Lobby) will not work until "
            "MONGODB_URI is set correctly.", exc,
        )

# ---------------------------------------------------------------------------
# Health check server (single combined endpoint, e.g. for UptimeRobot)
# ---------------------------------------------------------------------------

async def health_handler(request: web.Request) -> web.Response:
    connected = bot.is_ready() and not bot.is_closed()
    payload = {
        "status": "ok" if connected else "degraded",
        "discord_connected": connected,
        "latency_ms": round(bot.latency * 1000) if connected else None,
        "guild_count": len(bot.guilds) if connected else 0,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "role_connections_storage": "mongodb" if role_connections_storage._use_mongo else "json",
        "ticket_storage": "mongodb" if MONGODB_URI else "json",
        "voice_storage": "mongodb" if MONGODB_URI else "not configured",
    }
    return web.json_response(payload, status=200 if connected else 503)


async def root_handler(request: web.Request) -> web.Response:
    return web.Response(text="Mega Bot is running.")


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
    register_staff_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health check server listening on port %d (/, /health)", PORT)
    return runner
# ---------------------------------------------------------------------------
# Intents (union of what both original bots needed)
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
intents.voice_states = True  # required by the Voice (Cedar Cove) subsystem
# ---------------------------------------------------------------------------
# Permission helpers — Role Connections subsystem
# ---------------------------------------------------------------------------

def is_admin(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


async def require_admin(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction):
        message = "🚫 You need the **Administrator** permission to use this."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Permission & naming helpers — Ticket subsystem
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Turn a ticket type label into a valid, tidy Discord channel name segment."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:80] or "ticket"


def ticket_setup_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "You need Administrator permission to do this.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)


def _is_support(member: discord.Member, role_id) -> bool:
    if member.guild_permissions.administrator:
        return True
    role_ids = role_id if isinstance(role_id, (list, tuple, set)) else [role_id]
    valid_ids = {int(value) for value in role_ids if str(value or "").isdigit()}
    return any(role.id in valid_ids for role in member.roles)


# ---------------------------------------------------------------------------
# Ticket creation / lifecycle helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Domain logic, embeds, and interactive views — Role Connections subsystem
# ---------------------------------------------------------------------------

CONDITION_OPTIONS = [
    discord.SelectOption(label="Member HAS this role", value="has", emoji="✅"),
    discord.SelectOption(label="Member does NOT have this role", value="not_has", emoji="🚫"),
]

COMBINE_MODE_OPTIONS = [
    discord.SelectOption(label="ALL of these must be true (AND)", value="and", emoji="🔗"),
    discord.SelectOption(label="ANY ONE of these can be true (OR)", value="or", emoji="🔀"),
]


def condition_label(value: str) -> str:
    return "Has this role" if value == "has" else "Doesn't have this role"


def combine_mode_label(value: str) -> str:
    return "ALL must be true (AND)" if value == "and" else "ANY can be true (OR)"


def connection_roles_qualify(connection: dict, role_ids: set) -> bool:
    conditions = connection.get("connection_roles", [])
    if not conditions:
        return False
    results = []
    for c in conditions:
        in_set = c["role_id"] in role_ids
        results.append(in_set if c.get("condition", "has") == "has" else not in_set)
    if connection.get("combine_mode", "and") == "and":
        return all(results)
    return any(results)


def compute_role_target_states(connections: list, role_ids: set) -> dict:
    targets = {}
    for conn in connections:
        if not conn.get("enabled", True):
            continue
        qualifies = connection_roles_qualify(conn, role_ids)
        rid = conn["main_role_id"]
        targets[rid] = targets.get(rid, False) or qualifies
    return targets


def build_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🔗 Role Connections",
        description=(
            "Automatically grant or remove a role based on other roles a member has.\n\n"
            "**Connection Role(s)** are the roles a member already has that get checked. "
            "**Main Role** is the single role that gets automatically added or removed "
            "based on those.\n\n"
            "Use the buttons below to manage this server's role connections."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"{guild.name} • Role Connections")
    return embed


def build_connection_roles_text(guild: discord.Guild, connection: dict) -> str:
    conditions = connection.get("connection_roles", [])
    lines = []
    for c in conditions:
        role = guild.get_role(c["role_id"])
        role_text = role.mention if role else "*(deleted role)*"
        lines.append(f"{role_text} — {condition_label(c.get('condition', 'has'))}")
    if not lines:
        return "*None set*"
    text = "\n".join(lines)
    if len(conditions) > 1:
        text += f"\n**Combine:** {combine_mode_label(connection.get('combine_mode', 'and'))}"
    return text


def build_connections_list_embed(guild: discord.Guild, connections: list) -> discord.Embed:
    embed = discord.Embed(
        title="📋 Configured Role Connections",
        color=discord.Color.blurple(),
    )
    if not connections:
        embed.description = "No role connections have been configured yet."
        return embed

    for conn in connections:
        main_role = guild.get_role(conn["main_role_id"])
        main_role_text = main_role.mention if main_role else "*(deleted role)*"
        status = "🟢 Enabled" if conn.get("enabled", True) else "🔴 Disabled"
        connection_roles_text = build_connection_roles_text(guild, conn)

        embed.add_field(
            name=f"{conn['name']}  (`{conn['id']}`)",
            value=(
                f"**Status:** {status}\n"
                f"**Connection Role(s):**\n{connection_roles_text}\n"
                f"**Main Role (granted/removed):** {main_role_text}"
            ),
            inline=False,
        )
    return embed


def build_settings_embed(guild: discord.Guild, settings: dict) -> discord.Embed:
    status = "🟢 Enabled" if settings.get("enabled", True) else "🔴 Disabled"
    embed = discord.Embed(
        title="⚙️ Role Connections Settings",
        description=(
            f"**System Status:** {status}\n\n"
            "When disabled, no automatic role syncing will occur anywhere in this "
            "server, even if individual connections are enabled."
        ),
        color=discord.Color.blurple(),
    )
    return embed


def build_edit_embed(guild: discord.Guild, connection: dict) -> discord.Embed:
    main_role = guild.get_role(connection["main_role_id"])
    status = "🟢 Enabled" if connection.get("enabled", True) else "🔴 Disabled"
    connection_roles_text = build_connection_roles_text(guild, connection)

    embed = discord.Embed(
        title=f"✏️ Editing: {connection['name']}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Status", value=status, inline=False)
    embed.add_field(name="Connection Role(s)", value=connection_roles_text, inline=False)
    embed.add_field(
        name="Main Role (granted/removed)",
        value=main_role.mention if main_role else "*(deleted)*",
        inline=False,
    )
    embed.set_footer(text=f"Connection ID: {connection['id']}")
    return embed


def build_manage_connection_roles_embed(guild: discord.Guild, connection: dict) -> discord.Embed:
    connection_roles_text = build_connection_roles_text(guild, connection)
    embed = discord.Embed(
        title=f"🔗 Connection Roles — {connection['name']}",
        description=connection_roles_text,
        color=discord.Color.blurple(),
    )
    return embed


class ConnectionNameModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Create Role Connection")
        self.name_input = discord.ui.TextInput(
            label="Connection Name",
            placeholder="e.g. Staff via Admin",
            max_length=100,
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        view = ConnectionRoleBuilderView(name)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


class RenameConnectionModal(discord.ui.Modal):
    def __init__(self, connection_id: str, current_name: str):
        super().__init__(title="Rename Connection")
        self.connection_id = connection_id
        self.name_input = discord.ui.TextInput(
            label="New Connection Name",
            default=current_name,
            max_length=100,
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.name_input.value.strip()
        await role_connections_storage.update_connection(interaction.guild_id, self.connection_id, {"name": new_name})
        embed = discord.Embed(
            description=f"✅ Connection renamed to **{new_name}**.",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)


class ConnectionRoleBuilderView(discord.ui.View):
    def __init__(self, name: str):
        super().__init__(timeout=600)
        self.name = name
        self.roles: list = []
        self.staged_role: Optional[discord.Role] = None
        self.staged_condition: Optional[str] = None
        self.combine_mode = "and"

    def build_embed(self) -> discord.Embed:
        lines = [f"{r['role'].mention} — {condition_label(r['condition'])}" for r in self.roles]
        added_text = "\n".join(lines) if lines else "*None added yet*"

        staged_role_text = self.staged_role.mention if self.staged_role else "*(not selected)*"
        staged_cond_text = condition_label(self.staged_condition) if self.staged_condition else "*(not selected)*"

        embed = discord.Embed(
            title="Step 2 of 3 — Connection Role(s)",
            description=(
                "These are the role(s) a member must already have (or not have) to trigger this connection.\n\n"
                f"**Currently staged:** {staged_role_text} — {staged_cond_text}\n\n"
                "Pick a role **and** a condition below, then click **Add Role**. Repeat for each "
                "connection role. If you add more than one, choose AND/OR to decide how they combine.\n\n"
                f"**Added so far:**\n{added_text}\n\n"
                f"**Combine mode (only matters with 2+ roles):** {combine_mode_label(self.combine_mode)}"
            ),
            color=discord.Color.blurple(),
        )
        return embed

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Pick a connection role...",
                        min_values=1, max_values=1, row=0)
    async def pick_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await require_admin(interaction):
            return
        self.staged_role = select.values[0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.select(placeholder="Condition...", min_values=1, max_values=1, row=1, options=CONDITION_OPTIONS)
    async def pick_condition(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require_admin(interaction):
            return
        self.staged_condition = select.values[0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Add Role", style=discord.ButtonStyle.primary, emoji="➕", row=2)
    async def add_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        missing = []
        if not self.staged_role:
            missing.append("a **role**")
        if not self.staged_condition:
            missing.append("a **condition**")
        if missing:
            await interaction.response.send_message(
                f"⚠️ Please select {' and '.join(missing)} before clicking Add Role.", ephemeral=True
            )
            return

        for r in self.roles:
            if r["role"].id == self.staged_role.id:
                r["condition"] = self.staged_condition
                break
        else:
            self.roles.append({"role": self.staged_role, "condition": self.staged_condition})

        self.staged_role = None
        self.staged_condition = None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.select(placeholder="Combine mode (AND/OR)...", min_values=1, max_values=1,
                        row=3, options=COMBINE_MODE_OPTIONS)
    async def pick_combine_mode(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require_admin(interaction):
            return
        self.combine_mode = select.values[0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next: Main Role", style=discord.ButtonStyle.success, row=4)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        if not self.roles:
            await interaction.response.send_message(
                "⚠️ Add at least one connection role before continuing.", ephemeral=True
            )
            return
        view = MainRoleSelectView(self.name, self.roles, self.combine_mode)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️", row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description="❌ Connection creation cancelled.", color=discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class MainRoleSelectView(discord.ui.View):
    def __init__(self, name: str, connection_roles: list, combine_mode: str):
        super().__init__(timeout=300)
        self.name = name
        self.connection_roles = connection_roles
        self.combine_mode = combine_mode
        self.main_role: Optional[discord.Role] = None

    def build_embed(self) -> discord.Embed:
        lines = [f"{r['role'].mention} — {condition_label(r['condition'])}" for r in self.connection_roles]
        roles_text = "\n".join(lines)
        combine_text = f"\n**Combine:** {combine_mode_label(self.combine_mode)}" if len(self.connection_roles) > 1 else ""
        main_text = self.main_role.mention if self.main_role else "*(not selected)*"

        embed = discord.Embed(
            title="Step 3 of 3 — Main Role",
            description=(
                f"**Connection Role(s):**\n{roles_text}{combine_text}\n\n"
                f"**Main Role to grant/remove:** {main_text}\n\n"
                "Pick the single role that should be automatically added when the condition "
                "above is met, and removed when it's no longer met."
            ),
            color=discord.Color.blurple(),
        )
        return embed

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select the main role...",
                        min_values=1, max_values=1, row=0)
    async def pick_main_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await require_admin(interaction):
            return
        chosen = select.values[0]
        if any(r["role"].id == chosen.id for r in self.connection_roles):
            await interaction.response.send_message(
                "⚠️ The main role can't be the same as one of the connection roles.", ephemeral=True
            )
            return
        self.main_role = chosen
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Save Connection", style=discord.ButtonStyle.success, emoji="💾", row=1)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        if not self.main_role:
            await interaction.response.send_message("⚠️ Please select a main role first.", ephemeral=True)
            return

        connection_roles = [{"role_id": r["role"].id, "condition": r["condition"]} for r in self.connection_roles]
        connection_id = await role_connections_storage.create_connection(
            guild_id=interaction.guild_id,
            name=self.name,
            main_role_id=self.main_role.id,
            connection_roles=connection_roles,
            combine_mode=self.combine_mode,
        )
        embed = discord.Embed(
            description=f"✅ Connection **{self.name}** (`{connection_id}`) has been created!",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description="❌ Connection creation cancelled.", color=discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ConnectionPickerSelect(discord.ui.Select):
    def __init__(self, connections: list, mode: str):
        self.picker_mode = mode
        options = [
            discord.SelectOption(
                label=c["name"][:100],
                description=f"ID: {c['id']} • {'Enabled' if c.get('enabled', True) else 'Disabled'}",
                value=c["id"],
            )
            for c in connections
        ]
        placeholder = "Choose a connection to edit..." if mode == "edit" else "Choose a connection to delete..."
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        connection_id = self.values[0]
        connection = await role_connections_storage.get_connection(interaction.guild_id, connection_id)
        if not connection:
            await interaction.response.edit_message(
                content="⚠️ That connection no longer exists.", embed=None, view=None
            )
            return

        if self.picker_mode == "edit":
            embed = build_edit_embed(interaction.guild, connection)
            view = EditConnectionView(connection_id)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            embed = discord.Embed(
                title="⚠️ Confirm Deletion",
                description=(
                    f"Are you sure you want to delete the connection **{connection['name']}**?\n"
                    "This action cannot be undone."
                ),
                color=discord.Color.red(),
            )
            view = ConfirmDeleteView(connection_id, connection["name"])
            await interaction.response.edit_message(embed=embed, view=view)


class ConnectionPickerView(discord.ui.View):
    def __init__(self, connections: list, mode: str):
        super().__init__(timeout=180)
        self.add_item(ConnectionPickerSelect(connections, mode))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, connection_id: str, name: str):
        super().__init__(timeout=180)
        self.connection_id = connection_id
        self.name = name

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        deleted = await role_connections_storage.delete_connection(interaction.guild_id, self.connection_id)
        if deleted:
            embed = discord.Embed(
                description=f"🗑️ Connection **{self.name}** has been deleted.",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                description="⚠️ That connection could not be found (it may have already been deleted).",
                color=discord.Color.orange(),
            )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description="❌ Deletion cancelled.", color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ChangeMainRoleView(discord.ui.View):
    def __init__(self, connection_id: str):
        super().__init__(timeout=300)
        self.connection_id = connection_id

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select the new main role...",
                        min_values=1, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await require_admin(interaction):
            return
        new_role = select.values[0]
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if connection and any(c["role_id"] == new_role.id for c in connection.get("connection_roles", [])):
            await interaction.response.send_message(
                "⚠️ The main role can't be the same as one of the connection roles.", ephemeral=True
            )
            return
        await role_connections_storage.update_connection(interaction.guild_id, self.connection_id, {"main_role_id": new_role.id})
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        embed = build_edit_embed(interaction.guild, connection)
        embed.description = f"✅ Main role updated to {new_role.mention}."
        await interaction.response.edit_message(embed=embed, view=EditConnectionView(self.connection_id))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ManageConnectionRolesView(discord.ui.View):
    def __init__(self, connection_id: str):
        super().__init__(timeout=300)
        self.connection_id = connection_id

    @discord.ui.button(label="Add Role", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def add_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        view = AddConnectionRoleView(self.connection_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="Remove Role", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def remove_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if not connection or not connection.get("connection_roles"):
            await interaction.response.send_message("ℹ️ There are no connection roles to remove.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Remove Connection Role(s)",
            description="Select the role(s) to remove from this connection.",
            color=discord.Color.blurple(),
        )
        view = RemoveConnectionRoleView(self.connection_id, interaction.guild, connection["connection_roles"])
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Toggle AND/OR", style=discord.ButtonStyle.primary, emoji="🔁", row=0)
    async def toggle_combine_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if not connection:
            await interaction.response.send_message("⚠️ Connection not found.", ephemeral=True)
            return
        new_mode = "or" if connection.get("combine_mode", "and") == "and" else "and"
        await role_connections_storage.update_connection(interaction.guild_id, self.connection_id, {"combine_mode": new_mode})
        connection["combine_mode"] = new_mode
        embed = build_manage_connection_roles_embed(interaction.guild, connection)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.gray, emoji="⬅️", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        embed = build_edit_embed(interaction.guild, connection)
        await interaction.response.edit_message(embed=embed, view=EditConnectionView(self.connection_id))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class AddConnectionRoleView(discord.ui.View):
    def __init__(self, connection_id: str):
        super().__init__(timeout=300)
        self.connection_id = connection_id
        self.role: Optional[discord.Role] = None
        self.condition: Optional[str] = None

    def build_embed(self) -> discord.Embed:
        role_text = self.role.mention if self.role else "*(not selected)*"
        cond_text = condition_label(self.condition) if self.condition else "*(not selected)*"
        embed = discord.Embed(
            title="Add Connection Role",
            description=(
                f"**Role:** {role_text}\n"
                f"**Condition:** {cond_text}\n\n"
                "Pick a role **and** a condition, then click Confirm."
            ),
            color=discord.Color.blurple(),
        )
        return embed

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Pick a role...",
                        min_values=1, max_values=1, row=0)
    async def pick_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await require_admin(interaction):
            return
        self.role = select.values[0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.select(placeholder="Condition...", min_values=1, max_values=1, row=1, options=CONDITION_OPTIONS)
    async def pick_condition(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require_admin(interaction):
            return
        self.condition = select.values[0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Confirm Add", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        missing = []
        if not self.role:
            missing.append("a **role**")
        if not self.condition:
            missing.append("a **condition**")
        if missing:
            await interaction.response.send_message(
                f"⚠️ Please select {' and '.join(missing)} before confirming.", ephemeral=True
            )
            return
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if not connection:
            await interaction.response.send_message("⚠️ Connection not found.", ephemeral=True)
            return
        if self.role.id == connection["main_role_id"]:
            await interaction.response.send_message(
                "⚠️ This role can't be the same as the main role.", ephemeral=True
            )
            return

        connection_roles = connection.get("connection_roles", [])
        for entry in connection_roles:
            if entry["role_id"] == self.role.id:
                entry["condition"] = self.condition
                break
        else:
            connection_roles.append({"role_id": self.role.id, "condition": self.condition})

        await role_connections_storage.update_connection(interaction.guild_id, self.connection_id,
                                         {"connection_roles": connection_roles})
        connection["connection_roles"] = connection_roles
        embed = build_manage_connection_roles_embed(interaction.guild, connection)
        await interaction.response.edit_message(embed=embed, view=ManageConnectionRolesView(self.connection_id))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        embed = build_manage_connection_roles_embed(interaction.guild, connection)
        await interaction.response.edit_message(embed=embed, view=ManageConnectionRolesView(self.connection_id))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class RemoveConnectionRoleSelect(discord.ui.Select):
    def __init__(self, connection_id: str, options: list):
        self.connection_id = connection_id
        super().__init__(placeholder="Select role(s) to remove...", min_values=1,
                          max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if not connection:
            await interaction.response.edit_message(content="⚠️ Connection not found.", embed=None, view=None)
            return
        remove_ids = {int(v) for v in self.values}
        connection["connection_roles"] = [
            e for e in connection.get("connection_roles", []) if e["role_id"] not in remove_ids
        ]
        await role_connections_storage.update_connection(interaction.guild_id, self.connection_id,
                                         {"connection_roles": connection["connection_roles"]})
        embed = build_manage_connection_roles_embed(interaction.guild, connection)
        await interaction.response.edit_message(embed=embed, view=ManageConnectionRolesView(self.connection_id))


class RemoveConnectionRoleView(discord.ui.View):
    def __init__(self, connection_id: str, guild: discord.Guild, pairs: list):
        super().__init__(timeout=180)
        self.connection_id = connection_id
        options = []
        for p in pairs:
            role = guild.get_role(p["role_id"])
            role_name = role.name if role else f"Unknown Role ({p['role_id']})"
            cond = "Has" if p.get("condition", "has") == "has" else "Doesn't have"
            options.append(discord.SelectOption(label=f"{role_name} — {cond}"[:100], value=str(p["role_id"])))
        self.add_item(RemoveConnectionRoleSelect(connection_id, options))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.gray, emoji="⬅️", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        embed = build_manage_connection_roles_embed(interaction.guild, connection)
        await interaction.response.edit_message(embed=embed, view=ManageConnectionRolesView(self.connection_id))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class EditConnectionView(discord.ui.View):
    def __init__(self, connection_id: str):
        super().__init__(timeout=300)
        self.connection_id = connection_id

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if not connection:
            await interaction.response.send_message("⚠️ Connection not found.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameConnectionModal(self.connection_id, connection["name"]))

    @discord.ui.button(label="Connection Roles", style=discord.ButtonStyle.secondary, emoji="🔗", row=0)
    async def manage_connection_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if not connection:
            await interaction.response.send_message("⚠️ Connection not found.", ephemeral=True)
            return
        embed = build_manage_connection_roles_embed(interaction.guild, connection)
        view = ManageConnectionRolesView(self.connection_id)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Change Main Role", style=discord.ButtonStyle.secondary, emoji="🎯", row=0)
    async def change_main_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        embed = discord.Embed(
            title="Select a new Main Role",
            description="Choose the single role that should be granted/removed by this connection.",
            color=discord.Color.blurple(),
        )
        view = ChangeMainRoleView(self.connection_id)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Toggle Enabled", style=discord.ButtonStyle.primary, emoji="🔁", row=1)
    async def toggle_enabled(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connection = await role_connections_storage.get_connection(interaction.guild_id, self.connection_id)
        if not connection:
            await interaction.response.send_message("⚠️ Connection not found.", ephemeral=True)
            return
        new_state = not connection.get("enabled", True)
        await role_connections_storage.update_connection(interaction.guild_id, self.connection_id, {"enabled": new_state})
        connection["enabled"] = new_state
        embed = build_edit_embed(interaction.guild, connection)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.gray, emoji="⬅️", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_dashboard_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=DashboardView())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Toggle System", style=discord.ButtonStyle.primary, emoji="🔁")
    async def toggle_system(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        settings = await role_connections_storage.get_settings(interaction.guild_id)
        new_state = not settings.get("enabled", True)
        await role_connections_storage.update_settings(interaction.guild_id, {"enabled": new_state})
        settings["enabled"] = new_state
        embed = build_settings_embed(interaction.guild, settings)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.gray, emoji="⬅️")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_dashboard_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=DashboardView())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


def build_cross_server_list_embed(bot_user_guild_pairs: list) -> discord.Embed:
    embed = discord.Embed(
        title="🌐 Cross-Server Role Sync",
        color=discord.Color.blurple(),
    )
    if not bot_user_guild_pairs:
        embed.description = "No cross-server connections have been set up yet."
        return embed
    lines = []
    for link, other_guild_name in bot_user_guild_pairs:
        status = "🟢 Enabled" if link.get("enabled", True) else "🔴 Disabled"
        lines.append(f"**{other_guild_name}** — {status}  (`{link['id']}`)")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Roles with matching names sync automatically between linked servers.")
    return embed


class CrossServerGuildSelect(discord.ui.Select):
    def __init__(self, guilds: list):
        options = [
            discord.SelectOption(label=g.name[:100], value=str(g.id))
            for g in guilds
        ]
        super().__init__(placeholder="Choose a server to connect to...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        target_guild_id = int(self.values[0])
        target_guild = bot.get_guild(target_guild_id)
        if not target_guild:
            await interaction.response.edit_message(content="⚠️ That server is no longer available.", embed=None, view=None)
            return
        embed = discord.Embed(
            title="Confirm Cross-Server Connection",
            description=(
                f"Connect **{interaction.guild.name}** ↔ **{target_guild.name}**?\n\n"
                "Once connected, any role with a matching name in both servers will be "
                "automatically given or removed together — if a member gains or loses "
                "a role in one server, the same-named role syncs in the other."
            ),
            color=discord.Color.gold(),
        )
        view = ConfirmCrossServerView(target_guild_id, target_guild.name)
        await interaction.response.edit_message(embed=embed, view=view)


class CrossServerSelectView(discord.ui.View):
    def __init__(self, guilds: list):
        super().__init__(timeout=180)
        self.add_item(CrossServerGuildSelect(guilds))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ConfirmCrossServerView(discord.ui.View):
    def __init__(self, target_guild_id: int, target_guild_name: str):
        super().__init__(timeout=180)
        self.target_guild_id = target_guild_id
        self.target_guild_name = target_guild_name

    @discord.ui.button(label="Connect", style=discord.ButtonStyle.success, emoji="🌐")
    async def connect(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        existing_links = await role_connections_storage.get_guild_links(interaction.guild_id)
        for link in existing_links:
            other_id = link["guild_b_id"] if link["guild_a_id"] == interaction.guild_id else link["guild_a_id"]
            if other_id == self.target_guild_id:
                embed = discord.Embed(
                    description=f"ℹ️ This server is already connected to **{self.target_guild_name}**.",
                    color=discord.Color.orange(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

        await role_connections_storage.create_guild_link(interaction.guild_id, self.target_guild_id)
        embed = discord.Embed(
            description=(
                f"✅ Connected to **{self.target_guild_name}**! Matching role names will "
                "now sync automatically between both servers."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description="❌ Cross-server connection cancelled.", color=discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class RemoveCrossServerSelect(discord.ui.Select):
    def __init__(self, options: list):
        super().__init__(placeholder="Select a connection to remove...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        link_id = self.values[0]
        deleted = await role_connections_storage.delete_guild_link(link_id)
        if deleted:
            embed = discord.Embed(description="🗑️ Cross-server connection removed.", color=discord.Color.green())
        else:
            embed = discord.Embed(description="⚠️ That connection could not be found.", color=discord.Color.orange())
        await interaction.response.edit_message(embed=embed, view=None)


class RemoveCrossServerView(discord.ui.View):
    def __init__(self, options: list):
        super().__init__(timeout=180)
        self.add_item(RemoveCrossServerSelect(options))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class CrossServerManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Connect a Server", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def connect_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        mutual_guilds = []
        for g in bot.guilds:
            if g.id == interaction.guild_id:
                continue
            member = g.get_member(interaction.user.id)
            if member and member.guild_permissions.administrator:
                mutual_guilds.append(g)
        if not mutual_guilds:
            await interaction.response.send_message(
                "ℹ️ No other mutual servers found where you're also an Administrator and this bot is present.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="Cross-Server Connect",
            description="What server should we connect to?",
            color=discord.Color.blurple(),
        )
        view = CrossServerSelectView(mutual_guilds)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Remove a Connection", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def remove_connection(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        links = await role_connections_storage.get_guild_links(interaction.guild_id)
        if not links:
            await interaction.response.send_message("ℹ️ No cross-server connections to remove.", ephemeral=True)
            return
        options = []
        for link in links:
            other_id = link["guild_b_id"] if link["guild_a_id"] == interaction.guild_id else link["guild_a_id"]
            other_guild = bot.get_guild(other_id)
            other_name = other_guild.name if other_guild else f"Unknown Server ({other_id})"
            options.append(discord.SelectOption(label=other_name[:100], value=link["id"]))
        embed = discord.Embed(
            title="Remove a Cross-Server Connection",
            description="Select which connection to remove.",
            color=discord.Color.red(),
        )
        view = RemoveCrossServerView(options)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.gray, emoji="⬅️", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_dashboard_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=DashboardView())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Connection", style=discord.ButtonStyle.success,
                        emoji="➕", custom_id="rc_dashboard_create")
    async def create_connection(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        await interaction.response.send_modal(ConnectionNameModal())

    @discord.ui.button(label="Edit Connection", style=discord.ButtonStyle.primary,
                        emoji="✏️", custom_id="rc_dashboard_edit")
    async def edit_connection(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connections = await role_connections_storage.get_connections(interaction.guild_id)
        if not connections:
            await interaction.response.send_message(
                "ℹ️ There are no role connections to edit yet. Create one first!", ephemeral=True
            )
            return
        view = ConnectionPickerView(connections, mode="edit")
        await interaction.response.send_message(
            "Select a connection to edit:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Delete Connection", style=discord.ButtonStyle.danger,
                        emoji="🗑️", custom_id="rc_dashboard_delete")
    async def delete_connection(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connections = await role_connections_storage.get_connections(interaction.guild_id)
        if not connections:
            await interaction.response.send_message(
                "ℹ️ There are no role connections to delete yet.", ephemeral=True
            )
            return
        view = ConnectionPickerView(connections, mode="delete")
        await interaction.response.send_message(
            "Select a connection to delete:", view=view, ephemeral=True
        )

    @discord.ui.button(label="View Connections", style=discord.ButtonStyle.secondary,
                        emoji="📋", custom_id="rc_dashboard_view")
    async def view_connections(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        connections = await role_connections_storage.get_connections(interaction.guild_id)
        embed = build_connections_list_embed(interaction.guild, connections)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary,
                        emoji="⚙️", custom_id="rc_dashboard_settings")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        settings = await role_connections_storage.get_settings(interaction.guild_id)
        embed = build_settings_embed(interaction.guild, settings)
        await interaction.response.send_message(embed=embed, view=SettingsView(), ephemeral=True)

    @discord.ui.button(label="Cross-Server", style=discord.ButtonStyle.secondary,
                        emoji="🌐", custom_id="rc_dashboard_cross_server", row=1)
    async def cross_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction):
            return
        links = await role_connections_storage.get_guild_links(interaction.guild_id)
        pairs = []
        for link in links:
            other_id = link["guild_b_id"] if link["guild_a_id"] == interaction.guild_id else link["guild_a_id"]
            other_guild = bot.get_guild(other_id)
            other_name = other_guild.name if other_guild else f"Unknown Server ({other_id})"
            pairs.append((link, other_name))
        embed = build_cross_server_list_embed(pairs)
        await interaction.response.send_message(embed=embed, view=CrossServerManageView(), ephemeral=True)




# ---------------------------------------------------------------------------
# Domain logic, embeds, and interactive views — Ticket subsystem
# ---------------------------------------------------------------------------

async def next_ticket_number(guild_id: int) -> int:
    current = await counters_store.get(guild_id, 0)
    current += 1
    await counters_store.set(guild_id, current)
    return current


async def get_ticket_record(channel_id: int):
    return await tickets_store.get(channel_id)


async def open_ticket_for(interaction: discord.Interaction, panel: dict, option: dict):
    # Caller (button/select callback) must already have deferred before
    # reaching this point — don't defer again here.
    guild = interaction.guild
    user = interaction.user

    try:
        existing_key, existing = await tickets_store.find_one(
            lambda k, v: v.get("guild_id") == guild.id
            and v.get("opener_id") == user.id
            and v.get("status") == "open"
        )
        if existing:
            channel = guild.get_channel(int(existing_key))
            if channel:
                await interaction.followup.send(
                    f"You already have an open ticket: {channel.mention}", ephemeral=True
                )
                return

        category_id = option.get("category_id") or panel.get("category_id")
        category = guild.get_channel(category_id) if category_id else None
        role_ids = option.get("role_ids") or panel.get("role_ids") or ([option.get("role_id") or panel.get("role_id")] if (option.get("role_id") or panel.get("role_id")) else [])
        roles = [guild.get_role(int(value)) for value in role_ids if str(value or "").isdigit()]
        roles = [role for role in roles if role is not None]
        role = roles[0] if roles else None

        number = await next_ticket_number(guild.id)
        option_label = option.get("label", "General")
        channel_name = f"{_slugify(option_label)}-{number:04d}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for support_role in roles:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                reason=f"Ticket opened by {user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create the ticket channel. "
                "I need **Manage Channels** and **Manage Roles**, and my role must be "
                "positioned above the support role in Server Settings → Roles.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"Couldn't create the ticket channel: {e}", ephemeral=True)
            return

        custom_message = option.get("ticket_message") or panel.get("ticket_message")
        if custom_message:
            body = custom_message.replace("{user}", user.mention).replace("{type}", option_label)
        else:
            body = "Thank you for contacting support.\nPlease describe your issue and wait for a response."

        embed = discord.Embed(
            title="🎫 New Ticket",
            description=body,
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=f"{guild.name} • Ticket #{number:04d}",
            icon_url=guild.icon.url if guild.icon else None,
        )

        msg = await channel.send(
            content=" ".join(support_role.mention for support_role in roles) if roles else None,
            embed=embed,
            view=TicketControlView(),
        )

        await tickets_store.set(channel.id, {
            "guild_id": guild.id,
            "opener_id": user.id,
            "claimed_by": None,
            "status": "open",
            "panel_message_id": panel.get("message_id"),
            "option_label": option_label,
            "role_id": role.id if role else None,
            "role_ids": [support_role.id for support_role in roles],
            "message_id": msg.id,
            "number": number,
        })

        await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)

    except Exception as e:
        import traceback
        print("=== ERROR IN open_ticket_for ===")
        traceback.print_exc()
        try:
            await interaction.followup.send(
                f"Something went wrong creating your ticket: `{type(e).__name__}: {e}`\n"
                "This has been logged — please tell an admin.",
                ephemeral=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Persistent views (survive bot restarts because custom_id is fixed)
# ---------------------------------------------------------------------------

class LoggingView(discord.ui.View):
    """Base view that logs any unhandled exception from a button/select
    callback instead of letting it fail silently."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        import traceback
        print(f"=== VIEW ERROR ({item}) ===")
        traceback.print_exc()
        message = f"Something went wrong: `{type(error).__name__}: {error}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass


class TicketOpenView(LoggingView):
    def __init__(self, label: str = "Open Ticket", emoji: str | None = None):
        super().__init__(timeout=None)
        self.children[0].label = label
        if emoji:
            self.children[0].emoji = emoji

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.blurple, custom_id="persistent:ticket_open_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        panel = await panels_store.get(interaction.message.id)
        if not panel:
            await interaction.followup.send("This panel is no longer configured.", ephemeral=True)
            return
        await open_ticket_for(interaction, panel, panel["options"][0])


class TicketSelectView(LoggingView):
    def __init__(self, options: list[dict] | None = None):
        super().__init__(timeout=None)
        if options:
            self.children[0].options = [
                discord.SelectOption(
                    label=opt["label"],
                    description=opt.get("description") or None,
                    emoji=opt.get("emoji") or None,
                    value=str(i),
                )
                for i, opt in enumerate(options)
            ]

    @discord.ui.select(
        placeholder="Select a ticket type...",
        custom_id="persistent:ticket_select_menu",
        options=[discord.SelectOption(label="placeholder")],
    )
    async def select_ticket_type(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True, thinking=True)
        panel = await panels_store.get(interaction.message.id)
        if not panel:
            await interaction.followup.send("This panel is no longer configured.", ephemeral=True)
            return
        index = int(select.values[0])
        option = panel["options"][index]
        await open_ticket_for(interaction, panel, option)


class TicketControlView(LoggingView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🙋 Claim", style=discord.ButtonStyle.green, custom_id="persistent:ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_claim(interaction)

    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.red, custom_id="persistent:ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_close(interaction)


class TicketClaimedView(LoggingView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔓 Unclaim", style=discord.ButtonStyle.gray, custom_id="persistent:ticket_unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_unclaim(interaction)

    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.red, custom_id="persistent:ticket_close2")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_close(interaction)


class TicketClosedView(LoggingView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔓 Reopen", style=discord.ButtonStyle.green, custom_id="persistent:ticket_reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_reopen(interaction)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.red, custom_id="persistent:ticket_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only admins can delete tickets.", ephemeral=True)
            return
        await interaction.response.send_message("Deleting channel in 3 seconds...")
        await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        await tickets_store.delete(interaction.channel.id)


# ---------------------------------------------------------------------------
# Claim / close / reopen logic (shared by buttons and slash commands)
# ---------------------------------------------------------------------------

async def handle_claim(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
        return
    if record["status"] != "open":
        await interaction.followup.send("This ticket is closed.", ephemeral=True)
        return
    if not _is_support(interaction.user, record.get("role_id")):
        await interaction.followup.send("Only support staff can claim tickets.", ephemeral=True)
        return
    if record.get("claimed_by"):
        claimer = interaction.guild.get_member(record["claimed_by"])
        await interaction.followup.send(
            f"Already claimed by {claimer.mention if claimer else 'someone'}.", ephemeral=True
        )
        return

    record["claimed_by"] = interaction.user.id
    await tickets_store.set(interaction.channel.id, record)

    role = interaction.guild.get_role(record.get("role_id")) if record.get("role_id") else None
    if role:
        await interaction.channel.set_permissions(role, view_channel=True, send_messages=False, read_message_history=True)
    await interaction.channel.set_permissions(interaction.user, view_channel=True, send_messages=True, read_message_history=True)

    embed = discord.Embed(description=f"🙋 Claimed by {interaction.user.mention}", color=discord.Color.green())
    await interaction.channel.send(embed=embed, view=TicketClaimedView())
    await interaction.followup.send("Ticket claimed.", ephemeral=True)


async def handle_unclaim(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
        return
    if record.get("claimed_by") != interaction.user.id and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Only the claimer or an admin can unclaim this.", ephemeral=True)
        return

    record["claimed_by"] = None
    await tickets_store.set(interaction.channel.id, record)

    role = interaction.guild.get_role(record.get("role_id")) if record.get("role_id") else None
    if role:
        await interaction.channel.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True)

    embed = discord.Embed(description="🔓 Ticket unclaimed and reopened to the support team.", color=discord.Color.greyple())
    await interaction.channel.send(embed=embed, view=TicketControlView())
    await interaction.followup.send("Ticket unclaimed.", ephemeral=True)


async def handle_close(interaction: discord.Interaction, skip_defer: bool = False):
    if not skip_defer:
        await interaction.response.defer(ephemeral=True, thinking=True)
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
        return
    if record["status"] == "closed":
        await interaction.followup.send("This ticket is already closed.", ephemeral=True)
        return

    record["status"] = "closed"
    await tickets_store.set(interaction.channel.id, record)

    opener = interaction.guild.get_member(record["opener_id"])
    if opener:
        await interaction.channel.set_permissions(opener, view_channel=True, send_messages=False, read_message_history=True)

    number = record.get("number", 0)
    try:
        await interaction.channel.edit(name=f"closed-{number:04d}")
    except discord.HTTPException:
        pass

    embed = discord.Embed(
        description=f"🔒 Ticket closed by {interaction.user.mention}. The channel is now locked and archived.",
        color=discord.Color.red(),
    )
    await interaction.channel.send(embed=embed, view=TicketClosedView())
    await interaction.followup.send("Ticket closed.", ephemeral=True)


async def handle_reopen(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
        return

    record["status"] = "open"
    await tickets_store.set(interaction.channel.id, record)

    opener = interaction.guild.get_member(record["opener_id"])
    if opener:
        await interaction.channel.set_permissions(opener, view_channel=True, send_messages=True, read_message_history=True)

    number = record.get("number", 0)
    label = _slugify(record.get("option_label", "ticket"))
    try:
        await interaction.channel.edit(name=f"{label}-{number:04d}")
    except discord.HTTPException:
        pass

    embed = discord.Embed(description=f"🔓 Ticket reopened by {interaction.user.mention}.", color=discord.Color.green())
    await interaction.channel.send(embed=embed, view=TicketControlView())
    await interaction.followup.send("Ticket reopened.", ephemeral=True)


async def handle_transfer(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer(ephemeral=True, thinking=True)
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
        return
    if record["status"] != "open":
        await interaction.followup.send("This ticket is closed.", ephemeral=True)
        return
    if not _is_support(interaction.user, record.get("role_id")):
        await interaction.followup.send("Only support staff can transfer tickets.", ephemeral=True)
        return
    if not _is_support(target, record.get("role_id")):
        await interaction.followup.send(
            f"{target.mention} isn't part of the support role for this ticket, so they can't be assigned it.",
            ephemeral=True,
        )
        return
    if record.get("claimed_by") == target.id:
        await interaction.followup.send(f"{target.mention} already has this ticket.", ephemeral=True)
        return

    old_claimer_id = record.get("claimed_by")
    record["claimed_by"] = target.id
    await tickets_store.set(interaction.channel.id, record)

    if old_claimer_id and old_claimer_id != target.id:
        old_member = interaction.guild.get_member(old_claimer_id)
        if old_member:
            try:
                await interaction.channel.set_permissions(old_member, overwrite=None)
            except discord.HTTPException:
                pass

    role = interaction.guild.get_role(record.get("role_id")) if record.get("role_id") else None
    if role:
        await interaction.channel.set_permissions(role, view_channel=True, send_messages=False, read_message_history=True)
    await interaction.channel.set_permissions(target, view_channel=True, send_messages=True, read_message_history=True)

    embed = discord.Embed(
        description=f"🔁 Ticket transferred to {target.mention} by {interaction.user.mention}.",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(content=target.mention, embed=embed, view=TicketClaimedView())
    await interaction.followup.send(f"Transferred to {target.mention}.", ephemeral=True)


async def handle_add_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer(ephemeral=True, thinking=True)
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
        return
    if not _is_support(interaction.user, record.get("role_id")):
        await interaction.followup.send("Only support staff can add people to a ticket.", ephemeral=True)
        return

    # Target can be ANY server member, not just support staff — e.g. a
    # witness, another department, or a colleague of the ticket opener.
    try:
        await interaction.channel.set_permissions(
            target, view_channel=True, send_messages=True, read_message_history=True
        )
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to edit this channel's permissions.", ephemeral=True)
        return

    embed = discord.Embed(
        description=f"➕ {target.mention} was added to this ticket by {interaction.user.mention}.",
        color=discord.Color.green(),
    )
    await interaction.channel.send(embed=embed)
    await interaction.followup.send(f"Added {target.mention} to the ticket.", ephemeral=True)


async def handle_remove_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer(ephemeral=True, thinking=True)
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
        return
    if not _is_support(interaction.user, record.get("role_id")):
        await interaction.followup.send("Only support staff can remove people from a ticket.", ephemeral=True)
        return
    if target.id == record.get("opener_id"):
        await interaction.followup.send(
            "You can't remove the person who opened the ticket — use /close instead.", ephemeral=True
        )
        return

    try:
        await interaction.channel.set_permissions(target, overwrite=None)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to edit this channel's permissions.", ephemeral=True)
        return

    embed = discord.Embed(
        description=f"➖ {target.mention} was removed from this ticket by {interaction.user.mention}.",
        color=discord.Color.red(),
    )
    await interaction.channel.send(embed=embed)
    await interaction.followup.send(f"Removed {target.mention} from the ticket.", ephemeral=True)


class RequestCloseView(LoggingView):
    """Stateless persistent view — looks the ticket up by channel each time
    so it keeps working after a bot restart."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        record = await get_ticket_record(interaction.channel.id)
        if not record:
            await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
            return False
        if interaction.user.id != record.get("opener_id") and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only the person who opened this ticket can respond to this request.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Yes, close it", style=discord.ButtonStyle.red, custom_id="persistent:requestclose_yes")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"✅ {interaction.user.mention} confirmed — closing the ticket...", embed=None, view=None
        )
        await handle_close(interaction, skip_defer=True)

    @discord.ui.button(label="❌ No, keep open", style=discord.ButtonStyle.gray, custom_id="persistent:requestclose_no")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"❌ {interaction.user.mention} chose to keep this ticket open.", embed=None, view=None
        )


async def handle_request_close(interaction: discord.Interaction):
    record = await get_ticket_record(interaction.channel.id)
    if not record:
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    if record["status"] != "open":
        await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        return
    if not _is_support(interaction.user, record.get("role_id")):
        await interaction.response.send_message("Only support staff can request a close.", ephemeral=True)
        return

    opener = interaction.guild.get_member(record["opener_id"])
    embed = discord.Embed(
        title="Close this ticket?",
        description=(
            f"{interaction.user.mention} has requested to close this ticket.\n\n"
            "Do you want to close it now?"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(
        content=opener.mention if opener else None,
        embed=embed,
        view=RequestCloseView(),
    )


# ---------------------------------------------------------------------------
# /setup — panel builder UI
# ---------------------------------------------------------------------------

class SetupChoiceView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=180)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup session isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔘 Single Panel", style=discord.ButtonStyle.blurple)
    async def single(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SinglePanelModal())

    @discord.ui.button(label="🔳 Multi-Panel (Dropdown)", style=discord.ButtonStyle.blurple)
    async def multi(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MultiPanelModal())


class SinglePanelModal(discord.ui.Modal, title="Create Single Panel"):
    panel_title = discord.ui.TextInput(label="Panel Title", placeholder="Support Tickets", max_length=256)
    description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph,
        placeholder="Click below to open a ticket", max_length=1000,
    )
    button_label = discord.ui.TextInput(label="Button Label", default="Open Ticket", max_length=80)
    button_emoji = discord.ui.TextInput(label="Button Emoji (optional)", required=False, max_length=10)
    ticket_message = discord.ui.TextInput(
        label="Ticket Welcome Message (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Use {user} and {type} as placeholders. Leave blank for the default message.",
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        builder = {
            "type": "single",
            "title": str(self.panel_title),
            "description": str(self.description),
            "ticket_message": str(self.ticket_message) if self.ticket_message.value else None,
            "options": [{
                "label": str(self.button_label),
                "emoji": str(self.button_emoji) if self.button_emoji.value else None,
            }],
        }
        view = DestinationView(builder)
        await interaction.response.send_message(
            "Choose where to post the panel and which category/role tickets should use:",
            view=view, ephemeral=True,
        )


class MultiPanelModal(discord.ui.Modal, title="Create Multi-Panel"):
    panel_title = discord.ui.TextInput(label="Panel Title", placeholder="Need Help?", max_length=256)
    description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph,
        placeholder="Select a category below to open a ticket", max_length=1000,
    )
    ticket_message = discord.ui.TextInput(
        label="Default Ticket Welcome Message",
        style=discord.TextStyle.paragraph,
        placeholder="Use {user} and {type} as placeholders. Leave blank for the default message. Each ticket type can override this.",
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        builder = {
            "type": "multi",
            "title": str(self.panel_title),
            "description": str(self.description),
            "ticket_message": str(self.ticket_message) if self.ticket_message.value else None,
            "options": [],
        }
        active_builders[interaction.user.id] = builder
        await interaction.response.send_message(
            embed=build_manage_embed(builder),
            view=ManageMultiPanelView(interaction.user.id),
            ephemeral=True,
        )


def build_manage_embed(builder: dict) -> discord.Embed:
    embed = discord.Embed(title=f"Building: {builder['title']}", color=discord.Color.blurple())
    embed.description = builder["description"]
    if not builder["options"]:
        embed.add_field(name="Ticket Types", value="_None yet — click Add Option_", inline=False)
    else:
        lines = [f"{i+1}. **{o['label']}**" + (f" — {o['description']}" if o.get("description") else "") for i, o in enumerate(builder["options"])]
        embed.add_field(name="Ticket Types", value="\n".join(lines), inline=False)
    dest = "Not set" if "channel_id" not in builder else f"<#{builder['channel_id']}>"
    embed.add_field(name="Post to channel", value=dest, inline=True)
    return embed


class ManageMultiPanelView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=600)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup session isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="➕ Add Option", style=discord.ButtonStyle.blurple)
    async def add_option(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddOptionModal(self.author_id))

    @discord.ui.button(label="📍 Set Destination", style=discord.ButtonStyle.gray)
    async def set_destination(self, interaction: discord.Interaction, button: discord.ui.Button):
        builder = active_builders.get(self.author_id)
        if builder is None:
            await interaction.response.send_message("Session expired, please run /setup again.", ephemeral=True)
            return
        view = MultiDestinationPickView(self.author_id)
        await interaction.response.send_message(
            "Choose the channel to post in and the category tickets should be created under:",
            view=view, ephemeral=True,
        )

    @discord.ui.button(label="✅ Save & Post", style=discord.ButtonStyle.green)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        builder = active_builders.get(self.author_id)
        if not builder or not builder["options"]:
            await interaction.response.send_message("Add at least one ticket type first.", ephemeral=True)
            return
        if "channel_id" not in builder:
            await interaction.response.send_message("Set a destination channel first.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(builder["channel_id"])
        embed = discord.Embed(title=builder["title"], description=builder["description"], color=discord.Color.blurple())
        embed.set_footer(
            text=interaction.guild.name,
            icon_url=interaction.guild.icon.url if interaction.guild.icon else None,
        )
        msg = await channel.send(embed=embed, view=TicketSelectView(builder["options"]))

        builder["message_id"] = msg.id
        await panels_store.set(msg.id, builder)
        active_builders.pop(self.author_id, None)

        await interaction.response.edit_message(content=f"✅ Panel posted in {channel.mention}!", embed=None, view=None)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        active_builders.pop(self.author_id, None)
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class AddOptionModal(discord.ui.Modal, title="Add Ticket Type"):
    def __init__(self, author_id: int):
        super().__init__()
        self.author_id = author_id

    label = discord.ui.TextInput(label="Option Label", placeholder="Billing Issue", max_length=80)
    description = discord.ui.TextInput(label="Description (optional)", required=False, max_length=100)
    emoji = discord.ui.TextInput(label="Emoji (optional)", required=False, max_length=10)
    ticket_message = discord.ui.TextInput(
        label="Custom Welcome Message (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Overrides the panel default for this ticket type. {user} and {type} work here too.",
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        builder = active_builders.get(self.author_id)
        if builder is None:
            await interaction.response.send_message("Session expired, please run /setup again.", ephemeral=True)
            return

        option = {
            "label": str(self.label),
            "description": str(self.description) if self.description.value else None,
            "emoji": str(self.emoji) if self.emoji.value else None,
            "ticket_message": str(self.ticket_message) if self.ticket_message.value else None,
            "category_id": None,
            "role_id": None,
        }

        view = OptionRoleView(self.author_id, option)
        await interaction.response.send_message(
            f"Optionally pick a support role for **{option['label']}** (or skip to use the panel default):",
            view=view, ephemeral=True,
        )


class OptionRoleView(discord.ui.View):
    def __init__(self, author_id: int, option: dict):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.option = option

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Support role for this ticket type")
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.option["role_id"] = select.values[0].id
        await self._finish(interaction)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.gray)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)

    async def _finish(self, interaction: discord.Interaction):
        builder = active_builders.get(self.author_id)
        if builder is None:
            await interaction.response.edit_message(content="Session expired, please run /setup again.", view=None)
            return
        builder["options"].append(self.option)
        await interaction.response.edit_message(
            content=f"Added **{self.option['label']}**. Return to your setup message to add more or save.",
            view=None,
        )


class DestinationView(discord.ui.View):
    def __init__(self, builder: dict):
        super().__init__(timeout=300)
        self.builder = builder
        self.channel_id = None
        self.category_id = None
        self.role_id = None

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Channel to post the panel in", row=0)
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.channel_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.category], placeholder="Category for ticket channels", row=1)
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.category_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Support role", row=2)
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.role_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.button(label="✅ Confirm & Post", style=discord.ButtonStyle.green, row=3)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (self.channel_id and self.category_id and self.role_id):
            await interaction.response.send_message("Please select a channel, category, and role first.", ephemeral=True)
            return

        self.builder["channel_id"] = self.channel_id
        self.builder["category_id"] = self.category_id
        self.builder["role_id"] = self.role_id

        channel = interaction.guild.get_channel(self.channel_id)
        opt = self.builder["options"][0]
        embed = discord.Embed(title=self.builder["title"], description=self.builder["description"], color=discord.Color.blurple())
        embed.set_footer(
            text=interaction.guild.name,
            icon_url=interaction.guild.icon.url if interaction.guild.icon else None,
        )
        msg = await channel.send(embed=embed, view=TicketOpenView(opt["label"], opt.get("emoji")))

        self.builder["message_id"] = msg.id
        await panels_store.set(msg.id, self.builder)

        await interaction.response.edit_message(content=f"✅ Panel posted in {channel.mention}!", view=None)


class MultiDestinationPickView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.channel_id = None
        self.category_id = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Channel to post the panel in", row=0)
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.channel_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.category], placeholder="Default category for ticket channels", row=1)
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.category_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.button(label="✅ Set", style=discord.ButtonStyle.green, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (self.channel_id and self.category_id):
            await interaction.response.send_message("Please select both a channel and a category.", ephemeral=True)
            return
        builder = active_builders.get(self.author_id)
        if builder is None:
            await interaction.response.edit_message(content="Session expired, please run /setup again.", view=None)
            return
        builder["channel_id"] = self.channel_id
        builder["category_id"] = self.category_id
        await interaction.response.edit_message(content="Destination set. Return to your setup message to save & post.", view=None)




# ---------------------------------------------------------------------------
# Domain logic and interactive views — Voice (Cedar Cove) subsystem
# ---------------------------------------------------------------------------


class LobbyChannelSelect(discord.ui.ChannelSelect):

    def __init__(self):
        super().__init__(
            placeholder="Select the lobby voice channel...",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            row=0
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):
        self.view.lobby_channel = self.values[0].id
        print(f"Selected lobby: {self.values[0].id}")

        await interaction.response.defer()


class DestinationChannelSelect(discord.ui.ChannelSelect):

    def __init__(self):
        super().__init__(
            placeholder="Select destination voice channels...",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=25,
            row=1
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):
        self.view.destination_channels = [
            channel.id for channel in self.values
        ]
        print(f"Selected destinations: {[c.id for c in self.values]}")

        await interaction.response.defer()


class LobbySetupView(discord.ui.View):

    def __init__(self):
        super().__init__(
            timeout=None
        )

        self.lobby_channel = None
        self.destination_channels = []

        self.add_item(
            LobbyChannelSelect()
        )

        self.add_item(
            DestinationChannelSelect()
        )

    @discord.ui.button(
        label="Save",
        style=discord.ButtonStyle.green,
        emoji="✅",
        row=2
    )
    async def save(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:
            print("Save button clicked!")
            print(f"Lobby: {self.lobby_channel}")
            print(f"Destinations: {self.destination_channels}")

            if self.lobby_channel is None:
                print("No lobby channel selected")
                await interaction.response.send_message(
                    "Please select a lobby voice channel.",
                    ephemeral=True
                )
                return

            if len(self.destination_channels) == 0:
                print("No destination channels selected")
                await interaction.response.send_message(
                    "Please select destination channels.",
                    ephemeral=True
                )
                return

            # Acknowledge immediately, before any I/O, so we never
            # miss Discord's 3-second interaction response window.
            await interaction.response.defer()

            print("Saving to MongoDB...")

            await vc_guild_configs.update_one(
                {
                    "_id": interaction.guild.id
                },
                {
                    "$set": {
                        "lobby_channel": self.lobby_channel,
                        "destination_channels": self.destination_channels
                    }
                },
                upsert=True
            )

            print("Saved successfully!")

            embed = discord.Embed(
                title="Lobby Saved",
                description="Your lobby system has been configured.",
                color=discord.Color.green()
            )

            await interaction.edit_original_response(
                embed=embed,
                view=None
            )

        except Exception as e:
            print(f"SAVE ERROR: {e}")
            import traceback
            traceback.print_exc()

            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Error: {e}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Error: {e}",
                    ephemeral=True
                )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.red,
        emoji="❌",
        row=2
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.edit_message(
            content="Lobby setup cancelled.",
            embed=None,
            view=None
        )


async def get_temp_channel(member: discord.Member):
    if not member.voice or not member.voice.channel:
        return None, None

    channel = member.voice.channel

    doc = await temp_channels.find_one(
        {
            "channel_id": channel.id
        }
    )

    return channel, doc


async def claim_channel(channel: discord.VoiceChannel, member: discord.Member):
    await temp_channels.update_one(
        {
            "channel_id": channel.id
        },
        {
            "$set": {
                "owner": member.id
            }
        }
    )

    try:
        await channel.set_permissions(
            member,
            connect=True,
            manage_channels=True,
            move_members=True,
            reason="New channel owner"
        )
    except discord.HTTPException:
        pass


class RenameModal(discord.ui.Modal, title="Rename Channel"):

    name = discord.ui.TextInput(
        label="New channel name",
        max_length=100
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.channel.edit(
                name=str(self.name),
                reason="Renamed via control panel"
            )
            await interaction.response.send_message(
                f"📛 Channel renamed to **{self.name}**.",
                ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Couldn't rename the channel: {e}",
                ephemeral=True
            )


class LimitModal(discord.ui.Modal, title="Set User Limit"):

    limit = discord.ui.TextInput(
        label="User limit (0 = unlimited, max 99)",
        max_length=2
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(str(self.limit))
        except ValueError:
            await interaction.response.send_message(
                "Please enter a whole number.",
                ephemeral=True
            )
            return

        value = max(0, min(value, 99))

        try:
            await self.channel.edit(
                user_limit=value,
                reason="Limit changed via control panel"
            )
            await interaction.response.send_message(
                f"🔢 User limit set to {'unlimited' if value == 0 else value}.",
                ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Couldn't set the limit: {e}",
                ephemeral=True
            )


class UserActionSelect(discord.ui.UserSelect):

    def __init__(self, channel: discord.VoiceChannel, action: str):
        super().__init__(
            placeholder=f"Select a member to {action}...",
            min_values=1,
            max_values=1
        )
        self.channel = channel
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        channel = self.channel
        action = self.action
        guild = interaction.guild

        if action == "trust":
            await temp_channels.update_one(
                {"channel_id": channel.id},
                {"$addToSet": {"trusted": target.id}}
            )
            await channel.set_permissions(
                target, connect=True, speak=True,
                reason="Trusted via control panel"
            )
            msg = f"🤝 {target.mention} is now trusted."

        elif action == "untrust":
            await temp_channels.update_one(
                {"channel_id": channel.id},
                {"$pull": {"trusted": target.id}}
            )
            await channel.set_permissions(
                target, overwrite=None,
                reason="Untrusted via control panel"
            )
            msg = f"🚫 {target.mention} is no longer trusted."

        elif action == "kick":
            member = guild.get_member(target.id)
            if member and member.voice and member.voice.channel and member.voice.channel.id == channel.id:
                await member.move_to(None, reason="Kicked via control panel")
                msg = f"👢 {target.mention} was removed from the channel."
            else:
                msg = f"{target.mention} isn't in the channel."

        elif action == "block":
            await temp_channels.update_one(
                {"channel_id": channel.id},
                {"$addToSet": {"blocked": target.id}}
            )
            await channel.set_permissions(
                target, connect=False,
                reason="Blocked via control panel"
            )
            member = guild.get_member(target.id)
            if member and member.voice and member.voice.channel and member.voice.channel.id == channel.id:
                await member.move_to(None, reason="Blocked via control panel")
            msg = f"⛔ {target.mention} has been blocked."

        elif action == "unblock":
            await temp_channels.update_one(
                {"channel_id": channel.id},
                {"$pull": {"blocked": target.id}}
            )
            await channel.set_permissions(
                target, overwrite=None,
                reason="Unblocked via control panel"
            )
            msg = f"✅ {target.mention} has been unblocked."

        elif action == "transfer":
            member = guild.get_member(target.id)
            if not member or not member.voice or not member.voice.channel or member.voice.channel.id != channel.id:
                await interaction.response.edit_message(
                    content=f"{target.mention} needs to be in the channel to receive ownership.",
                    view=None
                )
                return
            await claim_channel(channel, member)
            msg = f"🔁 Ownership transferred to {target.mention}."

        else:
            msg = "Unknown action."

        await interaction.response.edit_message(content=msg, view=None)


class UserActionView(discord.ui.View):

    def __init__(self, channel: discord.VoiceChannel, action: str):
        super().__init__(timeout=60)
        self.add_item(UserActionSelect(channel, action))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item
    ):
        print(f"USER ACTION ERROR on {item}: {error}")
        import traceback
        traceback.print_exc()

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Something went wrong: {error}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Something went wrong: {error}",
                    ephemeral=True
                )
        except discord.HTTPException:
            pass


class RegionSelect(discord.ui.Select):

    def __init__(self, channel: discord.VoiceChannel):
        options = [
            discord.SelectOption(label="Automatic", value="auto"),
            discord.SelectOption(label="US East", value="us-east"),
            discord.SelectOption(label="US West", value="us-west"),
            discord.SelectOption(label="US Central", value="us-central"),
            discord.SelectOption(label="Europe", value="rotterdam"),
            discord.SelectOption(label="Singapore", value="singapore"),
            discord.SelectOption(label="Japan", value="japan"),
            discord.SelectOption(label="Sydney", value="sydney"),
            discord.SelectOption(label="India", value="india"),
            discord.SelectOption(label="Brazil", value="brazil"),
        ]
        super().__init__(
            placeholder="Choose a region...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        region = None if value == "auto" else value

        try:
            await self.channel.edit(
                rtc_region=region,
                reason="Region changed via control panel"
            )
            label = next(
                (o.label for o in self.options if o.value == value),
                value
            )
            await interaction.response.edit_message(
                content=f"🌍 Region set to **{label}**.",
                view=None
            )
        except discord.HTTPException as e:
            await interaction.response.edit_message(
                content=f"Couldn't change the region: {e}",
                view=None
            )


class RegionSelectView(discord.ui.View):

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(timeout=60)
        self.add_item(RegionSelect(channel))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item
    ):
        print(f"REGION SELECT ERROR on {item}: {error}")
        import traceback
        traceback.print_exc()

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Something went wrong: {error}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Something went wrong: {error}",
                    ephemeral=True
                )
        except discord.HTTPException:
            pass


class VoicePanelView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item
    ):
        print(f"VOICE PANEL ERROR on {item}: {error}")
        import traceback
        traceback.print_exc()

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Something went wrong: {error}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Something went wrong: {error}",
                    ephemeral=True
                )
        except discord.HTTPException:
            pass

    async def _require_temp_channel(self, interaction: discord.Interaction):
        channel, doc = await get_temp_channel(interaction.user)

        if not channel or not doc:
            await interaction.response.send_message(
                "You need to be in a temporary voice channel to use this.",
                ephemeral=True
            )
            return None, None

        return channel, doc

    async def _require_owner(self, interaction: discord.Interaction):
        channel, doc = await self._require_temp_channel(interaction)

        if not channel:
            return None, None

        if doc.get("owner") != interaction.user.id:
            await interaction.response.send_message(
                "Only the channel owner can do that.",
                ephemeral=True
            )
            return None, None

        return channel, doc

    @discord.ui.button(label="Name", emoji="📛", style=discord.ButtonStyle.secondary, custom_id="vcpanel:name", row=0)
    async def btn_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_modal(RenameModal(channel))

    @discord.ui.button(label="Limit", emoji="🔢", style=discord.ButtonStyle.secondary, custom_id="vcpanel:limit", row=0)
    async def btn_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_modal(LimitModal(channel))

    @discord.ui.button(label="Privacy", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="vcpanel:privacy", row=0)
    async def btn_privacy(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return

        locked = not doc.get("locked", False)
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.connect = not locked

        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason="Privacy toggled via control panel"
        )
        await temp_channels.update_one(
            {"channel_id": channel.id},
            {"$set": {"locked": locked}}
        )
        await interaction.response.send_message(
            f"🔒 Channel is now {'locked' if locked else 'unlocked'}.",
            ephemeral=True
        )

    @discord.ui.button(label="Waiting R.", emoji="⏱️", style=discord.ButtonStyle.secondary, custom_id="vcpanel:waiting", row=0)
    async def btn_waiting(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return

        waiting = not doc.get("waiting_room", False)
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.connect = not waiting
        overwrite.view_channel = True

        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason="Waiting room toggled via control panel"
        )
        await temp_channels.update_one(
            {"channel_id": channel.id},
            {"$set": {"waiting_room": waiting}}
        )
        await interaction.response.send_message(
            f"⏱️ Waiting room {'enabled' if waiting else 'disabled'}. "
            f"{'New joiners will need you to give them access.' if waiting else 'Anyone can join freely again.'}",
            ephemeral=True
        )

    @discord.ui.button(label="Chat", emoji="💬", style=discord.ButtonStyle.secondary, custom_id="vcpanel:chat", row=0)
    async def btn_chat(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return

        chat_locked = not doc.get("chat_locked", False)
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = not chat_locked

        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason="Chat toggled via control panel"
        )
        await temp_channels.update_one(
            {"channel_id": channel.id},
            {"$set": {"chat_locked": chat_locked}}
        )
        await interaction.response.send_message(
            f"💬 Text chat is now {'locked' if chat_locked else 'unlocked'}.",
            ephemeral=True
        )

    @discord.ui.button(label="Trust", emoji="🤝", style=discord.ButtonStyle.secondary, custom_id="vcpanel:trust", row=1)
    async def btn_trust(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_message(
            "Select a member to trust:",
            view=UserActionView(channel, "trust"),
            ephemeral=True
        )

    @discord.ui.button(label="Untrust", emoji="🚷", style=discord.ButtonStyle.secondary, custom_id="vcpanel:untrust", row=1)
    async def btn_untrust(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_message(
            "Select a member to untrust:",
            view=UserActionView(channel, "untrust"),
            ephemeral=True
        )

    @discord.ui.button(label="Invite", emoji="📨", style=discord.ButtonStyle.secondary, custom_id="vcpanel:invite", row=1)
    async def btn_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_temp_channel(interaction)
        if not channel:
            return
        try:
            invite = await channel.create_invite(
                max_age=3600,
                reason="Temp voice channel invite"
            )
            await interaction.response.send_message(
                f"📨 Invite link (expires in 1 hour): {invite.url}",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to create an invite for this channel.",
                ephemeral=True
            )

    @discord.ui.button(label="Kick", emoji="👢", style=discord.ButtonStyle.secondary, custom_id="vcpanel:kick", row=1)
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_message(
            "Select a member to remove from the channel:",
            view=UserActionView(channel, "kick"),
            ephemeral=True
        )

    @discord.ui.button(label="Region", emoji="🌍", style=discord.ButtonStyle.secondary, custom_id="vcpanel:region", row=1)
    async def btn_region(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_message(
            "Choose a voice region:",
            view=RegionSelectView(channel),
            ephemeral=True
        )

    @discord.ui.button(label="Block", emoji="⛔", style=discord.ButtonStyle.secondary, custom_id="vcpanel:block", row=2)
    async def btn_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_message(
            "Select a member to block:",
            view=UserActionView(channel, "block"),
            ephemeral=True
        )

    @discord.ui.button(label="Unblock", emoji="✅", style=discord.ButtonStyle.secondary, custom_id="vcpanel:unblock", row=2)
    async def btn_unblock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_message(
            "Select a member to unblock:",
            view=UserActionView(channel, "unblock"),
            ephemeral=True
        )

    @discord.ui.button(label="Claim", emoji="👑", style=discord.ButtonStyle.secondary, custom_id="vcpanel:claim", row=2)
    async def btn_claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_temp_channel(interaction)
        if not channel:
            return

        owner_id = doc.get("owner")
        owner_member = interaction.guild.get_member(owner_id) if owner_id else None
        owner_present = bool(
            owner_member and owner_member.voice
            and owner_member.voice.channel
            and owner_member.voice.channel.id == channel.id
        )

        if owner_present:
            await interaction.response.send_message(
                "The current owner is still in the channel.",
                ephemeral=True
            )
            return

        await claim_channel(channel, interaction.user)
        await interaction.response.send_message(
            f"👑 You are now the owner of {channel.mention}.",
            ephemeral=True
        )

    @discord.ui.button(label="Transfer", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="vcpanel:transfer", row=2)
    async def btn_transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return
        await interaction.response.send_message(
            "Select a member to transfer ownership to:",
            view=UserActionView(channel, "transfer"),
            ephemeral=True
        )

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="vcpanel:delete", row=2)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, doc = await self._require_owner(interaction)
        if not channel:
            return

        await interaction.response.send_message(
            "Channel will be deleted.",
            ephemeral=True
        )
        await temp_channels.delete_one({"channel_id": channel.id})
        try:
            await channel.delete(reason="Deleted via control panel")
        except discord.HTTPException:
            pass


def build_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="TempVoice Controls",
        description=(
            "This interface can be used to manage temporary voice channels.\n"
            "More options are available with `/vc` commands."
        ),
        color=discord.Color.dark_theme()
    )


# ---------------------------------------------------------------------------
# Bot + slash commands
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class MegaBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self._health_runner: Optional[web.AppRunner] = None

    async def setup_hook(self):
        # Start periodic role nickname syncing only after Discord's event loop is running.
        if not getattr(self, "_role_nickname_sync_task", None):
            from staff_dashboard import _role_nickname_sync_loop
            self._role_nickname_sync_task = asyncio.create_task(_role_nickname_sync_loop(self))

        # Role Connections storage needs an explicit connect step (it pings
        # MongoDB to decide whether to use it or fall back to JSON).
        try:
            await role_connections_storage.connect()
        except Exception:
            logger.exception("Storage failed to initialize; falling back to in-memory defaults where possible")

        # Cedar Cove (voice) storage has no fallback, so this just logs a
        # warning rather than blocking startup if MongoDB isn't reachable.
        await check_cedar_cove_mongodb()

        # Re-register every persistent (timeout=None) view so buttons/selects
        # created before a restart keep working.
        self.add_view(DashboardView())
        self.add_view(TicketOpenView())
        self.add_view(TicketSelectView())
        self.add_view(TicketControlView())
        self.add_view(TicketClaimedView())
        self.add_view(TicketClosedView())
        self.add_view(RequestCloseView())
        self.add_view(VoicePanelView())

        try:
            self._health_runner = await start_health_server()
        except Exception:
            logger.exception("Failed to start the health check server")

        try:
            if GUILD_ID:
                guild_obj = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                logger.info("Synced %d command(s) to guild %s.", len(synced), GUILD_ID)
            else:
                synced = await self.tree.sync()
                logger.info("Synced %d command(s) globally.", len(synced))
        except Exception:
            logger.exception(
                "Failed to sync application commands. The bot will still start, "
                "but slash commands may not appear until this is resolved."
            )

    async def close(self):
        if self._health_runner:
            await self._health_runner.cleanup()
        await super().close()

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        logger.info("Connected to %d guild(s).", len(self.guilds))


bot = MegaBot()
bot.ticket_open_view_cls = TicketOpenView
bot.ticket_select_view_cls = TicketSelectView

from staff_dashboard import setup_staff, register_staff_routes  # noqa: E402
setup_staff(bot, get_store, logger)

_processing_members: set[tuple[int, int]] = set()


@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Left over from the original bot's debugging — harmless, just noisy at
    # INFO level, so it's logged at DEBUG instead. Safe to remove.
    logger.debug("Interaction received: %s", interaction.type)


# ---------------------------------------------------------------------------
# Slash commands — Role Connections subsystem
# ---------------------------------------------------------------------------

@bot.tree.command(name="roleconnections", description="Manage automatic role connections for this server.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def roleconnections(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    embed = build_dashboard_embed(interaction.guild)
    view = DashboardView()
    await interaction.response.send_message(embed=embed, view=view)


@roleconnections.error
async def roleconnections_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "🚫 You need the **Administrator** permission to use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "🚫 This command can only be used inside a server."
    else:
        logger.exception("Unhandled error in /roleconnections", exc_info=error)
        message = "⚠️ An unexpected error occurred while running this command."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.exception("Unhandled application command error", exc_info=error)
    message = "⚠️ Something went wrong while processing that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass




# ---------------------------------------------------------------------------
# Slash commands — Ticket subsystem
# ---------------------------------------------------------------------------

@bot.tree.command(name="setup", description="Open the ticket panel builder")
@ticket_setup_admin_check()
async def setup_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🎫 **Ticket Panel Setup**\nChoose the type of panel you want to create:",
        view=SetupChoiceView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="claim", description="Claim this ticket")
async def claim_cmd(interaction: discord.Interaction):
    await handle_claim(interaction)


@bot.tree.command(name="unclaim", description="Release your claim on this ticket")
async def unclaim_cmd(interaction: discord.Interaction):
    await handle_unclaim(interaction)


@bot.tree.command(name="close", description="Close this ticket")
async def close_cmd(interaction: discord.Interaction):
    await handle_close(interaction)


@bot.tree.command(name="reopen", description="Reopen this ticket")
async def reopen_cmd(interaction: discord.Interaction):
    await handle_reopen(interaction)


@bot.tree.command(name="transfer", description="Transfer this ticket to another support member")
@app_commands.describe(user="The support member to transfer this ticket to")
async def transfer_cmd(interaction: discord.Interaction, user: discord.Member):
    await handle_transfer(interaction, user)


@bot.tree.command(name="requestclose", description="Ask the ticket opener to confirm closing this ticket")
async def requestclose_cmd(interaction: discord.Interaction):
    await handle_request_close(interaction)


@bot.tree.command(name="add", description="Add any user to this ticket")
@app_commands.describe(user="The user to add to this ticket")
async def add_cmd(interaction: discord.Interaction, user: discord.Member):
    await handle_add_user(interaction, user)


@bot.tree.command(name="remove", description="Remove a user from this ticket")
@app_commands.describe(user="The user to remove from this ticket")
async def remove_cmd(interaction: discord.Interaction, user: discord.Member):
    await handle_remove_user(interaction, user)




# ---------------------------------------------------------------------------
# Events — Role Connections subsystem (auto role sync on member update)


# ---------------------------------------------------------------------------
# Slash commands — Voice (Cedar Cove) subsystem
# ---------------------------------------------------------------------------

voice_group = app_commands.Group(
    name="vc",
    description="Manage your temporary voice channel"
)


@voice_group.command(
    name="panel",
    description="Post the temporary voice channel control panel"
)
async def vc_panel(interaction: discord.Interaction):

    if not isinstance(interaction.channel, discord.VoiceChannel):
        await interaction.response.send_message(
            "This command only works inside a voice channel's text chat.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        embed=build_panel_embed(),
        view=VoicePanelView()
    )


bot.tree.add_command(voice_group)

@bot.tree.command(
    name="tempchannel",
    description="Setup a Join to Create voice channel"
)
@app_commands.checks.has_permissions(
    administrator=True
)
@app_commands.describe(name="Name for the Join to Create channel")
async def tempchannel_setup(
    interaction: discord.Interaction,
    name: str = "➕️ Create Channel"
):
    category = interaction.channel.category if interaction.channel else None

    try:
        creator_channel = await interaction.guild.create_voice_channel(
            name=name,
            category=category,
            reason="Join to Create setup"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to create voice channels here.",
            ephemeral=True
        )
        return

    await vc_guild_configs.update_one(
        {"_id": interaction.guild.id},
        {"$set": {"create_channel": creator_channel.id}},
        upsert=True
    )

    await interaction.response.send_message(
        f"✅ Created {creator_channel.mention} — joining it will create your own "
        f"temporary voice channel. Run `/vc panel` to post the control panel.",
        ephemeral=True
    )


@tempchannel_setup.error
async def tempchannel_setup_error(
    interaction: discord.Interaction,
    error
):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message(
            "You need Administrator permissions to use this command.",
            ephemeral=True
        )


@bot.tree.command(
    name="setuplobby",
    description="Setup the voice lobby system"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def setuplobby(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="Voice Control",
        description=(
            "Select your settings below:\n\n"
            "🎧 First dropdown: Lobby voice channel\n"
            "🔊 Second dropdown: Channels users can be moved to\n\n"
            "Press Save when finished."
        ),
        color=discord.Color.blue()
    )

    view = LobbySetupView()

    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True
    )


@setuplobby.error
async def setuplobby_error(
    interaction: discord.Interaction,
    error
):

    if isinstance(
        error,
        app_commands.errors.MissingPermissions
    ):

        await interaction.response.send_message(
            "You need Administrator permissions to use this command.",
            ephemeral=True
        )

# ---------------------------------------------------------------------------

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.roles == after.roles:
        return

    guild = after.guild

    try:
        settings = await role_connections_storage.get_settings(guild.id)
        if not settings.get("enabled", True):
            return

        connections = await role_connections_storage.get_connections(guild.id)
        if not connections:
            return
    except Exception:
        logger.exception("Failed to load storage while handling on_member_update")
        return

    key = (guild.id, after.id)
    if key in _processing_members:
        return
    _processing_members.add(key)

    try:
        before_role_ids = {r.id for r in before.roles}
        after_role_ids = {r.id for r in after.roles}

        before_targets = compute_role_target_states(connections, before_role_ids)
        after_targets = compute_role_target_states(connections, after_role_ids)

        all_role_ids = set(before_targets) | set(after_targets)
        roles_to_add = {
            rid for rid in all_role_ids
            if after_targets.get(rid, False) and not before_targets.get(rid, False)
        }
        roles_to_remove = {
            rid for rid in all_role_ids
            if before_targets.get(rid, False) and not after_targets.get(rid, False)
        }

        add_role_objs = [guild.get_role(rid) for rid in roles_to_add]
        add_role_objs = [r for r in add_role_objs if r is not None and r not in after.roles]

        remove_role_objs = [guild.get_role(rid) for rid in roles_to_remove]
        remove_role_objs = [r for r in remove_role_objs if r is not None and r in after.roles]

        if add_role_objs:
            try:
                await after.add_roles(*add_role_objs, reason="Role Connections: condition met")
                logger.log(
                    ROLE_SYNC_LOG_LEVEL,
                    "Added roles %s to %s in guild %s",
                    [r.name for r in add_role_objs], after, guild.id,
                )
            except discord.Forbidden:
                logger.warning(
                    "Missing permissions to add roles for %s in guild %s. "
                    "Ensure the bot's role is above the connected roles.", after, guild.id,
                )
            except discord.HTTPException:
                logger.exception("Failed to add roles for %s in guild %s", after, guild.id)

        if remove_role_objs:
            try:
                await after.remove_roles(*remove_role_objs, reason="Role Connections: condition no longer met")
                logger.log(
                    ROLE_SYNC_LOG_LEVEL,
                    "Removed roles %s from %s in guild %s",
                    [r.name for r in remove_role_objs], after, guild.id,
                )
            except discord.Forbidden:
                logger.warning(
                    "Missing permissions to remove roles for %s in guild %s. "
                    "Ensure the bot's role is above the connected roles.", after, guild.id,
                )
            except discord.HTTPException:
                logger.exception("Failed to remove roles for %s in guild %s", after, guild.id)

        try:
            await sync_cross_server_roles(guild, after, add_role_objs, remove_role_objs)
        except Exception:
            logger.exception("Failed to sync cross-server roles for %s from guild %s", after, guild.id)

    finally:
        _processing_members.discard(key)


async def sync_cross_server_roles(guild: discord.Guild, member: discord.Member,
                                   added_role_objs: list, removed_role_objs: list):
    if not added_role_objs and not removed_role_objs:
        return

    links = await role_connections_storage.get_guild_links(guild.id)
    if not links:
        return

    for link in links:
        if not link.get("enabled", True):
            continue
        other_guild_id = link["guild_b_id"] if link["guild_a_id"] == guild.id else link["guild_a_id"]
        other_guild = bot.get_guild(other_guild_id)
        if not other_guild:
            continue

        other_member = other_guild.get_member(member.id)
        if other_member is None:
            try:
                other_member = await other_guild.fetch_member(member.id)
            except discord.NotFound:
                continue
            except discord.HTTPException:
                logger.exception("Failed to fetch member %s in linked guild %s", member.id, other_guild.id)
                continue

        other_roles_by_name = {r.name.lower(): r for r in other_guild.roles}
        member_role_names = {r.name.lower() for r in other_member.roles}

        to_add = []
        for r in added_role_objs:
            match = other_roles_by_name.get(r.name.lower())
            if match and match.name.lower() not in member_role_names:
                to_add.append(match)

        to_remove = []
        for r in removed_role_objs:
            match = other_roles_by_name.get(r.name.lower())
            if match and match.name.lower() in member_role_names:
                to_remove.append(match)

        if to_add:
            try:
                await other_member.add_roles(*to_add, reason=f"Cross-server sync from {guild.name}")
                logger.log(
                    ROLE_SYNC_LOG_LEVEL,
                    "Cross-server: added roles %s to %s in guild %s (synced from %s)",
                    [r.name for r in to_add], other_member, other_guild.id, guild.id,
                )
            except discord.Forbidden:
                logger.warning(
                    "Missing permissions for cross-server role add in guild %s. "
                    "Ensure the bot's role is above the synced roles there.", other_guild.id,
                )
            except discord.HTTPException:
                logger.exception("Failed cross-server role add in guild %s", other_guild.id)

        if to_remove:
            try:
                await other_member.remove_roles(*to_remove, reason=f"Cross-server sync from {guild.name}")
                logger.log(
                    ROLE_SYNC_LOG_LEVEL,
                    "Cross-server: removed roles %s from %s in guild %s (synced from %s)",
                    [r.name for r in to_remove], other_member, other_guild.id, guild.id,
                )
            except discord.Forbidden:
                logger.warning(
                    "Missing permissions for cross-server role removal in guild %s. "
                    "Ensure the bot's role is above the synced roles there.", other_guild.id,
                )
            except discord.HTTPException:
                logger.exception("Failed cross-server role removal in guild %s", other_guild.id)




# ---------------------------------------------------------------------------
# Global error logging (catches anything not handled by a more specific
# handler above, e.g. errors raised inside on_member_update)
# ---------------------------------------------------------------------------

@bot.event
async def on_error(event_method, *args, **kwargs):
    logger.exception("Unhandled exception in event: %s", event_method)




# ---------------------------------------------------------------------------
# Events — Voice (Cedar Cove) subsystem (Join to Create / Lobby routing)
# ---------------------------------------------------------------------------

@bot.event
async def on_voice_state_update(
    member,
    before,
    after
):

    # --- Handle leaving a tracked temp channel: cleanup + ownership transfer ---
    if before.channel and (not after.channel or after.channel.id != before.channel.id):

        doc = await temp_channels.find_one(
            {
                "channel_id": before.channel.id
            }
        )

        if doc:

            await temp_channels.update_one(
                {"channel_id": before.channel.id},
                {"$pull": {"join_order": member.id}}
            )

            remaining = [m for m in before.channel.members if not m.bot]

            if len(remaining) == 0:

                try:
                    await before.channel.delete(
                        reason="Temporary room empty"
                    )
                except discord.HTTPException:
                    pass

                await temp_channels.delete_one(
                    {"channel_id": before.channel.id}
                )

            elif doc.get("owner") == member.id:

                refreshed = await temp_channels.find_one(
                    {"channel_id": before.channel.id}
                )
                join_order = refreshed.get("join_order", []) if refreshed else []

                next_owner_id = next(
                    (uid for uid in join_order if any(m.id == uid for m in remaining)),
                    None
                )

                if next_owner_id is None:
                    next_owner_id = remaining[0].id

                next_owner = discord.utils.get(remaining, id=next_owner_id)

                if next_owner:
                    await claim_channel(before.channel, next_owner)

    if after.channel is None:
        return

    config = await vc_guild_configs.find_one(
        {
            "_id": member.guild.id
        }
    )

    # --- Join to Create: joining the designated channel spins up a new room ---
    if config and after.channel.id == config.get("create_channel"):

        category = after.channel.category

        try:
            new_channel = await member.guild.create_voice_channel(
                name=f"{member.display_name}'s Channel",
                category=category,
                reason="Join to Create"
            )
        except (discord.Forbidden, discord.HTTPException):
            return

        await temp_channels.insert_one(
            {
                "channel_id": new_channel.id,
                "guild_id": member.guild.id,
                "owner": member.id,
                "join_order": [member.id],
                "trusted": [],
                "blocked": [],
                "locked": False,
                "waiting_room": False,
                "chat_locked": False
            }
        )

        try:
            await new_channel.set_permissions(
                member.guild.me,
                view_channel=True,
                connect=True,
                speak=True,
                manage_channels=True,
                manage_roles=True,
                move_members=True,
                create_instant_invite=True,
                reason="Bot control permissions"
            )
        except discord.HTTPException:
            pass

        try:
            await new_channel.set_permissions(
                member,
                connect=True,
                manage_channels=True,
                move_members=True,
                reason="Channel owner"
            )
        except discord.HTTPException:
            pass

        try:
            await member.move_to(
                new_channel,
                reason="Join to Create"
            )
        except discord.HTTPException:
            pass

        try:
            await new_channel.send(
                embed=build_panel_embed(),
                view=VoicePanelView()
            )
        except discord.HTTPException:
            pass

        return

    # --- Track join order for any tracked temp channel ---
    joined_doc = await temp_channels.find_one(
        {"channel_id": after.channel.id}
    )

    if joined_doc:
        await temp_channels.update_one(
            {"channel_id": after.channel.id},
            {"$addToSet": {"join_order": member.id}}
        )

    if not config:
        return

    if after.channel.id != config.get(
        "lobby_channel"
    ):
        return

    destination_channels = config.get(
        "destination_channels",
        []
    )

    for channel_id in destination_channels:

        channel = member.guild.get_channel(
            channel_id
        )

        if channel:

            if (
                channel.user_limit == 0
                or len(channel.members) < channel.user_limit
            ):

                await member.move_to(
                    channel,
                    reason="Lobby routing"
                )

                return

    # Create a temporary channel if all channels are full

    category = after.channel.category

    try:
        new_channel = await member.guild.create_voice_channel(
            name=f"{member.display_name}'s Room",
            category=category,
            reason="Lobby system created room"
        )

    except discord.Forbidden:
        return

    except discord.HTTPException:
        return

    await temp_channels.insert_one(
        {
            "channel_id": new_channel.id,
            "guild_id": member.guild.id,
            "owner": member.id,
            "join_order": [member.id],
            "trusted": [],
            "blocked": [],
            "locked": False,
            "waiting_room": False,
            "chat_locked": False
        }
    )

    try:
        await new_channel.set_permissions(
            member.guild.me,
            view_channel=True,
            connect=True,
            speak=True,
            manage_channels=True,
            manage_roles=True,
            move_members=True,
            create_instant_invite=True,
            reason="Bot control permissions"
        )
    except discord.HTTPException:
        pass

    try:
        await new_channel.set_permissions(
            member,
            connect=True,
            manage_channels=True,
            move_members=True,
            reason="Channel owner"
        )
    except discord.HTTPException:
        pass

    await member.move_to(
        new_channel,
        reason="Created temporary lobby room"
    )

    try:
        await new_channel.send(
            embed=build_panel_embed(),
            view=VoicePanelView()
        )
    except discord.HTTPException:
        pass

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_BOT_TOKEN (or DISCORD_TOKEN) is not set. "
            "Please add it to your .env file before starting the bot."
        )
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
