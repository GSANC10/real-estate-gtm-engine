"""Named fault-injection points. Only tests arm them; the CLI never does.

Pipeline code calls ``faults.hit("<point>")`` at boundaries where a crash or a lost response
matters. Unarmed points do nothing.
"""

from __future__ import annotations

from collections.abc import Callable


class SimulatedCrash(BaseException):
    """Stops the worker like SIGKILL would. BaseException, so pipeline code cannot catch it."""


class CommitResponseLost(Exception):
    """The database committed, but the worker never received the response."""


class Faults:
    def __init__(self) -> None:
        self._armed: dict[str, tuple[int, Callable[[], BaseException]]] = {}
        self.hits: dict[str, int] = {}

    def arm(
        self, point: str, *, skip: int = 0, error: Callable[[], BaseException] = SimulatedCrash
    ) -> None:
        """Raise ``error()`` at ``point`` after letting ``skip`` earlier hits pass."""
        self._armed[point] = (skip, error)

    def hit(self, point: str) -> None:
        self.hits[point] = self.hits.get(point, 0) + 1
        armed = self._armed.get(point)
        if armed is None:
            return
        skip, error = armed
        if skip > 0:
            self._armed[point] = (skip - 1, error)
            return
        del self._armed[point]
        raise error()


NO_FAULTS = Faults()
