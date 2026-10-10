"""Queue abstraction.

``LocalQueue`` is a Postgres-backed adapter (SKIP LOCKED + visibility timeout) that mimics SQS
semantics (at-least-once delivery, redelivery after the visibility timeout, bounded receives)
so duplicate-delivery behaviour can be exercised without AWS. ``SqsQueue`` is used on AWS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from resilience_map.core.config import Settings, get_settings
from resilience_map.core.db import session_scope


@dataclass(frozen=True)
class QueueMessage:
    receipt: str
    body: dict[str, object]
    receive_count: int


class JobQueue(Protocol):
    def send(self, body: dict[str, object]) -> None: ...

    def receive(self, max_messages: int = 1, wait_seconds: int = 5) -> list[QueueMessage]: ...

    def delete(self, receipt: str) -> None: ...

    def extend_visibility(self, receipt: str, seconds: int) -> None: ...


class LocalQueue:
    def __init__(self, visibility_timeout_s: int, max_receive_count: int) -> None:
        self._vt = visibility_timeout_s
        self._max = max_receive_count

    def send(self, body: dict[str, object]) -> None:
        with session_scope() as s:
            s.execute(
                text("INSERT INTO queue_messages (body) VALUES (CAST(:b AS jsonb))"),
                {"b": json.dumps(body)},
            )

    def receive(self, max_messages: int = 1, wait_seconds: int = 5) -> list[QueueMessage]:
        import time

        deadline = time.monotonic() + wait_seconds
        while True:
            with session_scope() as s:
                # Move exhausted messages to the dead-letter state, mirroring a redrive policy.
                s.execute(
                    text(
                        "UPDATE queue_messages SET dead = true "
                        "WHERE NOT dead AND receive_count >= :m AND visible_at <= now()"
                    ),
                    {"m": self._max},
                )
                rows = s.execute(
                    text(
                        """
                        UPDATE queue_messages SET
                          receive_count = receive_count + 1,
                          visible_at = now() + make_interval(secs => :vt)
                        WHERE id IN (
                          SELECT id FROM queue_messages
                          WHERE NOT dead AND visible_at <= now()
                          ORDER BY id FOR UPDATE SKIP LOCKED LIMIT :n)
                        RETURNING id, body, receive_count
                        """
                    ),
                    {"vt": self._vt, "n": max_messages},
                ).all()
            if rows or time.monotonic() >= deadline:
                return [QueueMessage(str(r.id), dict(r.body), r.receive_count) for r in rows]
            time.sleep(0.5)

    def delete(self, receipt: str) -> None:
        with session_scope() as s:
            s.execute(text("DELETE FROM queue_messages WHERE id = :i"), {"i": int(receipt)})

    def extend_visibility(self, receipt: str, seconds: int) -> None:
        with session_scope() as s:
            s.execute(
                text(
                    "UPDATE queue_messages SET visible_at = now() + make_interval(secs => :s) "
                    "WHERE id = :i"
                ),
                {"s": seconds, "i": int(receipt)},
            )


class SqsQueue:
    def __init__(self, queue_url: str, region: str, visibility_timeout_s: int) -> None:
        import boto3

        self._url = queue_url
        self._vt = visibility_timeout_s
        self._client = boto3.client("sqs", region_name=region)

    def send(self, body: dict[str, object]) -> None:
        self._client.send_message(QueueUrl=self._url, MessageBody=json.dumps(body))

    def receive(self, max_messages: int = 1, wait_seconds: int = 5) -> list[QueueMessage]:
        resp = self._client.receive_message(
            QueueUrl=self._url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=min(wait_seconds, 20),
            VisibilityTimeout=self._vt,
            AttributeNames=["ApproximateReceiveCount"],
        )
        return [
            QueueMessage(
                receipt=m["ReceiptHandle"],
                body=json.loads(m["Body"]),
                receive_count=int(m["Attributes"]["ApproximateReceiveCount"]),
            )
            for m in resp.get("Messages", [])
        ]

    def delete(self, receipt: str) -> None:
        self._client.delete_message(QueueUrl=self._url, ReceiptHandle=receipt)

    def extend_visibility(self, receipt: str, seconds: int) -> None:
        self._client.change_message_visibility(
            QueueUrl=self._url, ReceiptHandle=receipt, VisibilityTimeout=seconds
        )


def get_queue(settings: Settings | None = None) -> JobQueue:
    s = settings or get_settings()
    if s.queue_backend == "sqs":
        return SqsQueue(s.sqs_queue_url, s.sqs_region, s.queue_visibility_timeout_s)
    return LocalQueue(s.queue_visibility_timeout_s, s.queue_max_receive_count)
