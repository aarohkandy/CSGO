import asyncio
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "venice/uncensored:free"
ROAST_HISTORY_SCAN_LIMIT = 400
ROAST_HISTORY_MESSAGE_LIMIT = 25
ROAST_MESSAGE_CHAR_LIMIT = 300
ROAST_HISTORY_CHAR_LIMIT = 6000
ROAST_MAX_TOKENS = 220
ROAST_REQUEST_TIMEOUT_SECONDS = 30
URL_PATTERN = re.compile(r"https?://\S+")


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


@dataclass
class RoastHistorySnapshot:
    messages: list[str]
    scanned_messages: int
    author_messages_seen: int

    @property
    def kept_messages(self) -> int:
        return len(self.messages)

    @property
    def total_chars(self) -> int:
        return roast_history_char_count(self.messages)


class RoastGenerationError(Exception):
    def __init__(
        self,
        user_message: str,
        *,
        status_code: Optional[int] = None,
        provider_detail: Optional[str] = None,
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code
        self.provider_detail = provider_detail


@dataclass
class RoastGenerationResult:
    text: str
    resolved_model: Optional[str] = None


@dataclass
class RoastDebugSnapshot:
    timestamp: str
    guild_id: int
    channel_id: int
    member_id: int
    requested_model: str
    scanned_messages: int
    author_messages_seen: int
    kept_messages: int
    prompt_chars: int
    status_code: Optional[int] = None
    resolved_model: Optional[str] = None
    error_message: Optional[str] = None
    provider_detail: Optional[str] = None


def get_openrouter_api_key() -> Optional[str]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if api_key is None:
        return None

    api_key = api_key.strip()
    return api_key or None


def get_openrouter_model() -> str:
    model = os.getenv("OPENROUTER_MODEL")
    if model is None:
        return DEFAULT_OPENROUTER_MODEL

    model = model.strip()
    return model or DEFAULT_OPENROUTER_MODEL


def get_command_guild_id() -> Optional[int]:
    return parse_snowflake(os.getenv("COMMAND_GUILD_ID"))


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


def normalize_roast_message(content: str) -> str:
    normalized = URL_PATTERN.sub("", content)
    normalized = " ".join(normalized.split())
    if len(normalized) > ROAST_MESSAGE_CHAR_LIMIT:
        normalized = normalized[: ROAST_MESSAGE_CHAR_LIMIT - 3].rstrip() + "..."

    return normalized


def roast_history_char_count(messages: list[str]) -> int:
    if not messages:
        return 0

    numbered_messages = [f"{index + 1}. {message}" for index, message in enumerate(messages)]
    return sum(len(message) for message in numbered_messages) + (len(numbered_messages) - 1)


def trim_roast_history(messages: list[str]) -> list[str]:
    trimmed_messages = list(messages)
    while trimmed_messages and roast_history_char_count(trimmed_messages) > ROAST_HISTORY_CHAR_LIMIT:
        trimmed_messages.pop(0)

    return trimmed_messages


def format_roast_history(messages: list[str]) -> str:
    return "\n".join(f"{index + 1}. {message}" for index, message in enumerate(messages))


def extract_openrouter_error_detail(response: requests.Response) -> Optional[str]:
    try:
        response_data = response.json()
    except ValueError:
        response_text = response.text.strip()
        return response_text[:500] if response_text else None

    if not isinstance(response_data, dict):
        return None

    error = response_data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

        metadata = error.get("metadata")
        if isinstance(metadata, dict):
            raw_message = metadata.get("raw")
            if isinstance(raw_message, str) and raw_message.strip():
                return raw_message.strip()

    message = response_data.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()

    return None


def extract_roast_text(response_data: Any) -> Optional[str]:
    if not isinstance(response_data, dict):
        return None

    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None

    message = first_choice.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if isinstance(content, str):
        normalized = " ".join(content.split())
        return normalized or None

    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue

        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)

    if not parts:
        return None

    normalized = " ".join(" ".join(parts).split())
    return normalized or None


def request_openrouter_roast(
    *,
    api_key: str,
    model: str,
    member_name: str,
    history_messages: list[str],
) -> RoastGenerationResult:
    system_prompt = (
        "You are a savage Discord roast comic. Write exactly one paragraph of 3 to 5 "
        "sentences. Make it sharp, mocking, and specific to the supplied messages. "
        "Target the person's habits, contradictions, try-hard energy, repetitive "
        "obsessions, awkward wording, and embarrassing priorities that show up in the "
        "messages. Do not soften the jokes, do not give advice, do not compliment them, "
        "do not add a preamble, do not mention being an AI, do not use bullet points, "
        "and do not invent facts not grounded in the messages. Avoid just listing bio "
        "facts unless the supplied messages themselves make those facts embarrassing."
    )
    user_prompt = (
        f"Roast {member_name} based only on these recent Discord messages from the "
        "current channel. Keep it as one paragraph. Make it cutting and funny, not "
        "generic, and do not sound supportive or polite.\n\n"
        f"{format_roast_history(history_messages)}"
    )
    payload = {
        "model": model,
        "temperature": 1.1,
        "max_tokens": ROAST_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "ColorSeg",
    }

    try:
        response = requests.post(
            OPENROUTER_API_URL,
            headers=headers,
            json=payload,
            timeout=ROAST_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RoastGenerationError(
            "I couldn't reach the roast model right now. Try again in a bit."
        ) from exc

    provider_detail = extract_openrouter_error_detail(response)
    if response.status_code == 401:
        raise RoastGenerationError(
            "The roast model API key is invalid or missing.",
            status_code=response.status_code,
            provider_detail=provider_detail,
        )
    if response.status_code == 429:
        raise RoastGenerationError(
            "The free roast model is rate-limited right now. Try again in a bit.",
            status_code=response.status_code,
            provider_detail=provider_detail,
        )
    if response.status_code >= 500:
        raise RoastGenerationError(
            "The roast model provider is having issues right now.",
            status_code=response.status_code,
            provider_detail=provider_detail,
        )
    if response.status_code >= 400:
        user_message = "The roast model rejected the request."
        if response.status_code == 404:
            user_message = (
                "The selected roast model is not available on OpenRouter right now."
            )
        raise RoastGenerationError(
            user_message,
            status_code=response.status_code,
            provider_detail=provider_detail,
        )

    try:
        response_data = response.json()
    except ValueError as exc:
        raise RoastGenerationError(
            "The roast model returned an unreadable response.",
            status_code=response.status_code,
            provider_detail=provider_detail,
        ) from exc

    roast_text = extract_roast_text(response_data)
    if roast_text is None:
        raise RoastGenerationError(
            "The roast model returned an empty response.",
            status_code=response.status_code,
            provider_detail=provider_detail,
        )

    resolved_model = response_data.get("model")
    if not isinstance(resolved_model, str):
        resolved_model = None

    return RoastGenerationResult(text=roast_text, resolved_model=resolved_model)


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
        intents.message_content = True

        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.state_path = get_state_path()
        self.state = BotState.load(self.state_path)
        self._resume_checked = False
        self._ignored_reaction_removals: set[tuple[int, int, str]] = set()
        self.last_roast_debug: Optional[RoastDebugSnapshot] = None

    async def setup_hook(self) -> None:
        self.tree.add_command(here)
        self.tree.add_command(roast)
        self.tree.add_command(test)
        command_guild_id = get_command_guild_id()
        if command_guild_id is not None:
            synced = await self.tree.sync(guild=discord.Object(id=command_guild_id))
            LOGGER.info(
                "Synced %s application command(s) to guild %s.",
                len(synced),
                command_guild_id,
            )
            return

        synced = await self.tree.sync()
        LOGGER.info("Synced %s application command(s) globally.", len(synced))

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
        manageable_roles_bottom_up = [
            existing_role
            for existing_role in guild.roles
            if existing_role.id not in {guild.default_role.id, me.top_role.id}
            and existing_role.position < me.top_role.position
        ]
        manageable_roles_top_down = list(reversed(manageable_roles_bottom_up))
        current_top_manageable_role = (
            manageable_roles_top_down[0] if manageable_roles_top_down else None
        )

        LOGGER.info(
            "Preparing to position role %s under bot_top=%s target_position=%s",
            describe_role(role),
            describe_role(me.top_role),
            target_position,
        )
        LOGGER.info(
            "Current manageable order below bot: %s",
            [describe_role(existing_role) for existing_role in manageable_roles_top_down],
        )

        if (
            role.position == target_position
            and current_top_manageable_role is not None
            and current_top_manageable_role.id == role.id
        ):
            LOGGER.info(
                "Role %s is already the highest manageable role below the bot.",
                describe_role(role),
            )
            return role

        try:
            other_manageable_roles_top_down = [
                existing_role
                for existing_role in manageable_roles_top_down
                if existing_role.id != role.id
            ]
            desired_order_top_down = [role, *other_manageable_roles_top_down]
            desired_positions: dict[discord.Role, int] = {}
            next_position = target_position
            for ordered_role in desired_order_top_down:
                desired_positions[ordered_role] = next_position
                next_position -= 1

            LOGGER.info(
                "Applying explicit manageable role order: %s",
                [
                    f"{describe_role(ordered_role)} -> {desired_positions[ordered_role]}"
                    for ordered_role in desired_order_top_down
                ],
            )
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

    async def collect_roast_history(
        self,
        channel: discord.TextChannel | discord.Thread,
        member: discord.Member,
    ) -> RoastHistorySnapshot:
        scanned_messages = 0
        author_messages_seen = 0
        collected_messages: list[str] = []

        async for message in channel.history(
            limit=ROAST_HISTORY_SCAN_LIMIT,
            oldest_first=False,
        ):
            scanned_messages += 1
            if message.author.id != member.id:
                continue

            author_messages_seen += 1
            normalized = normalize_roast_message(message.clean_content)
            if not normalized:
                continue

            collected_messages.append(normalized)
            if len(collected_messages) >= ROAST_HISTORY_MESSAGE_LIMIT:
                break

        collected_messages.reverse()
        return RoastHistorySnapshot(
            messages=trim_roast_history(collected_messages),
            scanned_messages=scanned_messages,
            author_messages_seen=author_messages_seen,
        )

    async def generate_roast(
        self,
        member: discord.Member,
        history_snapshot: RoastHistorySnapshot,
    ) -> RoastGenerationResult:
        api_key = get_openrouter_api_key()
        if api_key is None:
            raise RoastGenerationError(
                "OPENROUTER_API_KEY is missing from the environment."
            )

        model = get_openrouter_model()
        try:
            return await asyncio.to_thread(
                request_openrouter_roast,
                api_key=api_key,
                model=model,
                member_name=member.display_name,
                history_messages=history_snapshot.messages,
            )
        except RoastGenerationError:
            raise
        except Exception as exc:
            raise RoastGenerationError(
                "The roast model failed unexpectedly."
            ) from exc


def can_send_in_channel(
    channel: discord.TextChannel | discord.Thread,
    permissions: discord.Permissions,
) -> bool:
    if isinstance(channel, discord.Thread):
        return permissions.send_messages_in_threads or permissions.send_messages

    return permissions.send_messages


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


@app_commands.command(
    name="roast",
    description="Roast yourself based on your recent messages in this channel.",
)
@app_commands.guild_only()
async def roast(interaction: discord.Interaction) -> None:
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
    if not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "I couldn't resolve your server member information.",
            ephemeral=True,
        )
        return

    guild_member = interaction.guild.me
    if guild_member is None:
        await interaction.response.send_message(
            "I couldn't resolve my server permissions right now.",
            ephemeral=True,
        )
        return

    permissions = interaction.channel.permissions_for(guild_member)
    if not permissions.view_channel or not permissions.read_message_history:
        await interaction.response.send_message(
            "I need View Channel and Read Message History here before I can roast you.",
            ephemeral=True,
        )
        return

    if not can_send_in_channel(interaction.channel, permissions):
        await interaction.response.send_message(
            "I need permission to send messages in this channel before I can roast you.",
            ephemeral=True,
        )
        return

    if not client.intents.message_content:
        await interaction.response.send_message(
            "Message Content Intent needs to be enabled before I can read your messages.",
            ephemeral=True,
        )
        return

    LOGGER.info(
        "/roast invoked: guild=%s (%s) channel=%s member=%s (%s) build=%s",
        interaction.guild.name,
        interaction.guild.id,
        interaction.channel.id,
        member,
        member.id,
        BUILD_ID,
    )

    await interaction.response.send_message(
        "Cooking up your roast...",
        ephemeral=True,
    )

    try:
        history_snapshot = await client.collect_roast_history(interaction.channel, member)
    except discord.Forbidden:
        LOGGER.exception(
            "Missing permissions while reading history for /roast in guild %s channel %s.",
            interaction.guild.id,
            interaction.channel.id,
        )
        await interaction.followup.send(
            "I couldn't read message history in this channel.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        LOGGER.exception(
            "Unable to collect roast history in guild %s channel %s.",
            interaction.guild.id,
            interaction.channel.id,
        )
        await interaction.followup.send(
            "I couldn't read enough history from this channel to roast you right now.",
            ephemeral=True,
        )
        return

    LOGGER.info(
        "Collected roast history: guild=%s channel=%s member=%s scanned_messages=%s "
        "author_messages_seen=%s kept_messages=%s prompt_chars=%s model=%s",
        interaction.guild.id,
        interaction.channel.id,
        member.id,
        history_snapshot.scanned_messages,
        history_snapshot.author_messages_seen,
        history_snapshot.kept_messages,
        history_snapshot.total_chars,
        get_openrouter_model(),
    )

    client.last_roast_debug = RoastDebugSnapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        guild_id=interaction.guild.id,
        channel_id=interaction.channel.id,
        member_id=member.id,
        requested_model=get_openrouter_model(),
        scanned_messages=history_snapshot.scanned_messages,
        author_messages_seen=history_snapshot.author_messages_seen,
        kept_messages=history_snapshot.kept_messages,
        prompt_chars=history_snapshot.total_chars,
    )

    if not history_snapshot.messages:
        if history_snapshot.author_messages_seen > 0:
            client.last_roast_debug.error_message = "No usable message text was found."
            await interaction.followup.send(
                "I found your recent posts, but I couldn't read any usable message text. "
                "Make sure Message Content Intent is enabled and try again.",
                ephemeral=True,
            )
            return

        client.last_roast_debug.error_message = "No recent messages were found."
        await interaction.followup.send(
            "I couldn't find any recent messages from you in this channel to roast.",
            ephemeral=True,
        )
        return

    try:
        roast_result = await client.generate_roast(member, history_snapshot)
    except RoastGenerationError as exc:
        if client.last_roast_debug is not None:
            client.last_roast_debug.status_code = exc.status_code
            client.last_roast_debug.error_message = exc.user_message
            client.last_roast_debug.provider_detail = exc.provider_detail
        LOGGER.warning(
            "OpenRouter roast request failed: guild=%s channel=%s member=%s model=%s "
            "status_code=%s message=%s provider_detail=%s",
            interaction.guild.id,
            interaction.channel.id,
            member.id,
            get_openrouter_model(),
            exc.status_code,
            exc.user_message,
            exc.provider_detail,
        )
        await interaction.followup.send(exc.user_message, ephemeral=True)
        return

    if client.last_roast_debug is not None:
        client.last_roast_debug.status_code = 200
        client.last_roast_debug.resolved_model = roast_result.resolved_model

    LOGGER.info(
        "OpenRouter roast request succeeded: guild=%s channel=%s member=%s requested_model=%s "
        "resolved_model=%s",
        interaction.guild.id,
        interaction.channel.id,
        member.id,
        get_openrouter_model(),
        roast_result.resolved_model or "unknown",
    )

    escaped_roast_text = discord.utils.escape_mentions(roast_result.text)
    try:
        await interaction.channel.send(
            f"{member.mention} {escaped_roast_text}",
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True,
                replied_user=False,
            ),
        )
    except discord.HTTPException:
        LOGGER.exception(
            "Unable to send public roast message in guild %s channel %s for member %s.",
            interaction.guild.id,
            interaction.channel.id,
            member.id,
        )
        await interaction.followup.send(
            "I generated the roast, but I couldn't post it in this channel.",
            ephemeral=True,
        )


@app_commands.command(
    name="test",
    description="Show the latest roast debug info for this bot instance.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def test(interaction: discord.Interaction) -> None:
    client = interaction.client
    if not isinstance(client, ColorRoleBot):
        await interaction.response.send_message(
            "Bot is not configured correctly.",
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

    debug_snapshot = client.last_roast_debug
    if debug_snapshot is None:
        await interaction.response.send_message(
            "No roast debug info has been recorded yet on this bot instance.",
            ephemeral=True,
        )
        return

    debug_lines = [
        f"build: {BUILD_ID}",
        f"timestamp: {debug_snapshot.timestamp}",
        f"command_guild_id: {get_command_guild_id() or 'global'}",
        f"guild_id: {debug_snapshot.guild_id}",
        f"channel_id: {debug_snapshot.channel_id}",
        f"member_id: {debug_snapshot.member_id}",
        f"requested_model: {debug_snapshot.requested_model}",
        f"resolved_model: {debug_snapshot.resolved_model or 'none'}",
        f"status_code: {debug_snapshot.status_code if debug_snapshot.status_code is not None else 'none'}",
        f"scanned_messages: {debug_snapshot.scanned_messages}",
        f"author_messages_seen: {debug_snapshot.author_messages_seen}",
        f"kept_messages: {debug_snapshot.kept_messages}",
        f"prompt_chars: {debug_snapshot.prompt_chars}",
        f"message_content_intent: {client.intents.message_content}",
        f"api_key_present: {bool(get_openrouter_api_key())}",
        f"error_message: {debug_snapshot.error_message or 'none'}",
        f"provider_detail: {debug_snapshot.provider_detail or 'none'}",
    ]
    await interaction.response.send_message(
        "```text\n" + "\n".join(debug_lines) + "\n```",
        ephemeral=True,
    )


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
            "Discord rejected the connection because one or more privileged intents "
            "enabled in code are not enabled in the Discord developer portal."
        )
        LOGGER.error(
            "Open your application in the Discord developer portal, go to Bot, "
            "enable Server Members Intent and Message Content Intent, save, and then "
            "redeploy Railway."
        )
        raise


if __name__ == "__main__":
    main()
