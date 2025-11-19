from typing import Literal

import discord
from discord.ext import commands

from config import config_manager, CONFIG_SCHEMA

CHANNEL_CHOICES = {
    "ticket": "ticket_channel_id",
    "ticket_panel": "ticket_panel_channel_id",
    "ticket_queue": "ticket_queue_channel_id",
    "verify": "verify_channel_id",
    "music": "music_channel_id",
    "music_log": "music_log_channel_id",
}

ROLE_CHOICES = {
    "admin": "admin_role_id",
    "verified": "verified_role_id",
    "dj": "dj_role_id",
}


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = config_manager

    async def cog_check(self, ctx: commands.Context) -> bool:
        return bool(ctx.author.guild_permissions.administrator)

    async def _respond(self, ctx: commands.Context, message: str | None = None, *, embed: discord.Embed | None = None) -> None:
        content = message or ""
        if not content and embed is None:
            content = "✔️"
        if ctx.interaction:
            if not ctx.interaction.response.is_done():
                if embed:
                    await ctx.interaction.response.send_message(content, embed=embed, ephemeral=True)
                else:
                    await ctx.interaction.response.send_message(content, ephemeral=True)
            else:
                if embed:
                    await ctx.interaction.followup.send(content, embed=embed, ephemeral=True)
                else:
                    await ctx.interaction.followup.send(content, ephemeral=True)
        else:
            if embed:
                await ctx.reply(content, embed=embed)
            else:
                await ctx.reply(content)

    @commands.hybrid_group(name="config", description="Verwalte Bot-Einstellungen")
    @commands.has_permissions(administrator=True)
    async def config_group(self, ctx: commands.Context) -> None:
        if not ctx.invoked_subcommand:
            await self._respond(ctx, "Nutze /config <subcommand> – z.B. /config show")

    @config_group.command(name="show", description="Aktive Konfiguration anzeigen")
    async def config_show(self, ctx: commands.Context) -> None:
        embed = discord.Embed(title="Aktive Konfiguration", color=discord.Color.blurple())
        guild = ctx.guild
        for key, meta in CONFIG_SCHEMA.items():
            value = self.config.get(key)
            pretty = self._pretty_value(guild, key, value)
            embed.add_field(name=key, value=pretty, inline=False)
        await self._respond(ctx, "", embed=embed)

    @config_group.command(name="setchannel", description="Setze einen Zielkanal")
    async def config_set_channel(
        self,
        ctx: commands.Context,
        target: Literal["ticket", "ticket_panel", "ticket_queue", "verify", "music"],
        channel: discord.TextChannel,
    ) -> None:
        key = CHANNEL_CHOICES[target]
        self.config.set_value(key, channel.id)
        await self._respond(ctx, f"{target} Kanal gesetzt auf {channel.mention}")

    @config_group.command(name="setrole", description="Setze eine Rolle")
    async def config_set_role(
        self,
        ctx: commands.Context,
        target: Literal["admin", "verified", "dj"],
        role: discord.Role,
    ) -> None:
        key = ROLE_CHOICES[target]
        self.config.set_value(key, role.id)
        await self._respond(ctx, f"{target}-Rolle gesetzt auf {role.mention}")

    @config_group.command(name="setvalue", description="Allgemeinen Wert setzen")
    async def config_set_value(self, ctx: commands.Context, key: str, value: str) -> None:
        key = key.lower()
        if key not in CONFIG_SCHEMA:
            await self._respond(ctx, f"Unbekannter Schlüssel: {key}")
            return
        self.config.set_value(key, value)
        await self._respond(ctx, f"{key} aktualisiert.")

    @config_group.command(name="reload", description="Konfiguration von Disk laden")
    async def config_reload(self, ctx: commands.Context) -> None:
        self.config.load()
        await self._respond(ctx, "Konfiguration neu geladen.")

    @config_group.command(name="reset", description="Alle Werte auf Default setzen")
    async def config_reset(self, ctx: commands.Context) -> None:
        self.config.reset()
        await self._respond(ctx, "Konfiguration zurückgesetzt. Bitte setze die Werte neu.")

    def _pretty_value(self, guild: discord.Guild | None, key: str, value) -> str:
        if not value:
            return "(nicht gesetzt)"
        if key.endswith("_channel_id") and guild:
            channel = guild.get_channel(int(value))
            return channel.mention if channel else str(value)
        if key.endswith("_role_id") and guild:
            role = guild.get_role(int(value))
            return role.mention if role else str(value)
        return str(value)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
