# ColorSeg

Discord bot that turns emoji reactions into shared name-color roles.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `DISCORD_BOT_TOKEN`.
4. In the Discord developer portal, enable:
   - `Server Members Intent`
5. Run the bot:

   ```bash
   python bot.py
   ```

## Usage

- Invite the bot with permissions to manage roles and read message history.
- Run `/here` once in the channel that should host the picker message.
- Users react to that message with a standard Unicode emoji to get a shared `color-{emoji}` role.

## Notes

- The picker message location is persisted in `state.json`.
- Global slash command sync can take a little time to appear in Discord.
- Custom server emojis are ignored because the bot derives colors from Twemoji PNG assets.
- For Railway deployments, set `DISCORD_BOT_TOKEN` in service variables and enable `Server Members Intent` in the Discord developer portal.
