"""Sagent CLI adapter.

Sessions live at
``<data_dir>/rekursiv-ai/sagent/projects/<cwd-slug>/<12-hex id>/session.jsonl``,
appended one JSON record per line. Reading it is
``trackinizer.lib.agent.sessions.sagent``'s job, not this module's -- one IR reader
serves capture here AND the conversion paths.

Sagent mints a fresh session directory per run under the cwd's project directory,
so two runs in one workspace would land side by side with nothing to tell them
apart. Like claude's ``--session-id``, this adapter mints the directory itself and
hands it to sagent as ``--session``, and scopes capture to exactly that directory.
A run that names its own session (``--session``, ``--resume``, ``--continue``)
keeps it, and capture widens to the cwd's project directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import re
import uuid

from trackinizer.lib.agent.sessions import sagent
from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.lib.userdirs import data_dir


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from trackinizer.types.streams import TraxRecord


__all__ = ["SagentAdapter"]

_SESSION_FILE: Final = "session.jsonl"

_CHOOSES_SESSION: Final = frozenset(
    {"--session", "--resume", "--continue", "--resume-all", "--continue-all", "--ephemeral"},
)
"""Sagent flags that pick (or disable) the session themselves."""

_MAX_SLUG_LEN: Final = 200
"""Sagent's ``cwd_slug`` keeps paths up to this length verbatim, then hashes."""


def _cwd_slug(cwd: Path) -> str | None:
    """Sagent's project-directory name for ``cwd``, or None past its hash cutoff.

    Mirrors ``sagent.sessions.cwd_slug``: ``/`` maps to ``_``, alphanumerics pass
    through, and every other character escapes as ``-<utf-8 hex>-``. A longer slug
    is truncated with a hash this module does not reproduce, so it returns None
    and capture falls back to the projects root.
    """
    slug = re.sub(
        r"[^a-zA-Z0-9/]",
        lambda m: f"-{m.group().encode('utf-8').hex()}-",
        str(cwd.resolve()),
    ).replace("/", "_")
    return slug if len(slug) <= _MAX_SLUG_LEN else None


class SagentAdapter:
    """Locates the ``sagent`` CLI's per-session JSONL files.

    Built once per run, so the session directory it mints belongs to that run.
    """

    name: str = "sagent"
    cli_binary: str = "sagent"
    whole_file: bool = False
    parent_session_env: frozenset[str] = frozenset[str]()

    def __init__(self) -> None:
        slug = _cwd_slug(Path.cwd())
        self._project_dir = None if slug is None else self._projects_dir / slug
        self._session_dir = (
            None if self._project_dir is None else self._project_dir / uuid.uuid4().hex[:12]
        )

    @property
    def _projects_dir(self) -> Path:
        # Resolved per call, as the other adapters do, so a switched XDG env is seen.
        return data_dir() / "rekursiv-ai" / "sagent" / "projects"

    def session_args(self, cli_args: Sequence[str]) -> list[str]:
        """Return the ``--session`` argument this run hands sagent, if any.

        Empty when the caller already picks the session; capture then widens
        from the minted directory to the cwd's project directory.

        Args:
          cli_args: The arguments the caller passes sagent.

        Returns:
          args: ``["--session", <dir>]``, or empty.
        """
        if self._session_dir is None or _CHOOSES_SESSION.intersection(
            arg.split("=", 1)[0] for arg in cli_args
        ):
            self._session_dir = None
            return []
        return ["--session", str(self._session_dir)]

    def session_dirs(self) -> Iterable[Path]:
        """Return the directories this CLI writes sessions under."""
        # The projects ROOT, for the same reason as claude's: sagent mints the
        # per-project and per-session directories after the watch is armed.
        return (self._projects_dir,)

    def matches_session_file(self, path: Path) -> bool:
        """Return whether ``path`` is one of this CLI's session files."""
        return path.name == _SESSION_FILE and path.parent.parent.parent == self._projects_dir

    def session_scope(self) -> Path | None:
        """Return this run's minted session directory, else its project directory."""
        return self._session_dir or self._project_dir

    def session_id_from_path(self, path: Path) -> str | None:
        """Sagent's session id is the ``<12-hex id>`` directory holding the file.

        ``sagent --resume <id>`` reopens that directory, so the id correlates a
        resumed run to its prior AgentSession.

        Args:
          path: File path (typically ``<id>/session.jsonl``).

        Returns:
          result: The session directory name, or None for another file.
        """
        if path.name != _SESSION_FILE:
            return None
        return path.parent.name or None

    def reader(self) -> Tail[TraxRecord]:
        """Return a fresh IR reader for one sagent session file."""
        return Tail(sagent.normalize)
