"""Structured error model. Every failure that crosses the Skill boundary is a PublishError with a code from
ERROR_TABLE; the code decides the CLI exit code and whether retrying the same call can succeed. The CLI prints
{"ok": false, "error": {"code", "message", "retryable", "details"}}. Messages and details never carry a credential
value (credentials.redact runs over every document the CLI prints, as a second line of defence)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# code -> (exit code, retryable)
ERROR_TABLE: Dict[str, Tuple[int, bool]] = {
    "INVALID_ARGUMENT": (2, False),       # a flag value or request field is malformed
    "INPUT_NOT_FOUND": (2, False),        # the deliverable does not exist or is not a regular file
    "UNKNOWN_PLATFORM": (2, False),       # the platform id is not in the registry
    "CONFIRM_MISMATCH": (2, False),       # --confirm was given, but not the token the dry run printed for this deliverable
    "METADATA_INVALID": (2, False),       # the title / description / tags / privacy / thumbnail / captions break an upload limit
    "CREDENTIALS_MISSING": (3, False),    # a credential the platform needs is in neither the environment nor the keychain
    "KEYCHAIN_UNAVAILABLE": (3, False),   # `auth login/logout`: no usable OS keychain (keyring not installed, none viable, or off)
    "KEYCHAIN_ERROR": (3, False),         # the OS keychain refused to read, store or remove an item (e.g. locked)
    "NOT_IMPLEMENTED": (5, False),        # declared in the contract, not implemented yet (see the roadmap step)
    "CANCELLED": (6, True),               # interrupted by a signal
    "INTERNAL_ERROR": (7, False),         # a bug in this skill
}
ERROR_CODES = tuple(ERROR_TABLE)
EXIT_CODES = {code: ERROR_TABLE[code][0] for code in ERROR_CODES}


class PublishError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None, retryable: Optional[bool] = None) -> None:
        if code not in ERROR_TABLE:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = ERROR_TABLE[code][1] if retryable is None else bool(retryable)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}
