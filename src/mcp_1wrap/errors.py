"""Shared error type. Messages start with the stage in square brackets."""


class WrapError(Exception):
    """A startup problem the user can act on (reported on stderr, exit 1)."""


class OpError(WrapError):
    """The 1Password CLI itself failed (missing, timed out, non-zero exit, bad output)."""
