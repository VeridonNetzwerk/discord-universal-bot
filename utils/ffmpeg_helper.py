from __future__ import annotations

import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from config import config_manager

# URLs for prebuilt static ffmpeg binaries by platform
LINUX_AMD64_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"


def _is_executable(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def _resolve_ffmpeg(path_hint: str | None) -> Optional[str]:
    if path_hint:
        candidate = Path(path_hint)
        if candidate.is_absolute() or candidate.exists():
            if _is_executable(candidate):
                return str(candidate)
        # allow pointing to directory containing ffmpeg
        if candidate.is_dir():
            nested = candidate / "ffmpeg"
            if _is_executable(nested):
                return str(nested)
    if path_hint:
        via_which = shutil.which(path_hint)
        if via_which:
            return via_which
    default = shutil.which("ffmpeg")
    if default:
        return default
    return None


def ensure_ffmpeg(config=config_manager) -> Optional[str]:
    """Ensure ffmpeg is available; download static build if missing."""
    current = config.get("ffmpeg_path", "ffmpeg")
    resolved = _resolve_ffmpeg(current)
    if resolved:
        if resolved != current:
            try:
                config.set_value("ffmpeg_path", resolved)
            except Exception:
                pass
        return resolved

    system = platform.system().lower()
    arch = platform.machine().lower()

    if system == "linux" and arch in {"amd64", "x86_64"}:
        dest_dir = Path("data/bin")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / "ffmpeg"
        if _is_executable(dest_path):
            config.set_value("ffmpeg_path", str(dest_path))
            return str(dest_path)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                archive_path = Path(tmp_dir) / "ffmpeg.tar.xz"
                print("FFmpeg nicht gefunden. Lade statisches Linux-Build herunter...")
                urllib.request.urlretrieve(LINUX_AMD64_URL, archive_path)
                with tarfile.open(archive_path) as tar:
                    members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith("/ffmpeg")]
                    if not members:
                        raise RuntimeError("Konnte ffmpeg-Binary im Archiv nicht finden.")
                    ffmpeg_member = members[0]
                    tar.extract(ffmpeg_member, path=tmp_dir)
                    extracted_path = Path(tmp_dir) / ffmpeg_member.name
                    shutil.copy2(extracted_path, dest_path)
                    dest_path.chmod(0o755)
                config.set_value("ffmpeg_path", str(dest_path))
                print(f"FFmpeg bereitgestellt unter {dest_path}.")
                return str(dest_path)
        except Exception as exc:
            print(f"Automatischer Download von FFmpeg fehlgeschlagen: {exc}")
            return None

    print("FFmpeg konnte nicht automatisch bereitgestellt werden. Bitte installiere ffmpeg und setze FFMPEG_PATH.")
    return None


__all__ = ["ensure_ffmpeg"]
