"""Connection health tracking and periodic health logging."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger("live_ingestion.health")


class ConnectionState(str, Enum):
    STARTING = "starting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class ConnectionHealth:
    name: str
    state: ConnectionState = ConnectionState.STARTING
    connected_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None
    messages_received: int = 0
    reconnects: int = 0
    errors: int = 0
    last_error: Optional[str] = None

    def mark_connecting(self) -> None:
        self.state = ConnectionState.CONNECTING

    def mark_connected(self) -> None:
        if self.state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
            self.reconnects += 1
        self.state = ConnectionState.CONNECTED
        self.connected_at = datetime.now(timezone.utc)

    def mark_message(self) -> None:
        self.messages_received += 1
        self.last_message_at = datetime.now(timezone.utc)
        if self.state != ConnectionState.CONNECTED:
            self.mark_connected()

    def mark_disconnected(self) -> None:
        self.state = ConnectionState.DISCONNECTED

    def mark_error(self, exc: BaseException) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.state = ConnectionState.ERROR

    def mark_stopped(self) -> None:
        self.state = ConnectionState.STOPPED

    def is_stale(self, max_age_seconds: float) -> bool:
        if self.last_message_at is None:
            return self.state == ConnectionState.CONNECTED
        age = (datetime.now(timezone.utc) - self.last_message_at).total_seconds()
        return age > max_age_seconds

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "messages_received": self.messages_received,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "last_error": self.last_error,
        }


@dataclass
class HealthMonitor:
    log_interval_seconds: float = 30.0
    stale_after_seconds: float = 60.0
    connections: Dict[str, ConnectionHealth] = field(default_factory=dict)

    def register(self, name: str) -> ConnectionHealth:
        health = self.connections.setdefault(name, ConnectionHealth(name=name))
        return health

    def snapshot(self) -> dict:
        return {name: h.snapshot() for name, h in self.connections.items()}

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.log_interval_seconds)
            except asyncio.TimeoutError:
                pass
            for health in self.connections.values():
                stale = health.is_stale(self.stale_after_seconds)
                level = logging.WARNING if stale else logging.INFO
                logger.log(
                    level,
                    "health[%s] state=%s messages=%d reconnects=%d errors=%d last_message_at=%s%s",
                    health.name,
                    health.state.value,
                    health.messages_received,
                    health.reconnects,
                    health.errors,
                    health.last_message_at.isoformat() if health.last_message_at else "never",
                    " (STALE)" if stale else "",
                )
