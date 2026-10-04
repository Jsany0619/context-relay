"""Local display choices, separate from task state and execution permissions."""
import json
import os
from pathlib import Path
import tempfile


DEFAULTS = {"theme": "blue", "font_size": "standard", "density": "comfortable"}
CHOICES = {"theme": ("blue", "mint"), "font_size": ("standard", "large"),
           "density": ("comfortable", "compact")}


def _path(path):
    if path is not None:
        return Path(path)
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
    return base / "ContextRelayUI" / "preferences.json"


def load_preferences(path=None):
    result = dict(DEFAULTS)
    try:
        with _path(path).open("rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            return result
        values = json.loads(data)
        if isinstance(values, dict):
            for key, options in CHOICES.items():
                if values.get(key) in options:
                    result[key] = values[key]
    except (OSError, ValueError, UnicodeError):
        pass
    return result


def save_preferences(values, path=None):
    if (not isinstance(values, dict) or values.keys() != DEFAULTS.keys()
            or any(values[key] not in options for key, options in CHOICES.items())):
        raise ValueError("请选择有效的配色、字号和间距。")
    settings = {key: values[key] for key in DEFAULTS}
    destination = _path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".preferences-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return settings
