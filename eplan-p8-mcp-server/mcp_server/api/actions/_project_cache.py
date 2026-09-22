"""
Project-scoped cache invalidation.

Anything cached about the CURRENTLY OPEN project (symbol libraries, catalogs)
is only valid until that project is closed or swapped, and this process cannot
observe those events - the user can switch projects in the GUI at any time.
What it can observe is its own project actions and connection changes, so those
call `bump()`, and every project-scoped cache stores the generation it was read
under and treats a different generation as stale.
"""

_generation = 0


def generation() -> int:
    return _generation


def bump() -> int:
    """Invalidate every project-scoped cache. Call on open/close/disconnect."""
    global _generation
    _generation += 1
    return _generation
