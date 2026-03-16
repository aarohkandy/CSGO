import asyncio
import io
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import discord
import requests
from colorthief import ColorThief
from discord import app_commands
from dotenv import load_dotenv
from PIL import Image


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / "state.json"
WATCH_MESSAGE_TEXT = "React with any emoji to get a name color! 🎨"
COLOR_ROLE_PREFIX = "color-"
DEFAULT_COLOR = (128, 128, 128)
BUILD_ID = os.getenv("RENDER_GIT_COMMIT") or os.getenv("RAILWAY_GIT_COMMIT_SHA") or "unknown"
TWEMOJI_BASE_URL = (
    "https://cdn.jsdelivr.net/gh/twitter/twemoji/assets/72x72/{codepoints}.png"
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("colorseg-bot")


def get_state_path() -> Path:
    raw_path = os.getenv("STATE_FILE_PATH")
    if not raw_path:
        return DEFAULT_STATE_PATH

    return Path(raw_path).expanduser()


def parse_snowflake(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WatchState:
    channel_id: Optional[int] = None
    message_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Any) -> Optional["WatchState"]:
        if not isinstance(data, dict):
            return None

        channel_id = parse_snowflake(data.get("channel_id"))
        message_id = parse_snowflake(data.get("message_id"))
        if channel_id is None or message_id is None:
            return None

        return cls(channel_id=channel_id, message_id=message_id)

    def to_dict(self) -> dict[str, int]:
        if not self.configured:
            raise ValueError("Cannot serialize an unconfigured watch state.")

        return {
            "channel_id": self.channel_id,
            "message_id": self.message_id,
        }

    @property
    def configured(self) -> bool:
        return self.channel_id is not None and self.message_id is not None

    def matches(self, channel_id: int, message_id: int) -> bool:
        return (
            self.configured
            and self.channel_id == channel_id
            and self.message_id == message_id
        )


@dataclass
class BotState:
    guilds: dict[int, WatchState]
    legacy_watch_state: Optional[WatchState] = None

    @classmethod
    def load(cls, path: Path) -> "BotState":
        if not path.exists():
            return cls(guilds={})

        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Unable to load %s. Starting with an empty state.", path)
            return cls(guilds={})

        if not isinstance(data, dict):
            LOGGER.warning("Unexpected state format in %s. Starting with an empty state.", path)
            return cls(guilds={})

        guild_entries = data.get("guilds")
        if isinstance(guild_entries, dict):
            guilds: dict[int, WatchState] = {}
            for guild_id_raw, guild_state_raw in guild_entries.items():
                guild_id = parse_snowflake(guild_id_raw)
                guild_state = WatchState.from_dict(guild_state_raw)
                if guild_id is None or guild_state is None:
                    LOGGER.warning(
                        "Skipping invalid guild watch entry for key %r in %s.",
                        guild_id_raw,
                        path,
                    )
                    continue

                guilds[guild_id] = guild_state

            return cls(guilds=guilds)

        legacy_watch_state = WatchState.from_dict(data)
        if legacy_watch_state is not None:
            LOGGER.info("Loaded legacy global watch state from %s.", path)

        return cls(guilds={}, legacy_watch_state=legacy_watch_state)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "guilds": {
                        str(guild_id): guild_state.to_dict()
                        for guild_id, guild_state in sorted(self.guilds.items())
                    }
                },
                handle,
                indent=2,
            )

    def get_guild_state(self, guild_id: int) -> Optional[WatchState]:
        return self.guilds.get(guild_id)

    def set_guild_state(self, guild_id: int, guild_state: WatchState) -> None:
        self.guilds[guild_id] = guild_state

    def remove_guild_state(self, guild_id: int) -> None:
        self.guilds.pop(guild_id, None)


def color_role_name(emoji: str) -> str:
    return f"{COLOR_ROLE_PREFIX}{emoji}"


def is_color_role(role: discord.Role) -> bool:
    return role.name.startswith(COLOR_ROLE_PREFIX)


def describe_role(role: discord.Role) -> str:
    return f"{role.name} (id={role.id}, position={role.position})"


def emoji_to_codepoints(emoji: str) -> str:
    codepoints = [f"{ord(char):x}" for char in emoji]
    has_zwj = "200d" in codepoints

    if not has_zwj:
        codepoints = [value for value in codepoints if value != "fe0f"]

    return "-".join(codepoints)


def twemoji_url_for_emoji(emoji: str) -> str:
    return TWEMOJI_BASE_URL.format(codepoints=emoji_to_codepoints(emoji))


def dominant_color_for_emoji(emoji: str) -> tuple[int, int, int]:
    response = requests.get(twemoji_url_for_emoji(emoji), timeout=15)
    response.raise_for_status()

    with Image.open(io.BytesIO(response.content)) as image:
        rgba_image = image.convert("RGBA")
        filtered = Image.new("RGBA", rgba_image.size, (0, 0, 0, 0))
        source_pixels = rgba_image.load()
        filtered_pixels = filtered.load()

        opaque_pixels = 0
        for x in range(rgba_image.width):
            for y in range(rgba_image.height):
                red, green, blue, alpha = source_pixels[x, y]
                if alpha >= 128:
                    filtered_pixels[x, y] = (red, green, blue, 255)
                    opaque_pixels += 1

        if opaque_pixels == 0:
            return DEFAULT_COLOR

        image_bytes = io.BytesIO()
        filtered.save(image_bytes, format="PNG")
        image_bytes.seek(0)

    return ColorThief(image_bytes).get_color(quality=1)


class ColorRoleBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_reactions = True
        intents.members = True

        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.state_path = get_state_path()
        self.state = BotState.load(self.state_path)
        self._resume_checked = False
        self._ignored_reaction_removals: set[tuple[int, int, str]] = set()

    async def setup_hook(self) -> None:
        self.tree.add_command(here)
        synced = await self.tree.sync()
        LOGGER.info("Synced %s application command(s).", len(synced))

    async def on_ready(self) -> None:
        if self.user is None:
            return

        LOGGER.info(
            "Logged in as %s (%s) with build %s",
            self.user,
            self.user.id,
            BUILD_ID,
        )

        if self._resume_checked:
            return

        self._resume_checked = True

        await self.migrate_legacy_state()

        if not self.state.guilds:
            LOGGER.info(
                "No watched messages configured yet. Using state file %s.",
                self.state_path,
            )
            return

        stale_guild_ids: list[int] = []
        for guild_id, guild_state in list(self.state.guilds.items()):
            is_valid = await self.validate_guild_watch_state(guild_id, guild_state)
            if not is_valid:
                stale_guild_ids.append(guild_id)

        if stale_guild_ids:
            for guild_id in stale_guild_ids:
                self.state.remove_guild_state(guild_id)

            self.state.save(self.state_path)
            LOGGER.warning(
                "Pruned %s stale watched message configuration(s).",
                len(stale_guild_ids),
            )

        if not self.state.guilds:
            LOGGER.info(
                "No watched messages remain configured after startup validation."
            )

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if self.user is not None and payload.user_id == self.user.id:
            return

        guild_watch_state = self.get_guild_watch_state(payload.guild_id)
        if guild_watch_state is None or not guild_watch_state.matches(
            payload.channel_id, payload.message_id
        ):
            return

        if payload.emoji.id is not None:
            LOGGER.info("Ignoring custom emoji reaction for watched message.")
            return

        guild = self.get_guild(payload.guild_id)
        if guild is None:
            LOGGER.warning("Guild %s is not available in cache.", payload.guild_id)
            return

        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                LOGGER.exception("Unable to fetch member %s.", payload.user_id)
                return

        if member.bot:
            return

        emoji = payload.emoji.name
        if emoji is None:
            return

        LOGGER.info(
            "Reaction add received: guild=%s (%s) channel=%s message=%s member=%s (%s) "
            "emoji=%s",
            guild.name,
            guild.id,
            payload.channel_id,
            payload.message_id,
            member,
            member.id,
            emoji,
        )
        LOGGER.info(
            "Hierarchy snapshot before assignment: bot_top=%s member_top=%s",
            describe_role(guild.me.top_role) if guild.me else "unknown",
            describe_role(member.top_role),
        )

        try:
            rgb = await asyncio.to_thread(dominant_color_for_emoji, emoji)
        except requests.RequestException:
            LOGGER.exception(
                "Unable to download Twemoji asset for %s. Falling back to gray.",
                emoji,
            )
            rgb = DEFAULT_COLOR
        except Exception:
            LOGGER.exception(
                "Unable to extract dominant color for %s. Falling back to gray.",
                emoji,
            )
            rgb = DEFAULT_COLOR

        role, created_new_role = await self.get_or_create_color_role(guild, emoji, rgb)
        if role is None:
            return

        LOGGER.info(
            "Color role ready for assignment: created=%s role=%s",
            created_new_role,
            describe_role(role),
        )

        old_roles = [
            existing_role
            for existing_role in member.roles
            if is_color_role(existing_role) and existing_role.id != role.id
        ]
        LOGGER.info(
            "Existing color roles on member %s before swap: %s",
            member.id,
            [describe_role(existing_role) for existing_role in old_roles],
        )

        if guild.me is None:
            LOGGER.warning(
                "Bot member cache is unavailable in guild %s during assignment.",
                guild.id,
            )
            if created_new_role:
                await self.delete_role_if_unused(role)
            return

        if guild.me.top_role.position <= member.top_role.position:
            LOGGER.error(
                "Cannot assign %s to member %s because bot_top=%s is not above member_top=%s.",
                role.name,
                member.id,
                describe_role(guild.me.top_role),
                describe_role(member.top_role),
            )
            if created_new_role:
                await self.delete_role_if_unused(role)
            return

        if guild.me.top_role.position <= role.position:
            LOGGER.error(
                "Cannot assign %s because bot_top=%s is not above role=%s after positioning.",
                role.name,
                describe_role(guild.me.top_role),
                describe_role(role),
            )
            if created_new_role:
                await self.delete_role_if_unused(role)
            return

        member_has_role = any(existing_role.id == role.id for existing_role in member.roles)
        if not member_has_role:
            try:
                await member.add_roles(
                    role,
                    reason="Assigning emoji color role from watched reaction message.",
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "Unable to add role %s to member %s. bot_top=%s member_top=%s role=%s",
                    role.id,
                    member.id,
                    describe_role(guild.me.top_role),
                    describe_role(member.top_role),
                    describe_role(role),
                )
                if created_new_role:
                    await self.delete_role_if_unused(role)
                return

        try:
            verified_member = await guild.fetch_member(member.id)
        except discord.HTTPException:
            LOGGER.exception(
                "Unable to refetch member %s after assigning role %s.",
                member.id,
                role.id,
            )
            if created_new_role:
                await self.delete_role_if_unused(role)
            return

        if all(existing_role.id != role.id for existing_role in verified_member.roles):
            LOGGER.error(
                "Role assignment verification failed for member %s and role %s. "
                "member_top=%s role=%s",
                verified_member.id,
                role.id,
                describe_role(verified_member.top_role),
                describe_role(role),
            )
            if created_new_role:
                await self.delete_role_if_unused(role)
            return

        member = verified_member
        LOGGER.info(
            "Role assignment verified: member=%s role=%s final_member_top=%s",
            member.id,
            describe_role(role),
            describe_role(member.top_role),
        )

        if old_roles:
            try:
                await member.remove_roles(
                    *old_roles,
                    reason="Swapping member to a new emoji color role.",
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "Unable to remove old color roles from member %s after verifying new role %s.",
                    member.id,
                    role.id,
                )
                return

        await self.remove_other_member_reactions(
            guild=guild,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            member=member,
            keep_emoji=emoji,
        )

        for old_role in old_roles:
            await self.delete_role_if_unused(old_role)

    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if self.user is not None and payload.user_id == self.user.id:
            return

        guild_watch_state = self.get_guild_watch_state(payload.guild_id)
        if guild_watch_state is None or not guild_watch_state.matches(
            payload.channel_id, payload.message_id
        ):
            return

        if payload.emoji.id is not None:
            return

        emoji = payload.emoji.name
        if emoji is None:
            return

        ignored_key = (payload.message_id, payload.user_id, emoji)
        if ignored_key in self._ignored_reaction_removals:
            self._ignored_reaction_removals.discard(ignored_key)
            return

        guild = self.get_guild(payload.guild_id)
        if guild is None:
            LOGGER.warning("Guild %s is not available in cache.", payload.guild_id)
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                LOGGER.exception("Unable to fetch member %s.", payload.user_id)
                return

        if member.bot:
            return

        color_roles = [role for role in member.roles if is_color_role(role)]
        if not color_roles:
            return

        try:
            await member.remove_roles(
                *color_roles,
                reason="Removing emoji color role after reaction removal.",
            )
        except discord.HTTPException:
            LOGGER.exception(
                "Unable to remove color roles from member %s.",
                member.id,
            )
            return

        for role in color_roles:
            await self.delete_role_if_unused(role)

    async def get_or_create_color_role(
        self, guild: discord.Guild, emoji: str, rgb: tuple[int, int, int]
    ) -> tuple[Optional[discord.Role], bool]:
        existing_role = discord.utils.get(guild.roles, name=color_role_name(emoji))
        if existing_role is not None:
            LOGGER.info("Reusing existing color role %s", describe_role(existing_role))
            positioned_role = await self.ensure_color_role_position(guild, existing_role)
            return positioned_role, False

        try:
            role = await guild.create_role(
                name=color_role_name(emoji),
                colour=discord.Color.from_rgb(*rgb),
                reason="Creating emoji color role.",
            )
        except discord.HTTPException:
            LOGGER.exception("Unable to create color role for %s.", emoji)
            return None, False

        LOGGER.info("Created new color role %s", describe_role(role))
        positioned_role = await self.ensure_color_role_position(guild, role)

        return positioned_role, True

    async def delete_role_if_unused(self, role: discord.Role) -> None:
        if role.members:
            return

        try:
            await role.delete(reason="Deleting unused emoji color role.")
        except discord.HTTPException:
            LOGGER.exception("Unable to delete unused role %s.", role.name)

    def get_guild_watch_state(self, guild_id: Optional[int]) -> Optional[WatchState]:
        if guild_id is None:
            return None

        return self.state.get_guild_state(guild_id)

    async def ensure_color_role_position(
        self, guild: discord.Guild, role: discord.Role
    ) -> discord.Role:
        me = guild.me or guild.get_member(self.user.id if self.user else 0)
        if me is None:
            LOGGER.warning(
                "Unable to resolve the bot member in guild %s while positioning %s.",
                guild.id,
                role.name,
            )
            return role

        target_position = max(1, me.top_role.position - 1)
        LOGGER.info(
            "Preparing to position role %s under bot_top=%s target_position=%s",
            describe_role(role),
            describe_role(me.top_role),
            target_position,
        )

        if role.position == target_position:
            LOGGER.info("Role %s is already at target position.", describe_role(role))
            return role

        try:
            desired_positions = {
                existing_role: existing_role.position
                for existing_role in guild.roles
                if existing_role.id != role.id and existing_role.position < me.top_role.position
            }
            desired_positions[role] = target_position
            updated_roles = await guild.edit_role_positions(
                positions=desired_positions,
                reason="Placing color role as high as the bot can manage.",
            )
        except discord.HTTPException:
            LOGGER.exception(
                "Unable to position role %s to %s in guild %s. "
                "The bot can only move roles below its own top role.",
                role.name,
                target_position,
                guild.id,
            )
            return role

        refreshed_role = discord.utils.get(updated_roles, id=role.id)
        if refreshed_role is None:
            refreshed_role = discord.utils.get(guild.roles, id=role.id)
        if refreshed_role is None:
            try:
                refreshed_role = discord.utils.get(await guild.fetch_roles(), id=role.id)
            except discord.HTTPException:
                LOGGER.exception(
                    "Unable to refetch roles for guild %s after positioning role %s.",
                    guild.id,
                    role.id,
                )
                return role

        if refreshed_role is None:
            LOGGER.warning(
                "Role %s disappeared after attempting to reposition it in guild %s.",
                role.id,
                guild.id,
            )
            return role

        LOGGER.info(
            "Role positioning result: role=%s target_position=%s actual_position=%s",
            describe_role(refreshed_role),
            target_position,
            refreshed_role.position,
        )
        return refreshed_role

    async def migrate_legacy_state(self) -> None:
        legacy_watch_state = self.state.legacy_watch_state
        if legacy_watch_state is None:
            return

        self.state.legacy_watch_state = None

        try:
            channel = await self.fetch_channel(legacy_watch_state.channel_id)
        except discord.HTTPException:
            LOGGER.warning(
                "Unable to resolve legacy watched channel %s. Dropping legacy state.",
                legacy_watch_state.channel_id,
            )
            self.state.save(self.state_path)
            return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            LOGGER.warning(
                "Legacy watched channel %s cannot host the picker message. "
                "Dropping legacy state.",
                legacy_watch_state.channel_id,
            )
            self.state.save(self.state_path)
            return

        try:
            await channel.fetch_message(legacy_watch_state.message_id)
        except discord.HTTPException:
            LOGGER.warning(
                "Legacy watched message %s is unavailable. Dropping legacy state.",
                legacy_watch_state.message_id,
            )
            self.state.save(self.state_path)
            return

        guild_id = channel.guild.id
        if self.state.get_guild_state(guild_id) is None:
            self.state.set_guild_state(guild_id, legacy_watch_state)
            LOGGER.info(
                "Migrated legacy watched message %s to guild %s.",
                legacy_watch_state.message_id,
                guild_id,
            )
        else:
            LOGGER.info(
                "Dropping legacy watched message %s because guild %s already has "
                "a guild-scoped picker.",
                legacy_watch_state.message_id,
                guild_id,
            )

        self.state.save(self.state_path)

    async def validate_guild_watch_state(
        self, guild_id: int, guild_watch_state: WatchState
    ) -> bool:
        try:
            channel = await self.fetch_channel(guild_watch_state.channel_id)
        except discord.HTTPException:
            LOGGER.warning(
                "Configured watched channel %s for guild %s could not be fetched.",
                guild_watch_state.channel_id,
                guild_id,
            )
            return False

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            LOGGER.warning(
                "Configured watched channel %s for guild %s cannot host the "
                "picker message.",
                guild_watch_state.channel_id,
                guild_id,
            )
            return False

        if channel.guild.id != guild_id:
            LOGGER.warning(
                "Configured watched channel %s belongs to guild %s, not guild %s.",
                guild_watch_state.channel_id,
                channel.guild.id,
                guild_id,
            )
            return False

        try:
            await channel.fetch_message(guild_watch_state.message_id)
        except discord.HTTPException:
            LOGGER.warning(
                "Configured watched message %s for guild %s could not be fetched.",
                guild_watch_state.message_id,
                guild_id,
            )
            return False

        LOGGER.info(
            "Watching configured message %s in guild %s channel %s.",
            guild_watch_state.message_id,
            guild_id,
            guild_watch_state.channel_id,
        )
        return True

    async def remove_other_member_reactions(
        self,
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
        member: discord.Member,
        keep_emoji: str,
    ) -> None:
        channel = guild.get_channel(channel_id)
        if channel is None and hasattr(guild, "get_thread"):
            channel = guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                LOGGER.exception("Unable to fetch channel %s.", channel_id)
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        permissions = channel.permissions_for(guild.me) if guild.me is not None else None
        LOGGER.info(
            "Starting reaction pruning for member=%s keep_emoji=%s channel=%s "
            "manage_messages=%s",
            member.id,
            keep_emoji,
            channel.id,
            permissions.manage_messages if permissions is not None else "unknown",
        )

        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            LOGGER.exception("Unable to fetch watched message %s.", message_id)
            return

        for reaction in message.reactions:
            reaction_emoji = str(reaction.emoji)
            if reaction_emoji == keep_emoji:
                LOGGER.info(
                    "Skipping kept reaction %s for member %s.",
                    reaction_emoji,
                    member.id,
                )
                continue

            LOGGER.info(
                "Inspecting reaction %s for member %s during pruning.",
                reaction_emoji,
                member.id,
            )

            found_member_reaction = False

            try:
                async for user in reaction.users(limit=None):
                    if user.id != member.id:
                        continue

                    found_member_reaction = True
                    self._ignored_reaction_removals.add(
                        (message.id, member.id, reaction_emoji)
                    )
                    await message.remove_reaction(reaction.emoji, member)
                    LOGGER.info(
                        "Removed reaction %s for member %s from watched message %s.",
                        reaction_emoji,
                        member.id,
                        message.id,
                    )
                    break
            except discord.HTTPException:
                LOGGER.exception(
                    "Unable to prune reaction %s for member %s. manage_messages=%s",
                    reaction_emoji,
                    member.id,
                    permissions.manage_messages if permissions is not None else "unknown",
                )

            if not found_member_reaction:
                LOGGER.info(
                    "Member %s did not have reaction %s on watched message %s.",
                    member.id,
                    reaction_emoji,
                    message.id,
                )

        LOGGER.info(
            "Finished reaction pruning for member=%s keep_emoji=%s message=%s",
            member.id,
            keep_emoji,
            message.id,
        )


@app_commands.command(
    name="here",
    description="Post the reaction message that assigns emoji color roles.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def here(interaction: discord.Interaction) -> None:
    client = interaction.client
    if not isinstance(client, ColorRoleBot):
        await interaction.response.send_message(
            "Bot is not configured correctly.",
            ephemeral=True,
        )
        return

    if interaction.guild is None or not isinstance(
        interaction.channel, (discord.TextChannel, discord.Thread)
    ):
        await interaction.response.send_message(
            "This command must be used in a server text channel.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await interaction.response.send_message(
            "Admins only.",
            ephemeral=True,
        )
        return

    LOGGER.info(
        "/here invoked: guild=%s (%s) channel=%s member=%s (%s) build=%s",
        interaction.guild.name,
        interaction.guild.id,
        interaction.channel.id,
        member,
        member.id,
        BUILD_ID,
    )

    if client.get_guild_watch_state(interaction.guild.id) is not None:
        LOGGER.info(
            "/here skipped because guild %s already has a picker configured.",
            interaction.guild.id,
        )
        await interaction.response.send_message("Already set!", ephemeral=True)
        return

    await interaction.response.send_message(
        "Creating the color role message here...",
        ephemeral=True,
    )

    try:
        message = await interaction.channel.send(WATCH_MESSAGE_TEXT)
    except discord.HTTPException:
        LOGGER.exception(
            "Unable to send watched picker message in guild %s channel %s.",
            interaction.guild.id,
            interaction.channel.id,
        )
        await interaction.followup.send(
            "I couldn't post the picker message in this channel.",
            ephemeral=True,
        )
        return

    client.state.set_guild_state(
        interaction.guild.id,
        WatchState(channel_id=interaction.channel.id, message_id=message.id),
    )
    try:
        client.state.save(client.state_path)
    except OSError:
        LOGGER.exception(
            "Unable to save picker state for guild %s after posting message %s.",
            interaction.guild.id,
            message.id,
        )
        await interaction.followup.send(
            "I posted the message, but saving the picker state failed.",
            ephemeral=True,
        )
        return

    LOGGER.info(
        "/here configured picker successfully: guild=%s channel=%s message=%s",
        interaction.guild.id,
        interaction.channel.id,
        message.id,
    )

    await interaction.followup.send("Color role message created.", ephemeral=True)


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN")

    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing from the environment.")

    bot = ColorRoleBot()
    try:
        bot.run(token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        LOGGER.error(
            "Discord rejected the connection because Server Members Intent is "
            "enabled in code but not in the Discord developer portal."
        )
        LOGGER.error(
            "Open your application in the Discord developer portal, go to Bot, "
            "enable Server Members Intent, save, and then redeploy Railway."
        )
        raise


if __name__ == "__main__":
    main()
