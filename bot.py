import asyncio

import discord
from discord.ext import commands

from config import config_manager, get_token
from utils.ffmpeg_helper import ensure_ffmpeg
from utils.health_monitor import HealthMonitor
from web.server import maybe_start_management_server, ManagementServer


COGS = [
    "admin",
    "tickets",
    "verification",
    "music",
]


def dynamic_prefix(_bot: commands.Bot, _message: discord.Message):
    return config_manager.get("command_prefix", "!")


class PrimeBot(commands.Bot):
    def __init__(self) -> None:
        config = config_manager
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        if config.get("enable_message_content_intent", True):
            intents.message_content = True
        if config.get("enable_members_intent", True):
            intents.members = True
        if config.get("enable_presence_intent", False):
            intents.presences = True
        super().__init__(command_prefix=dynamic_prefix, intents=intents)
        self.config = config
        self.health_monitor = HealthMonitor(self)

    async def setup_hook(self) -> None:
        for extension in COGS:
            try:
                await self.load_extension(f"cogs.{extension}")
            except Exception as exc:
                print(f"Fehler beim Laden von {extension}: {exc}")
        guild_id = self.config.get_int("guild_id")
        if guild_id:
            guild_obj = discord.Object(id=guild_id)
            await self.tree.sync(guild=guild_obj)
            print(f"Slash-Commands ausschließlich für Guild {guild_id} synchronisiert.")
        else:
            await self.tree.sync()
            print("Globale Slash-Commands synchronisiert (kann bis zu 1h dauern).")
        await self.health_monitor.start()

    async def on_ready(self) -> None:
        print(f"Bot ist online als {self.user} (Guilds: {len(self.guilds)})")

    async def close(self) -> None:
        await self.health_monitor.shutdown()
        await super().close()


async def main():
    ensure_ffmpeg(config_manager)
    bot = PrimeBot()
    management: ManagementServer | None = None
    try:
        management = await maybe_start_management_server(bot, config_manager)
    except Exception as exc:
        print(f"Management-Webinterface konnte nicht gestartet werden: {exc}")
    try:
        await bot.start(get_token())
    finally:
        if management:
            await management.stop()


if __name__ == "__main__":
    asyncio.run(main())
