"""Typed errors for the lanlink P2P transport."""


class LanlinkError(Exception):
    """Base class for all lanlink failures."""


class LineBusy(LanlinkError):
    """The remote peer answered BUSY (it is already on a call)."""


class PeerGone(LanlinkError):
    """The peer could not be reached (connection refused/timeout)."""


class CallRejected(LanlinkError):
    """The remote peer declined the call (the user pressed Reject)."""


class HandshakeError(LanlinkError):
    """The peer sent a malformed or incompatible handshake."""


class TransferAborted(LanlinkError):
    """The connection closed in the middle of a transfer."""
