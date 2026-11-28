from pathlib import Path

from specify_cli.extensions._commands import _refresh_events_and_warn

_refresh_events_and_warn(Path.cwd())
