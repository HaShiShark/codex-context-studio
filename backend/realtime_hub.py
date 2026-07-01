from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class RealtimeSubscription:
    id: str
    session_id: str
    start_event_id: int
    queue: asyncio.Queue[dict[str, Any]]


class RealtimeHub:
    def __init__(self, *, max_events: int = 500) -> None:
        self._lock = asyncio.Lock()
        self._next_event_id = 0
        self._max_events = max(1, max_events)
        self._events: list[dict[str, Any]] = []
        self._subscriptions: dict[str, RealtimeSubscription] = {}

    async def direct_event(self, event: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return self._stamp(event)

    async def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            payload = self._stamp(event)
            self._events.append(payload)
            self._events = self._events[-self._max_events :]
            subscribers = list(self._subscriptions.values())

        for subscriber in subscribers:
            if self._matches(payload, subscriber.session_id):
                await subscriber.queue.put(copy.deepcopy(payload))
        return payload

    async def subscribe(self, session_id: str) -> RealtimeSubscription:
        async with self._lock:
            subscription = RealtimeSubscription(
                id=uuid.uuid4().hex,
                session_id=session_id,
                start_event_id=self._next_event_id,
                queue=asyncio.Queue(),
            )
            self._subscriptions[subscription.id] = subscription
        return subscription

    async def unsubscribe(self, subscription_id: str) -> None:
        async with self._lock:
            self._subscriptions.pop(subscription_id, None)

    async def replay_after(
        self,
        event_id: int,
        session_id: str,
        *,
        up_to_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                copy.deepcopy(event)
                for event in self._events
                if int(event.get("event_id") or 0) > event_id
                and (up_to_event_id is None or int(event.get("event_id") or 0) <= up_to_event_id)
                and self._matches(event, session_id)
            ]

    def _stamp(self, event: dict[str, Any]) -> dict[str, Any]:
        self._next_event_id += 1
        payload = copy.deepcopy(event)
        payload["event_id"] = self._next_event_id
        return payload

    @staticmethod
    def _matches(event: dict[str, Any], session_id: str) -> bool:
        if not session_id:
            return True
        event_type = str(event.get("type") or "")
        event_session_id = str(event.get("session_id") or "")
        return event_type in {"session_list_update"} or event_session_id == session_id
