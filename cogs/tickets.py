from __future__ import annotations

import datetime as dt
from typing import cast

import discord
from discord.ext import commands
from discord.utils import utcnow

from config import config_manager


def build_ticket_panel_embed(admin_role_id: int | None) -> discord.Embed:
    description = (
        "Braucht du Hilfe oder Support?\n\n"
        "1️⃣ Klicke auf **Ticket öffnen**\n"
        "2️⃣ Beschreibe dein Anliegen im Thread\n"
        "3️⃣ Warte, bis ein Teammitglied antwortet"
    )
    embed = discord.Embed(title="Support Tickets", description=description, color=discord.Color.orange())
    if admin_role_id:
        embed.add_field(name="Team informiert", value=f"<@&{admin_role_id}> wird benachrichtigt.", inline=False)
    embed.set_footer(text="Missbrauch führt zum Ausschluss aus dem Ticketsystem.")
    embed.timestamp = utcnow()
    return embed


class TicketPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Ticket öffnen", style=discord.ButtonStyle.green, custom_id="prime_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        config = self.bot.config
        ticket_channel_id = config.get_int("ticket_channel_id")
        ticket_queue_channel_id = config.get_int("ticket_queue_channel_id")
        admin_role_id = config.get_int("admin_role_id")

        if not ticket_channel_id:
            await interaction.response.send_message("Ticket-Kanal ist nicht konfiguriert.", ephemeral=True)
            return

        ticket_channel = guild.get_channel(ticket_channel_id)
        if not isinstance(ticket_channel, discord.TextChannel):
            await interaction.response.send_message("Ticket-Kanal konnte nicht gefunden werden.", ephemeral=True)
            return

        opener = interaction.user
        thread_name = f"ticket-{opener.name}-{dt.datetime.utcnow().strftime('%H%M%S')}"
        thread = await ticket_channel.create_thread(
            name=thread_name,
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        await thread.add_user(opener)
        await thread.send(f"Ticket erstellt von {opener.mention}. Bitte warte, bis ein Admin es übernimmt.")
        admin_role = guild.get_role(admin_role_id)
        if admin_role:
            for member in guild.members:
                if admin_role in member.roles:
                    try:
                        await thread.add_user(member)
                    except discord.HTTPException:
                        continue

        if ticket_queue_channel_id:
            queue_channel = guild.get_channel(ticket_queue_channel_id)
            if isinstance(queue_channel, discord.TextChannel):
                embed = discord.Embed(
                    title="Neues Ticket",
                    description=f"{thread.mention} wurde von {opener.mention} erstellt.",
                    color=discord.Color.green(),
                )
                if admin_role_id:
                    embed.add_field(name="Erwartete Rolle", value=f"<@&{admin_role_id}>", inline=False)
                embed.timestamp = dt.datetime.utcnow()
                view = TicketClaimView(self.bot, thread.id, opener.id)
                await queue_channel.send(embed=embed, view=view)

        await interaction.response.send_message(f"Ticket erstellt: {thread.mention}", ephemeral=True)


class TicketClaimView(discord.ui.View):
    def __init__(self, bot: commands.Bot, thread_id: int, opener_id: int) -> None:
        super().__init__(timeout=86400)
        self.bot = bot
        self.thread_id = thread_id
        self.opener_id = opener_id

    @discord.ui.button(label="Ticket übernehmen", style=discord.ButtonStyle.primary)
    async def claim(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        member = cast(discord.Member, interaction.user)
        admin_role_id = self.bot.config.get_int("admin_role_id")
        if admin_role_id and not any(role.id == admin_role_id for role in member.roles):
            await interaction.response.send_message("Du hast keine Rechte für Tickets.", ephemeral=True)
            return
        thread = guild.get_thread(self.thread_id)
        if not thread:
            await interaction.response.send_message("Ticket existiert nicht mehr.", ephemeral=True)
            return
        opener = guild.get_member(self.opener_id)
        if opener:
            await thread.add_user(opener)
        await thread.add_user(member)
        await interaction.response.send_message(f"{thread.mention} übernommen.", ephemeral=True)
        await thread.send(f"{member.mention} hat dieses Ticket übernommen.")


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = config_manager

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanelView(self.bot))

    @commands.hybrid_command(name="ticketpanel", description="Sendet das Ticket-Panel in einen Kanal")
    @commands.has_permissions(manage_guild=True)
    async def ticket_panel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        target = channel or ctx.guild.get_channel(self.config.get_int("ticket_panel_channel_id"))
        if not isinstance(target, discord.TextChannel):
            await ctx.reply("Kein Zielkanal für das Panel konfiguriert.")
            return
        embed = build_ticket_panel_embed(self.config.get_int("admin_role_id"))
        await target.send(embed=embed, view=TicketPanelView(self.bot))
        await ctx.reply(f"Ticket-Panel in {target.mention} bereitgestellt.")

    @commands.hybrid_command(name="ticketclose", description="Ticket-Thread schließen")
    @commands.has_permissions(manage_threads=True)
    async def ticket_close(self, ctx: commands.Context, reason: str = "Erledigt") -> None:
        thread = ctx.channel
        if not isinstance(thread, discord.Thread):
            await ctx.reply("Dieser Befehl muss in einem Ticket-Thread genutzt werden.")
            return
        await thread.edit(archived=True, locked=True, reason=reason)
        await thread.send(f"Ticket geschlossen: {reason}")

    @commands.hybrid_command(name="ticketaccept", description="Ticket und Nutzer hinzufügen")
    @commands.has_permissions(manage_threads=True)
    async def ticket_accept(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        thread: discord.Thread | None = None,
    ) -> None:
        target_thread = thread or (ctx.channel if isinstance(ctx.channel, discord.Thread) else None)
        if not isinstance(target_thread, discord.Thread):
            await ctx.reply("Kein gültiger Ticket-Thread angegeben.")
            return
        opener_id = member.id if member else None
        if opener_id:
            await target_thread.add_user(member)
        await target_thread.add_user(ctx.author)
        await ctx.reply(f"{ctx.author.mention} ist dem Ticket beigetreten.")
        await target_thread.send(f"{ctx.author.mention} bearbeitet das Ticket nun.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
