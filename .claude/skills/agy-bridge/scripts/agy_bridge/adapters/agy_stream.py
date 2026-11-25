"""The agy stream-json protocol of an editing turn and the stderr markers that classify a failed run."""
from __future__ import annotations

import json
import re
from typing import Dict

_CONVERSATION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def valid_conversation_id(value) -> bool:
    return isinstance(value, str) and _CONVERSATION_ID.fullmatch(value) is not None


_MARKERS = {
    "quota": ("quota", "rate limit", "rate-limit", "resource_exhausted", "429"),
    "auth": ("unauthenticated", "not logged in", "login required", "please log in", "sign in", "401"),
    "soft_deny": ("auto-denied", "permission denied by"),
    "provider_timeout": ("print timeout",),
}


def _markers(err: bytes) -> Dict[str, bool]:
    low = err.decode("utf-8", "replace").lower()
    return {name: any(w in low for w in words) for name, words in _MARKERS.items()}


_MARKER_OVERLAP = max(len(word) for words in _MARKERS.values() for word in words) - 1


class MarkerScan:
    """The stderr markers seen so far, including a marker split across two reads; never the stderr text."""

    def __init__(self):
        self.found = _markers(b"")
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        window = self._tail + chunk
        for name, hit in _markers(window).items():
            self.found[name] |= hit
        self._tail = window[-_MARKER_OVERLAP:]


_EDIT_STATUSES = {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"}


class _EditProtocolError(Exception):
    pass


class _EditStream:
    def __init__(self, conversation_id, on_conversation):
        self.conversation_id = conversation_id
        self.on_conversation = on_conversation
        self.initialized = False
        self.status = None
        self.response = ""
        self.permission_denied = False
        self.provider_timeout = False

    def _conversation(self, conversation_id):
        if not valid_conversation_id(conversation_id):
            raise _EditProtocolError
        if self.conversation_id is not None and conversation_id != self.conversation_id:
            raise _EditProtocolError
        self.conversation_id = conversation_id
        if self.on_conversation is not None:
            self.on_conversation(conversation_id)
            self.on_conversation = None

    def line(self, line):
        if not line.strip():
            return
        try:
            body = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise _EditProtocolError from None
        if not isinstance(body, dict) or not isinstance(body.get("event"), str) or self.status is not None:
            raise _EditProtocolError
        event = body["event"]
        if event == "init":
            init = body.get("init")
            if self.initialized or not isinstance(init, dict):
                raise _EditProtocolError
            conversation_id = body.get("conversation_id", init.get("conversation_id"))
            if conversation_id is not None:
                self._conversation(conversation_id)
            self.initialized = True
        elif event == "result":
            result = body.get("result")
            if not isinstance(result, dict):
                raise _EditProtocolError
            status = result.get("status")
            response = result.get("response", "")
            if not isinstance(status, str) or status not in _EDIT_STATUSES or not isinstance(response, str):
                raise _EditProtocolError
            if result.get("conversation_id") or status == "SUCCESS":
                self._conversation(result.get("conversation_id"))
            if status == "SUCCESS" and not self.initialized:
                raise _EditProtocolError
            self.status = status
            self.response = response
            self.permission_denied |= bool(result.get("denied_actions"))
            self.provider_timeout = result.get("finished_reason") == "print_timeout"
        elif event == "step_update":
            # The provider may report a denied tool without a failing process.
            # The raw step may contain source files, so keep only a marker.
            step = body.get("step_update")
            info = step.get("tool_info") if isinstance(step, dict) else None
            error = info.get("error") if isinstance(info, dict) else None
            if isinstance(error, dict):
                kind = error.get("type")
                message = error.get("message")
                self.permission_denied |= kind == "PERMISSION_DENIED" or (
                    isinstance(message, str) and _markers(message.encode("utf-8", "replace"))["soft_deny"])


def _structured_agy_error(line):
    prefix = b"AGY_ERROR:"
    line = line.lstrip()
    if not line.startswith(prefix):
        return None
    try:
        error = json.loads(line[len(prefix):])
    except (ValueError, UnicodeError, RecursionError):
        return {}
    return error if isinstance(error, dict) else {}


def _edit_error(stream, returncode, timed_out, truncated, cancelled, malformed, err_markers, agy_error):
    if cancelled:
        return "cancelled"
    if timed_out or stream.provider_timeout or err_markers["provider_timeout"]:
        return "timeout"
    if stream.status in ("CANCELED", "INTERRUPTED"):
        return "cancelled"
    if truncated:
        return "output_limit"
    error = agy_error or {}
    status = error.get("status") or error.get("canonical_status")
    code = error.get("error_code")
    if status == "RESOURCE_EXHAUSTED" or code == 429:
        return "quota"
    if status == "UNAUTHENTICATED" or code == 401:
        return "auth"
    if status == "DEADLINE_EXCEEDED" or code == 408:
        return "timeout"
    if (stream.permission_denied or err_markers["soft_deny"] or
            stream.status == "WAITING" or status == "PERMISSION_DENIED"):
        return "permission"
    if returncode != 0 or agy_error is not None:
        if err_markers["quota"]:
            return "quota"
        if err_markers["auth"]:
            return "auth"
        return "protocol" if malformed else "agy_failed"
    if malformed or stream.status is None:
        return "protocol"
    return None if stream.status == "SUCCESS" else "agy_failed"
