from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from typing import Dict, List, Optional, Tuple, Literal

import discord
from discord.ext import commands
import yt_dlp
from discord.utils import utcnow

from config import config_manager


YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "no_warnings": True,
}

FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def build_music_panel_embed(dj_role_id: int | None) -> discord.Embed:
    description = (
        "Steuere hier die Musik des Servers.\n\n"
        "🎧 **Song hinzufügen** – öffnet ein Eingabefenster\n"
        "⏯️ **Pause/Fortsetzen** – kontrolliere die Wiedergabe\n"
        "⏭️ **Überspringen** – springt zum nächsten Track\n"
        "⏹️ **Stoppen** – leert die Queue"
    )
    embed = discord.Embed(title="Musiksteuerung", description=description, color=discord.Color.blurple())
    if dj_role_id:
        embed.add_field(name="Benötigte Rolle", value=f"<@&{dj_role_id}>", inline=False)
    embed.set_footer(text="Nutze das Panel, statt viele Slash-Befehle zu spammen.")
    embed.timestamp = utcnow()
    return embed


@dataclass
class Track:
    title: str
    stream_url: str
    webpage_url: str
    duration: Optional[int]
    requester: discord.Member


class MusicPlayer:
    def __init__(self, bot: commands.Bot, guild_id: int, ffmpeg_path: str) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.ffmpeg_path = ffmpeg_path
        self.pending: deque[Track] = deque()
        self.queue_event = asyncio.Event()
        self.next = asyncio.Event()
        self.current: Optional[Track] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.volume = 0.5
        self.monitor = getattr(bot, "health_monitor", None)
        self.loop_task = bot.loop.create_task(self.player_loop())

    def set_voice(self, voice: discord.VoiceClient) -> None:
        self.voice = voice

    def skip_current(self) -> bool:
        if self.voice and self.voice.is_playing():
            self.voice.stop()
            self.next.set()
            return True
        return False

    async def add_track(self, track: Track) -> None:
        self.pending.append(track)
        self.queue_event.set()

    def clear_queue(self) -> None:
        self.pending.clear()

    def queue_items(self) -> List[Track]:
        return list(self.pending)

    def queue_size(self) -> int:
        return len(self.pending)

    async def player_loop(self) -> None:
        try:
            while True:
                self.next.clear()
                wait_started = time.perf_counter()
                track = await self._wait_for_track()
                wait_duration = (time.perf_counter() - wait_started) * 1000.0
                if self.monitor:
                    self.monitor.record_bot_task(
                        "music.queue_wait",
                        wait_duration,
                        {
                            "guild_id": self.guild_id,
                            "queue_size": self.queue_size(),
                            "await_seconds": round(wait_duration / 1000.0, 2),
                        },
                    )
                if not self.voice or not self.voice.is_connected():
                    self.pending.appendleft(track)
                    await asyncio.sleep(1)
                    continue

                self.current = track

                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        track.stream_url,
                        executable=self.ffmpeg_path,
                        before_options=FFMPEG_BEFORE_OPTS,
                        options="-vn",
                    ),
                    volume=self.volume,
                )

                def after_play(error: Optional[Exception]) -> None:
                    if error:
                        print(f"Player error: {error}")
                    self.bot.loop.call_soon_threadsafe(self.next.set)

                play_started = time.perf_counter()
                self.voice.play(source, after=after_play)
                await self.next.wait()
                play_duration = (time.perf_counter() - play_started) * 1000.0
                if self.monitor:
                    self.monitor.record_bot_task(
                        "music.track_play",
                        play_duration,
                        {
                            "guild_id": self.guild_id,
                            "title": track.title,
                            "duration": track.duration,
                        },
                    )
                try:
                    source.cleanup()
                except (ValueError, OSError):
                    # ffmpeg process might already be cleaned up after manual stop
                    pass
                self.current = None
        except asyncio.CancelledError:
            pass

    async def _wait_for_track(self) -> Track:
        while True:
            if self.pending:
                return self.pending.popleft()
            self.queue_event.clear()
            await self.queue_event.wait()


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = config_manager
        self.players: Dict[int, MusicPlayer] = {}
        self.ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)
        self.debug_enabled: Dict[int, bool] = {}

    def dj_allowed(self, member: discord.Member) -> bool:
        dj_role_id = self.config.get_int("dj_role_id")
        return dj_role_id == 0 or any(role.id == dj_role_id for role in member.roles)

    def check_music_channel(
        self,
        guild: Optional[discord.Guild],
        channel_id: Optional[int],
        *,
        channel_obj: Optional[discord.abc.GuildChannel] = None,
    ) -> Tuple[bool, Optional[str]]:
        allowed_id = self.config.get_int("music_channel_id")
        if not allowed_id:
            return True, None
        if channel_id == allowed_id:
            return True, None
        if channel_obj is None and guild and channel_id:
            resolved = guild.get_channel(channel_id)
            if isinstance(resolved, discord.abc.GuildChannel):
                channel_obj = resolved
        parent_id = None
        if channel_obj is not None:
            parent_id = getattr(channel_obj, "parent_id", None)
        if parent_id == allowed_id:
            return True, None
        mention = f"<#{allowed_id}>"
        if guild:
            allowed_channel = guild.get_channel(allowed_id)
            if isinstance(allowed_channel, discord.TextChannel):
                mention = allowed_channel.mention
        return False, f"Musikbefehle sind nur in {mention} erlaubt."

    async def ensure_music_channel_ctx(self, ctx: commands.Context) -> bool:
        guild = ctx.guild
        channel = ctx.channel if isinstance(ctx.channel, discord.abc.GuildChannel) else None
        channel_id = getattr(ctx.channel, "id", None)
        allowed, message = self.check_music_channel(guild, channel_id, channel_obj=channel)
        if not allowed and message:
            await self.send_ctx_message(ctx, content=message, ephemeral=True)
            if guild:
                await self.send_debug(guild, f"Command in falschem Kanal ({channel_id}); Nachricht: {message}")
            return False
        return True

    async def cog_load(self) -> None:
        self.bot.add_view(MusicPanelView(self))

    async def send_ctx_message(
        self,
        ctx: commands.Context,
        *,
        replace: bool = True,
        ephemeral: bool = False,
        **kwargs,
    ) -> None:
        interaction = ctx.interaction
        if interaction:
            responded = getattr(ctx, "_music_response_sent", False)
            if not responded and replace:
                await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
                ctx._music_response_sent = True
                ctx._music_response_ephemeral = ephemeral
            else:
                kwargs.pop("ephemeral", None)
                if replace:
                    try:
                        await interaction.edit_original_response(**kwargs)
                    except discord.InteractionResponded:
                        await interaction.followup.send(**kwargs)
                else:
                    await interaction.followup.send(**kwargs, ephemeral=ephemeral)
        else:
            message = getattr(ctx, "_music_response_message", None)
            if message and replace:
                await message.edit(**kwargs)
            else:
                kwargs.pop("ephemeral", None)
                msg = await ctx.reply(**kwargs)
                if replace:
                    ctx._music_response_message = msg

    async def send_interaction_message(self, interaction: discord.Interaction, **kwargs) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def send_debug(self, guild: discord.Guild | None, message: str) -> None:
        if not guild:
            return
        if not self.debug_enabled.get(guild.id, False):
            return
        log_channel_id = self.config.get_int("music_log_channel_id")
        if not log_channel_id:
            return
        channel = guild.get_channel(log_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(message[:1900])
            except discord.HTTPException:
                pass

    def validate_interaction_member(
        self, interaction: discord.Interaction
    ) -> Tuple[Optional[discord.Guild], Optional[discord.Member], Optional[str]]:
        guild = interaction.guild
        if guild is None:
            return None, None, "Diese Aktion funktioniert nur in einer Guild."
        channel_obj = None
        if interaction.channel_id:
            channel_obj = guild.get_channel(interaction.channel_id)
        allowed, message = self.check_music_channel(guild, interaction.channel_id, channel_obj=channel_obj)
        if not allowed:
            return guild, None, message
        member = interaction.user
        if not isinstance(member, discord.Member):
            return guild, None, "Diese Aktion funktioniert nur innerhalb eines Servers."
        if not self.dj_allowed(member):
            return guild, member, "Du hast keine DJ-Rechte."
        return guild, member, None

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        player = self.players.get(guild.id)
        if not player:
            player = MusicPlayer(self.bot, guild.id, self.config.get("ffmpeg_path"))
            self.players[guild.id] = player
        return player

    async def ensure_voice(self, ctx: commands.Context) -> Tuple[Optional[discord.VoiceClient], Optional[str]]:
        if not ctx.guild:
            return None, "Dieser Befehl kann nur in einer Guild genutzt werden."
        author = ctx.author
        if not isinstance(author, discord.Member) or not author.voice or not author.voice.channel:
            return None, "Du musst zuerst in einen Voice Channel."
        voice = ctx.voice_client
        channel = author.voice.channel
        if voice and voice.is_connected():
            if voice.channel != channel:
                try:
                    await voice.move_to(channel)
                except discord.DiscordException as exc:
                    await self.send_debug(ctx.guild, f"Voice move_to fehlgeschlagen: {exc}")
                    return None, "Konnte den Voice Channel nicht wechseln."
            player = self.get_player(ctx.guild)
            player.set_voice(voice)
            return voice, None
        try:
            voice = await channel.connect()
        except discord.DiscordException as exc:
            await self.send_debug(ctx.guild, f"Voice connect fehlgeschlagen: {exc}")
            return None, "Konnte dem Voice Channel nicht beitreten."
        player = self.get_player(ctx.guild)
        player.set_voice(voice)
        return voice, None

    async def ensure_voice_interaction(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> Tuple[Optional[discord.VoiceClient], Optional[str]]:
        guild = interaction.guild
        if guild is None:
            return None, "Diese Aktion funktioniert nur in einer Guild."
        if not member.voice or not member.voice.channel:
            return None, "Du musst zuerst in einen Voice Channel."
        voice = guild.voice_client
        channel = member.voice.channel
        if voice and voice.is_connected():
            if voice.channel != channel:
                try:
                    await voice.move_to(channel)
                except discord.DiscordException as exc:
                    await self.send_debug(guild, f"Voice move_to (Interaction) fehlgeschlagen: {exc}")
                    return None, "Konnte den Voice Channel nicht wechseln."
            player = self.get_player(guild)
            player.set_voice(voice)
            return voice, None
        try:
            voice = await channel.connect()
        except discord.DiscordException as exc:
            await self.send_debug(guild, f"Voice connect (Interaction) fehlgeschlagen: {exc}")
            return None, "Konnte dem Voice Channel nicht beitreten."
        player = self.get_player(guild)
        player.set_voice(voice)
        return voice, None

    async def create_track(self, query: str, requester: discord.Member) -> Track:
        loop = asyncio.get_running_loop()
        attempted_error: Optional[Exception] = None

        async def extract(search_term: str) -> dict:
            return await loop.run_in_executor(None, partial(self.ytdl.extract_info, search_term, download=False))

        search_terms = [query]
        if "://" not in query and not query.lower().startswith("ytsearch:"):
            search_terms.append(f"ytsearch:{query}")

        info: Optional[dict] = None
        for term in search_terms:
            try:
                data = await extract(term)
            except Exception as exc:  # yt_dlp wirft DownloadError u.a.
                attempted_error = exc
                continue
            entries = data.get("entries") if isinstance(data, dict) else None
            if entries:
                # ytsearch liefert eine Playlist struktur zurück
                first_entry = next((entry for entry in entries if entry), None)
                if first_entry is None:
                    attempted_error = RuntimeError("Keine Ergebnisse gefunden.")
                    continue
                data = first_entry
            info = data
            break

        if info is None:
            raise attempted_error or RuntimeError("Keine Ergebnisse gefunden.")

        stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("Konnte keinen Stream für den Track finden.")
        return Track(
            title=info.get("title", "Unbekannt"),
            stream_url=stream_url,
            webpage_url=info.get("webpage_url", "https://youtube.com"),
            duration=info.get("duration"),
            requester=requester,
        )

    async def process_modal_request(self, interaction: discord.Interaction, query: str) -> None:
        if not query.strip():
            await self.send_interaction_message(interaction, content="Bitte gib einen Titel oder Link an.", ephemeral=True)
            await self.send_debug(interaction.guild, "Song-Modal: Leere Eingabe verweigert.")
            return
        guild, member, error = self.validate_interaction_member(interaction)
        if error:
            await self.send_interaction_message(interaction, content=error, ephemeral=True)
            await self.send_debug(guild, f"Song-Modal verweigert: {error}")
            return
        assert guild is not None and member is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        voice, voice_error = await self.ensure_voice_interaction(interaction, member)
        if voice_error:
            await interaction.followup.send(voice_error, ephemeral=True)
            await self.send_debug(guild, f"Song-Modal Voice-Fehler: {voice_error}")
            return
        player = self.get_player(guild)
        try:
            track = await self.create_track(query, member)
        except Exception as exc:
            await interaction.followup.send(f"❌ Konnte den Song nicht laden: {exc}", ephemeral=True)
            await self.send_debug(guild, f"Song-Modal: Fehler bei '{query}': {exc}")
            return
        await player.add_track(track)
        await interaction.followup.send(
            f"🎵 **{track.title}** wurde zur Queue hinzugefügt.",
            ephemeral=True,
        )
        await self.send_debug(guild, f"Song-Modal: Track '{track.title}' von {member} hinzugefügt.")

    @commands.hybrid_command(name="join", description="Trete dem Voice-Channel bei")
    async def join(self, ctx: commands.Context) -> None:
        if not await self.ensure_music_channel_ctx(ctx):
            if ctx.guild:
                await self.send_debug(ctx.guild, f"Join-Befehl abgelehnt: falscher Kanal {getattr(ctx.channel, 'id', 'unknown')}")
            return
        ephemeral = ctx.interaction is not None
        await self.send_ctx_message(ctx, content="🔄 Verbinde mit deinem Voice Channel...", ephemeral=ephemeral)
        voice, error = await self.ensure_voice(ctx)
        if error:
            await self.send_ctx_message(ctx, content=f"❌ {error}", ephemeral=ephemeral)
            await self.send_debug(ctx.guild, f"Join-Befehl: {error}")
            return
        await self.send_ctx_message(ctx, content="✅ Verbunden.", ephemeral=ephemeral)
        await self.send_debug(ctx.guild, f"Join-Befehl erfolgreich: verbunden in {voice.channel}")

    @commands.hybrid_command(name="leave", description="Voice Channel verlassen")
    async def leave(self, ctx: commands.Context) -> None:
        if not await self.ensure_music_channel_ctx(ctx):
            return
        voice = ctx.voice_client
        if voice and voice.is_connected():
            await voice.disconnect()
            if ctx.guild and ctx.guild.id in self.players:
                self.players[ctx.guild.id].clear_queue()
            await self.send_ctx_message(ctx, content="Verbindung getrennt.")
            await self.send_debug(ctx.guild, "Leave-Befehl: Verbindung getrennt und Queue geleert.")
        else:
            await self.send_ctx_message(ctx, content="Ich bin in keinem Voice Channel.")
            await self.send_debug(ctx.guild, "Leave-Befehl: Bot war nicht verbunden.")

    @commands.hybrid_command(name="play", description="Musik abspielen")
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        if not ctx.guild:
            return
        if not await self.ensure_music_channel_ctx(ctx):
            if ctx.guild:
                await self.send_debug(ctx.guild, f"Play-Befehl abgelehnt: falscher Kanal {getattr(ctx.channel, 'id', 'unknown')}")
            return
        member = ctx.author
        if not isinstance(member, discord.Member):
            return
        if not self.dj_allowed(member):
            await self.send_ctx_message(ctx, content="Du hast keine DJ-Rechte.", ephemeral=True)
            if ctx.guild:
                await self.send_debug(ctx.guild, f"Play-Befehl verweigert: {member} besitzt keine DJ-Rolle.")
            return
        await self.send_ctx_message(ctx, content="🔄 Füge Song hinzu...", ephemeral=False)
        voice, error = await self.ensure_voice(ctx)
        if error:
            await self.send_ctx_message(ctx, content=f"❌ {error}")
            if ctx.guild:
                await self.send_debug(ctx.guild, f"Play-Befehl: {error}")
            return
        player = self.get_player(ctx.guild)
        await self.send_debug(ctx.guild, f"Play-Befehl: Suche nach '{query}' gestartet.")
        try:
            track = await self.create_track(query, member)
        except Exception as exc:  # yt_dlp kann diverse Fehler werfen
            error_msg = f"Konnte den Song nicht laden: {exc}"
            await self.send_ctx_message(ctx, content=f"❌ {error_msg}")
            await self.send_debug(ctx.guild, f"Play-Befehl fehlgeschlagen bei '{query}': {exc}")
            return
        await self.send_debug(ctx.guild, f"Play-Befehl: Track gefunden '{track.title}'.")
        await player.add_track(track)
        await self.send_ctx_message(ctx, content=f"✅ Zur Queue hinzugefügt: **{track.title}**")
        await self.send_debug(ctx.guild, f"Play-Befehl abgeschlossen: Track '{track.title}' zur Queue hinzugefügt.")

    @commands.hybrid_command(name="skip", description="Song überspringen")
    async def skip(self, ctx: commands.Context) -> None:
        if not await self.ensure_music_channel_ctx(ctx):
            if ctx.guild:
                await self.send_debug(ctx.guild, f"Skip-Befehl abgelehnt: falscher Kanal {getattr(ctx.channel, 'id', 'unknown')}")
            return
        if not isinstance(ctx.author, discord.Member) or not self.dj_allowed(ctx.author):
            await self.send_ctx_message(ctx, content="Du hast keine DJ-Rechte.", ephemeral=True)
            await self.send_debug(ctx.guild, f"Skip-Befehl verweigert: {ctx.author} ohne DJ-Rechte.")
            return
        player = self.players.get(ctx.guild.id) if ctx.guild else None
        if player and player.skip_current():
            await self.send_ctx_message(ctx, content="Track übersprungen.")
            await self.send_debug(ctx.guild, "Skip-Befehl: Aktueller Track gestoppt.")
        else:
            await self.send_ctx_message(ctx, content="Es läuft nichts.")
            await self.send_debug(ctx.guild, "Skip-Befehl: Kein Track aktiv.")

    @commands.hybrid_command(name="pause", description="Song pausieren")
    async def pause(self, ctx: commands.Context) -> None:
        if not await self.ensure_music_channel_ctx(ctx):
            return
        if not isinstance(ctx.author, discord.Member) or not self.dj_allowed(ctx.author):
            await self.send_ctx_message(ctx, content="Du hast keine DJ-Rechte.", ephemeral=True)
            await self.send_debug(ctx.guild, f"Pause-Befehl verweigert: {ctx.author} ohne DJ-Rechte.")
            return
        voice = ctx.voice_client
        if voice and voice.is_playing():
            voice.pause()
            await self.send_ctx_message(ctx, content="Pausiert.")
            await self.send_debug(ctx.guild, "Pause-Befehl: Wiedergabe pausiert.")
        else:
            await self.send_ctx_message(ctx, content="Es läuft nichts zum Pausieren.")
            await self.send_debug(ctx.guild, "Pause-Befehl: Kein Track aktiv.")

    @commands.hybrid_command(name="resume", description="Song fortsetzen")
    async def resume(self, ctx: commands.Context) -> None:
        if not await self.ensure_music_channel_ctx(ctx):
            return
        if not isinstance(ctx.author, discord.Member) or not self.dj_allowed(ctx.author):
            await self.send_ctx_message(ctx, content="Du hast keine DJ-Rechte.", ephemeral=True)
            await self.send_debug(ctx.guild, f"Resume-Befehl verweigert: {ctx.author} ohne DJ-Rechte.")
            return
        voice = ctx.voice_client
        if voice and voice.is_paused():
            voice.resume()
            await self.send_ctx_message(ctx, content="Fortgesetzt.")
            await self.send_debug(ctx.guild, "Resume-Befehl: Wiedergabe fortgesetzt.")
        else:
            await self.send_ctx_message(ctx, content="Es ist nichts pausiert.")
            await self.send_debug(ctx.guild, "Resume-Befehl: Keine pausierte Wiedergabe.")

    @commands.hybrid_command(name="stop", description="Musik stoppen und Queue leeren")
    async def stop(self, ctx: commands.Context) -> None:
        if not await self.ensure_music_channel_ctx(ctx):
            return
        if not isinstance(ctx.author, discord.Member) or not self.dj_allowed(ctx.author):
            await self.send_ctx_message(ctx, content="Du hast keine DJ-Rechte.", ephemeral=True)
            await self.send_debug(ctx.guild, f"Stop-Befehl verweigert: {ctx.author} ohne DJ-Rechte.")
            return
        voice = ctx.voice_client
        if voice:
            voice.stop()
            await self.send_debug(ctx.guild, "Stop-Befehl: Voice gestoppt.")
        if ctx.guild and ctx.guild.id in self.players:
            self.players[ctx.guild.id].clear_queue()
            await self.send_debug(ctx.guild, "Stop-Befehl: Queue geleert.")
        await self.send_ctx_message(ctx, content="Queue geleert und Playback gestoppt.")

    @commands.hybrid_command(name="queue", description="Aktuelle Queue anzeigen")
    async def queue_cmd(self, ctx: commands.Context) -> None:
        if not await self.ensure_music_channel_ctx(ctx):
            return
        if not ctx.guild or ctx.guild.id not in self.players:
            await self.send_ctx_message(ctx, content="Keine Queue vorhanden.")
            await self.send_debug(ctx.guild, "Queue-Befehl: Keine Queue vorhanden.")
            return
        player = self.players[ctx.guild.id]
        items: List[Track] = player.queue_items()
        embed = discord.Embed(title="Aktuelle Queue", color=discord.Color.blurple())
        if player.current:
            embed.add_field(name="Jetzt", value=f"**{player.current.title}** – angefragt von {player.current.requester.mention}", inline=False)
        if items:
            for idx, track in enumerate(items[:10], start=1):
                embed.add_field(name=f"#{idx}", value=f"{track.title} – {track.requester.mention}", inline=False)
        else:
            embed.description = "Die Queue ist leer."
        await self.send_ctx_message(ctx, embed=embed)
        await self.send_debug(ctx.guild, f"Queue-Befehl: {len(items)} Einträge gelistet.")

    @commands.hybrid_command(name="musicpanel", description="Stellt das Musikpanel bereit")
    @commands.has_permissions(manage_guild=True)
    async def music_panel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        if not ctx.guild:
            return
        target = channel or ctx.guild.get_channel(self.config.get_int("music_channel_id"))
        if not isinstance(target, discord.TextChannel):
            await self.send_ctx_message(ctx, content="Kein gültiger Kanal für das Musikpanel konfiguriert.")
            return
        embed = build_music_panel_embed(self.config.get_int("dj_role_id"))
        await target.send(embed=embed, view=MusicPanelView(self))
        await self.send_ctx_message(ctx, content=f"Musikpanel in {target.mention} bereitgestellt.")
        await self.send_debug(ctx.guild, f"Musikpanel erneut bereitgestellt in {target.mention} durch {ctx.author}.")

    @commands.hybrid_command(name="musicdebug", description="Debug-Logs für Musikbefehle steuern")
    @commands.has_permissions(administrator=True)
    async def music_debug(self, ctx: commands.Context, mode: Literal["on", "off", "toggle"] = "toggle") -> None:
        guild = ctx.guild
        if not guild:
            await self.send_ctx_message(ctx, content="Dieser Befehl kann nur in einer Guild genutzt werden.", ephemeral=True)
            return
        current = self.debug_enabled.get(guild.id, False)
        if mode == "on":
            new_state = True
        elif mode == "off":
            new_state = False
        else:
            new_state = not current
        self.debug_enabled[guild.id] = new_state
        status = "aktiviert" if new_state else "deaktiviert"
        message = f"Musik-Debug wurde {status}."
        log_channel_id = self.config.get_int("music_log_channel_id")
        if new_state and not log_channel_id:
            message += " Hinweis: Setze zuerst einen Musik-Log-Kanal mit /config setchannel music_log <#kanal>."
        await self.send_ctx_message(ctx, content=message, ephemeral=True)
        if new_state:
            await self.send_debug(guild, f"Musik-Debug aktiviert von {ctx.author} (Modus {mode}).")

    async def handle_join_button(self, interaction: discord.Interaction) -> None:
        guild, member, error = self.validate_interaction_member(interaction)
        if error:
            await self.send_interaction_message(interaction, content=error, ephemeral=True)
            await self.send_debug(guild, f"Join-Button verweigert: {error}")
            return
        assert member is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        _voice, voice_error = await self.ensure_voice_interaction(interaction, member)
        if voice_error:
            await interaction.followup.send(voice_error, ephemeral=True)
            await self.send_debug(guild, f"Join-Button Voice-Fehler: {voice_error}")
            return
        if _voice is None:
            await interaction.followup.send("Konnte keine Verbindung zum Voice Channel herstellen.", ephemeral=True)
            await self.send_debug(guild, "Join-Button: Voice-Client blieb None ohne Fehler.")
            return
        await interaction.followup.send("Verbunden mit deinem Voice Channel.", ephemeral=True)
        await self.send_debug(guild, f"Join-Button erfolgreich ausgeführt durch {member}.")

    async def handle_leave_button(self, interaction: discord.Interaction) -> None:
        guild, member, error = self.validate_interaction_member(interaction)
        if error:
            await self.send_interaction_message(interaction, content=error, ephemeral=True)
            await self.send_debug(guild, f"Leave-Button verweigert: {error}")
            return
        if guild is None:
            return
        voice = guild.voice_client
        if voice and voice.is_connected():
            await voice.disconnect()
            player = self.players.get(guild.id)
            if player:
                player.clear_queue()
            await self.send_interaction_message(interaction, content="Voice Channel verlassen und Queue geleert.", ephemeral=True)
            await self.send_debug(guild, f"Leave-Button: Verbindung getrennt von {interaction.user}.")
        else:
            await self.send_interaction_message(interaction, content="Ich bin aktuell mit keinem Channel verbunden.", ephemeral=True)
            await self.send_debug(guild, "Leave-Button: Bot war nicht verbunden.")

    async def handle_skip_button(self, interaction: discord.Interaction) -> None:
        guild, member, error = self.validate_interaction_member(interaction)
        if error:
            await self.send_interaction_message(interaction, content=error, ephemeral=True)
            await self.send_debug(guild, f"Skip-Button verweigert: {error}")
            return
        if guild is None:
            return
        player = self.players.get(guild.id)
        if player and player.skip_current():
            await self.send_interaction_message(interaction, content="Track übersprungen.", ephemeral=True)
            await self.send_debug(guild, f"Skip-Button: Track von {interaction.user} übersprungen.")
        else:
            await self.send_interaction_message(interaction, content="Es läuft nichts zum Überspringen.", ephemeral=True)
            await self.send_debug(guild, "Skip-Button: Kein Track aktiv.")

    async def handle_pause_button(self, interaction: discord.Interaction) -> None:
        guild, member, error = self.validate_interaction_member(interaction)
        if error:
            await self.send_interaction_message(interaction, content=error, ephemeral=True)
            await self.send_debug(guild, f"Pause-Button verweigert: {error}")
            return
        if guild is None:
            return
        voice = guild.voice_client
        if voice and voice.is_playing():
            voice.pause()
            await self.send_interaction_message(interaction, content="Wiedergabe pausiert.", ephemeral=True)
            await self.send_debug(guild, f"Pause-Button: Wiedergabe pausiert von {interaction.user}.")
        else:
            await self.send_interaction_message(interaction, content="Aktuell läuft kein Track.", ephemeral=True)
            await self.send_debug(guild, "Pause-Button: Kein Track aktiv.")

    async def handle_resume_button(self, interaction: discord.Interaction) -> None:
        guild, member, error = self.validate_interaction_member(interaction)
        if error:
            await self.send_interaction_message(interaction, content=error, ephemeral=True)
            await self.send_debug(guild, f"Resume-Button verweigert: {error}")
            return
        if guild is None:
            return
        voice = guild.voice_client
        if voice and voice.is_paused():
            voice.resume()
            await self.send_interaction_message(interaction, content="Weiter geht's!", ephemeral=True)
            await self.send_debug(guild, f"Resume-Button: Wiedergabe fortgesetzt von {interaction.user}.")
        else:
            await self.send_interaction_message(interaction, content="Es ist nichts pausiert.", ephemeral=True)
            await self.send_debug(guild, "Resume-Button: Keine pausierte Wiedergabe.")

    async def handle_stop_button(self, interaction: discord.Interaction) -> None:
        guild, member, error = self.validate_interaction_member(interaction)
        if error:
            await self.send_interaction_message(interaction, content=error, ephemeral=True)
            await self.send_debug(guild, f"Stop-Button verweigert: {error}")
            return
        if guild is None:
            return
        voice = guild.voice_client
        if voice:
            voice.stop()
            await self.send_debug(guild, f"Stop-Button: Wiedergabe gestoppt von {interaction.user}.")
        player = self.players.get(guild.id)
        if player:
            player.clear_queue()
            await self.send_debug(guild, f"Stop-Button: Queue geleert (Restgröße {player.queue_size()}).")
        await self.send_interaction_message(interaction, content="Playback gestoppt und Queue geleert.", ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.guild.id not in self.players:
            return
        player = self.players[member.guild.id]
        voice = player.voice
        if voice and voice.channel and len(voice.channel.members) == 1 and voice.channel.members[0] == voice.guild.me:
            await voice.disconnect()
            player.clear_queue()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))


class SongRequestModal(discord.ui.Modal, title="Song anfragen"):
    query = discord.ui.TextInput(
        label="Titel oder Link",
        placeholder="z.B. Eminem - Mockingbird oder https://youtu.be/...",
        style=discord.TextStyle.short,
        max_length=200,
    )

    def __init__(self, cog: "Music") -> None:
        super().__init__(title="Song anfragen")
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.process_modal_request(interaction, str(self.query.value))


class MusicPanelView(discord.ui.View):
    def __init__(self, cog: "Music") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Song anfragen",
        style=discord.ButtonStyle.blurple,
        emoji="🎧",
        custom_id="prime_music_request",
    )
    async def request_song(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SongRequestModal(self.cog))

    @discord.ui.button(
        label="Beitreten",
        style=discord.ButtonStyle.success,
        emoji="🔊",
        custom_id="prime_music_join",
        row=1,
    )
    async def join_voice(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_join_button(interaction)

    @discord.ui.button(
        label="Verlassen",
        style=discord.ButtonStyle.danger,
        emoji="📤",
        custom_id="prime_music_leave",
        row=1,
    )
    async def leave_voice(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_leave_button(interaction)

    @discord.ui.button(
        label="Überspringen",
        style=discord.ButtonStyle.primary,
        emoji="⏭️",
        custom_id="prime_music_skip",
        row=1,
    )
    async def skip_track(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_skip_button(interaction)

    @discord.ui.button(
        label="Pause",
        style=discord.ButtonStyle.secondary,
        emoji="⏸️",
        custom_id="prime_music_pause",
        row=2,
    )
    async def pause_track(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_pause_button(interaction)

    @discord.ui.button(
        label="Fortsetzen",
        style=discord.ButtonStyle.secondary,
        emoji="▶️",
        custom_id="prime_music_resume",
        row=2,
    )
    async def resume_track(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_resume_button(interaction)

    @discord.ui.button(
        label="Stoppen",
        style=discord.ButtonStyle.danger,
        emoji="⏹️",
        custom_id="prime_music_stop",
        row=2,
    )
    async def stop_tracks(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_stop_button(interaction)
