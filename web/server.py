from __future__ import annotations

import asyncio
import html
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List, cast
from urllib.parse import quote

import discord
from aiohttp import web
from discord import AllowedMentions
from discord.abc import Messageable
from discord.ext import commands

from utils.config_manager import ConfigManager

COOKIE_NAME = "primeblocks_session"

ROLE_TARGETS = {
    "admin": ("admin_role_id", "Admin Rolle"),
    "verified": ("verified_role_id", "Verified Rolle"),
    "dj": ("dj_role_id", "DJ Rolle"),
}


class ManagementServer:
    def __init__(
        self,
        bot: commands.Bot,
        config: ConfigManager,
        *,
        username: str,
        password: str,
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        self.bot = bot
        self.config = config
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.sessions: Dict[str, str] = {}
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.BaseSite] = None
        self._session_lock = asyncio.Lock()
        self.app = web.Application(middlewares=[self._metrics_middleware])
        self._register_routes()

    # ------------------------------------------------------------------
    # Bootstrapping
    # ------------------------------------------------------------------
    def _register_routes(self) -> None:
        router = self.app.router
        router.add_get("/", self.handle_index)
        router.add_get("/members", self.handle_members)
        router.add_post("/members/action", self.handle_member_action)
        router.add_get("/announcements", self.handle_announcements)
        router.add_post("/announcements/send", self.handle_announcement_send)
        router.add_get("/roles", self.handle_roles)
        router.add_post("/roles/update", self.handle_role_update)
        router.add_get("/modlog", self.handle_modlog)
        router.add_get("/music", self.handle_music)
        router.add_post("/music/action", self.handle_music_action)
        router.add_get("/cogs", self.handle_cogs)
        router.add_post("/cogs/action", self.handle_cogs_action)
        router.add_get("/login", self.handle_login_form)
        router.add_post("/login", self.handle_login_submit)
        router.add_get("/logout", self.handle_logout)
        router.add_post("/config", self.handle_config_update)
        router.add_post("/message", self.handle_message_send)
        router.add_get("/health", self.handle_health)

    @web.middleware
    async def _metrics_middleware(self, request: web.Request, handler):
        start = time.perf_counter()
        try:
            response = await handler(request)
        except Exception:
            duration = (time.perf_counter() - start) * 1000.0
            monitor = getattr(self.bot, "health_monitor", None)
            if monitor:
                monitor.record_http_request(request.method, request.rel_url.path, duration, 500)
            raise
        else:
            duration = (time.perf_counter() - start) * 1000.0
            monitor = getattr(self.bot, "health_monitor", None)
            if monitor:
                monitor.record_http_request(request.method, request.rel_url.path, duration, getattr(response, "status", 200))
            return response

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        print(f"Management-Webinterface aktiv: http://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _is_authenticated(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE_NAME)
        return token in self.sessions

    async def _create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        async with self._session_lock:
            self.sessions[token] = self.username
        return token

    async def _invalidate_session(self, request: web.Request) -> None:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return
        async with self._session_lock:
            self.sessions.pop(token, None)

    def _redirect(self, location: str, message: Optional[str] = None) -> web.HTTPFound:
        target = location
        if message:
            suffix = "&" if "?" in target else "?"
            target = f"{target}{suffix}msg={quote(message)}"
        response = web.HTTPFound(location=target)
        return response

    def _require_auth(self, request: web.Request) -> None:
        if not self._is_authenticated(request):
            raise web.HTTPFound(location="/login")

    def _safe_redirect_target(self, target: Optional[str]) -> str:
        if target and isinstance(target, str) and target.startswith("/"):
            return target
        return "/"

    def _primary_guild(self) -> Optional[discord.Guild]:
        guild_id = 0
        try:
            guild_id = int(self.config.get("guild_id", 0) or 0)
        except (TypeError, ValueError):
            guild_id = 0
        if guild_id:
            guild = self.bot.get_guild(guild_id)
            if guild:
                return guild
        if self.bot.guilds:
            return self.bot.guilds[0]
        return None

    @staticmethod
    def _format_datetime(value: Optional[datetime]) -> str:
        if not value:
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M UTC")

    def _status_badge(self, status: Optional[discord.Status]) -> str:
        if status is None:
            label = "UNKNOWN"
            background = "rgba(148,163,184,0.35)"
        else:
            label = status.name.upper()
            palette = {
                discord.Status.online: "rgba(74,222,128,0.25)",
                discord.Status.idle: "rgba(250,204,21,0.25)",
                discord.Status.dnd: "rgba(248,113,113,0.3)",
                discord.Status.offline: "rgba(100,116,139,0.25)",
                discord.Status.invisible: "rgba(100,116,139,0.25)",
            }
            background = palette.get(status, "rgba(148,163,184,0.35)")
        return (
            "<span class='status-pill' style='background:" + background + ";color:#e2e8f0;'>"
            f"{html.escape(label)}"
            "</span>"
        )

    @staticmethod
    def _coerce_form_value(value: object, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except Exception:
                return default
        return str(value)

    @staticmethod
    def _parse_timespan(value: str) -> Optional[int]:
        text = (value or "").strip().lower()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            pass
        else:
            if numeric <= 0:
                return None
            return int(numeric)
        matches = re.findall(r"(\d+)([smhd])", text)
        if not matches:
            return None
        unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        total = 0
        for amount_str, unit in matches:
            amount = int(amount_str)
            factor = unit_map.get(unit, 0)
            total += amount * factor
        return total or None

    def _available_extensions(self) -> List[str]:
        base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cogs")
        if not os.path.isdir(base_dir):
            return []
        extensions: List[str] = []
        for entry in os.listdir(base_dir):
            if not entry.endswith(".py"):
                continue
            if entry.startswith("_"):
                continue
            module = entry[:-3]
            extensions.append(f"cogs.{module}")
        extensions.sort()
        return extensions

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        if seconds is None or seconds <= 0:
            return "?"
        minutes, rest = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{rest:02d}"
        return f"{minutes}:{rest:02d}"


    def _render_layout(
        self,
        *,
        active: str,
        body: str,
        message: Optional[str] = None,
        subtitle: str = "Server Management Console",
    ) -> str:
        tabs = [
            ("dashboard", "Dashboard", "/"),
            ("members", "Members", "/members"),
            ("announcements", "Announcements", "/announcements"),
            ("roles", "Roles", "/roles"),
            ("modlog", "Mod Log", "/modlog"),
            ("music", "Music", "/music"),
            ("cogs", "Cogs", "/cogs"),
        ]
        nav_links = []
        for slug, label, href in tabs:
            classes = "active" if slug == active else ""
            nav_links.append(f"<a class='{classes}' href='{href}'>{html.escape(label)}</a>")
        banner = ""
        if message:
            banner = (
                "<div class='toast'>"
                f"{html.escape(message)}"
                "</div>"
            )
        css = """
<style>@import url('https://fonts.googleapis.com/css2?family=Oxanium:wght@400;500;600;700&family=Montserrat:wght@400;600&display=swap');
:root{--bg:#050818;--panel:#0b1229;--panel-soft:#111a3d;--accent:#615bff;--accent-2:#7f5bff;--accent-3:#56b4ff;--muted:#8790c9;--text:#eef2ff;--border:rgba(116,128,255,0.28);--shadow:0 28px 68px rgba(6,10,30,0.55);--radius:22px;}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:rgba(120,140,255,0.55) transparent;}
body{margin:0;background:radial-gradient(circle at top,#131c44 0%,#050818 52%,#050818 100%);font-family:'Montserrat',sans-serif;color:var(--text);min-height:100vh;}
body::-webkit-scrollbar{width:8px;height:8px;}
body::-webkit-scrollbar-thumb{background:rgba(120,140,255,0.55);border-radius:999px;}
.dashboard{max-width:1280px;margin:0 auto;padding:42px 30px 90px;display:flex;flex-direction:column;gap:32px;}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:26px 30px;border-radius:var(--radius);background:linear-gradient(135deg,rgba(28,35,73,0.95),rgba(13,18,44,0.92));border:1px solid var(--border);box-shadow:var(--shadow);gap:26px;}
.logo{width:58px;height:58px;border-radius:18px;background:linear-gradient(135deg,#5f5bff,#7c5bff);display:flex;align-items:center;justify-content:center;box-shadow:0 18px 36px rgba(95,91,255,0.45);}
.logo svg{width:30px;height:30px;fill:#06081a;}
.titles h1{margin:0;font-family:'Oxanium',cursive;font-size:28px;letter-spacing:0.1em;text-transform:uppercase;color:var(--text);}
.titles span{display:block;margin-top:6px;font-size:13px;letter-spacing:0.3em;text-transform:uppercase;color:var(--muted);}
.nav-tabs{display:flex;gap:14px;flex-wrap:wrap;padding:0 6px;}
.nav-tabs a{flex:1 1 150px;text-align:center;padding:14px 18px;border-radius:18px;background:rgba(13,20,46,0.85);border:1px solid rgba(110,126,255,0.15);color:#b8c0ff;text-decoration:none;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;transition:all 0.2s ease;}
.nav-tabs a.active,.nav-tabs a:hover{background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#f8faff;box-shadow:0 18px 36px rgba(96,96,255,0.4);}
.nav-tabs .logout{flex:0 0 auto;padding:14px 26px;}
main{display:flex;flex-direction:column;gap:30px;}
.toast{padding:16px 20px;border-radius:18px;background:linear-gradient(135deg,rgba(98,109,255,0.35),rgba(128,91,255,0.25));border:1px solid rgba(120,134,255,0.45);box-shadow:0 22px 48px rgba(83,94,255,0.35);font-weight:600;}
.toast.error{background:rgba(239,68,68,0.18);border-color:rgba(248,113,113,0.4);color:#fecaca;}
.hero{padding:36px;border-radius:26px;background:linear-gradient(140deg,rgba(20,28,62,0.96),rgba(10,16,40,0.92));border:1px solid var(--border);box-shadow:var(--shadow);display:flex;flex-direction:column;gap:28px;}
.hero-head{display:flex;flex-direction:column;gap:8px;}
.hero-head h2{margin:0;font-family:'Oxanium',cursive;font-size:32px;letter-spacing:0.14em;text-transform:uppercase;}
.hero-head span{font-size:12px;letter-spacing:0.36em;text-transform:uppercase;color:var(--muted);}
.stats-grid{display:grid;gap:20px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));}
.stat-card{padding:22px;border-radius:20px;background:linear-gradient(145deg,rgba(18,25,56,0.95),rgba(9,14,34,0.95));border:1px solid rgba(110,126,255,0.2);box-shadow:0 20px 52px rgba(8,12,34,0.55);display:flex;flex-direction:column;gap:10px;}
.stat-title{font-size:13px;letter-spacing:0.18em;text-transform:uppercase;color:var(--muted);}
.stat-value{font-family:'Oxanium',cursive;font-size:36px;letter-spacing:0.12em;color:#ffffff;}
.stat-foot{font-size:12px;color:var(--accent-3);letter-spacing:0.18em;text-transform:uppercase;}
.panel-grid{display:grid;gap:26px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));}
.panel-grid.single-column{grid-template-columns:1fr;}
.panel{padding:28px;border-radius:var(--radius);background:linear-gradient(135deg,rgba(17,24,55,0.95),rgba(11,16,40,0.92));border:1px solid var(--border);box-shadow:var(--shadow);display:flex;flex-direction:column;gap:20px;}
.panel.wide{grid-column:1/-1;}
.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;}
.panel-head h3{margin:0;font-family:'Oxanium',cursive;text-transform:uppercase;letter-spacing:0.16em;font-size:18px;}
.panel-head p{margin:6px 0 0;font-size:13px;color:var(--muted);}
.filter-bar{display:flex;gap:12px;flex-wrap:wrap;padding:10px;border-radius:16px;background:rgba(16,23,52,0.6);border:1px solid rgba(110,126,255,0.2);}
.filter-bar input,.filter-bar select{flex:1 1 200px;padding:12px 14px;border-radius:14px;border:1px solid rgba(120,136,255,0.3);background:rgba(6,10,28,0.8);color:var(--text);}
.filter-bar button{margin:0;padding:12px 22px;border-radius:16px;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;font-weight:600;text-transform:uppercase;letter-spacing:0.12em;border:none;cursor:pointer;box-shadow:0 16px 36px rgba(95,91,255,0.4);}
.config-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px;}
.config-card{padding:18px;border-radius:18px;background:rgba(12,18,44,0.92);border:1px solid rgba(108,124,255,0.18);box-shadow:0 20px 46px rgba(6,10,30,0.5);display:flex;flex-direction:column;gap:12px;}
.config-key{font-family:'Oxanium',cursive;font-size:12px;letter-spacing:0.18em;text-transform:uppercase;color:#aeb8ff;}
.config-value{font-size:15px;font-weight:600;color:#f5f7ff;word-break:break-word;}
.config-meta{font-size:11px;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;}
.config-desc{font-size:12px;color:#9ca7ff;line-height:1.4;}
.config-inline{display:flex;flex-direction:column;gap:10px;}
.config-inline input,.config-inline textarea,.config-inline select{padding:10px 12px;border-radius:12px;border:1px solid rgba(110,126,255,0.26);background:rgba(7,12,30,0.85);color:#f8faff;}
.config-inline button{margin:0;align-self:flex-start;}
.config-form{display:flex;flex-direction:column;gap:12px;}
.config-form input,.config-form textarea,.config-form select{padding:12px 14px;border-radius:14px;border:1px solid rgba(120,136,255,0.3);background:rgba(6,10,28,0.82);color:#f8faff;}
textarea{min-height:140px;resize:vertical;}
button{margin-top:16px;padding:12px 22px;border:none;border-radius:18px;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;font-weight:700;text-transform:uppercase;letter-spacing:0.14em;cursor:pointer;box-shadow:0 20px 44px rgba(90,96,255,0.45);transition:transform 0.2s ease,box-shadow 0.2s ease;}
button:hover{transform:translateY(-3px);box-shadow:0 26px 60px rgba(90,96,255,0.6);}
.list-table{width:100%;border-collapse:collapse;border-radius:18px;overflow:hidden;}
.list-table th{font-size:12px;letter-spacing:0.12em;text-transform:uppercase;text-align:left;padding:12px 14px;color:#a3abff;background:rgba(16,23,52,0.65);border-bottom:1px solid rgba(104,120,255,0.2);}
.list-table td{padding:14px;border-bottom:1px solid rgba(60,74,120,0.4);}
.list-table tr:hover{background:rgba(17,26,58,0.6);}
.list-table.compact td{padding:12px 10px;}
.muted{font-size:12px;color:var(--muted);}
.status-pill{display:inline-block;padding:4px 10px;border-radius:999px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;font-weight:600;background:rgba(96,122,255,0.2);color:#cbd4ff;}
.role-pill{display:inline-block;padding:4px 10px;border-radius:12px;font-size:11px;background:rgba(95,91,255,0.18);color:#cbd5ff;margin:2px 4px 2px 0;}
.role-pill.meta{margin-left:auto;background:rgba(95,91,255,0.3);color:#fff;}
.role-chip{display:inline-block;padding:4px 10px;border-radius:12px;font-size:11px;background:rgba(95,91,255,0.18);color:#cbd5ff;margin:2px 4px 2px 0;}
.pill-button{display:inline-flex;align-items:center;justify-content:center;padding:8px 16px;border-radius:999px;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;text-decoration:none;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;font-weight:600;box-shadow:0 18px 36px rgba(95,91,255,0.4);}
.nitro-pill{display:inline-block;padding:4px 8px;border-radius:10px;background:rgba(255,155,0,0.28);color:#ffdca8;font-size:11px;margin-left:6px;letter-spacing:0.05em;}
.manage-panel{gap:18px;}
.manage-meta{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);}
.manage-grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));}
.manage-card{padding:18px;border-radius:16px;background:rgba(15,22,50,0.85);border:1px solid rgba(108,124,255,0.2);display:flex;flex-direction:column;gap:14px;}
.stack-form{display:flex;flex-direction:column;gap:12px;}
.stack-form input,.stack-form select{padding:10px 12px;border-radius:12px;border:1px solid rgba(108,124,255,0.26);background:rgba(8,12,32,0.85);color:#f8faff;}
.stack-form button{margin:0;}
.action-row{display:flex;gap:10px;flex-wrap:wrap;}
.action-row input{flex:1 1 160px;}
.checkbox-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;}
.checkbox-tile{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:12px;background:rgba(12,18,44,0.8);border:1px solid rgba(108,124,255,0.2);font-size:12px;color:#d5dcff;}
.checkbox-tile input{margin:0;}
.role-card{border-radius:18px;border:1px solid rgba(110,126,255,0.18);background:rgba(12,18,44,0.7);margin-bottom:12px;overflow:hidden;}
.role-card summary{cursor:pointer;display:flex;align-items:center;gap:12px;padding:16px 18px;font-weight:600;letter-spacing:0.06em;color:#dde4ff;}
.role-card[open] summary{background:rgba(17,26,58,0.85);}
.color-dot{width:14px;height:14px;border-radius:50%;box-shadow:0 0 12px rgba(255,255,255,0.2);}
.role-body{padding:16px 20px;background:rgba(10,16,40,0.92);}
.member-list{list-style:none;margin:0;padding:0;display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));}
.member-list li{padding:12px;border-radius:12px;background:rgba(16,23,52,0.6);display:flex;flex-direction:column;gap:4px;}
.music-panel{position:relative;padding:32px;border-radius:26px;background:linear-gradient(135deg,#6235ff,#a14cff);box-shadow:0 26px 64px rgba(98,53,255,0.45);color:#fff;overflow:hidden;}
.music-panel:after{content:'';position:absolute;inset:0;background:linear-gradient(120deg,rgba(255,255,255,0.08),rgba(255,255,255,0));pointer-events:none;}
.music-header{display:flex;justify-content:space-between;font-size:13px;letter-spacing:0.24em;text-transform:uppercase;color:rgba(255,255,255,0.68);}
.music-title{font-family:'Oxanium',cursive;font-size:28px;margin:14px 0 6px;letter-spacing:0.12em;}
.music-title a{color:#fff;text-decoration:none;}
.music-sub{font-size:13px;letter-spacing:0.06em;color:rgba(255,255,255,0.75);}
.music-progress{display:flex;align-items:center;gap:12px;margin:22px 0 16px;font-size:12px;letter-spacing:0.1em;}
.progress-track{flex:1;height:6px;border-radius:999px;background:rgba(255,255,255,0.2);overflow:hidden;}
.progress-fill{width:42%;height:100%;background:rgba(255,255,255,0.65);}
.music-controls{display:flex;gap:12px;flex-wrap:wrap;}
.music-controls button{margin:0;padding:12px 18px;border-radius:14px;border:none;background:rgba(6,10,28,0.35);color:#fff;font-size:18px;cursor:pointer;transition:transform 0.2s ease,background 0.2s ease;}
.music-controls button:hover{transform:translateY(-2px);background:rgba(6,10,28,0.55);}
.empty-state{padding:22px;border-radius:16px;background:rgba(15,22,52,0.6);border:1px dashed rgba(110,126,255,0.35);text-align:center;color:#a6b0ff;}
.tab-actions{display:flex;gap:12px;flex-wrap:wrap;}
.tab-actions button{margin:0;}
.tab-actions form{margin:0;}
.checkbox-row{display:flex;align-items:center;gap:10px;font-size:12px;color:#d0d7ff;}
.checkbox-row input{width:auto;margin:0;}
@media(max-width:820px){.dashboard{padding:32px 18px 80px;}.topbar{flex-direction:column;align-items:flex-start;gap:18px;}.nav-tabs a{flex:1 1 45%;}.music-panel{padding:26px;}.music-title{font-size:24px;}.panel-grid{grid-template-columns:1fr;}}
</style>
""".strip()
        return (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'><title>Primeblocks Manager</title>"
            f"{css}</head><body>"
            "<div class='dashboard'>"
            "<header class='topbar'>"
            "<div class='logo'><svg viewBox='0 0 24 24'><path d='M12 2l8 4v6c0 5.25-3.5 10-8 10s-8-4.75-8-10V6l8-4zm0 2.18L6 6.53v5.47c0 4.05 2.46 7.8 6 7.8s6-3.75 6-7.8V6.53l-6-2.35zm0 3.32a3.5 3.5 0 0 1 2.48 5.98L12 17.96l-2.48-4.48A3.5 3.5 0 0 1 12 7.5zm0 2a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z'/></svg></div>"
            f"<div class='titles'><h1>Discord Dashboard</h1><span>{html.escape(subtitle)}</span></div>"
            "</header>"
            f"<nav class='nav-tabs'>{''.join(nav_links)}<a class='logout' href='/logout'>Logout</a></nav>"
            + banner
            + f"<main>{body}</main>"
            "</div></body></html>"
        )

    def _render_login(self, error: Optional[str] = None) -> str:
        error_block = ""
        if error:
            error_block = (
                "<div class='toast error'>"
                f"{html.escape(error)}"
                "</div>"
            )
        return (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'><title>Primeblocks Login</title>"
            "<style>@import url('https://fonts.googleapis.com/css2?family=Oxanium:wght@400;600&family=Montserrat:wght@400;600&display=swap');"
            "*{box-sizing:border-box;}body{margin:0;background:#040312;font-family:'Montserrat',sans-serif;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;}"
            ".card{width:360px;padding:38px;border-radius:24px;background:linear-gradient(135deg,rgba(14,16,34,0.95),rgba(9,12,28,0.9));border:1px solid rgba(86,101,255,0.15);box-shadow:0 30px 80px rgba(5,8,25,0.75);display:flex;flex-direction:column;gap:20px;}"
            ".brand{display:flex;flex-direction:column;gap:6px;align-items:flex-start;}"
            ".brand h1{margin:0;font-family:'Oxanium',cursive;font-size:24px;letter-spacing:0.12em;text-transform:uppercase;color:#f1f5f9;}"
            ".brand span{font-size:12px;text-transform:uppercase;letter-spacing:0.24em;color:#a5b4ff;}"
            "label{display:block;margin:12px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#cbd5f5;font-weight:600;}"
            "input{width:100%;padding:12px 14px;border-radius:16px;border:1px solid rgba(111,130,255,0.25);background:rgba(10,17,35,0.85);color:#f8faff;font-family:'Montserrat',sans-serif;}"
            "input:focus{outline:none;border-color:#60a5fa;box-shadow:0 0 0 3px rgba(96,165,250,0.25);}"
            "button{margin-top:18px;width:100%;padding:12px 16px;border:none;border-radius:18px;background:linear-gradient(135deg,#5b5bff,#8b5bff);color:#f9faff;font-weight:700;text-transform:uppercase;letter-spacing:0.18em;cursor:pointer;box-shadow:0 22px 48px rgba(91,91,255,0.35);}" 
            ".toast{padding:12px 16px;border-radius:14px;background:rgba(239,68,68,0.18);border:1px solid rgba(248,113,113,0.45);color:#fecaca;font-weight:600;text-align:center;letter-spacing:0.04em;}"
            "</style></head><body>"
            "<div class='card'>"
            "<div class='brand'><h1>Primeblocks</h1><span>Secure Access</span></div>"
            f"{error_block}"
            "<form method='post' action='/login'>"
            "<label for='username'>Username</label>"
            "<input id='username' name='username' autocomplete='username' required>"
            "<label for='password'>Passwort</label>"
            "<input id='password' name='password' type='password' autocomplete='current-password' required>"
            "<button type='submit'>Login</button>"
            "</form>"
            "</div></body></html>"
        )

    def _format_value(self, value: object) -> str:
        if isinstance(value, bool):
            return "Ja" if value else "Nein"
        if value is None:
            return "—"
        if isinstance(value, (list, tuple, set)):
            joined = ", ".join(html.escape(str(item)) for item in value)
            return joined or "—"
        return html.escape(str(value))

    def _build_config_cards(
        self,
        *,
        filter_text: Optional[str] = None,
        type_filter: Optional[str] = None,
        redirect: str = "/",
    ) -> str:
        display_values = self.config.to_display_dict()
        descriptions = self.config.schema_description
        query = (filter_text or "").strip().lower()
        type_filter_value = (type_filter or "all").strip().lower()
        cards: List[str] = []
        schema = self.config.schema
        for key in sorted(display_values.keys()):
            value = display_values[key]
            description = descriptions.get(key, "")
            if query and query not in key.lower() and query not in description.lower():
                continue
            meta = schema.get(key, {})
            type_hint = meta.get("type")
            if callable(type_hint):
                type_label = getattr(type_hint, "__name__", str(type_hint))
            else:
                type_label = str(type_hint or "str")
            if type_filter_value not in {"", "all"}:
                if type_label.lower() != type_filter_value:
                    continue
            default_value = meta.get("default")
            default_display = self._format_value(default_value)
            safe_value = "" if value is None else str(value)
            cards.append(
                "<div class='config-card'>"
                f"<span class='config-key'>{html.escape(key)}</span>"
                f"<div class='config-value'>{self._format_value(value)}</div>"
                "<form class='config-inline' method='post' action='/config'>"
                f"<input type='hidden' name='redirect' value='{html.escape(redirect)}'>"
                f"<input type='hidden' name='key' value='{html.escape(key)}'>"
                f"<input name='value' placeholder='Neuer Wert' value='{html.escape(safe_value)}'>"
                "<button type='submit'>Speichern</button>"
                "</form>"
                f"<span class='config-meta'>Typ: {html.escape(type_label)} • Default: {default_display}</span>"
                f"<p class='config-desc'>{html.escape(description)}</p>"
                "</div>"
            )
        if not cards:
            return "<p>Keine passenden Eintraege gefunden.</p>"
        return "<div class='config-grid'>" + "".join(cards) + "</div>"

    async def handle_index(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        message = request.query.get("msg")
        search = request.query.get("search", "").strip()
        type_filter = request.query.get("type", "all").strip().lower()
        guild_count = len(self.bot.guilds)
        total_members = sum(g.member_count or len(getattr(g, "members", ())) for g in self.bot.guilds)
        voice_connections = len(self.bot.voice_clients)
        latency_ms = int(round((self.bot.latency or 0.0) * 1000))
        command_count = len({cmd.qualified_name for cmd in self.bot.commands})
        monitor = getattr(self.bot, "health_monitor", None)
        monitor_snapshot = None
        if monitor:
            monitor_snapshot = await monitor.snapshot()
            latency_ms = int(monitor_snapshot.get("avg_latency", latency_ms))
        subtitle_parts: list[str] = []
        if self.bot.guilds:
            subtitle_parts.append(self.bot.guilds[0].name)
        subtitle_parts.append(f"{guild_count} Server")
        if total_members:
            subtitle_parts.append(f"{total_members} Members")
        subtitle = " • ".join(subtitle_parts) if subtitle_parts else "Server Management Console"
        stats = [
            ("Server", str(guild_count), "verbundene Gilden"),
            ("Mitglieder", f"{total_members:,}".replace(',', '.'), "registrierte Nutzer"),
            ("Voice", str(voice_connections), "aktive Sprachsessions"),
            ("Ping", f"{latency_ms} ms", "Gateway Latenz"),
            ("Commands", str(command_count), "verfuegbare Befehle"),
        ]
        if monitor_snapshot:
            stats.extend(
                [
                    ("Uptime", monitor_snapshot.get("uptime", "-"), "seit Start"),
                    (
                        "HTTP avg",
                        f"{monitor_snapshot.get('http_avg_ms', 0.0):.1f} ms",
                        "Dashboard Requests",
                    ),
                    (
                        "Tasks",
                        str(len(monitor_snapshot.get("tasks", []))),
                        "verfolgt",
                    ),
                ]
            )
        stats_cards = "".join(
            "<div class='stat-card'>"
            f"<div class='stat-title'>{html.escape(title)}</div>"
            f"<div class='stat-value'>{html.escape(value)}</div>"
            f"<div class='stat-foot'>{html.escape(foot)}</div>"
            "</div>"
            for title, value, foot in stats
        )
        type_labels = set()
        for meta in self.config.schema.values():
            type_hint = meta.get("type")
            if callable(type_hint):
                label = getattr(type_hint, "__name__", str(type_hint))
            else:
                label = str(type_hint or "str")
            type_labels.add(label.lower())
        type_options = ["<option value='all' " + ("selected" if type_filter in {"", "all"} else "") + ">Alle Typen</option>"]
        for label in sorted(type_labels):
            selected = "selected" if label == type_filter else ""
            display = label.capitalize()
            type_options.append(f"<option value='{html.escape(label)}' {selected}>{html.escape(display)}</option>")
        filter_form = (
            "<form class='filter-bar' method='get'>"
            f"<input name='search' placeholder='Konfiguration durchsuchen...' value='{html.escape(search)}'>"
            f"<select name='type'>{''.join(type_options)}</select>"
            "<button type='submit'>Filter anwenden</button>"
            "</form>"
        )
        health_panel = ""
        if monitor_snapshot and monitor:
            health_panel = (
                "<div class='panel'>"
                "<div class='panel-head'><div><h3>System Health</h3><p>Laufzeit, Tasks und Request-Statistiken.</p></div></div>"
                f"{monitor.render_table(monitor_snapshot)}"
                "</div>"
            )

        config_section = (
            "<section class='panel-grid single-column'>"
            "<div class='panel wide'>"
            "<div class='panel-head'>"
            "<div><h3>Konfiguration</h3><p>Alle Werte werden direkt in <code>data/config.json</code> gespeichert.</p></div>"
            "</div>"
            f"{filter_form}"
            f"{self._build_config_cards(filter_text=search, type_filter=type_filter, redirect='/')}"
            "</div>"
            "</section>"
        )
        body = (
            "<section class='hero'>"
            "<div class='hero-head'><span>dashboard</span><h2>Server Overview</h2></div>"
            f"<div class='stats-grid'>{stats_cards}</div>"
            "</section>"
            f"<section class='panel-grid'>{health_panel}</section>"
            f"{config_section}"
        )
        page = self._render_layout(active="dashboard", subtitle=subtitle, body=body, message=message)
        return web.Response(text=page, content_type="text/html")

    async def handle_members(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        message = request.query.get("msg")
        search_display = request.query.get("search", "").strip()
        search_term = search_display.lower()
        filter_choice = request.query.get("filter", "all").strip().lower()
        manage_param = request.query.get("manage", "").strip()
        guild = self._primary_guild()
        if guild is None:
            body = (
                "<section class='panel'>"
                "<h3>Keine Guild verbunden</h3>"
                "<p>Bitte setze <code>guild_id</code> in der Konfiguration.</p>"
                "</section>"
            )
            page = self._render_layout(active="members", subtitle="Kein Server", body=body, message=message)
            return web.Response(text=page, content_type="text/html")

        members = list(getattr(guild, "members", []))
        if guild.member_count is not None and guild.member_count > 0:
            total_members = guild.member_count
        else:
            total_members = len(members)
        if not members and total_members == 0:
            subtitle = f"{guild.name} • Memberverwaltung"
            body = (
                "<section class='panel'>"
                "<h3>Keine Mitglieder geladen</h3>"
                "<p>Aktiviere den Member Intent und starte den Bot neu.</p>"
                "</section>"
            )
            page = self._render_layout(active="members", subtitle=subtitle, body=body, message=message)
            return web.Response(text=page, content_type="text/html")

        def member_online(member: discord.Member) -> bool:
            status = getattr(member, "status", None)
            raw_status = getattr(member, "raw_status", None)
            if status in {discord.Status.online, discord.Status.idle, discord.Status.dnd}:
                return True
            if raw_status and raw_status not in {"offline", "invisible", None}:  # type: ignore[arg-type]
                return True
            voice_state = getattr(member, "voice", None)
            if voice_state and getattr(voice_state, "channel", None) is not None:
                return True
            return False

        humans = sum(1 for m in members if not m.bot)
        bots = max(total_members - humans, 0)
        online = sum(1 for m in members if member_online(m))
        nitro_count = sum(1 for m in members if getattr(m, "premium_since", None))

        filtered_members: List[discord.Member] = []
        for member in members:
            if filter_choice == "humans" and member.bot:
                continue
            if filter_choice == "bots" and not member.bot:
                continue
            if filter_choice == "online" and not member_online(member):
                continue
            if filter_choice == "nitro" and not getattr(member, "premium_since", None):
                continue
            filtered_members.append(member)

        if search_term:
            filtered_members = [
                member
                for member in filtered_members
                if search_term in member.display_name.lower()
                or search_term in member.name.lower()
                or search_term in str(member.id)
            ]

        def sort_key(member: discord.Member) -> tuple:
            joined = getattr(member, "joined_at", None)
            if joined is None:
                joined_key = datetime.fromtimestamp(0, timezone.utc)
            elif joined.tzinfo is None:
                joined_key = joined.replace(tzinfo=timezone.utc)
            else:
                joined_key = joined.astimezone(timezone.utc)
            return (not member_online(member), joined_key)

        display_members = sorted(filtered_members, key=sort_key)[:40]

        filter_options = [
            ("all", "Alle"),
            ("humans", "Humans"),
            ("bots", "Bots"),
            ("online", "Online"),
            ("nitro", "Nitro"),
        ]
        filter_select = []
        for value, label in filter_options:
            selected = "selected" if value == filter_choice else ""
            filter_select.append(f"<option value='{value}' {selected}>{label}</option>")

        rows: List[str] = []
        for member in display_members:
            status_badge = self._status_badge(getattr(member, "status", None))
            roles = [role for role in getattr(member, "roles", []) if hasattr(role, "is_default") and not role.is_default()]
            roles_html = "".join(
                f"<span class='role-chip'>{html.escape(role.name)}</span>" for role in roles[:3]
            )
            if not roles_html:
                roles_html = "<span class='muted'>Keine Rollen</span>"
            joined_text = self._format_datetime(getattr(member, "joined_at", None))
            nitro_badge = "<span class='nitro-pill'>Nitro</span>" if getattr(member, "premium_since", None) else "—"
            manage_params = []
            if search_display:
                manage_params.append("search=" + quote(search_display))
            if filter_choice not in {"", "all"}:
                manage_params.append("filter=" + quote(filter_choice))
            manage_params.append(f"manage={member.id}")
            manage_link = "/members?" + "&".join(manage_params) + "#member-manage"
            rows.append(
                "<tr>"
                f"<td><strong>{html.escape(member.display_name)}</strong><br><span class='muted'>@{html.escape(member.name)} • {member.id}</span></td>"
                f"<td>{status_badge}</td>"
                f"<td>{'Bot' if member.bot else 'User'}</td>"
                f"<td>{nitro_badge}</td>"
                f"<td>{html.escape(joined_text)}</td>"
                f"<td>{roles_html}</td>"
                f"<td><a class='pill-button' href='{manage_link}'>Manage</a></td>"
                "</tr>"
            )

        if rows:
            members_table = (
                "<table class='list-table'>"
                "<thead><tr><th>Member</th><th>Status</th><th>Typ</th><th>Nitro</th><th>Join</th><th>Rollen</th><th></th></tr></thead>"
                "<tbody>" + "".join(rows) + "</tbody></table>"
            )
        else:
            if search_term or filter_choice not in {"", "all"}:
                empty_text = "Keine passenden Mitglieder gefunden."
            else:
                empty_text = "Mitglieder konnten nicht geladen werden."
            members_table = f"<div class='empty-state'>{html.escape(empty_text)}</div>"

        filter_form = (
            "<form class='filter-bar' method='get'>"
            f"<input name='search' placeholder='Member suchen...' value='{html.escape(search_display)}'>"
            f"<select name='filter'>{''.join(filter_select)}</select>"
            "<button type='submit'>Filter anwenden</button>"
            "</form>"
        )

        manage_panel = ""
        if manage_param.isdigit():
            manage_id = int(manage_param)
            manage_member = guild.get_member(manage_id)
            if manage_member is None:
                try:
                    manage_member = await guild.fetch_member(manage_id)
                except (discord.NotFound, discord.Forbidden):
                    manage_member = None
            if manage_member is not None:
                joined_text = self._format_datetime(getattr(manage_member, "joined_at", None))
                created_text = self._format_datetime(getattr(manage_member, "created_at", None))
                nitro_label = "Ja" if getattr(manage_member, "premium_since", None) else "Nein"
                roles_sorted = [role for role in manage_member.guild.roles if not role.is_default()]
                role_checkboxes: List[str] = []
                for role in roles_sorted:
                    checked = "checked" if role in manage_member.roles else ""
                    role_checkboxes.append(
                        "<label class='checkbox-tile'>"
                        f"<input type='checkbox' name='roles' value='{role.id}' {checked}>"
                        f"<span>{html.escape(role.name)}</span>"
                        "</label>"
                    )
                redirect_params: List[str] = []
                if search_display:
                    redirect_params.append("search=" + quote(search_display))
                if filter_choice not in {"", "all"}:
                    redirect_params.append("filter=" + quote(filter_choice))
                redirect_params.append(f"manage={manage_member.id}")
                redirect_url = "/members"
                if redirect_params:
                    redirect_url += "?" + "&".join(redirect_params)
                action_form = (
                    "<form method='post' action='/members/action' class='stack-form'>"
                    f"<input type='hidden' name='redirect' value='{html.escape(redirect_url)}'>"
                    f"<input type='hidden' name='member_id' value='{manage_member.id}'>"
                    "<label>Aktion</label>"
                    "<div class='action-row'>"
                    "<select name='operation'>"
                    "<option value='kick'>Kick</option>"
                    "<option value='ban'>Ban</option>"
                    "<option value='timeout'>Timeout</option>"
                    "<option value='untimeout'>Timeout entfernen</option>"
                    "<option value='unban'>Unban (ID)</option>"
                    "</select>"
                    "<input name='duration' placeholder='Dauer z.B. 15m'>"
                    "</div>"
                    "<label>Grund</label><input name='reason' placeholder='Optional'>"
                    "<button type='submit'>Aktion ausfuehren</button>"
                    "</form>"
                )
                roles_form = (
                    "<form method='post' action='/members/action' class='stack-form'>"
                    f"<input type='hidden' name='redirect' value='{html.escape(redirect_url)}'>"
                    f"<input type='hidden' name='member_id' value='{manage_member.id}'>"
                    "<input type='hidden' name='operation' value='set_roles'>"
                    "<label>Rollen verwalten</label>"
                    "<div class='checkbox-grid'>" + "".join(role_checkboxes) + "</div>"
                    "<label>Grund</label><input name='reason' placeholder='Optional'>"
                    "<button type='submit'>Rollen speichern</button>"
                    "</form>"
                )
                manage_panel = (
                    "<div class='panel wide manage-panel' id='member-manage'>"
                    f"<div class='panel-head'><div><h3>Manage {html.escape(manage_member.display_name)}</h3><p>ID {manage_member.id}</p></div></div>"
                    f"<div class='manage-meta'><span>Joined: {html.escape(joined_text)}</span><span>Account: {html.escape(created_text)}</span><span>Nitro: {nitro_label}</span></div>"
                    "<div class='manage-grid'>"
                    f"<div class='manage-card'>{action_form}</div>"
                    f"<div class='manage-card'>{roles_form}</div>"
                    "</div>"
                    "</div>"
                )
            else:
                manage_panel = "<div class='panel wide manage-panel'><p class='muted'>Member konnte nicht geladen werden.</p></div>"

        stats = [
            ("Mitglieder", f"{total_members:,}".replace(',', '.'), "gesamt"),
            ("Online", str(online), "aktuell"),
            ("Humans", str(max(humans, 0)), "Personen"),
            ("Bots", str(max(bots, 0)), "Automationen"),
            ("Nitro", str(nitro_count), "Boosts"),
        ]
        stats_cards = "".join(
            "<div class='stat-card'>"
            f"<div class='stat-title'>{html.escape(title)}</div>"
            f"<div class='stat-value'>{html.escape(value)}</div>"
            f"<div class='stat-foot'>{html.escape(foot)}</div>"
            "</div>"
            for title, value, foot in stats
        )

        hero = (
            "<section class='hero'>"
            "<div class='hero-head'><span>members</span><h2>Member Verwaltung</h2></div>"
            f"<div class='stats-grid'>{stats_cards}</div>"
            "</section>"
        )

        table_panel = (
            "<div class='panel wide'>"
            "<div class='panel-head'><div><h3>Memberliste</h3><p>Bis zu 40 Eintraege, sortiert nach Aktivitaet.</p></div></div>"
            f"{filter_form}"
            f"{members_table}"
            "</div>"
        )

        body = hero + "<section class='panel-grid single-column'>" + table_panel + (manage_panel or "") + "</section>"
        subtitle = f"{guild.name} • Memberverwaltung"
        page = self._render_layout(active="members", subtitle=subtitle, body=body, message=message)
        return web.Response(text=page, content_type="text/html")

    async def handle_member_action(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = await request.post()
        redirect_raw = self._coerce_form_value(data.get("redirect")) or "/members"
        redirect_target = self._safe_redirect_target(redirect_raw)
        member_id_raw = self._coerce_form_value(data.get("member_id")).strip()
        operation = self._coerce_form_value(data.get("operation")).strip().lower()
        reason = self._coerce_form_value(data.get("reason")).strip() or "Dashboard Aktion"
        duration_raw = self._coerce_form_value(data.get("duration")).strip()
        guild = self._primary_guild()
        if guild is None:
            raise self._redirect(redirect_target, "Keine Guild verbunden.")
        if not member_id_raw:
            raise self._redirect(redirect_target, "Bitte eine Member ID angeben.")
        valid_operations = {"kick", "ban", "timeout", "untimeout", "unban", "set_roles"}
        if operation not in valid_operations:
            raise self._redirect(redirect_target, "Unbekannte Aktion.")
        try:
            member_id = int(member_id_raw)
        except ValueError:
            raise self._redirect(redirect_target, "Member ID muss eine Zahl sein.")

        member: Optional[discord.Member] = None
        if operation in {"kick", "timeout", "untimeout", "ban", "set_roles"}:
            member = guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except (discord.Forbidden, discord.NotFound):
                    member = None
                except discord.HTTPException as exc:
                    raise self._redirect(redirect_target, f"Member konnte nicht geladen werden: {exc}")

        try:
            if operation == "kick":
                if member is None:
                    raise self._redirect(redirect_target, "Member ist nicht auf dem Server.")
                await guild.kick(member, reason=reason)
                raise self._redirect(redirect_target, f"{member.display_name} gekickt.")
            if operation == "ban":
                target = member or discord.Object(id=member_id)
                await guild.ban(target, reason=reason, delete_message_seconds=0)
                raise self._redirect(redirect_target, f"ID {member_id} gebannt.")
            if operation == "timeout":
                if member is None:
                    raise self._redirect(redirect_target, "Member nicht gefunden.")
                seconds = self._parse_timespan(duration_raw)
                if not seconds:
                    raise self._redirect(redirect_target, "Bitte eine Dauer (z.B. 10m) angeben.")
                until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                await member.timeout(until, reason=reason)
                raise self._redirect(redirect_target, f"Timeout fuer {member.display_name} gesetzt.")
            if operation == "untimeout":
                if member is None:
                    raise self._redirect(redirect_target, "Member nicht gefunden.")
                await member.timeout(None, reason=reason)
                raise self._redirect(redirect_target, f"Timeout fuer {member.display_name} aufgehoben.")
            if operation == "unban":
                try:
                    ban_entry = await guild.fetch_ban(discord.Object(id=member_id))
                except discord.NotFound:
                    raise self._redirect(redirect_target, "Kein Ban fuer diese ID gefunden.")
                await guild.unban(ban_entry.user, reason=reason)
                raise self._redirect(redirect_target, f"{ban_entry.user} wurde entbannt.")
            if operation == "set_roles":
                if member is None:
                    raise self._redirect(redirect_target, "Member nicht gefunden.")
                raw_roles = []
                try:
                    raw_roles = data.getall("roles")  # type: ignore[attr-defined]
                except AttributeError:
                    value = data.get("roles")
                    if value is not None:
                        raw_roles = [value]
                role_ids = []
                for raw in raw_roles:
                    if not raw:
                        continue
                    try:
                        role_ids.append(int(str(raw)))
                    except ValueError:
                        continue
                default_role = guild.default_role
                new_roles: List[discord.Role] = [default_role]
                bot_member = guild.me
                top_role = getattr(bot_member, "top_role", None)
                for role_id in role_ids:
                    role_obj = guild.get_role(role_id)
                    if role_obj is None:
                        continue
                    if top_role and role_obj >= top_role:
                        continue
                    new_roles.append(role_obj)
                await member.edit(roles=new_roles, reason=reason or None)
                raise self._redirect(redirect_target, f"Rollen fuer {member.display_name} aktualisiert.")
        except discord.Forbidden:
            raise self._redirect(redirect_target, "Bot hat keine Berechtigung fuer diese Aktion.")
        except discord.HTTPException as exc:
            raise self._redirect(redirect_target, f"Discord Fehler: {exc}")

        raise self._redirect(redirect_target, "Keine Aktion ausgefuehrt.")

    async def handle_announcements(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        message = request.query.get("msg")
        guild = self._primary_guild()
        if guild is None:
            body = (
                "<section class='panel'>"
                "<h3>Keine Guild verbunden</h3>"
                "<p>Ohne Server kann keine Ankuendigung gesendet werden.</p>"
                "</section>"
            )
            page = self._render_layout(active="announcements", subtitle="Kein Server", body=body, message=message)
            return web.Response(text=page, content_type="text/html")

        channels = [channel for channel in guild.text_channels]
        selected_channel_id: Optional[int] = None
        query_channel = request.query.get("channel", "").strip()
        if query_channel.isdigit():
            selected_channel_id = int(query_channel)
        if selected_channel_id is None and channels:
            selected_channel_id = channels[0].id

        channel_options = []
        for channel in channels:
            selected = "selected" if selected_channel_id == channel.id else ""
            category = f"[{channel.category.name}] " if channel.category else ""
            label = f"{category}#{channel.name}"
            channel_options.append(
                f"<option value='{channel.id}' {selected}>{html.escape(label)}</option>"
            )

        roles = [role for role in guild.roles if not role.is_default()]
        role_options = ["<option value='0'>Keine Rolle</option>"]
        for role in roles:
            role_options.append(f"<option value='{role.id}'>{html.escape(role.name)}</option>")

        stats = [
            ("Kanaele", str(len(channels)), "Text"),
            ("Rollen", str(len(roles)), "pingbar"),
            ("Embeds", "Pflicht", "gestaltet"),
        ]
        stats_cards = "".join(
            "<div class='stat-card'>"
            f"<div class='stat-title'>{html.escape(title)}</div>"
            f"<div class='stat-value'>{html.escape(value)}</div>"
            f"<div class='stat-foot'>{html.escape(foot)}</div>"
            "</div>"
            for title, value, foot in stats
        )

        hero = (
            "<section class='hero'>"
            "<div class='hero-head'><span>announcements</span><h2>Embed Ankuendigungen</h2></div>"
            f"<div class='stats-grid'>{stats_cards}</div>"
            "</section>"
        )

        form_panel = (
            "<div class='panel'>"
            "<h3>Nachricht verfassen</h3>"
            "<p>Sende stilvolle Embeds mit kontrollierten Mentions.</p>"
            "<form method='post' action='/announcements/send' class='config-form'>"
            "<input type='hidden' name='redirect' value='/announcements'>"
            "<label>Zielkanal</label>"
            f"<select name='channel_id'>{''.join(channel_options)}</select>"
            "<label>Embed Titel</label><input name='embed_title' placeholder='Titel'>"
            "<label>Embed Beschreibung</label><textarea name='embed_description' placeholder='Beschreibung' required></textarea>"
            "<label>Embed Farbe</label><input name='embed_color' placeholder='#5865f2'>"
            "<label>Rolle pingen</label>"
            f"<select name='mention_role'>{''.join(role_options)}</select>"
            "<div class='checkbox-row'><input type='checkbox' name='allow_everyone' id='allow_everyone'><label for='allow_everyone'>@everyone erlauben</label></div>"
            "<div class='checkbox-row'><input type='checkbox' name='allow_users' id='allow_users'><label for='allow_users'>User-Mentions erlauben</label></div>"
            "<button type='submit'>Senden</button>"
            "</form>"
            "</div>"
        )

        body = hero + "<section class='panel-grid single-column'>" + form_panel + "</section>"
        subtitle = f"{guild.name} • Ankuendigungen"
        page = self._render_layout(active="announcements", subtitle=subtitle, body=body, message=message)
        return web.Response(text=page, content_type="text/html")

    async def handle_announcement_send(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = await request.post()
        redirect_raw = self._coerce_form_value(data.get("redirect")) or "/announcements"
        redirect_target = self._safe_redirect_target(redirect_raw)
        channel_id_raw = self._coerce_form_value(data.get("channel_id")).strip()
        embed_title = self._coerce_form_value(data.get("embed_title")).strip()
        embed_description = self._coerce_form_value(data.get("embed_description")).strip()
        embed_color_raw = self._coerce_form_value(data.get("embed_color")).strip()
        mention_role_raw = self._coerce_form_value(data.get("mention_role")).strip()
        allow_everyone = self._coerce_form_value(data.get("allow_everyone")).lower() in {"1", "true", "on", "yes"}
        allow_users = self._coerce_form_value(data.get("allow_users")).lower() in {"1", "true", "on", "yes"}

        if not channel_id_raw:
            raise self._redirect(redirect_target, "Bitte einen Kanal auswaehlen.")
        try:
            channel_id = int(channel_id_raw)
        except ValueError:
            raise self._redirect(redirect_target, "Kanal ID muss numerisch sein.")

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound) as exc:
                raise self._redirect(redirect_target, f"Kanal konnte nicht geladen werden: {exc}")
            except discord.HTTPException as exc:
                raise self._redirect(redirect_target, f"Discord Fehler: {exc}")

        if not isinstance(channel, Messageable):
            raise self._redirect(redirect_target, "Dieser Kanal akzeptiert keine Nachrichten.")

        embed = None
        embed_color: Optional[int] = None
        if embed_color_raw:
            value = embed_color_raw.lstrip("#")
            try:
                embed_color = int(value, 16)
            except ValueError:
                raise self._redirect(redirect_target, "Farbe muss hexadezimal sein (z.B. #5865f2).")
        if not embed_description and not embed_title:
            raise self._redirect(redirect_target, "Embed muss mindestens einen Titel oder Beschreibung haben.")

        embed = discord.Embed(title=embed_title or None, description=embed_description or None)
        if embed_color is not None:
            embed.color = discord.Color(embed_color)

        mention_role_text = None
        allow_roles = False
        if mention_role_raw and mention_role_raw != "0":
            try:
                role_id = int(mention_role_raw)
            except ValueError:
                raise self._redirect(redirect_target, "Rollen ID muss numerisch sein.")
            allow_roles = True
            mention_role_text = f"<@&{role_id}>"
        else:
            allow_roles = False

        allowed_mentions = AllowedMentions(
            everyone=allow_everyone,
            roles=allow_roles,
            users=allow_users,
        )

        final_content = (mention_role_text or "").strip()
        content_to_send = final_content or None
        try:
            if embed is not None:
                await channel.send(content_to_send, embed=embed, allowed_mentions=allowed_mentions)
            else:
                await channel.send(content_to_send, allowed_mentions=allowed_mentions)
        except discord.Forbidden:
            raise self._redirect(redirect_target, "Bot darf in diesem Kanal nicht senden.")
        except discord.HTTPException as exc:
            raise self._redirect(redirect_target, f"Nachricht fehlgeschlagen: {exc}")

        raise self._redirect(redirect_target, "Nachricht gesendet.")

    async def handle_roles(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        message = request.query.get("msg")
        guild = self._primary_guild()
        if guild is None:
            body = (
                "<section class='panel'>"
                "<h3>Keine Guild verbunden</h3>"
                "<p>Rollen koennen erst verwaltet werden, wenn ein Server gesetzt ist.</p>"
                "</section>"
            )
            page = self._render_layout(active="roles", subtitle="Kein Server", body=body, message=message)
            return web.Response(text=page, content_type="text/html")

        search_display = request.query.get("search", "").strip()
        search_term = search_display.lower()
        filter_choice = request.query.get("filter", "all").strip().lower()

        roles = [role for role in guild.roles if not role.is_default()]
        roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)
        total_roles = len(roles_sorted)
        highest_role = roles_sorted[0].name if roles_sorted else "—"
        admin_roles = sum(1 for role in roles_sorted if role.permissions.administrator)
        mentionable_roles = sum(1 for role in roles_sorted if role.mentionable)

        filtered_roles: List[discord.Role] = []
        for role in roles_sorted:
            if search_term and search_term not in role.name.lower():
                continue
            if filter_choice == "admin" and not role.permissions.administrator:
                continue
            if filter_choice == "managed" and not role.managed:
                continue
            if filter_choice == "mentionable" and not role.mentionable:
                continue
            filtered_roles.append(role)

        stats = [
            ("Rollen", str(total_roles), "gesamt"),
            ("Admin", str(admin_roles), "mit Rechten"),
            ("Mentionable", str(mentionable_roles), "pingbar"),
            ("Top Rolle", highest_role, "Position"),
        ]
        stats_cards = "".join(
            "<div class='stat-card'>"
            f"<div class='stat-title'>{html.escape(title)}</div>"
            f"<div class='stat-value'>{html.escape(value)}</div>"
            f"<div class='stat-foot'>{html.escape(foot)}</div>"
            "</div>"
            for title, value, foot in stats
        )

        hero = (
            "<section class='hero'>"
            "<div class='hero-head'><span>roles</span><h2>Rollen Verwaltung</h2></div>"
            f"<div class='stats-grid'>{stats_cards}</div>"
            "</section>"
        )

        filter_options = [
            ("all", "Alle"),
            ("admin", "Admin"),
            ("managed", "Managed"),
            ("mentionable", "Mentionable"),
        ]
        filter_select = []
        for value, label in filter_options:
            selected = "selected" if value == filter_choice else ""
            filter_select.append(f"<option value='{value}' {selected}>{label}</option>")

        filter_form = (
            "<form class='filter-bar' method='get'>"
            f"<input name='search' placeholder='Rollen suchen...' value='{html.escape(search_display)}'>"
            f"<select name='filter'>{''.join(filter_select)}</select>"
            "<button type='submit'>Filter anwenden</button>"
            "</form>"
        )

        role_blocks: List[str] = []
        for role in filtered_roles:
            members_list = sorted(role.members, key=lambda m: m.display_name.lower())
            member_items = []
            for member in members_list[:40]:
                nitro_icon = "<span class='nitro-pill'>Nitro</span>" if getattr(member, "premium_since", None) else ""
                member_items.append(
                    "<li>"
                    f"<span>{html.escape(member.display_name)}</span>"
                    f"<span class='muted'>@{html.escape(member.name)} • {member.id}</span>"
                    f"{nitro_icon}"
                    "</li>"
                )
            if members_list and len(members_list) > 40:
                member_items.append("<li class='muted'>… weitere Mitglieder</li>")
            if not member_items:
                member_items.append("<li class='muted'>Keine Mitglieder</li>")
            color_hex = f"#{role.color.value:06x}" if role.color.value else "#5865f2"
            summary = (
                f"<span class='role-name'>{html.escape(role.name)}</span>"
                f"<span class='role-count'>{len(role.members)} Member</span>"
                f"<span class='role-meta'>{html.escape(color_hex)}</span>"
            )
            pills = []
            if role.permissions.administrator:
                pills.append("<span class='role-pill meta'>Admin</span>")
            if role.mentionable:
                pills.append("<span class='role-pill meta'>Mentionable</span>")
            if role.managed:
                pills.append("<span class='role-pill meta'>Managed</span>")
            details_html = (
                "<details class='role-card'>"
                f"<summary><span class='color-dot' style='background:{color_hex}'></span>{summary}{''.join(pills)}</summary>"
                f"<div class='role-body'><ul class='member-list'>{''.join(member_items)}</ul></div>"
                "</details>"
            )
            role_blocks.append(details_html)

        role_section = (
            "<div class='panel wide'>"
            "<div class='panel-head'><div><h3>Rollenliste</h3><p>Klappe eine Rolle auf, um Mitglieder zu sehen.</p></div></div>"
            f"{filter_form}"
            f"{''.join(role_blocks) if role_blocks else '<div class=\'empty-state\'>Keine Rollen entsprechen dem Filter.</div>'}"
            "</div>"
        )

        body = hero + "<section class='panel-grid single-column'>" + role_section + "</section>"
        subtitle = f"{guild.name} • Rollen"
        page = self._render_layout(active="roles", subtitle=subtitle, body=body, message=message)
        return web.Response(text=page, content_type="text/html")

    async def handle_role_update(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = await request.post()
        redirect_raw = self._coerce_form_value(data.get("redirect")) or "/roles"
        redirect_target = self._safe_redirect_target(redirect_raw)
        target = self._coerce_form_value(data.get("target")).strip().lower()
        role_id_raw = self._coerce_form_value(data.get("role_id")).strip()
        guild = self._primary_guild()
        if guild is None:
            raise self._redirect(redirect_target, "Keine Guild verbunden.")
        mapping = ROLE_TARGETS.get(target)
        if mapping is None:
            raise self._redirect(redirect_target, "Unbekannter Rollen-Typ.")
        config_key, label = mapping
        if not role_id_raw:
            role_id = 0
        else:
            try:
                role_id = int(role_id_raw)
            except ValueError:
                raise self._redirect(redirect_target, "Rollen ID muss numerisch sein.")
            if role_id and guild.get_role(role_id) is None:
                raise self._redirect(redirect_target, "Rolle nicht gefunden.")
        self.config.set_value(config_key, role_id)
        if role_id:
            role_obj = guild.get_role(role_id)
            role_name = role_obj.name if role_obj else str(role_id)
            raise self._redirect(redirect_target, f"{label} gesetzt auf {role_name}.")
        raise self._redirect(redirect_target, f"{label} entfernt.")

    async def handle_modlog(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        message = request.query.get("msg")
        guild = self._primary_guild()
        if guild is None:
            body = (
                "<section class='panel'>"
                "<h3>Keine Guild verbunden</h3>"
                "<p>Modlog erfordert einen aktiven Server.</p>"
                "</section>"
            )
            page = self._render_layout(active="modlog", subtitle="Kein Server", body=body, message=message)
            return web.Response(text=page, content_type="text/html")

        entries = []
        error_box = ""
        try:
            async for entry in guild.audit_logs(limit=20):
                entries.append(entry)
        except discord.Forbidden:
            error_box = "<div class='toast error'>Bot benoetigt 'View Audit Log'.</div>"
        except discord.HTTPException as exc:
            error_box = f"<div class='toast error'>Audit Logs nicht verfuegbar: {html.escape(str(exc))}</div>"

        rows = []
        for entry in entries:
            action_name = entry.action.name.replace("_", " ").title()
            moderator = entry.user
            target = entry.target
            reason = entry.reason or "—"
            timestamp = self._format_datetime(entry.created_at)
            moderator_text = html.escape(str(moderator)) if moderator else "—"
            target_text = html.escape(str(target)) if target else "—"
            rows.append(
                "<tr>"
                f"<td>{html.escape(action_name)}</td>"
                f"<td>{target_text}</td>"
                f"<td>{moderator_text}</td>"
                f"<td>{html.escape(timestamp)}</td>"
                f"<td>{html.escape(reason)}</td>"
                "</tr>"
            )

        if rows:
            table_html = (
                "<table class='list-table'>"
                "<thead><tr><th>Aktion</th><th>Ziel</th><th>Moderator</th><th>Zeit</th><th>Grund</th></tr></thead>"
                "<tbody>" + "".join(rows) + "</tbody></table>"
            )
        else:
            table_html = "<div class='empty-state'>Keine Audit Log Eintraege verfuegbar.</div>"

        stats = [
            ("Audit Logs", str(len(entries)), "Eintraege"),
            ("Moderatoren", str(len({entry.user for entry in entries if entry.user})), "beteiligt"),
            ("Aktionen", "Ban/Kick/etc", "ueberblick"),
        ]
        stats_cards = "".join(
            "<div class='stat-card'>"
            f"<div class='stat-title'>{html.escape(title)}</div>"
            f"<div class='stat-value'>{html.escape(value)}</div>"
            f"<div class='stat-foot'>{html.escape(foot)}</div>"
            "</div>"
            for title, value, foot in stats
        )

        hero = (
            "<section class='hero'>"
            "<div class='hero-head'><span>mod log</span><h2>Moderation Log</h2></div>"
            f"<div class='stats-grid'>{stats_cards}</div>"
            "</section>"
        )

        panel = (
            "<div class='panel wide'>"
            "<h3>Letzte Aktionen</h3>"
            "<p>Zeigt bis zu 20 Eintraege aus dem Audit Log.</p>"
            f"{table_html}"
            "</div>"
        )

        body = hero + error_box + "<section class='panel-grid'>" + panel + "</section>"
        subtitle = f"{guild.name} • Mod Log"
        page = self._render_layout(active="modlog", subtitle=subtitle, body=body, message=message)
        return web.Response(text=page, content_type="text/html")

    async def handle_music(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        message = request.query.get("msg")
        guild = self._primary_guild()
        if guild is None:
            body = (
                "<section class='panel'>"
                "<h3>Keine Guild verbunden</h3>"
                "<p>Verbinde zuerst einen Server, um die Musiksteuerung zu nutzen.</p>"
                "</section>"
            )
            page = self._render_layout(active="music", subtitle="Kein Server", body=body, message=message)
            return web.Response(text=page, content_type="text/html")

        music_cog = self.bot.get_cog("Music")
        if music_cog is None or not hasattr(music_cog, "get_player"):
            body = (
                "<section class='panel'>"
                "<h3>Music Cog nicht geladen</h3>"
                "<p>Lade die Erweiterung <code>cogs.music</code>, um die Queue zu verwalten.</p>"
                "</section>"
            )
            page = self._render_layout(active="music", subtitle=f"{guild.name} • Music", body=body, message=message)
            return web.Response(text=page, content_type="text/html")

        player = None
        queue_items = []
        current_track = None
        try:
            player = music_cog.get_player(guild)  # type: ignore[attr-defined]
        except Exception:
            player = None
        if player is not None:
            current_track = getattr(player, "current", None)
            if hasattr(player, "queue_items"):
                try:
                    queue_items = list(player.queue_items())  # type: ignore[call-arg]
                except Exception:
                    queue_items = []

        voice_client = cast(Optional[discord.VoiceClient], guild.voice_client)
        voice_channel = getattr(getattr(voice_client, "channel", None), "name", "—") if voice_client else "—"
        debug_enabled = False
        debug_map = getattr(music_cog, "debug_enabled", None)
        if isinstance(debug_map, dict):
            debug_enabled = bool(debug_map.get(guild.id))

        queue_length = len(queue_items)
        queue_filter = request.query.get("queue_filter", "all").strip().lower()
        if queue_filter == "humans":
            queue_items_display = [track for track in queue_items if not getattr(track.requester, "bot", False)]
        elif queue_filter == "bots":
            queue_items_display = [track for track in queue_items if getattr(track.requester, "bot", False)]
        else:
            queue_items_display = queue_items

        stats = [
            ("Queue", str(queue_length), "wartend"),
            ("Aktuell", getattr(current_track, "title", "—"), "Track"),
            ("Voice", voice_channel, "Channel"),
            ("Debug", "Aktiv" if debug_enabled else "Aus", "Logging"),
        ]
        stats_cards = "".join(
            "<div class='stat-card'>"
            f"<div class='stat-title'>{html.escape(title)}</div>"
            f"<div class='stat-value'>{html.escape(str(value))}</div>"
            f"<div class='stat-foot'>{html.escape(foot)}</div>"
            "</div>"
            for title, value, foot in stats
        )

        hero = (
            "<section class='hero'>"
            "<div class='hero-head'><span>music</span><h2>Musik Steuerung</h2></div>"
            f"<div class='stats-grid'>{stats_cards}</div>"
            "</section>"
        )

        current_title = getattr(current_track, "title", "Kein Track") if current_track else "Kein Track"
        current_duration = self._format_duration(getattr(current_track, "duration", None)) if current_track else "0:00"
        current_url = getattr(current_track, "webpage_url", "") if current_track else ""
        requester = getattr(getattr(current_track, "requester", None), "display_name", "—") if current_track else "—"
        playback_state = "Paused" if voice_client and voice_client.is_paused() else ("Playing" if voice_client and voice_client.is_playing() else "Idle")
        progress = "00:00"
        link_html = (
            f"<a href='{html.escape(current_url)}' target='_blank'>{html.escape(current_title)}</a>"
            if current_url else html.escape(current_title)
        )

        toggle_label = "Debug an" if not debug_enabled else "Debug aus"

        player_card = (
            "<div class='panel music-panel'>"
            "<div class='music-header'>"
            f"<span class='music-state'>{html.escape(playback_state)}</span>"
            f"<span class='music-channel'>{html.escape(voice_channel)}</span>"
            "</div>"
            f"<div class='music-title'>{link_html}</div>"
            f"<div class='music-sub'>Requester: {html.escape(requester)} • Dauer: {html.escape(current_duration)}</div>"
            "<div class='music-progress'>"
            f"<span>{progress}</span>"
            "<div class='progress-track'><div class='progress-fill'></div></div>"
            f"<span>{html.escape(current_duration)}</span>"
            "</div>"
            "<form method='post' action='/music/action' class='music-controls'>"
            "<input type='hidden' name='redirect' value='/music'>"
            "<button name='operation' value='stop' title='Stop'>⏹</button>"
            "<button name='operation' value='pause' title='Pause'>⏸</button>"
            "<button name='operation' value='resume' title='Play'>▶️</button>"
            "<button name='operation' value='skip' title='Skip'>⏭</button>"
            "<button name='operation' value='clear' title='Queue leeren'>🧹</button>"
            "<button name='operation' value='toggle_debug' title='Debug umschalten'>🛠</button>"
            "</form>"
            "</div>"
        )

        queue_rows = []
        for index, track in enumerate(queue_items_display, start=1):
            title = getattr(track, "title", "Unbekannt")
            duration_text = self._format_duration(getattr(track, "duration", None))
            requester_obj = getattr(track, "requester", None)
            requester_name = getattr(requester_obj, "display_name", str(requester_obj)) if requester_obj else "—"
            queue_rows.append(
                "<tr>"
                f"<td>#{index}</td>"
                f"<td>{html.escape(title)}</td>"
                f"<td>{html.escape(duration_text)}</td>"
                f"<td>{html.escape(requester_name)}</td>"
                "</tr>"
            )

        if queue_rows:
            queue_table = (
                "<table class='list-table compact'>"
                "<thead><tr><th>#</th><th>Titel</th><th>Dauer</th><th>Requester</th></tr></thead>"
                "<tbody>" + "".join(queue_rows) + "</tbody></table>"
            )
        else:
            queue_table = "<div class='empty-state'>Keine Tracks in der Queue.</div>"

        queue_filter_options = [
            ("all", "Alle"),
            ("humans", "Nur User"),
            ("bots", "Nur Bot"),
        ]
        queue_select = []
        for value, label in queue_filter_options:
            selected = "selected" if value == queue_filter else ""
            queue_select.append(f"<option value='{value}' {selected}>{label}</option>")

        queue_panel = (
            "<div class='panel wide'>"
            "<div class='panel-head'><div><h3>Queue</h3><p>Filter nach Requester-Kategorie.</p></div></div>"
            "<form class='filter-bar' method='get'>"
            f"<select name='queue_filter'>{''.join(queue_select)}</select>"
            "<button type='submit'>Filter anwenden</button>"
            "</form>"
            f"{queue_table}"
            "</div>"
        )

        body = hero + "<section class='panel-grid single-column'>" + player_card + queue_panel + "</section>"
        subtitle = f"{guild.name} • Music"
        page = self._render_layout(active="music", subtitle=subtitle, body=body, message=message)
        return web.Response(text=page, content_type="text/html")

    async def handle_music_action(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = await request.post()
        redirect_raw = self._coerce_form_value(data.get("redirect")) or "/music"
        redirect_target = self._safe_redirect_target(redirect_raw)
        operation = self._coerce_form_value(data.get("operation")).strip().lower()
        guild = self._primary_guild()
        if guild is None:
            raise self._redirect(redirect_target, "Keine Guild verbunden.")
        music_cog = self.bot.get_cog("Music")
        if music_cog is None or not hasattr(music_cog, "get_player"):
            raise self._redirect(redirect_target, "Music Cog nicht geladen.")
        player = music_cog.get_player(guild)  # type: ignore[attr-defined]
        voice = cast(Optional[discord.VoiceClient], guild.voice_client)

        try:
            if operation == "pause":
                if voice and voice.is_playing():
                    voice.pause()
                    raise self._redirect(redirect_target, "Wiedergabe pausiert.")
                raise self._redirect(redirect_target, "Keine laufende Wiedergabe.")
            if operation == "resume":
                if voice and voice.is_paused():
                    voice.resume()
                    raise self._redirect(redirect_target, "Wiedergabe fortgesetzt.")
                raise self._redirect(redirect_target, "Es ist nichts pausiert.")
            if operation == "skip":
                if hasattr(player, "skip_current") and player.skip_current():
                    raise self._redirect(redirect_target, "Track uebersprungen.")
                raise self._redirect(redirect_target, "Kein aktiver Track.")
            if operation == "stop":
                if voice and (voice.is_playing() or voice.is_paused()):
                    voice.stop()
                if hasattr(player, "clear_queue"):
                    player.clear_queue()
                raise self._redirect(redirect_target, "Wiedergabe gestoppt und Queue geleert.")
            if operation == "clear":
                if hasattr(player, "clear_queue"):
                    player.clear_queue()
                raise self._redirect(redirect_target, "Queue geleert.")
            if operation == "toggle_debug":
                debug_map = getattr(music_cog, "debug_enabled", None)
                if isinstance(debug_map, dict):
                    current = bool(debug_map.get(guild.id))
                    debug_map[guild.id] = not current
                    state = "aktiviert" if not current else "deaktiviert"
                    raise self._redirect(redirect_target, f"Debug Logging {state}.")
                raise self._redirect(redirect_target, "Debug Status nicht verfuegbar.")
        except discord.DiscordException as exc:
            raise self._redirect(redirect_target, f"Discord Fehler: {exc}")

        raise self._redirect(redirect_target, "Unbekannte Aktion.")

    async def handle_cogs(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        message = request.query.get("msg")
        guild = self._primary_guild()
        subtitle = guild.name + " • Cogs" if guild else "Cogs"
        available_extensions = self._available_extensions()
        loaded_extensions = set(self.bot.extensions.keys())
        filter_choice = request.query.get("filter", "all").strip().lower()

        stats = [
            ("Geladen", str(len(loaded_extensions)), "Extensions"),
            ("Verfuegbar", str(len(available_extensions)), "Dateien"),
            ("Bot", self.bot.user.name if self.bot.user else "Bot", "Identity"),
        ]
        stats_cards = "".join(
            "<div class='stat-card'>"
            f"<div class='stat-title'>{html.escape(title)}</div>"
            f"<div class='stat-value'>{html.escape(value)}</div>"
            f"<div class='stat-foot'>{html.escape(foot)}</div>"
            "</div>"
            for title, value, foot in stats
        )

        hero = (
            "<section class='hero'>"
            "<div class='hero-head'><span>cogs</span><h2>Erweiterungen</h2></div>"
            f"<div class='stats-grid'>{stats_cards}</div>"
            "</section>"
        )

        filter_options = [
            ("all", "Alle"),
            ("active", "Aktiv"),
            ("inactive", "Inaktiv"),
        ]
        filter_select = []
        for value, label in filter_options:
            selected = "selected" if value == filter_choice else ""
            filter_select.append(f"<option value='{value}' {selected}>{label}</option>")

        rows = []
        for ext in available_extensions:
            active = ext in loaded_extensions
            if filter_choice == "active" and not active:
                continue
            if filter_choice == "inactive" and active:
                continue
            status = "Aktiv" if active else "Inaktiv"
            actions: List[str] = []
            if active:
                actions.append(
                    "<form method='post' action='/cogs/action'>"
                    "<input type='hidden' name='redirect' value='/cogs'>"
                    f"<input type='hidden' name='extension' value='{html.escape(ext)}'>"
                    "<input type='hidden' name='operation' value='reload'>"
                    "<button type='submit'>Reload</button>"
                    "</form>"
                )
                actions.append(
                    "<form method='post' action='/cogs/action'>"
                    "<input type='hidden' name='redirect' value='/cogs'>"
                    f"<input type='hidden' name='extension' value='{html.escape(ext)}'>"
                    "<input type='hidden' name='operation' value='unload'>"
                    "<button type='submit'>Unload</button>"
                    "</form>"
                )
            else:
                actions.append(
                    "<form method='post' action='/cogs/action'>"
                    "<input type='hidden' name='redirect' value='/cogs'>"
                    f"<input type='hidden' name='extension' value='{html.escape(ext)}'>"
                    "<input type='hidden' name='operation' value='load'>"
                    "<button type='submit'>Load</button>"
                    "</form>"
                )
            action_html = "<div class='tab-actions'>" + "".join(actions) + "</div>"
            rows.append(
                "<tr>"
                f"<td>{html.escape(ext)}</td>"
                f"<td>{status}</td>"
                f"<td>{action_html}</td>"
                "</tr>"
            )

        if rows:
            table_html = (
                "<table class='list-table'>"
                "<thead><tr><th>Extension</th><th>Status</th><th>Aktionen</th></tr></thead>"
                "<tbody>" + "".join(rows) + "</tbody></table>"
            )
        else:
            table_html = "<div class='empty-state'>Keine Cogs gefunden.</div>"

        manual_form = (
            "<form method='post' action='/cogs/action' class='config-form'>"
            "<input type='hidden' name='redirect' value='/cogs'>"
            "<label>Extension Pfad</label><input name='extension' placeholder='cogs.beispiel' required>"
            "<input type='hidden' name='operation' value='load'>"
            "<button type='submit'>Load</button>"
            "</form>"
        )

        table_panel = (
            "<div class='panel wide'>"
            "<h3>Extensions</h3>"
            "<p>Lade oder entlade Discord Cogs direkt.</p>"
            "<form class='filter-bar' method='get'>"
            f"<select name='filter'>{''.join(filter_select)}</select>"
            "<button type='submit'>Filter anwenden</button>"
            "</form>"
            f"{table_html}"
            "</div>"
        )
        form_panel = (
            "<div class='panel'>"
            "<h3>Manuelles Laden</h3>"
            "<p>Pfad angeben, um neue Module zu laden.</p>"
            f"{manual_form}"
            "</div>"
        )

        body = hero + "<section class='panel-grid'>" + table_panel + form_panel + "</section>"
        page = self._render_layout(active="cogs", subtitle=subtitle, body=body, message=message)
        return web.Response(text=page, content_type="text/html")

    async def handle_cogs_action(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = await request.post()
        redirect_raw = self._coerce_form_value(data.get("redirect")) or "/cogs"
        redirect_target = self._safe_redirect_target(redirect_raw)
        extension_raw = self._coerce_form_value(data.get("extension")).strip()
        operation = self._coerce_form_value(data.get("operation")).strip().lower()
        if not extension_raw:
            raise self._redirect(redirect_target, "Bitte eine Extension angeben.")
        if "." not in extension_raw:
            extension = f"cogs.{extension_raw}"
        else:
            extension = extension_raw
        valid_operations = {"load", "unload", "reload"}
        if operation not in valid_operations:
            raise self._redirect(redirect_target, "Unbekannte Operation.")
        try:
            if operation == "load":
                await self.bot.load_extension(extension)
                raise self._redirect(redirect_target, f"{extension} geladen.")
            if operation == "unload":
                await self.bot.unload_extension(extension)
                raise self._redirect(redirect_target, f"{extension} entladen.")
            if operation == "reload":
                await self.bot.reload_extension(extension)
                raise self._redirect(redirect_target, f"{extension} neu geladen.")
        except commands.ExtensionNotLoaded:
            raise self._redirect(redirect_target, "Extension war nicht geladen.")
        except commands.ExtensionAlreadyLoaded:
            raise self._redirect(redirect_target, "Extension ist bereits aktiv.")
        except commands.ExtensionError as exc:
            raise self._redirect(redirect_target, f"Extension Fehler: {exc}")

        raise self._redirect(redirect_target, "Keine Aktion ausgefuehrt.")

    async def handle_login_form(self, request: web.Request) -> web.Response:
        if self._is_authenticated(request):
            raise web.HTTPFound(location="/")
        error = request.query.get("msg")
        return web.Response(text=self._render_login(error), content_type="text/html")

    async def handle_login_submit(self, request: web.Request) -> web.Response:
        if self._is_authenticated(request):
            raise web.HTTPFound(location="/")
        data = await request.post()
        username = data.get("username", "")
        password = data.get("password", "")
        if username != self.username or password != self.password:
            return web.Response(text=self._render_login("Ungültige Zugangsdaten."), content_type="text/html", status=401)
        token = await self._create_session()
        response = web.HTTPFound(location="/")
        response.set_cookie(COOKIE_NAME, token, httponly=True, secure=False, max_age=86400)
        raise response

    async def handle_logout(self, request: web.Request) -> web.Response:
        await self._invalidate_session(request)
        response = web.HTTPFound(location="/login")
        response.del_cookie(COOKIE_NAME)
        raise response

    async def handle_config_update(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = await request.post()
        key = str(data.get("key", "")).strip()
        raw_value = data.get("value", "")
        redirect_value = data.get("redirect")
        if isinstance(redirect_value, bytes):
            redirect_value = redirect_value.decode("utf-8", "ignore")
        if redirect_value is not None and not isinstance(redirect_value, str):
            redirect_value = str(redirect_value)
        redirect_target = self._safe_redirect_target(redirect_value)
        if not key:
            raise self._redirect(redirect_target, "Bitte einen Schlüssel angeben.")
        if key not in self.config.schema:
            raise self._redirect(redirect_target, f"Unbekannter Schlüssel: {key}")
        try:
            self.config.set_value(key, raw_value)
        except Exception as exc:  # ValueError, KeyError etc.
            raise self._redirect(redirect_target, f"Fehler: {exc}")
        raise self._redirect(redirect_target, f"{key} aktualisiert.")

    async def handle_message_send(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = await request.post()
        channel_id_raw = str(data.get("channel_id", "")).strip()
        content = str(data.get("content", "")).strip()
        if not channel_id_raw or not content:
            raise self._redirect("/", "Channel ID und Nachricht dürfen nicht leer sein.")
        try:
            channel_id = int(channel_id_raw)
        except ValueError:
            raise self._redirect("/", "Channel ID muss eine Zahl sein.")
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise self._redirect("/", f"Kanal konnte nicht geladen werden: {exc}")
        if not isinstance(channel, Messageable):
            raise self._redirect("/", "Dieser Kanal unterstützt keine Textnachrichten.")
        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise self._redirect("/", f"Nachricht konnte nicht gesendet werden: {exc}")
        raise self._redirect("/", "Nachricht gesendet.")

    async def handle_health(self, request: web.Request) -> web.Response:
        monitor = getattr(self.bot, "health_monitor", None)
        if monitor:
            data = await monitor.snapshot()
            data.setdefault("status", "ok")
        else:
            data = {"status": "offline"}
        return web.json_response(data)


async def maybe_start_management_server(bot: commands.Bot, config: ConfigManager) -> Optional[ManagementServer]:
    username = os.getenv("WEB_USERNAME")
    password = os.getenv("WEB_PASSWORD")
    if not username or not password:
        print("WEB_USERNAME/WEB_PASSWORD nicht gesetzt – Management-Webinterface deaktiviert.")
        return None
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port_raw = os.getenv("WEB_PORT", "8080")
    try:
        port = int(port_raw)
    except ValueError:
        raise RuntimeError("WEB_PORT muss eine Zahl sein")
    server = ManagementServer(bot, config, username=username, password=password, host=host, port=port)
    await server.start()
    return server


__all__ = ["ManagementServer", "maybe_start_management_server"]
