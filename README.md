# ColorSeg

Discord bot that turns emoji reactions into shared name-color roles and can roast users based on their recent channel history.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `DISCORD_BOT_TOKEN` and `OPENROUTER_API_KEY`.
4. Optional: set `STATE_FILE_PATH` if you want `state.json` stored somewhere else.
5. In the Discord developer portal, enable:
   - `Server Members Intent`
   - `Message Content Intent`
6. Run the bot:

   ```bash
   python bot.py
   ```

## Usage

- Invite the bot with permissions to manage roles, read message history, and send messages.
- Run `/here` once per server in the channel that should host the picker message.
- Users react to that message with a standard Unicode emoji to get a shared `color-{emoji}` role.
- Run `/roast` in any server text channel or thread to roast yourself based on your recent messages in that channel.
- Run `/test` as a server admin to see the latest in-memory roast debug snapshot for that bot instance.

## Notes

- Picker message locations are persisted per server in `state.json`.
- `STATE_FILE_PATH` can point at a persistent location, which is useful for hosted deployments.
- `OPENROUTER_MODEL` is optional and defaults to `venice/uncensored:free`.
- `COMMAND_GUILD_ID` is optional. Set it to your test server ID to sync slash commands only to that guild for fast iteration; leave it unset for global sync.
- OpenRouter free models are useful for small testing but can still rate-limit or temporarily lose routing.
- Global slash command sync can take a little time to appear in Discord when `COMMAND_GUILD_ID` is unset.
- Custom server emojis are ignored because the bot derives colors from Twemoji PNG assets.
- Color roles are moved as high as the bot can manage, directly under the bot's highest role. If another role still overrides the name color, move the bot's role higher in Discord's role list.
- `/roast` only uses the invoking user's recent messages from the current channel and does not store roast history.
- For Railway deployments, set `DISCORD_BOT_TOKEN`, `OPENROUTER_API_KEY`, and optionally `OPENROUTER_MODEL` / `COMMAND_GUILD_ID` in service variables. Enable both `Server Members Intent` and `Message Content Intent` in the Discord developer portal.
