"""Domain errors raised by the query, proposal and approval code.

``server.py`` turns these into MCP ``ToolError`` so the model sees a readable message;
anything else (e.g. a database outage) propagates as an unexpected error.
"""


class DomainError(Exception):
    """An anticipated failure caused by the caller's input or the current state."""


class NotFoundError(DomainError):
    """A referenced restaurant, menu item or action does not exist (for this restaurant)."""


class InvalidStateError(DomainError):
    """The request is valid but the current state does not allow it."""
