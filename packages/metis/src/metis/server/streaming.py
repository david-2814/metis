"""WebSocket subscriber: bridges the event bus to a per-client outbound queue.

See streaming-protocol.md §3 (lifecycle) and §4 (wire frames). Each
connected WebSocket is wired as a bus Subscription whose handler enqueues
the matching event onto a bounded per-client asyncio.Queue. A writer task
drains the queue and writes JSON frames.

Streaming-only events (`message.*`, `text.*`, `thinking.*`, `tool.use_*`)
do NOT pass through the bus catalog (per streaming-protocol.md §5.1). For
v1, the bridge forwards bus events only. Live token-level deltas reach
clients through a separate hook in the session manager (Phase 2 follow-up).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass

import msgspec
from starlette.websockets import WebSocket, WebSocketDisconnect

from metis.core.canonical.messages import Message
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Actor, Event
from metis.core.sessions.store import Session, SessionStore
from metis.server.hub import StreamingHub

logger = logging.getLogger(__name__)

OUTBOUND_QUEUE_SIZE = 1_000

# Streaming-only event types reserved by streaming-protocol.md §5.3.
# Accepted in subscribe filters even though the bus catalog doesn't list them.
_STREAMING_ONLY_TYPES = frozenset(
    {
        "message.start",
        "message.complete",
        "text.delta",
        "thinking.delta",
        "tool.use_start",
        "tool.use_input_delta",
        "tool.use_end",
    }
)

# Preset filter expansions per streaming-protocol.md §3.3.
_PRESET_CHAT = frozenset(
    {
        "turn.started",
        "turn.completed",
        "turn.cancelled",
        "route.decided",
        "llm.call_started",
        "llm.call_completed",
        "llm.call_failed",
        "tool.called",
        "tool.completed",
        "tool.failed",
        "tool.confirmation_requested",
        "tool.confirmation_resolved",
        "memory.updated",
        "memory.eviction",
    }
    | _STREAMING_ONLY_TYPES
)


@dataclass
class _Subscribed:
    filter: EventFilter
    snapshot: bool
    since: str | None
    streaming_event_types: frozenset[str] | None  # None means accept all streaming types


class StreamingConnection:
    """One WebSocket connection's lifecycle: subscribe → snapshot → live."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        session_id: str,
        bus: EventBus,
        session_store: SessionStore,
        hub: StreamingHub | None = None,
    ) -> None:
        self._ws = websocket
        self._session_id = session_id
        self._bus = bus
        self._store = session_store
        self._hub = hub
        self._outbound: asyncio.Queue[dict] = asyncio.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
        self._closed = False
        self._subscription_handle = None
        self._hub_unsubscribe = None
        self._streaming_types_filter: frozenset[str] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_callback = None  # set by the app to bridge to TurnExecutor

    def on_cancel(self, callback) -> None:
        """Register a callback invoked when the client sends a `cancel` frame.

        Signature: callback(session_id: str, turn_id: str, reason: str) -> bool
        """
        self._cancel_callback = callback

    async def run(self) -> None:
        """Drive the full lifecycle. Returns when the connection closes."""
        await self._ws.accept()
        self._loop = asyncio.get_running_loop()
        try:
            first = await _recv_json(self._ws)
        except WebSocketDisconnect:
            return
        if first.get("type") != "subscribe":
            await self._send_error("invalid_filter", "first frame must be `subscribe`")
            await self._ws.close(code=1008)
            return

        try:
            subscribed = self._parse_subscribe(first)
        except _SubscribeError as exc:
            await self._send_error(exc.code, exc.message)
            await self._ws.close(code=1008)
            return

        # Verify session exists.
        try:
            session = self._store.get_session(self._session_id)
        except KeyError:
            await self._send_error("session_not_found", "session does not exist")
            await self._ws.close(code=1008)
            return

        self._streaming_types_filter = subscribed.streaming_event_types

        # Wire the bus subscription BEFORE sending snapshot so no events are
        # lost between snapshot and live streaming.
        self._subscription_handle = self._bus.subscribe(
            Subscription(
                filter=subscribed.filter,
                handler=self._enqueue,
                name=f"ws-{self._session_id}-{secrets.token_hex(4)}",
                fast_path=False,
            )
        )

        # Subscribe to streaming-only events from the hub.
        if self._hub is not None:
            self._hub_unsubscribe = self._hub.subscribe(
                self._session_id, self._enqueue_streaming_frame
            )

        await self._send(
            {
                "type": "subscribe_ack",
                "resolved_filter": subscribed.filter.to_dict(),
                "since": subscribed.since,
                "snapshot": subscribed.snapshot,
                "replay_event_count": 0,
            }
        )

        if subscribed.snapshot:
            await self._send_snapshot(session)

        # Two concurrent loops: drain outbound queue → ws; read inbound → handle.
        writer = asyncio.create_task(self._writer_loop(), name="ws-writer")
        reader = asyncio.create_task(self._reader_loop(), name="ws-reader")
        try:
            _done, pending = await asyncio.wait(
                {writer, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            await self._teardown()

    # ---- Internal helpers ---------------------------------------------------

    def _parse_subscribe(self, frame: dict) -> _Subscribed:
        flt_raw = frame.get("filter")
        if flt_raw == "preset:chat":
            event_types = _PRESET_CHAT
        elif flt_raw == "preset:full":
            event_types = None  # all types
        elif isinstance(flt_raw, dict):
            ev_types = flt_raw.get("event_types")
            if ev_types is not None:
                _validate_event_types(ev_types)
                event_types = frozenset(ev_types)
            else:
                event_types = None
        elif flt_raw is None:
            event_types = None
        else:
            raise _SubscribeError("invalid_filter", f"unexpected filter shape: {flt_raw!r}")

        # Filter excludes streaming-only types because they don't flow through
        # the bus in v1. The set remains in the resolved_filter echo for
        # forward-compat with clients that filter on them.
        bus_event_types = (
            frozenset(t for t in event_types if t not in _STREAMING_ONLY_TYPES)
            if event_types is not None
            else None
        )
        actors = None
        if isinstance(flt_raw, dict) and flt_raw.get("actors") is not None:
            try:
                actors = frozenset(Actor(a) for a in flt_raw["actors"])
            except ValueError as exc:
                raise _SubscribeError("invalid_filter", f"unknown actor: {exc}") from exc

        flt = EventFilter(
            session_ids=frozenset({self._session_id}),
            event_types=bus_event_types,
            actors=actors,
        )
        if event_types is None:
            streaming_filter: frozenset[str] | None = None
        else:
            streaming_filter = frozenset(t for t in event_types if t in _STREAMING_ONLY_TYPES)
        return _Subscribed(
            filter=flt,
            snapshot=bool(frame.get("snapshot", False)),
            since=frame.get("since"),
            streaming_event_types=streaming_filter,
        )

    async def _send_snapshot(self, session: Session) -> None:
        messages = self._store.get_messages(self._session_id)
        # Cap at most-recent 50 per streaming-protocol §3.4.
        if len(messages) > 50:
            messages = messages[-50:]
        await self._send(
            {
                "type": "snapshot",
                "session": _session_to_dict(session),
                "messages": [_message_to_dict(m) for m in messages],
                "snapshot_at_event_id": None,
            }
        )

    async def _enqueue(self, event: Event) -> None:
        """Bus subscription handler. Drops the event if the queue is full
        per streaming-protocol §8 — but for v1 simplicity we just log and
        close on overflow."""
        try:
            self._outbound.put_nowait({"type": "event", "event": _event_to_dict(event)})
        except asyncio.QueueFull:
            logger.warning("outbound queue overflow for session %s; closing", self._session_id)
            try:
                self._outbound.put_nowait({"type": "_close", "code": 1008})
            except asyncio.QueueFull:
                pass

    def _enqueue_streaming_frame(self, frame: dict) -> None:
        """Hub subscriber. Called synchronously from `on_streaming_event`
        on the same loop as the turn task; safe to call put_nowait."""
        event_type = frame.get("event", {}).get("type")
        if (
            self._streaming_types_filter is not None
            and event_type not in self._streaming_types_filter
        ):
            return
        try:
            self._outbound.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning(
                "outbound streaming queue overflow for session %s; closing",
                self._session_id,
            )
            try:
                self._outbound.put_nowait({"type": "_close", "code": 1008})
            except asyncio.QueueFull:
                pass

    async def _writer_loop(self) -> None:
        while not self._closed:
            frame = await self._outbound.get()
            if frame.get("type") == "_close":
                await self._ws.close(code=frame.get("code", 1000))
                self._closed = True
                return
            try:
                await self._send(frame)
            except (WebSocketDisconnect, RuntimeError):
                self._closed = True
                return

    async def _reader_loop(self) -> None:
        while not self._closed:
            try:
                frame = await _recv_json(self._ws)
            except WebSocketDisconnect:
                self._closed = True
                return
            ftype = frame.get("type")
            if ftype == "cancel":
                turn_id = frame.get("turn_id", "")
                reason = frame.get("reason", "user_cancel")
                if self._cancel_callback is not None:
                    self._cancel_callback(self._session_id, turn_id, reason)
            elif ftype == "ping":
                await self._send({"type": "pong", "nonce": frame.get("nonce", "")})
            elif ftype == "pong":
                continue
            else:
                # Unknown control frame; ignore per forward-compat.
                continue

    async def _send(self, frame: dict) -> None:
        await self._ws.send_text(msgspec.json.encode(frame).decode("utf-8"))

    async def _send_error(self, code: str, message: str) -> None:
        try:
            await self._send({"type": "subscribe_error", "code": code, "message": message})
        except Exception:
            pass

    async def _teardown(self) -> None:
        if self._subscription_handle is not None:
            try:
                self._bus.unsubscribe(self._subscription_handle)
            except Exception:
                pass
        if self._hub_unsubscribe is not None:
            try:
                self._hub_unsubscribe()
            except Exception:
                pass
        try:
            if not self._closed:
                await self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-private helpers and exceptions
# ---------------------------------------------------------------------------


class _SubscribeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


async def _recv_json(ws: WebSocket) -> dict:
    raw = await ws.receive_text()
    return msgspec.json.decode(raw)


def _validate_event_types(types: list[str]) -> None:
    from metis.core.events.payloads import PAYLOAD_REGISTRY

    catalog = set(PAYLOAD_REGISTRY.keys())
    for t in types:
        if t in catalog or t in _STREAMING_ONLY_TYPES:
            continue
        raise _SubscribeError("invalid_filter", f"unknown event type: {t}")


def _session_to_dict(session: Session) -> dict:
    return {
        "id": session.id,
        "workspace_path": session.workspace_path,
        "active_model": session.active_model,
        "created_at": session.created_at.isoformat(),
        "cost_so_far_usd": session.cost_so_far_usd,
        "turn_count": session.turn_count,
        "current_turn_id": None,
        "current_turn_status": None,
    }


def _message_to_dict(message: Message) -> dict:
    return msgspec.to_builtins(message)


def _event_to_dict(event: Event) -> dict:
    return {
        "id": event.id,
        "timestamp": event.timestamp.isoformat(),
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "parent_event_id": event.parent_event_id,
        "type": event.type,
        "actor": event.actor.value,
        "payload": event.payload,
        "sensitivity": event.sensitivity.value,
    }
