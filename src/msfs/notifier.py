"""
In-sim NOTAM text notifications via SimConnect_Text.

Current behavior intentionally avoids using SimConnect text-result events for
acknowledgement because some simulator builds report ambiguous values
(e.g. 0x00010000 / 0x00010001), which caused false acknowledgements and
suppressed future messages.

To keep alerts reliable and visible:
- messages are sent as high-contrast SCROLL_YELLOW text,
- each NOTAM is shown once per session,
- persistent acknowledgement is disabled for now.
"""

from __future__ import annotations

import ctypes
import threading
from typing import Optional

from loguru import logger

# ── SimConnect text constants ───────────────────────────────────────────────────
# We try multiple styles because different MSFS UI presets may hide one channel.
_FALLBACK_TEXT_TYPE_CODES = (1, 5)  # common visible styles in many builds


class NotamNotifier:
    """
    Sends NOTAM alert text into MSFS via SimConnect_Text, one at a time.
    Each NOTAM is shown once per session.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._shown_this_session: set[str] = set()
        self._queue: list[tuple[str, str, str]] = []   # (notam_id, icao, text)
        self._current_notam_id: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def queue_notam(self, notam_id: str, icao: str, text: str) -> bool:
        """
        Enqueue a NOTAM for display if it has not been shown in this session.
        Returns True only when a new item was added to the queue.
        """
        with self._lock:
            if notam_id in self._shown_this_session:
                return False
            if self._current_notam_id == notam_id:
                return False
            if any(q[0] == notam_id for q in self._queue):
                return False
            self._queue.append((notam_id, icao, text))
            logger.debug(f"[notifier] Queued NOTAM {notam_id} @ {icao} ({len(self._queue)} in queue)")
            return True

    def pump(self, sm: object) -> None:
        """
        Show the next queued NOTAM if SimConnect is available.
        Call once per scheduler poll cycle.
        """
        if sm is None:
            return

        with self._lock:
            if not self._queue:
                return
            notam_id, icao, text = self._queue.pop(0)
            event_id = 0
            self._current_notam_id = notam_id
            self._shown_this_session.add(notam_id)

        self._send_text(sm, notam_id, icao, text, event_id)

    def on_text_result(self, event_id: int, result: int) -> None:
        """No-op: text-result events are not used for acknowledgement right now."""
        return

    def clear_session(self) -> None:
        """Reset per-session tracking (e.g. on reconnect)."""
        with self._lock:
            self._shown_this_session.clear()
            self._current_notam_id = None
            self._queue.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send_text(
        self, sm: object, notam_id: str, icao: str, text: str, event_id: int
    ) -> None:
        """Fire SimConnect_Text for a single NOTAM popup."""
        msg = f"NOTAM {icao}:\n{text}"[:254]   # SimConnect_Text practical limit
        data = msg.encode("ascii", errors="replace") + b"\x00"

        try:
            fn = self._raw_fn(sm, "Text")
            sent_types: list[int] = []
            for text_type in self._text_type_codes():
                hr = fn(
                    sm.hSimConnect,
                    ctypes.c_uint32(text_type),
                    ctypes.c_float(30.0),
                    ctypes.c_uint32(event_id),
                    ctypes.c_uint32(len(data)),
                    ctypes.c_char_p(data),
                )
                if int(hr) == 0:
                    sent_types.append(int(text_type))

            if sent_types:
                logger.info(
                    f"[notifier] Displaying NOTAM popup for {notam_id} @ {icao} "
                    f"(event_id={event_id}, text_types={sent_types})"
                )
            else:
                logger.warning(
                    f"[notifier] SimConnect_Text rejected for {notam_id} "
                    f"(text_types={list(self._text_type_codes())})"
                )
                # Roll back so the next pump() can retry
                with self._lock:
                    self._shown_this_session.discard(notam_id)
                    self._current_notam_id = None

        except Exception as exc:
            logger.warning(f"[notifier] SimConnect_Text error for {notam_id}: {exc}")
            with self._lock:
                self._shown_this_session.discard(notam_id)
                self._current_notam_id = None
        else:
            with self._lock:
                self._current_notam_id = None

    def _raw_fn(self, sm: object, name: str):
        fn = getattr(sm.dll.SimConnect, f"SimConnect_{name}")
        fn.restype = ctypes.c_long   # HRESULT
        fn.argtypes = None           # bypass argtypes to avoid cross-import mismatch
        return fn

    def _text_type_codes(self) -> tuple[int, ...]:
        """Resolve preferred text types from SimConnect enum if available."""
        try:
            from SimConnect.Enum import SIMCONNECT_TEXT_TYPE  # type: ignore

            preferred_names = (
                "SIMCONNECT_TEXT_TYPE_PRINT_WHITE",
                "SIMCONNECT_TEXT_TYPE_SCROLL_WHITE",
                "SIMCONNECT_TEXT_TYPE_SCROLL_YELLOW",
            )
            values: list[int] = []
            for name in preferred_names:
                member = getattr(SIMCONNECT_TEXT_TYPE, name, None)
                if member is None:
                    continue
                values.append(int(getattr(member, "value", int(member))))
            if values:
                return tuple(dict.fromkeys(values))
        except Exception:
            pass

        return _FALLBACK_TEXT_TYPE_CODES

    # Persistent acknowledgement intentionally disabled for now.
