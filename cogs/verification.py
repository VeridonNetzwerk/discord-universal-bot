import discord
from discord.ext import commands
from discord.utils import utcnow

from config import config_manager


def build_verify_embed(role_id: int | None) -> discord.Embed:
    description = (
        "Willkommen auf dem Primeblocks Discord!\n\n"
        "✅ Klicke auf den Button, um dich zu verifizieren und Zugriff auf alle Channels zu erhalten.\n"
        "🛡️ Lies vorher unsere Regeln und handle respektvoll."
    )
    embed = discord.Embed(title="Verifizierung", description=description, color=discord.Color.brand_green())
    if role_id:
        embed.add_field(name="Rolle nach der Bestätigung", value=f"<@&{role_id}>", inline=False)
    embed.set_footer(text="Nur ein Klick trennt dich vom kompletten Server.")
    embed.timestamp = utcnow()
    return embed


class VerifyView(discord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Verifizieren", style=discord.ButtonStyle.blurple, custom_id="prime_verify")
    async def verify_user(self, interaction: discord.Interaction, _button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return
        role_id = self.bot.config.get_int("verified_role_id")
        if not role_id:
            await interaction.response.send_message("Verifizierungsrolle ist nicht gesetzt.", ephemeral=True)
            return
        role = guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("Verifizierungsrolle existiert nicht mehr.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Diese Aktion funktioniert nur innerhalb eines Servers.", ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message("Du bist bereits verifiziert.", ephemeral=True)
            return
        await member.add_roles(role, reason="Verifizierung")
        await interaction.response.send_message("Perfekt! Du hast jetzt Zugriff auf alle Channels.", ephemeral=True)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = config_manager

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyView(self.bot))

    @commands.hybrid_command(name="verifypanel", description="Platziert das Verifizierungspanel")
    @commands.has_permissions(manage_roles=True)
    async def verify_panel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        target = channel or ctx.guild.get_channel(self.config.get_int("verify_channel_id"))
        if not isinstance(target, discord.TextChannel):
            await ctx.reply("Kein gültiger Kanal für Verifizierung gesetzt.")
            return
        embed = build_verify_embed(self.config.get_int("verified_role_id"))
        await target.send(embed=embed, view=VerifyView(self.bot))
        await ctx.reply(f"Verifizierungspanel in {target.mention} bereitgestellt.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        verify_channel_id = self.config.get_int("verify_channel_id")
        channel = member.guild.get_channel(verify_channel_id)
        channel_hint = channel.mention if isinstance(channel, discord.TextChannel) else "dem Verifizierungskanal"
        message = (
            f"Hey {member.mention}! Willkommen auf Primeblocks. "
            f"Öffne bitte {channel_hint} und bestätige dich über das Panel, um Zugriff zu erhalten."
        )
        try:
            await member.send(message)
        except discord.Forbidden:
            if isinstance(channel, discord.TextChannel):
                await channel.send(f"{member.mention} konnte nicht per DM erreicht werden. Bitte nutze das Panel zur Verifizierung.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Verification(bot))
