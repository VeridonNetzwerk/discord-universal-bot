import json
from pathlib import Path
from typing import Any, Dict, Optional, Callable


class ConfigManager:
    """Simple JSON-backed configuration helper with schema validation."""

    def __init__(
        self,
        schema: Dict[str, Dict[str, Any]],
        storage_path: str = "data/config.json",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.schema = schema
        self.file_path = Path(storage_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = {key: meta.get("default") for key, meta in schema.items()}
        self.load()
        if overrides:
            for key, value in overrides.items():
                if value not in (None, "") and key in self.schema:
                    self._data[key] = self._cast_value(key, value)
        self.save()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def load(self) -> None:
        if self.file_path.exists():
            with self.file_path.open("r", encoding="utf-8") as handle:
                stored = json.load(handle)
            for key, value in stored.items():
                if key in self.schema:
                    self._data[key] = self._cast_value(key, value)

    def save(self) -> None:
        with self.file_path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)

    def reset(self) -> None:
        self._data = {key: meta.get("default") for key, meta in self.schema.items()}
        self.save()

    # ------------------------------------------------------------------
    # Access helpers
    # ------------------------------------------------------------------
    def get(self, key: str, default: Optional[Any] = None) -> Any:
        return self._data.get(key, default)

    def get_int(self, key: str) -> int:
        return int(self._data.get(key, 0) or 0)

    def get_str(self, key: str) -> str:
        return str(self._data.get(key, ""))

    def items(self):
        return self._data.items()

    def to_display_dict(self) -> Dict[str, Any]:
        return {key: self._data.get(key) for key in self.schema.keys()}

    def set_value(self, key: str, raw_value: Any) -> Any:
        if key not in self.schema:
            raise KeyError(f"Unbekannter Konfigurationsschlüssel: {key}")
        value = self._cast_value(key, raw_value)
        self._data[key] = value
        self.save()
        return value

    def update_many(self, values: Dict[str, Any]) -> None:
        for key, value in values.items():
            if key in self.schema:
                self._data[key] = self._cast_value(key, value)
        self.save()

    # ------------------------------------------------------------------
    def _cast_value(self, key: str, value: Any) -> Any:
        meta = self.schema.get(key, {})
        expected_type: Optional[Callable[[Any], Any]] = meta.get("type")
        if expected_type is None or value is None:
            return value
        if expected_type is bool:
            if isinstance(value, str):
                return value.lower() in {"1", "true", "yes", "on"}
            return bool(value)
        try:
            return expected_type(value)
        except (ValueError, TypeError):
            raise ValueError(f"Wert '{value}' konnte nicht nach {expected_type.__name__} konvertiert werden")

    @property
    def schema_description(self) -> Dict[str, str]:
        return {key: meta.get("description", "") for key, meta in self.schema.items()}


__all__ = ["ConfigManager"]
