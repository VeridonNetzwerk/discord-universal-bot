import os
from typing import Dict, Any

from dotenv import load_dotenv

from utils.config_manager import ConfigManager

load_dotenv()


CONFIG_SCHEMA: Dict[str, Dict[str, Any]] = {
	"command_prefix": {"type": str, "default": "!", "description": "Präfix für Textbefehle."},
	"guild_id": {"type": int, "default": 0, "description": "Primärer Guild / Server ID."},
	"ticket_channel_id": {"type": int, "default": 0, "description": "Kanal, in dem Ticket-Threads erstellt werden."},
	"ticket_panel_channel_id": {"type": int, "default": 0, "description": "Kanal für das Ticket-Panel."},
	"ticket_queue_channel_id": {"type": int, "default": 0, "description": "Kanal, in dem Admins Tickets annehmen."},
	"verify_channel_id": {"type": int, "default": 0, "description": "Kanal für die Verifizierung."},
	"music_channel_id": {"type": int, "default": 0, "description": "Kanal für Musikbefehle."},
	"music_log_channel_id": {"type": int, "default": 0, "description": "Kanal für Musik-Debug/Logs."},
	"admin_role_id": {"type": int, "default": 0, "description": "Admin-Rolle für Ticket-Handling."},
	"verified_role_id": {"type": int, "default": 0, "description": "Rolle für verifizierte Nutzer."},
	"dj_role_id": {"type": int, "default": 0, "description": "Optionale DJ-Rolle für Musikbefehle."},
	"ffmpeg_path": {"type": str, "default": "ffmpeg", "description": "Pfad oder Kommando für FFmpeg."},
	"enable_message_content_intent": {"type": bool, "default": True, "description": "Message Content Intent aktivieren."},
	"enable_members_intent": {"type": bool, "default": True, "description": "Server Members Intent aktivieren."},
	"enable_presence_intent": {"type": bool, "default": False, "description": "Presence Intent aktivieren."},
}

ENV_OVERRIDES = {
	"command_prefix": os.getenv("COMMAND_PREFIX"),
	"guild_id": os.getenv("GUILD_ID"),
	"ticket_channel_id": os.getenv("TICKET_CHANNEL_ID"),
	"ticket_panel_channel_id": os.getenv("TICKET_PANEL_CHANNEL_ID"),
	"ticket_queue_channel_id": os.getenv("TICKET_QUEUE_CHANNEL_ID"),
	"verify_channel_id": os.getenv("VERIFY_CHANNEL_ID"),
	"music_channel_id": os.getenv("MUSIC_CHANNEL_ID"),
	"music_log_channel_id": os.getenv("MUSIC_LOG_CHANNEL_ID"),
	"admin_role_id": os.getenv("ADMIN_ROLE_ID"),
	"verified_role_id": os.getenv("VERIFIED_ROLE_ID"),
	"dj_role_id": os.getenv("DJ_ROLE_ID"),
	"ffmpeg_path": os.getenv("FFMPEG_PATH"),
	"enable_message_content_intent": os.getenv("ENABLE_MESSAGE_CONTENT_INTENT"),
	"enable_members_intent": os.getenv("ENABLE_MEMBERS_INTENT"),
	"enable_presence_intent": os.getenv("ENABLE_PRESENCE_INTENT"),
}

config_manager = ConfigManager(CONFIG_SCHEMA, overrides=ENV_OVERRIDES)


def get_token() -> str:
	token = os.getenv("DISCORD_TOKEN")
	if not token:
		raise RuntimeError("DISCORD_TOKEN fehlt. Bitte setze ihn in der .env oder via Hosting-Konfiguration.")
	return token


__all__ = ["config_manager", "get_token", "CONFIG_SCHEMA"]
