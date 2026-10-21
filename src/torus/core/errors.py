"""Domain errors. MCP tools will turn these into structured tool errors."""


class TorusError(Exception):
    """Base class for all Torus domain errors."""


class NotFoundError(TorusError):
    def __init__(self, kind: str, ref: str) -> None:
        super().__init__(f"{kind} not found: {ref}")
        self.kind = kind
        self.ref = ref


class EvidenceRequiredError(TorusError):
    """A claim type that needs support was recorded without it."""


class NotCurrentError(TorusError):
    """An operation targeted a superseded or retracted record."""


class InvalidTransitionError(TorusError):
    """A thesis status change that the lifecycle does not allow."""
