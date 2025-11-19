from __future__ import annotations

import asyncio
import html
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional


@dataclass
class RollingMetric:
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0
    last: float = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.last = value
        if value > self.maximum:
            self.maximum = value

    @property
    def average(self) -> float:
        if not self.count:
            return 0.0
        return self.total / self.count


class HealthMonitor:
    """Collects lightweight runtime metrics for the bot and dashboard."""

    UPDATE_INTERVAL = 10.0

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.started_at = time.time()
        self._started_monotonic = time.monotonic()
        self.latency_ms: float = 0.0
        self.guild_count: int = 0
        self.member_count: int = 0
        self.voice_connections: int = 0
        self.pending_tasks: int = 0
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._http_metrics: Dict[str, RollingMetric] = {}
        self._http_status: Dict[int, int] = defaultdict(int)
        self._http_recent: Deque[Dict[str, Any]] = deque(maxlen=40)
        self._task_metrics: Dict[str, RollingMetric] = {}
        self._task_recent: Deque[Dict[str, Any]] = deque(maxlen=40)
        self._events: Deque[Dict[str, Any]] = deque(maxlen=40)
        self._gauges: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._loop_task = self.bot.loop.create_task(self._update_loop(), name="health-monitor")

    async def shutdown(self) -> None:
        task = self._loop_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._loop_task = None

    async def _update_loop(self) -> None:
        try:
            while True:
                await self.collect_guild_metrics()
                await asyncio.sleep(self.UPDATE_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def collect_guild_metrics(self) -> None:
        async with self._lock:
            latency = getattr(self.bot, "latency", None)
            self.latency_ms = float(latency * 1000) if latency is not None else 0.0
            guilds = list(getattr(self.bot, "guilds", []))
            self.guild_count = len(guilds)
            self.member_count = sum(
                getattr(guild, "member_count", 0) or len(getattr(guild, "members", []))
                for guild in guilds
            )
            voice_clients = getattr(self.bot, "voice_clients", [])
            self.voice_connections = sum(1 for client in voice_clients if client and client.is_connected())
            self.pending_tasks = sum(1 for task in asyncio.all_tasks() if not task.done())

    def record_http_request(self, method: str, path: str, duration_ms: float, status: int) -> None:
        route = path.split("?", 1)[0]
        key = f"{method.upper()} {route}"
        metric = self._http_metrics.setdefault(key, RollingMetric())
        metric.add(duration_ms)
        self._http_status[status] += 1
        self._http_recent.appendleft(
            {
                "timestamp": time.time(),
                "method": method.upper(),
                "path": route,
                "status": status,
                "duration_ms": duration_ms,
            }
        )

    def record_bot_task(self, name: str, duration_ms: float, context: Optional[Dict[str, Any]] = None) -> None:
        metric = self._task_metrics.setdefault(name, RollingMetric())
        metric.add(duration_ms)
        self._task_recent.appendleft(
            {
                "timestamp": time.time(),
                "name": name,
                "duration_ms": duration_ms,
                "context": context or {},
            }
        )
        if duration_ms > 10_000:
            self._record_event(
                name="task.slow",
                duration_ms=duration_ms,
                context={"task": name, **(context or {})},
            )

    def set_gauge(self, name: str, value: float) -> None:
        self._gauges[name] = float(value)

    def get_gauge(self, name: str, default: float = 0.0) -> float:
        return self._gauges.get(name, default)

    def _record_event(self, *, name: str, duration_ms: float, context: Optional[Dict[str, Any]] = None) -> None:
        entry = {
            "timestamp": time.time(),
            "name": name,
            "duration_ms": duration_ms,
            "context": context or {},
        }
        self._events.appendleft(entry)
        print(
            f"[HealthMonitor] Event {name} duration={duration_ms:.1f}ms context={entry['context']}"
        )

    async def snapshot(self) -> Dict[str, Any]:
        await self.collect_guild_metrics()
        async with self._lock:
            uptime_seconds = time.monotonic() - self._started_monotonic
            http_total_count = sum(metric.count for metric in self._http_metrics.values())
            http_total_ms = sum(metric.total for metric in self._http_metrics.values())
            http_avg_ms = http_total_ms / http_total_count if http_total_count else 0.0
            http_stats = [
                {
                    "route": key,
                    "count": metric.count,
                    "avg_ms": metric.average,
                    "max_ms": metric.maximum,
                    "last_ms": metric.last,
                }
                for key, metric in sorted(
                    self._http_metrics.items(),
                    key=lambda item: item[1].average,
                    reverse=True,
                )
            ][:8]
            task_stats = [
                {
                    "name": key,
                    "count": metric.count,
                    "avg_ms": metric.average,
                    "max_ms": metric.maximum,
                    "last_ms": metric.last,
                }
                for key, metric in sorted(
                    self._task_metrics.items(),
                    key=lambda item: item[1].average,
                    reverse=True,
                )
            ][:8]

            return {
                "status": "ok",
                "uptime_seconds": uptime_seconds,
                "uptime": self._format_uptime(uptime_seconds),
                "avg_latency": self.latency_ms,
                "guilds": self.guild_count,
                "members": self.member_count,
                "voice": self.voice_connections,
                "pending_tasks": self.pending_tasks,
                "http_avg_ms": http_avg_ms,
                "http": http_stats,
                "http_recent": list(self._http_recent),
                "http_status": dict(self._http_status),
                "tasks": task_stats,
                "task_recent": list(self._task_recent),
                "gauges": dict(self._gauges),
                "events": list(self._events),
            }

    def render_table(self, snapshot: Optional[Dict[str, Any]] = None) -> str:
        snap = snapshot or {}
        general_rows = [
            ("Uptime", snap.get("uptime", "-")),
            ("Latency", f"{snap.get('avg_latency', 0.0):.1f} ms"),
            ("Guilds", str(snap.get("guilds", 0))),
            ("Members", str(snap.get("members", 0))),
            ("Voice", str(snap.get("voice", 0))),
            ("Pending Tasks", str(snap.get("pending_tasks", 0))),
        ]
        general_html = self._render_simple_table("Runtime", general_rows)

        http_rows = [
            (
                entry.get("route", "-"),
                str(entry.get("count", 0)),
                f"{entry.get('avg_ms', 0.0):.1f}",
                f"{entry.get('max_ms', 0.0):.1f}",
            )
            for entry in snap.get("http", [])
        ]
        http_html = self._render_simple_table(
            "HTTP Endpoints",
            http_rows,
            headers=("Route", "Calls", "Avg ms", "Max ms"),
        )

        task_rows = [
            (
                entry.get("name", "-"),
                str(entry.get("count", 0)),
                f"{entry.get('avg_ms', 0.0):.1f}",
                f"{entry.get('max_ms', 0.0):.1f}",
            )
            for entry in snap.get("tasks", [])
        ]
        task_html = self._render_simple_table(
            "Bot Tasks",
            task_rows,
            headers=("Task", "Runs", "Avg ms", "Max ms"),
        )

        recent_rows = [
            (
                time.strftime("%H:%M:%S", time.localtime(event.get("timestamp", 0.0))),
                event.get("name", "-"),
                f"{event.get('duration_ms', 0.0):.1f} ms",
            )
            for event in list(snap.get("events", []))[:6]
        ]
        events_html = self._render_simple_table(
            "Events",
            recent_rows,
            headers=("Time", "Name", "Duration"),
        )

        return (
            "<div class='panel-grid'>"
            f"<div class='panel'>{general_html}</div>"
            f"<div class='panel'>{http_html}</div>"
            f"<div class='panel'>{task_html}</div>"
            f"<div class='panel'>{events_html}</div>"
            "</div>"
        )

    def _render_simple_table(
        self,
        title: str,
        rows: List[tuple[str, ...]],
        *,
        headers: Optional[tuple[str, ...]] = None,
    ) -> str:
        escaped_title = html.escape(title)
        head_html = ""
        if headers:
            header_cells = "".join(f"<th>{html.escape(col)}</th>" for col in headers)
            head_html = f"<thead><tr>{header_cells}</tr></thead>"
        body_cells = "".join(
            "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        if not body_cells:
            body_cells = "<tr><td colspan='4'>Keine Daten</td></tr>"
        return (
            f"<h4>{escaped_title}</h4>"
            "<table class='list-table compact'>"
            f"{head_html}"
            f"<tbody>{body_cells}</tbody>"
            "</table>"
        )

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        total_seconds = int(seconds)
        minutes, sec = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m {sec}s"
        if minutes:
            return f"{minutes}m {sec}s"
        return f"{sec}s"


__all__ = ["HealthMonitor"]
