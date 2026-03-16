import asyncio
import io
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


@dataclass
class WatchState:
    channel_id: Optional[int] = None
    message_id: Optional[int] = None

    @classmethod
    def load(cls, path: Path) -> "WatchState":
        if not path.exists():
            return cls()

        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Unable to load %s. Starting with an empty state.", path)
            return cls()

        return cls(
            channel_id=data.get("channel_id"),
            message_id=data.get("message_id"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "channel_id": self.channel_id,
                    "message_id": self.message_id,
                },
                handle,
                indent=2,
            )

    @property
    def configured(self) -> bool:
        return self.channel_id is not None and self.message_id is not None

    def matches(self, channel_id: int, message_id: int) -> bool:
        return (
            self.configured
            and self.channel_id == channel_id
            and self.message_id == message_id
        )


def color_role_name(emoji: str) -> str:
    return f"{COLOR_ROLE_PREFIX}{emoji}"


def is_color_role(role: discord.Role) -> bool:
    return role.name.startswith(COLOR_ROLE_PREFIX)


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
        self.watch_state = WatchState.load(self.state_path)
        self._resume_checked = False
        self._ignored_reaction_removals: set[tuple[int, int, str]] = set()

    async def setup_hook(self) -> None:
        self.tree.add_command(here)
        synced = await self.tree.sync()
        LOGGER.info("Synced %s application command(s).", len(synced))

    async def on_ready(self) -> None:
        if self.user is None:
            return

        LOGGER.info("Logged in as %s (%s)", self.user, self.user.id)

        if self._resume_checked:
            return

        self._resume_checked = True

        if not self.watch_state.configured:
            LOGGER.info("No watched message configured yet. Using state file %s.", self.state_path)
            return

        try:
            channel = await self.fetch_channel(self.watch_state.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                LOGGER.warning(
                    "Configured channel %s cannot host the watched message.",
                    self.watch_state.channel_id,
                )
                return

            await channel.fetch_message(self.watch_state.message_id)
            LOGGER.info(
                "Watching configured message %s in channel %s.",
                self.watch_state.message_id,
                self.watch_state.channel_id,
            )
        except discord.HTTPException:
            LOGGER.exception("Unable to fetch the configured watched message.")

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if self.user is not None and payload.user_id == self.user.id:
            return

        if payload.guild_id is None or not self.watch_state.matches(
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

        role = await self.get_or_create_color_role(guild, emoji, rgb)
        if role is None:
            return

        old_roles = [
            existing_role
            for existing_role in member.roles
            if is_color_role(existing_role) and existing_role.id != role.id
        ]

        if old_roles:
            try:
                await member.remove_roles(
                    *old_roles,
                    reason="Swapping member to a new emoji color role.",
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "Unable to remove old color roles from member %s.",
                    member.id,
                )
                return

        if role not in member.roles:
            try:
                await member.add_roles(
                    role,
                    reason="Assigning emoji color role from watched reaction message.",
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "Unable to add role %s to member %s.",
                    role.id,
                    member.id,
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

        if payload.guild_id is None or not self.watch_state.matches(
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
    ) -> Optional[discord.Role]:
        existing_role = discord.utils.get(guild.roles, name=color_role_name(emoji))
        if existing_role is not None:
            return existing_role

        try:
            role = await guild.create_role(
                name=color_role_name(emoji),
                colour=discord.Color.from_rgb(*rgb),
                reason="Creating emoji color role.",
            )
        except discord.HTTPException:
            LOGGER.exception("Unable to create color role for %s.", emoji)
            return None

        me = guild.me or guild.get_member(self.user.id if self.user else 0)
        if me is not None:
            target_position = max(1, me.top_role.position - 1)
            try:
                await role.edit(
                    position=target_position,
                    reason="Placing new color role below the bot's top role.",
                )
            except discord.HTTPException:
                LOGGER.exception("Unable to position role %s.", role.name)

        return role

    async def delete_role_if_unused(self, role: discord.Role) -> None:
        if role.members:
            return

        try:
            await role.delete(reason="Deleting unused emoji color role.")
        except discord.HTTPException:
            LOGGER.exception("Unable to delete unused role %s.", role.name)

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

        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            LOGGER.exception("Unable to fetch watched message %s.", message_id)
            return

        for reaction in message.reactions:
            reaction_emoji = str(reaction.emoji)
            if reaction_emoji == keep_emoji:
                continue

            try:
                async for user in reaction.users(limit=None):
                    if user.id != member.id:
                        continue

                    self._ignored_reaction_removals.add(
                        (message.id, member.id, reaction_emoji)
                    )
                    await message.remove_reaction(reaction.emoji, member)
                    break
            except discord.HTTPException:
                LOGGER.exception(
                    "Unable to prune reaction %s for member %s.",
                    reaction_emoji,
                    member.id,
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

    if client.watch_state.configured:
        await interaction.response.send_message("Already set!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    message = await interaction.channel.send(WATCH_MESSAGE_TEXT)

    client.watch_state.channel_id = interaction.channel.id
    client.watch_state.message_id = message.id
    client.watch_state.save(client.state_path)

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
