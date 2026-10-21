from torus.core.store.base import Backend
from torus.core.store.sqlite import SQLiteBackend

__all__ = ["Backend", "SQLiteBackend"]


def _conforms(store: SQLiteBackend) -> Backend:
    """Type-level check (mypy): SQLiteBackend satisfies the Backend protocol."""
    return store
