"""Durable idempotency, leases, checkpoints and replay for Agent turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.chat import service as chat_service
from webhub.chat.models import ConversationMessage
from webhub.chat.schemas import ConversationMessageResponse
from webhub.db.database import Database
from webhub.db.models import AgentTurnRun
from webhub.db.models._base import utc_now
from webhub.db.models.agent_turns import MAX_AGENT_TURN_ERROR_CODE_LENGTH
from webhub.space_batch_state import space_batch_state_artifacts

from .runner import AgentRunRequest

TURN_LEASE_DURATION = timedelta(seconds=60)
TURN_HEARTBEAT_INTERVAL_SECONDS = 15.0
TURN_CHECKPOINT_INTERVAL_SECONDS = 2.0
TURN_EXPIRED_CODE = "turn_lease_expired"
TURN_ABORTED_CODE = "turn_aborted"
TURN_RUNNER_ERROR_CODE = "runner_unavailable"
MAX_PERSISTED_AGENT_TEXT = 32_000
MAX_PERSISTED_AGENT_REASONING = 32_000
MAX_STALE_TURN_CLEANUP_BATCH = 500
logger = logging.getLogger(__name__)

TurnState = Literal["running", "complete", "error", "aborted"]
ClaimAction = Literal["execute", "replay", "in_progress", "conflict", "close_expired"]


class AgentTurnLeaseLostError(RuntimeError):
    """Raised when a stale executor attempts to publish after losing its fence."""


@dataclass(frozen=True, slots=True)
class AgentTurnLease:
    user_id: str
    run_id: str
    token_hash: str


@dataclass(frozen=True, slots=True)
class AgentTurnClaim:
    action: ClaimAction
    run_id: str
    state: TurnState
    conversation_id: str | None
    user_message_id: str | None
    assistant_message_id: str | None
    error_code: str | None
    retry_after_seconds: int | None = None
    lease: AgentTurnLease | None = None


@dataclass(frozen=True, slots=True)
class AgentTurnMessages:
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    assistant_version: int


@dataclass(frozen=True, slots=True)
class _JournalSnapshot:
    content_revision: int
    content: str
    parts: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lease_token_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _request_hash(request: AgentRunRequest) -> str:
    """Bind a turn id to semantic input while allowing first-turn conversation learning."""

    if request.idempotency_payload is not None:
        request_payload = dict(request.idempotency_payload)
        # Conversation identity is fenced separately by _conversation_matches.
        # Excluding it here lets a first-turn retry include the conversation id
        # that the client learned from the initial stream without turning the
        # otherwise identical request into an idempotency conflict.
        request_payload.pop("conversationId", None)
        request_payload.pop("conversation_id", None)
        payload = {"version": 3, "request": request_payload}
    else:
        payload = {
            "version": 1,
            "message": request.message,
            "slashCommand": (
                request.slash_command.metadata() if request.slash_command is not None else None
            ),
            "metadata": dict(request.metadata),
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(encoded)


def _conversation_matches(run: AgentTurnRun, requested: str | None) -> bool:
    if run.requested_conversation_id is not None:
        return requested == run.requested_conversation_id
    return requested is None or requested == run.conversation_id


def _claim_from_run(
    run: AgentTurnRun,
    *,
    action: ClaimAction,
    lease: AgentTurnLease | None = None,
    retry_after_seconds: int | None = None,
) -> AgentTurnClaim:
    return AgentTurnClaim(
        action=action,
        run_id=run.id,
        state=run.state,  # type: ignore[arg-type]
        conversation_id=run.conversation_id,
        user_message_id=run.user_message_id,
        assistant_message_id=run.assistant_message_id,
        error_code=run.error_code,
        retry_after_seconds=retry_after_seconds,
        lease=lease,
    )


def _retry_after_seconds(expires_at: datetime, now: datetime) -> int:
    remaining = int((_aware(expires_at) - now).total_seconds()) + 1
    return max(1, min(int(TURN_LEASE_DURATION.total_seconds()), remaining))


async def _fence_expired_turn(
    database: Database,
    run: AgentTurnRun,
) -> AgentTurnClaim | None:
    """Acquire a fresh fence for cleanup without authorizing Provider execution."""

    now = utc_now()
    token_hash = _lease_token_hash()
    async with database.sessions() as session:
        fenced = await session.execute(
            update(AgentTurnRun)
            .where(
                AgentTurnRun.user_id == run.user_id,
                AgentTurnRun.id == run.id,
                AgentTurnRun.state == "running",
                AgentTurnRun.lease_token_hash == run.lease_token_hash,
                or_(
                    AgentTurnRun.lease_expires_at.is_(None),
                    AgentTurnRun.lease_expires_at <= now,
                ),
            )
            .values(
                lease_token_hash=token_hash,
                lease_expires_at=now + TURN_LEASE_DURATION,
                heartbeat_at=now,
                attempt_count=AgentTurnRun.attempt_count + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if fenced.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return None
        await session.commit()
    # The candidate row may have been populated with message ids after the
    # cleanup query but before the fence update. Reload it after the commit so
    # cleanup never loses a freshly bound assistant placeholder.
    fresh = await _load_turn_run(database, user_id=run.user_id, run_id=run.id)
    if fresh is None:
        return None
    return _claim_from_run(
        fresh,
        action="close_expired",
        lease=AgentTurnLease(fresh.user_id, fresh.id, token_hash),
    )


async def _load_turn_run(
    database: Database,
    *,
    user_id: str,
    run_id: str,
) -> AgentTurnRun | None:
    async with database.sessions() as session:
        return await session.scalar(
            select(AgentTurnRun).where(
                AgentTurnRun.user_id == user_id,
                AgentTurnRun.id == run_id,
            )
        )


async def claim_turn(database: Database, request: AgentRunRequest) -> AgentTurnClaim:
    """Claim a turn, replay a terminal receipt, or reject duplicate execution.

    Expired runs are fenced and closed before this function returns. Callers
    therefore only receive the four public decisions: execute, replay,
    in_progress, or conflict. The internal ``close_expired`` action is retained
    solely as the cleanup capability passed to :func:`close_expired_turn`.
    """

    turn_id_hash = _sha256_text(request.turn_id)
    request_hash = _request_hash(request)
    for _ in range(5):
        now = utc_now()
        token_hash = _lease_token_hash()
        async with database.sessions() as session:
            run = await session.scalar(
                select(AgentTurnRun).where(
                    AgentTurnRun.user_id == request.account_id,
                    AgentTurnRun.turn_id_hash == turn_id_hash,
                )
            )
            if run is None:
                run = AgentTurnRun(
                    user_id=request.account_id,
                    turn_id_hash=turn_id_hash,
                    request_hash=request_hash,
                    requested_conversation_id=request.conversation_id,
                    conversation_id=request.conversation_id,
                    state="running",
                    attempt_count=1,
                    lease_token_hash=token_hash,
                    lease_expires_at=now + TURN_LEASE_DURATION,
                    heartbeat_at=now,
                    checkpointed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    concurrent = await session.scalar(
                        select(AgentTurnRun.id).where(
                            AgentTurnRun.user_id == request.account_id,
                            AgentTurnRun.turn_id_hash == turn_id_hash,
                        )
                    )
                    if concurrent is None:
                        raise
                    continue
                await session.refresh(run)
                lease = AgentTurnLease(request.account_id, run.id, token_hash)
                return _claim_from_run(run, action="execute", lease=lease)

            if run.request_hash != request_hash or not _conversation_matches(
                run, request.conversation_id
            ):
                return _claim_from_run(run, action="conflict")
            if run.state != "running":
                return _claim_from_run(run, action="replay")
            expires_at = run.lease_expires_at
            if expires_at is not None and _aware(expires_at) > now:
                return _claim_from_run(
                    run,
                    action="in_progress",
                    retry_after_seconds=_retry_after_seconds(expires_at, now),
                )

        stale_claim = await _fence_expired_turn(database, run)
        if stale_claim is None:
            continue
        await close_expired_turn(database, stale_claim)
        closed = await _load_turn_run(
            database,
            user_id=request.account_id,
            run_id=run.id,
        )
        if closed is None:
            continue
        if closed.state == "running":
            return _claim_from_run(
                closed,
                action="in_progress",
                retry_after_seconds=int(TURN_LEASE_DURATION.total_seconds()),
            )
        return _claim_from_run(closed, action="replay")
    raise AgentTurnLeaseLostError("turn claim did not converge")


async def bind_turn_messages_in_session(
    session: AsyncSession,
    lease: AgentTurnLease,
    messages: AgentTurnMessages,
) -> None:
    """Bind transcript rows while the caller still owns the write transaction."""

    now = utc_now()
    rows = (
        await session.execute(
            select(ConversationMessage.id, ConversationMessage.role).where(
                ConversationMessage.user_id == lease.user_id,
                ConversationMessage.conversation_id == messages.conversation_id,
                ConversationMessage.id.in_(
                    [messages.user_message_id, messages.assistant_message_id]
                ),
            )
        )
    ).all()
    roles = {message_id: role for message_id, role in rows}
    if (
        roles.get(messages.user_message_id) != "user"
        or roles.get(messages.assistant_message_id) != "assistant"
    ):
        raise ValueError("turn messages must be an owned user/assistant pair")

    bound = await session.execute(
        update(AgentTurnRun)
        .where(
            AgentTurnRun.user_id == lease.user_id,
            AgentTurnRun.id == lease.run_id,
            AgentTurnRun.state == "running",
            AgentTurnRun.lease_token_hash == lease.token_hash,
            AgentTurnRun.lease_expires_at >= now,
            or_(
                AgentTurnRun.requested_conversation_id.is_(None),
                AgentTurnRun.requested_conversation_id == messages.conversation_id,
            ),
            or_(
                AgentTurnRun.conversation_id.is_(None),
                AgentTurnRun.conversation_id == messages.conversation_id,
            ),
            or_(
                AgentTurnRun.user_message_id.is_(None),
                AgentTurnRun.user_message_id == messages.user_message_id,
            ),
            or_(
                AgentTurnRun.assistant_message_id.is_(None),
                AgentTurnRun.assistant_message_id == messages.assistant_message_id,
            ),
        )
        .values(
            conversation_id=messages.conversation_id,
            user_message_id=messages.user_message_id,
            assistant_message_id=messages.assistant_message_id,
            checkpointed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if bound.rowcount != 1:  # type: ignore[attr-defined]
        raise AgentTurnLeaseLostError("turn lease was lost while binding messages")


async def bind_turn_messages(
    database: Database,
    lease: AgentTurnLease,
    messages: AgentTurnMessages,
) -> None:
    """Bind transcript rows to the owning account's turn in a fresh transaction."""

    async with database.sessions() as session:
        try:
            await bind_turn_messages_in_session(session, lease, messages)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def renew_turn_lease(database: Database, lease: AgentTurnLease) -> bool:
    """Renew one live lease; callers schedule this at the 15-second heartbeat."""

    now = utc_now()
    async with database.sessions() as session:
        renewed = await session.execute(
            update(AgentTurnRun)
            .where(
                AgentTurnRun.user_id == lease.user_id,
                AgentTurnRun.id == lease.run_id,
                AgentTurnRun.state == "running",
                AgentTurnRun.lease_token_hash == lease.token_hash,
                AgentTurnRun.lease_expires_at >= now,
            )
            .values(
                lease_expires_at=now + TURN_LEASE_DURATION,
                heartbeat_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if renewed.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return False
        await session.commit()
        return True


async def _record_turn_checkpoint_in_session(
    session: AsyncSession,
    lease: AgentTurnLease,
) -> bool:
    """Fence a dirty checkpoint inside the Assistant update transaction."""

    now = utc_now()
    recorded = await session.execute(
        update(AgentTurnRun)
        .where(
            AgentTurnRun.user_id == lease.user_id,
            AgentTurnRun.id == lease.run_id,
            AgentTurnRun.state == "running",
            AgentTurnRun.lease_token_hash == lease.token_hash,
            AgentTurnRun.lease_expires_at >= now,
        )
        .values(checkpointed_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return recorded.rowcount == 1  # type: ignore[attr-defined]


def _validate_terminal_error_code(error_code: str | None) -> None:
    if error_code is not None and len(error_code) > MAX_AGENT_TURN_ERROR_CODE_LENGTH:
        raise ValueError("turn error code is too long")


async def _mark_turn_terminal_in_session(
    session: AsyncSession,
    lease: AgentTurnLease,
    *,
    state: Literal["complete", "error", "aborted"],
    error_code: str | None,
) -> bool:
    """Fence and stage a terminal receipt without committing the transaction."""

    _validate_terminal_error_code(error_code)
    now = utc_now()
    completed = await session.execute(
        update(AgentTurnRun)
        .where(
            AgentTurnRun.user_id == lease.user_id,
            AgentTurnRun.id == lease.run_id,
            AgentTurnRun.state == "running",
            AgentTurnRun.lease_token_hash == lease.token_hash,
            AgentTurnRun.lease_expires_at >= now,
        )
        .values(
            state=state,
            lease_token_hash=None,
            lease_expires_at=None,
            error_code=None if state == "complete" else error_code,
            checkpointed_at=now,
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return completed.rowcount == 1  # type: ignore[attr-defined]


async def mark_turn_terminal(
    database: Database,
    lease: AgentTurnLease,
    *,
    state: Literal["complete", "error", "aborted"],
    error_code: str | None,
) -> None:
    async with database.sessions() as session:
        if not await _mark_turn_terminal_in_session(
            session,
            lease,
            state=state,
            error_code=error_code,
        ):
            await session.rollback()
            raise AgentTurnLeaseLostError("turn lease was lost before terminal commit")
        await session.commit()


async def load_turn_assistant(
    database: Database,
    *,
    user_id: str,
    message_id: str | None,
) -> ConversationMessageResponse | None:
    if message_id is None:
        return None
    async with database.sessions() as session:
        message = await session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.user_id == user_id,
                ConversationMessage.id == message_id,
                ConversationMessage.role == "assistant",
            )
        )
        if message is None:
            return None
        from webhub.chat.service._common import _message_response

        return _message_response(message)


@dataclass(slots=True)
class AgentTurnJournal:
    """Per-executor snapshot with dirty checkpoints and a separate heartbeat."""

    database: Database
    lease: AgentTurnLease
    turn_id: str
    messages: AgentTurnMessages
    metadata: dict[str, Any]
    text_fragments: list[str] = field(default_factory=list)
    reasoning_fragments: list[str] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    source_parts: list[dict[str, Any]] = field(default_factory=list)
    _assistant_version: int = field(init=False)
    _text_length: int = field(init=False)
    _reasoning_length: int = field(init=False)
    _content_revision: int = field(default=0, init=False)
    _content_dirty: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _maintenance_task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._assistant_version = self.messages.assistant_version
        self._text_length = sum(map(len, self.text_fragments))
        self._reasoning_length = sum(map(len, self.reasoning_fragments))

    def start(self) -> None:
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def ensure_active(self) -> None:
        task = self._maintenance_task
        if task is not None and task.done():
            await task

    def _mark_content_dirty(self) -> None:
        self._content_revision += 1
        self._content_dirty = True

    def add_text(self, value: str) -> None:
        if not value:
            return
        remaining = MAX_PERSISTED_AGENT_TEXT - self._text_length
        if remaining <= 0:
            return
        fragment = value[:remaining]
        self.text_fragments.append(fragment)
        self._text_length += len(fragment)
        self._mark_content_dirty()

    def add_reasoning(self, value: str) -> None:
        if not value:
            return
        remaining = MAX_PERSISTED_AGENT_REASONING - self._reasoning_length
        if remaining <= 0:
            return
        fragment = value[:remaining]
        self.reasoning_fragments.append(fragment)
        self._reasoning_length += len(fragment)
        self._mark_content_dirty()

    def add_tool_result(self, value: Mapping[str, Any]) -> None:
        self.tool_results.append(dict(value))

    def add_source(self, value: Mapping[str, Any]) -> None:
        source_id = value.get("sourceId")
        if isinstance(source_id, str) and any(
            part.get("sourceId") == source_id for part in self.source_parts
        ):
            return
        self.source_parts.append(dict(value))

    def update_metadata(self, value: Mapping[str, Any]) -> None:
        changed = {key: item for key, item in value.items() if self.metadata.get(key) != item}
        if not changed:
            return
        self.metadata.update(changed)

    def _parts(self) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        reasoning = "".join(self.reasoning_fragments)
        if reasoning:
            parts.append({"type": "reasoning", "text": reasoning})
        parts.extend(dict(part) for part in self.source_parts)
        text = "".join(self.text_fragments)
        if text:
            parts.append({"type": "text", "text": text})
        return parts

    def _snapshot(self) -> _JournalSnapshot:
        tool_results = [dict(source) for source in self.tool_results]
        return _JournalSnapshot(
            content_revision=self._content_revision,
            content="".join(self.text_fragments),
            parts=self._parts(),
            sources=tool_results,
            artifacts=space_batch_state_artifacts(tool_results),
        )

    def _message_metadata(
        self,
        state: TurnState,
        *,
        persisted: bool,
        extra: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        metadata = {
            **self.metadata,
            **dict(extra or {}),
            "assistantMessageId": self.messages.assistant_message_id,
            "turnState": state,
            "turnPersisted": persisted,
            "messageStatus": "streaming" if state == "running" else state,
        }
        if self.turn_id:
            metadata["turnId"] = self.turn_id
        if error_code:
            metadata["errorCode"] = error_code
        return metadata

    def _acknowledge_snapshot(self, snapshot: _JournalSnapshot) -> None:
        if self._content_revision == snapshot.content_revision:
            self._content_dirty = False

    async def checkpoint(self, *, force: bool = False) -> None:
        """Persist dirty content, or explicitly flush the current snapshot."""

        async with self._lock:
            if self._closed:
                return
            if not force and not self._content_dirty:
                return
            snapshot = self._snapshot()
            async with self.database.sessions() as session:
                try:
                    if not await _record_turn_checkpoint_in_session(session, self.lease):
                        raise AgentTurnLeaseLostError("turn lease expired during checkpoint")
                    updated = await chat_service.update_message(
                        session,
                        self.lease.user_id,
                        self.messages.conversation_id,
                        self.messages.assistant_message_id,
                        expected_version=self._assistant_version,
                        expected_status="streaming",
                        content=snapshot.content,
                        parts=snapshot.parts,
                        sources=snapshot.sources,
                        artifacts=snapshot.artifacts,
                        metadata=self._message_metadata("running", persisted=False),
                        status="streaming",
                        commit=False,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
            self._assistant_version = updated.version
            self._acknowledge_snapshot(snapshot)

    async def finish(
        self,
        state: Literal["complete", "error", "aborted"],
        *,
        metadata: Mapping[str, Any],
        error_code: str | None = None,
    ) -> None:
        async with self._lock:
            if self._closed:
                return
            snapshot = self._snapshot()
            async with self.database.sessions() as session:
                try:
                    if not await _mark_turn_terminal_in_session(
                        session,
                        self.lease,
                        state=state,
                        error_code=error_code,
                    ):
                        raise AgentTurnLeaseLostError(
                            "turn lease was lost before terminal commit"
                        )
                    updated = await chat_service.update_message(
                        session,
                        self.lease.user_id,
                        self.messages.conversation_id,
                        self.messages.assistant_message_id,
                        expected_version=self._assistant_version,
                        expected_status="streaming",
                        content=snapshot.content,
                        parts=snapshot.parts,
                        sources=snapshot.sources,
                        artifacts=snapshot.artifacts,
                        metadata=self._message_metadata(
                            state,
                            persisted=True,
                            extra=metadata,
                            error_code=error_code,
                        ),
                        status=state,
                        commit=False,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
            self._assistant_version = updated.version
            self._acknowledge_snapshot(snapshot)
            self._closed = True
            self._stop.set()

    async def close(self) -> None:
        self._stop.set()
        task = self._maintenance_task
        if task is not None and task is not asyncio.current_task():
            await task

    async def _heartbeat(self) -> None:
        async with self._lock:
            if self._closed:
                return
            if not await renew_turn_lease(self.database, self.lease):
                raise AgentTurnLeaseLostError("turn lease expired during heartbeat")

    async def _maintenance_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_checkpoint = loop.time() + TURN_CHECKPOINT_INTERVAL_SECONDS
        next_heartbeat = loop.time() + TURN_HEARTBEAT_INTERVAL_SECONDS
        while not self._stop.is_set():
            timeout = max(0.0, min(next_checkpoint, next_heartbeat) - loop.time())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
                continue
            except TimeoutError:
                pass

            now = loop.time()
            if now >= next_heartbeat:
                await self._heartbeat()
                next_heartbeat = loop.time() + TURN_HEARTBEAT_INTERVAL_SECONDS
            if now >= next_checkpoint:
                if self._content_dirty:
                    await self.checkpoint()
                next_checkpoint = loop.time() + TURN_CHECKPOINT_INTERVAL_SECONDS


def _persisted_error_code(message: ConversationMessageResponse) -> str | None:
    value = message.metadata.get("errorCode")
    return value if isinstance(value, str) and value else None


def _journal_from_persisted_message(
    database: Database,
    lease: AgentTurnLease,
    message: ConversationMessageResponse,
    *,
    turn_id: str,
    user_message_id: str | None,
) -> AgentTurnJournal:
    text_fragments: list[str] = []
    reasoning_fragments: list[str] = []
    source_parts: list[dict[str, Any]] = []
    for part in message.parts:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "reasoning" and isinstance(part.get("text"), str):
            reasoning_fragments.append(str(part["text"]))
        elif part.get("type") == "text" and isinstance(part.get("text"), str):
            text_fragments.append(str(part["text"]))
        elif part.get("type") == "source-url":
            source_parts.append(dict(part))
    if not text_fragments and message.content:
        text_fragments.append(message.content)
    persisted_turn_id = message.metadata.get("turnId")
    return AgentTurnJournal(
        database=database,
        lease=lease,
        turn_id=(
            turn_id
            or (str(persisted_turn_id) if isinstance(persisted_turn_id, str) else "")
        ),
        messages=AgentTurnMessages(
            conversation_id=message.conversation_id,
            user_message_id=user_message_id or "",
            assistant_message_id=message.id,
            assistant_version=message.version,
        ),
        metadata=dict(message.metadata),
        text_fragments=text_fragments,
        reasoning_fragments=reasoning_fragments,
        tool_results=[
            dict(source) for source in message.sources if isinstance(source, Mapping)
        ],
        source_parts=source_parts,
    )


async def finish_claimed_turn(
    database: Database,
    lease: AgentTurnLease,
    *,
    turn_id: str,
    state: Literal["complete", "error", "aborted"],
    metadata: Mapping[str, Any],
    error_code: str | None,
) -> ConversationMessageResponse | None:
    """Close a claimed turn even when cancellation happened before journal setup."""

    run = await _load_turn_run(database, user_id=lease.user_id, run_id=lease.run_id)
    if run is None:
        return None
    message = await load_turn_assistant(
        database,
        user_id=lease.user_id,
        message_id=run.assistant_message_id,
    )
    if (
        run.state != "running"
        or run.lease_token_hash != lease.token_hash
    ):
        return message
    if message is None:
        await mark_turn_terminal(
            database,
            lease,
            state=state,
            error_code=error_code,
        )
        return None
    if message.status != "streaming":
        await mark_turn_terminal(
            database,
            lease,
            state=message.status,
            error_code=_persisted_error_code(message),
        )
        return message

    journal = _journal_from_persisted_message(
        database,
        lease,
        message,
        turn_id=turn_id,
        user_message_id=run.user_message_id,
    )
    await journal.finish(state, metadata=metadata, error_code=error_code)
    return await load_turn_assistant(
        database,
        user_id=lease.user_id,
        message_id=message.id,
    )


async def close_expired_turn(
    database: Database,
    claim: AgentTurnClaim,
) -> ConversationMessageResponse | None:
    """Terminalize one fenced, abandoned run without repeating Provider work."""

    lease = claim.lease
    if claim.action != "close_expired" or lease is None:
        raise TypeError("close_expired_turn requires an expired-turn lease")
    return await finish_claimed_turn(
        database,
        lease,
        turn_id="",
        state="aborted",
        metadata={},
        error_code=TURN_EXPIRED_CODE,
    )


async def close_expired_turns(
    database: Database,
    *,
    user_id: str | None = None,
    limit: int = 100,
) -> int:
    """Close a bounded batch of stale runs for request-entry or startup recovery."""

    if not 1 <= limit <= MAX_STALE_TURN_CLEANUP_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_STALE_TURN_CLEANUP_BATCH}")
    now = utc_now()
    conditions = [
        AgentTurnRun.state == "running",
        or_(
            AgentTurnRun.lease_expires_at.is_(None),
            AgentTurnRun.lease_expires_at <= now,
        ),
    ]
    if user_id is not None:
        conditions.append(AgentTurnRun.user_id == user_id)
    async with database.sessions() as session:
        candidates = list(
            (
                await session.scalars(
                    select(AgentTurnRun)
                    .where(*conditions)
                    .order_by(AgentTurnRun.lease_expires_at, AgentTurnRun.id)
                    .limit(limit)
                )
            ).all()
        )

    closed = 0
    for run in candidates:
        try:
            claim = await _fence_expired_turn(database, run)
            if claim is None:
                continue
            await close_expired_turn(database, claim)
            closed += 1
        except Exception as error:
            # Cleanup is maintenance work. One corrupt or concurrently changed
            # receipt must not prevent startup or an unrelated turn. Keep the
            # log deliberately free of exception text and message payloads.
            logger.warning(
                "agent_turn_cleanup_failed user_id=%s run_id=%s error_type=%s",
                run.user_id,
                run.id,
                type(error).__name__,
            )
    return closed


__all__ = [
    "TURN_ABORTED_CODE",
    "TURN_CHECKPOINT_INTERVAL_SECONDS",
    "TURN_EXPIRED_CODE",
    "TURN_HEARTBEAT_INTERVAL_SECONDS",
    "TURN_LEASE_DURATION",
    "TURN_RUNNER_ERROR_CODE",
    "MAX_PERSISTED_AGENT_REASONING",
    "MAX_PERSISTED_AGENT_TEXT",
    "AgentTurnClaim",
    "AgentTurnJournal",
    "AgentTurnLease",
    "AgentTurnLeaseLostError",
    "AgentTurnMessages",
    "bind_turn_messages",
    "bind_turn_messages_in_session",
    "claim_turn",
    "close_expired_turn",
    "close_expired_turns",
    "finish_claimed_turn",
    "load_turn_assistant",
    "mark_turn_terminal",
    "renew_turn_lease",
]
