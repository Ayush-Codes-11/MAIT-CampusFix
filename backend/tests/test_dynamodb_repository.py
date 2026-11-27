"""Contract and stateful Moto tests for DynamoDBComplaintRepository.

Covers:
1. Always-on Botocore ``Stubber`` tests verifying exact single-table keys,
   canonical UTC formatting, single ``sha256`` attribute naming, atomic 2-item
   ``begin_copy_attempt`` transactions, conditional ``pin_source_version``,
   4-action ``commit_verified_attachment_versioned`` Decision 1 cancellation
   mapping, Decision 2 microsecond boundaries, Decision 3 outbox pagination +
   HMAC cursor validation, and bounded transient retries (``StorageThrottleError``).
2. Stateful ``moto`` ``@mock_aws`` integration tests (skipped cleanly only when
   ``moto`` is not installed in the active Python environment).
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import unittest
from unittest.mock import MagicMock

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from campusfix.domain.models import (
    AtomicAttachmentCommitResult,
    Complaint,
    ComplaintFeedback,
    ComplaintStatus,
    IssueCategory,
    StatusHistoryEntry,
    TagOutboxRecord,
    UploadAttemptStatus,
    UploadIntent,
    UploadIntentStatus,
    ValidationError,
    format_canonical_utc_timestamp,
)
from campusfix.domain.rules import (
    ActiveFollowUpExistsError,
    ComplaintAlreadyExistsError,
    ComplaintNotFoundError,
    FollowUpEligibilityError,
    ImmutableFieldViolationError,
    OptimisticLockError,
    StaleLeaseError,
    StorageThrottleError,
    UnauthorizedError,
    UploadFailedError,
    UploadIntentExpiredError,
    UploadIntentNotFoundError,
)
from campusfix.repositories.dynamodb import DynamoDBComplaintRepository
from campusfix.services.boto3_s3_storage import Boto3S3StorageService
from campusfix.services.repository_state_adapter import RepositoryVersionedUploadStateAdapter
from campusfix.services.upload_confirmation_worker import UploadConfirmationWorker

try:
    from moto import mock_aws  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    mock_aws = None


_DUMMY_AWS_CREDS = {
    "region_name": "us-east-1",
    "aws_access_key_id": "test",
    "aws_secret_access_key": "test",
}
_TABLE_NAME = "campusfix-test-table"
_SHA256 = "a" * 64
_SERIALIZER = TypeSerializer()


def _utc(
    year: int = 2026,
    month: int = 1,
    day: int = 1,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc)


def _to_ddb(item: dict) -> dict:
    return {k: _SERIALIZER.serialize(v) for k, v in item.items() if v is not None}


def _make_complaint(
    complaint_id: str = "CF-2026-0001",
    *,
    status: ComplaintStatus = ComplaintStatus.PENDING,
    submitted_at: datetime | None = None,
    building: str = "Block A",
    category: IssueCategory = IssueCategory.ELECTRICAL,
    version: int = 1,
    attachment_key: str | None = None,
    attachment_status: str | None = None,
    is_follow_up: bool = False,
    original_complaint_id: str | None = None,
    follow_up_reason: str | None = None,
    upvotes: list[str] | None = None,
) -> Complaint:
    ts = submitted_at or _utc(2026, 1, 1, 10, 0, 0)
    return Complaint(
        id=complaint_id,
        title="Broken ceiling fan",
        description="The ceiling fan in lab 204 is making loud noise and wobbling.",
        building=building,
        floor="2nd",
        room_number="204",
        category=category,
        status=status,
        submitted_at=ts,
        submitted_by="student-1",
        upvotes=list(upvotes or []),
        version=version,
        attachment_key=attachment_key,
        attachment_status=attachment_status,
        status_history=[
            StatusHistoryEntry(
                from_status=ComplaintStatus.PENDING,
                to_status=status,
                changed_at=ts,
                changed_by="student-1",
            )
        ] if status != ComplaintStatus.PENDING else [],
        is_follow_up=is_follow_up,
        original_complaint_id=original_complaint_id,
        follow_up_reason=follow_up_reason,
    )


def _make_intent(
    *,
    complaint_id: str = "CF-2026-0001",
    intent_id: str = "I1",
    owner_id: str = "student-1",
    status: UploadIntentStatus = UploadIntentStatus.CONFIRMING,
    lease_token: str | None = "tok-1",
    lease_expires_at: datetime | None = None,
    source_version_id: str | None = "vid-1",
    expected_sha256: str = _SHA256,
    expected_mime: str = "image/jpeg",
    max_byte_size: int = 1_000_000,
) -> UploadIntent:
    now = _utc(2026, 1, 1, 0, 0, 0)
    return UploadIntent(
        intent_id=intent_id,
        complaint_id=complaint_id,
        owner_id=owner_id,
        status=status,
        staging_key=f"staging/complaints/{complaint_id}/{intent_id}/upload.tmp",
        final_key=f"complaints/{complaint_id}/images/{intent_id}.jpg",
        expected_sha256=expected_sha256,
        expected_mime=expected_mime,
        max_byte_size=max_byte_size,
        created_at=now,
        upload_expires_at=now + timedelta(minutes=10),
        confirmation_deadline=now + timedelta(minutes=15),
        recovery_deadline=now + timedelta(hours=1),
        lease_token=lease_token,
        lease_expires_at=lease_expires_at if lease_expires_at is not None else (now + timedelta(minutes=10)),
        source_version_id=source_version_id,
        updated_at=now,
    )


def _complaint_item_dict(c: Complaint) -> dict:
    ts = format_canonical_utc_timestamp(c.submitted_at)
    d = {
        "PK": f"COMPLAINT#{c.id}",
        "SK": "META",
        "GSI1PK": f"STATUS#{c.status.value}",
        "GSI1SK": f"SUBMITTED_AT#{ts}#ID#{c.id}",
        "entity_type": "COMPLAINT",
        "id": c.id,
        "title": c.title,
        "description": c.description,
        "building": c.building,
        "buildingLower": c.building.strip().lower(),
        "floor": c.floor,
        "roomNumber": c.room_number,
        "category": c.category.value,
        "status": c.status.value,
        "submittedAt": ts,
        "submittedBy": c.submitted_by,
        "overdueThresholdDays": c.overdue_threshold_days,
        "statusHistory": [
            {
                "from": h.from_status.value,
                "to": h.to_status.value,
                "changedAt": format_canonical_utc_timestamp(h.changed_at),
                "changedBy": h.changed_by,
            }
            for h in c.status_history
        ],
        "isFollowUp": c.is_follow_up,
        "version": c.version,
    }
    if c.attachment_key is not None:
        d["attachmentKey"] = c.attachment_key
    if c.attachment_status is not None:
        d["attachmentStatus"] = c.attachment_status
    return d


def _intent_item_dict(intent: UploadIntent) -> dict:
    d: dict = {
        "PK": UploadIntent.intent_pk(intent.complaint_id),
        "SK": UploadIntent.intent_sk(intent.intent_id),
        "entity_type": "UPLOAD_INTENT",
        "complaint_id": intent.complaint_id,
        "intent_id": intent.intent_id,
        "owner_id": intent.owner_id,
        "status": intent.status.value,
        "staging_key": intent.staging_key,
        "final_key": intent.final_key,
        "sha256": intent.expected_sha256,
        "expected_mime": intent.expected_mime,
        "max_byte_size": intent.max_byte_size,
        "confirm_attempts": intent.confirm_attempts,
        "created_at": format_canonical_utc_timestamp(intent.created_at),
        "upload_expires_at": format_canonical_utc_timestamp(intent.upload_expires_at),
        "confirmation_deadline": format_canonical_utc_timestamp(intent.confirmation_deadline),
        "recovery_deadline": format_canonical_utc_timestamp(intent.recovery_deadline),
    }
    if intent.lease_token is not None:
        d["lease_token"] = intent.lease_token
    if intent.lease_expires_at is not None:
        d["lease_expires_at"] = format_canonical_utc_timestamp(intent.lease_expires_at)
    if intent.source_version_id is not None:
        d["source_version_id"] = intent.source_version_id
    if intent.updated_at is not None:
        d["updated_at"] = format_canonical_utc_timestamp(intent.updated_at)
    if intent.completed_at is not None:
        d["completed_at"] = format_canonical_utc_timestamp(intent.completed_at)
    if intent.failure_reason is not None:
        d["failure_reason"] = intent.failure_reason
    return d


def _attempt_item_dict(
    *,
    complaint_id: str = "CF-2026-0001",
    intent_id: str = "I1",
    lease_token: str = "tok-1",
    status: str = "COPYING",
    source_version_id: str = "vid-1",
    final_key: str = "complaints/CF-2026-0001/images/I1.jpg",
    sha256: str = _SHA256,
    content_type: str = "image/jpeg",
    max_byte_size: int = 1_000_000,
    destination_version_id: str | None = None,
    created_at: datetime | None = None,
) -> dict:
    ts = format_canonical_utc_timestamp(created_at or _utc(2026, 1, 1, 0, 5, 0))
    d: dict = {
        "PK": f"ATTEMPT#{complaint_id}#{intent_id}",
        "SK": f"LEASE#{lease_token}",
        "entity_type": "UPLOAD_ATTEMPT",
        "complaint_id": complaint_id,
        "intent_id": intent_id,
        "lease_token": lease_token,
        "status": status,
        "source_version_id": source_version_id,
        "final_key": final_key,
        "sha256": sha256,
        "content_type": content_type,
        "max_byte_size": max_byte_size,
        "created_at": ts,
        "updated_at": ts,
    }
    if destination_version_id is not None:
        d["destination_version_id"] = destination_version_id
    return d


def _outbox_item_dict(
    *,
    intent_id: str = "I1",
    complaint_id: str = "CF-2026-0001",
    s3_key: str = "complaints/CF-2026-0001/images/I1.jpg",
    destination_version_id: str = "dst-vid-1",
    committed_at: datetime | None = None,
    sweep_attempts: int = 0,
) -> dict:
    return {
        "PK": "OUTBOX#TAG_PROMOTION",
        "SK": f"INTENT#{intent_id}",
        "entity_type": "TAG_OUTBOX",
        "intent_id": intent_id,
        "complaint_id": complaint_id,
        "s3_key": s3_key,
        "destination_version_id": destination_version_id,
        "committed_at": format_canonical_utc_timestamp(committed_at or _utc(2026, 1, 1, 0, 5, 0)),
        "sweep_attempts": sweep_attempts,
    }


def _client_error(code: str, *, reasons: list[dict] | None = None) -> ClientError:
    payload: dict = {"Error": {"Code": code, "Message": f"Stubbed {code}"}}
    if reasons is not None:
        payload["CancellationReasons"] = reasons
    return ClientError(payload, "TransactWriteItems")


def _make_stub_repo(**kwargs) -> tuple[DynamoDBComplaintRepository, MagicMock]:
    client = MagicMock()
    repo = DynamoDBComplaintRepository(
        _TABLE_NAME,
        client=client,
        cursor_secret="test-hmac-secret",
        base_delay_seconds=0.01,
        sleep_fn=lambda _: None,
        **kwargs,
    )
    return repo, client


# ===========================================================================
# 1. Botocore Stubber Protocol & Wire Format Tests
# ===========================================================================

class TestDynamoDBStubberContracts(unittest.TestCase):

    def test_create_intent_writes_canonical_sha256_and_maps_missing_complaint(self) -> None:
        client = boto3.client("dynamodb", **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        repo = DynamoDBComplaintRepository(_TABLE_NAME, client=client, cursor_secret="s")
        intent = _make_intent(status=UploadIntentStatus.PENDING_UPLOAD, lease_token=None, source_version_id=None)

        def _check_tx(params, model, **kwargs) -> None:
            if model.name == "TransactWriteItems":
                items = params["TransactItems"]
                self.assertEqual(len(items), 2)
                self.assertEqual(
                    items[0]["ConditionCheck"]["Key"],
                    {"PK": {"S": "COMPLAINT#CF-2026-0001"}, "SK": {"S": "META"}},
                )
                put_item = items[1]["Put"]["Item"]
                self.assertEqual(put_item["PK"], {"S": "UPLOAD_INTENT#CF-2026-0001"})
                self.assertEqual(put_item["SK"], {"S": "INTENT#I1"})
                self.assertIn("sha256", put_item)
                self.assertNotIn("expected_sha256", put_item)
                self.assertEqual(put_item["created_at"], {"S": "2026-01-01T00:00:00.000000Z"})

        client.meta.events.register("before-parameter-build.dynamodb.TransactWriteItems", _check_tx)
        stubber.add_response("transact_write_items", {})
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            modeled_fields={"CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}]},
        )

        with stubber:
            repo.create_intent(intent)
            with self.assertRaises(ComplaintNotFoundError):
                repo.create_intent(intent)
        stubber.assert_no_pending_responses()

    def test_begin_copy_attempt_is_atomic_2_item_transaction_without_pre_read(self) -> None:
        client = boto3.client("dynamodb", **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        repo = DynamoDBComplaintRepository(_TABLE_NAME, client=client, cursor_secret="s")
        now = _utc(2026, 1, 1, 0, 5, 0)

        stubber.add_response(
            "transact_write_items",
            {},
            expected_params={
                "TransactItems": [
                    {
                        "ConditionCheck": {
                            "TableName": _TABLE_NAME,
                            "Key": {"PK": {"S": "UPLOAD_INTENT#CF-2026-0001"}, "SK": {"S": "INTENT#I1"}},
                            "ConditionExpression": (
                                "attribute_exists(PK) AND #status = :confirming "
                                "AND lease_token = :lease_token "
                                "AND :now_iso < lease_expires_at "
                                "AND source_version_id = :src_vid "
                                "AND sha256 = :sha256 "
                                "AND expected_mime = :expected_mime "
                                "AND max_byte_size = :max_byte_size "
                                "AND final_key = :final_key"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":confirming": {"S": "CONFIRMING"},
                                ":lease_token": {"S": "tok-1"},
                                ":now_iso": {"S": "2026-01-01T00:05:00.000000Z"},
                                ":src_vid": {"S": "vid-1"},
                                ":sha256": {"S": _SHA256},
                                ":expected_mime": {"S": "image/jpeg"},
                                ":max_byte_size": {"N": "1000000"},
                                ":final_key": {"S": "complaints/CF-2026-0001/images/I1.jpg"},
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": _TABLE_NAME,
                            "Item": _to_ddb(_attempt_item_dict(created_at=now)),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            },
        )

        with stubber:
            attempt = repo.begin_copy_attempt(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-1",
                source_version_id="vid-1",
                final_key="complaints/CF-2026-0001/images/I1.jpg",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                now=now,
            )
        stubber.assert_no_pending_responses()
        self.assertEqual(attempt.status, UploadAttemptStatus.COPYING)
        self.assertFalse(hasattr(attempt, "expected_complaint_version"))

    def test_begin_copy_attempt_cancellation_mapping_and_idempotent_replay(self) -> None:
        now = _utc(2026, 1, 1, 0, 5, 0)
        lease_exp = _utc(2026, 1, 1, 0, 10, 0)
        base_kw = dict(
            complaint_id="CF-2026-0001",
            intent_id="I1",
            lease_token="tok-1",
            source_version_id="vid-1",
            final_key="complaints/CF-2026-0001/images/I1.jpg",
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            now=now,
        )

        # Case 1: Existing matching COPYING attempt + active lease -> idempotent replay
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException",
            reasons=[{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
        )
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_intent_item_dict(_make_intent(lease_expires_at=lease_exp))),
                    _to_ddb(_attempt_item_dict(status="COPYING")),
                ]
            }
        }
        replayed = repo.begin_copy_attempt(**base_kw)
        self.assertEqual(replayed.status, UploadAttemptStatus.COPYING)

        # Case 2: Lease expired AT exact boundary (now == lease_expires_at) even if attempt exists -> StaleLeaseError
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException",
            reasons=[{"Code": "ConditionalCheckFailed"}, {"Code": "ConditionalCheckFailed"}],
        )
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_intent_item_dict(_make_intent(lease_expires_at=now))),
                    _to_ddb(_attempt_item_dict(status="COPYING")),
                ]
            }
        }
        with self.assertRaises(StaleLeaseError):
            repo.begin_copy_attempt(**base_kw)

        # Case 3: Attempt already COMMITTED on same lease -> ImmutableFieldViolationError
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException",
            reasons=[{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
        )
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_intent_item_dict(_make_intent(lease_expires_at=lease_exp))),
                    _to_ddb(_attempt_item_dict(status="COMMITTED", destination_version_id="dst-1")),
                ]
            }
        }
        with self.assertRaises(ImmutableFieldViolationError):
            repo.begin_copy_attempt(**base_kw)

        # Case 4: Intent missing -> UploadIntentNotFoundError
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException",
            reasons=[{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
        )
        client.batch_get_item.return_value = {"Responses": {_TABLE_NAME: []}}
        with self.assertRaises(UploadIntentNotFoundError):
            repo.begin_copy_attempt(**base_kw)

    def test_pin_source_version_boundary_and_disambiguation(self) -> None:
        now = _utc(2026, 1, 1, 0, 5, 0)
        client = boto3.client("dynamodb", **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        repo = DynamoDBComplaintRepository(_TABLE_NAME, client=client, cursor_secret="s")

        # Conflicting source_version_id in store -> ImmutableFieldViolationError
        stubber.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        stubber.add_response(
            "get_item",
            {"Item": _to_ddb(_intent_item_dict(_make_intent(lease_expires_at=now + timedelta(minutes=5), source_version_id="vid-OTHER")))},
        )
        with stubber:
            with self.assertRaises(ImmutableFieldViolationError):
                repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-NEW", now)
        stubber.assert_no_pending_responses()

    def test_commit_versioned_decision1_cancellation_matrix(self) -> None:
        now = _utc(2026, 1, 1, 0, 5, 0)
        lease_exp = _utc(2026, 1, 1, 0, 10, 0)
        commit_kw = dict(
            complaint_id="CF-2026-0001",
            intent_id="I1",
            lease_token="tok-1",
            final_key="complaints/CF-2026-0001/images/I1.jpg",
            source_version_id="vid-1",
            destination_version_id="dst-vid-1",
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            expected_complaint_version=1,
            staging_sha256=_SHA256,
            staging_size=50_000,
            now=now,
        )

        # 1. Existing outbox WITHOUT committed Complaint/Intent/Attempt MUST NOT be treated as success
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException",
            reasons=[{"Code": "None"}, {"Code": "None"}, {"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
        )
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_complaint_item_dict(_make_complaint(version=1))),
                    _to_ddb(_intent_item_dict(_make_intent(lease_expires_at=lease_exp))),
                    _to_ddb(_attempt_item_dict(status="COPYING")),
                    _to_ddb(_outbox_item_dict()),
                ]
            }
        }
        with self.assertRaises(ImmutableFieldViolationError):
            repo.commit_verified_attachment_versioned(**commit_kw)

        # 2. Genuine CW-5 replay (Complaint + Intent + Attempt committed, outbox already swept & deleted)
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException",
            reasons=[{"Code": "ConditionalCheckFailed"}] * 3 + [{"Code": "None"}],
        )
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_complaint_item_dict(_make_complaint(
                        version=2,
                        attachment_key="complaints/CF-2026-0001/images/I1.jpg",
                        attachment_status="VERIFIED",
                    ))),
                    _to_ddb(_intent_item_dict(_make_intent(
                        status=UploadIntentStatus.VERIFIED,
                        lease_expires_at=lease_exp,
                    ))),
                    _to_ddb(_attempt_item_dict(status="COMMITTED", destination_version_id="dst-vid-1")),
                ]
            }
        }
        res = repo.commit_verified_attachment_versioned(**commit_kw)
        self.assertIsNone(res.outbox)
        self.assertEqual(res.complaint.version, 2)

        # 3. Fenced lease_token even if Complaint already VERIFIED -> StaleLeaseError
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error("TransactionCanceledException")
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_complaint_item_dict(_make_complaint(
                        version=2,
                        attachment_key="complaints/CF-2026-0001/images/I1.jpg",
                        attachment_status="VERIFIED",
                    ))),
                    _to_ddb(_intent_item_dict(_make_intent(
                        status=UploadIntentStatus.VERIFIED,
                        lease_token="tok-WINNER",
                    ))),
                    _to_ddb(_attempt_item_dict(lease_token="tok-1", status="COPYING")),
                ]
            }
        }
        with self.assertRaises(StaleLeaseError):
            repo.commit_verified_attachment_versioned(**commit_kw)

        # 4. Complaint version conflict -> OptimisticLockError
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error("TransactionCanceledException")
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_complaint_item_dict(_make_complaint(version=2))),
                    _to_ddb(_intent_item_dict(_make_intent())),
                    _to_ddb(_attempt_item_dict(status="COPYING")),
                ]
            }
        }
        with self.assertRaises(OptimisticLockError):
            repo.commit_verified_attachment_versioned(**commit_kw)

        # 5. Missing Attempt -> UploadIntentNotFoundError
        repo, client = _make_stub_repo()
        client.transact_write_items.side_effect = _client_error("TransactionCanceledException")
        client.batch_get_item.return_value = {
            "Responses": {
                _TABLE_NAME: [
                    _to_ddb(_complaint_item_dict(_make_complaint(version=1))),
                    _to_ddb(_intent_item_dict(_make_intent())),
                ]
            }
        }
        with self.assertRaises(UploadIntentNotFoundError):
            repo.commit_verified_attachment_versioned(**commit_kw)

    def test_transient_retry_succeeds_and_exhaustion_raises_storage_throttle_error(self) -> None:
        sleeps: list[float] = []
        repo, client = _make_stub_repo(max_retries=2)
        repo._sleep = sleeps.append

        # Succeeds after 1 TransactionCanceledException(TransactionConflict) + 1 ThrottlingException
        client.get_item.side_effect = [
            _client_error("ThrottlingException"),
            {"Item": _to_ddb(_outbox_item_dict())},
        ]
        rec = repo.get_outbox_record("I1")
        self.assertIsNotNone(rec)
        self.assertEqual(len(sleeps), 1)

        # Exhausts retries on TransactionConflict cancellation reason
        sleeps.clear()
        client.delete_item.side_effect = _client_error(
            "TransactionCanceledException",
            reasons=[{"Code": "TransactionConflict"}],
        )
        with self.assertRaises(StorageThrottleError):
            repo.delete_outbox_record("I1")
        self.assertEqual(len(sleeps), 2)

    def test_outbox_pagination_lek_and_hmac_tamper_and_cross_scope_rejection(self) -> None:
        client = boto3.client("dynamodb", **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        repo = DynamoDBComplaintRepository(_TABLE_NAME, client=client, cursor_secret="hmac-key")

        stubber.add_response(
            "query",
            {
                "Items": [_to_ddb(_outbox_item_dict(intent_id="I1"))],
                "LastEvaluatedKey": {
                    "PK": {"S": "OUTBOX#TAG_PROMOTION"},
                    "SK": {"S": "INTENT#I1"},
                },
            },
            expected_params={
                "TableName": _TABLE_NAME,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": "OUTBOX#TAG_PROMOTION"}},
                "ConsistentRead": True,
                "ScanIndexForward": True,
                "Limit": 1,
            },
        )
        stubber.add_response(
            "query",
            {"Items": [_to_ddb(_outbox_item_dict(intent_id="I2"))]},
            expected_params={
                "TableName": _TABLE_NAME,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": "OUTBOX#TAG_PROMOTION"}},
                "ConsistentRead": True,
                "ScanIndexForward": True,
                "Limit": 1,
                "ExclusiveStartKey": {
                    "PK": {"S": "OUTBOX#TAG_PROMOTION"},
                    "SK": {"S": "INTENT#I1"},
                },
            },
        )

        with stubber:
            page1, cursor1 = repo.list_pending_outbox_records_paginated(limit=1)
            self.assertEqual([r.intent_id for r in page1], ["I1"])
            self.assertIsNotNone(cursor1)
            page2, cursor2 = repo.list_pending_outbox_records_paginated(limit=1, cursor=cursor1)
            self.assertEqual([r.intent_id for r in page2], ["I2"])
            self.assertIsNone(cursor2)
        stubber.assert_no_pending_responses()

        # Tampered cursor & wrong-scope cursor must fail with ValidationError
        assert cursor1 is not None
        tampered = cursor1[:-2] + ("AA" if not cursor1.endswith("AA") else "BB")
        with self.assertRaises(ValidationError):
            repo.list_pending_outbox_records_paginated(cursor=tampered)
        with self.assertRaises(ValidationError):
            repo.list_paginated(cursor=cursor1)


# ===========================================================================
# 2. Full Stateful Moto Tests (S3 Versioned + DynamoDB Single-Table)
# ===========================================================================

@unittest.skipIf(mock_aws is None, "moto is not installed in this Python environment")
class TestDynamoDBStatefulMotoIntegration(unittest.TestCase):

    def _create_table(self, ddb_client) -> None:
        ddb_client.create_table(
            TableName=_TABLE_NAME,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
                {"AttributeName": "GSI2PK", "AttributeType": "S"},
                {"AttributeName": "GSI2SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2",
                    "KeySchema": [
                        {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )

    def test_end_to_end_stage_b1_worker_with_moto_s3_and_dynamodb(self) -> None:
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-east-1")
            s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
            self._create_table(ddb)
            s3.create_bucket(Bucket="campusfix-uploads")
            s3.put_bucket_versioning(
                Bucket="campusfix-uploads",
                VersioningConfiguration={"Status": "Enabled"},
            )

            repo = DynamoDBComplaintRepository(_TABLE_NAME, client=ddb, cursor_secret="moto-secret")
            adapter = RepositoryVersionedUploadStateAdapter(repo, repo)
            storage = Boto3S3StorageService(client=s3)

            now = _utc(2026, 2, 1, 12, 0, 0)
            payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00" + (b"\x00" * 1100) + b"\xff\xd9"
            sha_hex = hashlib.sha256(payload).hexdigest()
            staging_key = "staging/complaints/CF-2026-0001/I1/upload.tmp"
            final_key = "complaints/CF-2026-0001/images/I1.jpg"

            s3.put_object(
                Bucket="campusfix-uploads",
                Key=staging_key,
                Body=payload,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )

            complaint = _make_complaint("CF-2026-0001", submitted_at=now)
            repo.create(complaint)
            intent = _make_intent(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                status=UploadIntentStatus.PENDING_UPLOAD,
                lease_token=None,
                lease_expires_at=None,
                source_version_id=None,
                expected_sha256=sha_hex,
                expected_mime="image/jpeg",
                max_byte_size=1024 * 1024,
            )
            intent = replace(
                intent,
                created_at=now,
                confirmation_deadline=now + timedelta(minutes=15),
                recovery_deadline=now + timedelta(hours=1),
                updated_at=now,
            )
            repo.create_intent(intent)
            repo.claim_intent_lease("CF-2026-0001", "I1", "worker-1", 120, now + timedelta(seconds=10))

            worker = UploadConfirmationWorker(
                s3_storage=storage,
                state_port=adapter,
                source_bucket="campusfix-uploads",
                now_fn=lambda: now + timedelta(seconds=11),
            )
            worker.confirm_upload(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                worker_token="worker-1",
            )

            stored_c = repo.get_consistent("CF-2026-0001")
            stored_i = repo.get_intent_consistent("CF-2026-0001", "I1")
            stored_a = repo.get_attempt("CF-2026-0001", "I1", "worker-1")
            stored_o = repo.get_outbox_record("I1")
            assert stored_c is not None and stored_i is not None and stored_a is not None and stored_o is not None
            self.assertEqual(stored_c.version, 2)
            self.assertEqual(stored_c.attachment_status, "VERIFIED")
            self.assertEqual(stored_c.attachment_key, final_key)
            self.assertEqual(stored_i.status, UploadIntentStatus.VERIFIED)
            self.assertIsNotNone(stored_i.source_version_id)
            self.assertEqual(stored_a.status, UploadAttemptStatus.COMMITTED)
            self.assertIsNotNone(stored_a.destination_version_id)
            self.assertEqual(stored_o.destination_version_id, stored_a.destination_version_id)

            # Verify raw DynamoDB item does NOT have expected_sha256 or expected_complaint_version
            raw_intent = ddb.get_item(
                TableName=_TABLE_NAME,
                Key={"PK": {"S": "UPLOAD_INTENT#CF-2026-0001"}, "SK": {"S": "INTENT#I1"}},
                ConsistentRead=True,
            )["Item"]
            self.assertIn("sha256", raw_intent)
            self.assertNotIn("expected_sha256", raw_intent)
            raw_attempt = ddb.get_item(
                TableName=_TABLE_NAME,
                Key={"PK": {"S": "ATTEMPT#CF-2026-0001#I1"}, "SK": {"S": "LEASE#worker-1"}},
                ConsistentRead=True,
            )["Item"]
            self.assertNotIn("expected_complaint_version", raw_attempt)

            # CW-5 already-swept replay with pre-copy version=1 still succeeds with outbox=None
            repo.delete_outbox_record("I1")
            swept = repo.commit_verified_attachment_versioned(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="worker-1",
                final_key=final_key,
                source_version_id=stored_i.source_version_id,
                destination_version_id=stored_a.destination_version_id,
                expected_sha256=sha_hex,
                expected_mime="image/jpeg",
                max_byte_size=1024 * 1024,
                expected_complaint_version=1,
                staging_sha256=sha_hex,
                staging_size=len(payload),
                now=now + timedelta(seconds=20),
            )
            self.assertIsNone(swept.outbox)
            self.assertEqual(swept.complaint.version, 2)

    def test_cw2_lease_takeover_and_decision2_microsecond_boundaries_in_moto(self) -> None:
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-east-1")
            self._create_table(ddb)
            repo = DynamoDBComplaintRepository(_TABLE_NAME, client=ddb, cursor_secret="moto-secret")

            now = _utc(2026, 2, 1, 12, 0, 0)
            repo.create(_make_complaint("CF-2026-0001", submitted_at=now))
            repo.create_intent(replace(
                _make_intent(
                    complaint_id="CF-2026-0001",
                    intent_id="I1",
                    status=UploadIntentStatus.PENDING_UPLOAD,
                    lease_token=None,
                    lease_expires_at=None,
                    source_version_id=None,
                ),
                created_at=now,
                confirmation_deadline=now + timedelta(minutes=15),
                recovery_deadline=now + timedelta(hours=1),
                updated_at=now,
            ))

            # Lease lasts 60s -> lease_expires = 12:01:00.000000
            claimed1 = repo.claim_intent_lease("CF-2026-0001", "I1", "tok-1", 60, now)
            exp = claimed1.lease_expires_at
            assert exp is not None
            before_1us = exp - timedelta(microseconds=1)
            after_1us = exp + timedelta(microseconds=1)

            # pin_source_version & begin_copy_attempt: -1us allowed, exact == & +1us rejected
            pinned = repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-1", before_1us)
            self.assertEqual(pinned.source_version_id, "vid-1")
            with self.assertRaises(StaleLeaseError):
                repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-1", exp)

            a1 = repo.begin_copy_attempt(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-1",
                source_version_id="vid-1",
                final_key="complaints/CF-2026-0001/images/I1.jpg",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                now=before_1us,
            )
            with self.assertRaises(StaleLeaseError):
                repo.begin_copy_attempt(
                    complaint_id="CF-2026-0001",
                    intent_id="I1",
                    lease_token="tok-1",
                    source_version_id="vid-1",
                    final_key="complaints/CF-2026-0001/images/I1.jpg",
                    expected_sha256=_SHA256,
                    expected_mime="image/jpeg",
                    max_byte_size=1_000_000,
                    now=exp,
                )

            # claim_intent_lease preserves now <= lease_expires_at: exact == blocked, +1us succeeds
            with self.assertRaises(StaleLeaseError):
                repo.claim_intent_lease("CF-2026-0001", "I1", "tok-2", 60, exp)
            claimed2 = repo.claim_intent_lease("CF-2026-0001", "I1", "tok-2", 60, after_1us)
            self.assertEqual(claimed2.lease_token, "tok-2")

            a2 = repo.begin_copy_attempt(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-2",
                source_version_id="vid-1",
                final_key="complaints/CF-2026-0001/images/I1.jpg",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                now=after_1us,
            )
            self.assertEqual([a.lease_token for a in repo.list_attempts("CF-2026-0001", "I1")], ["tok-1", "tok-2"])
            self.assertEqual(a1.status, UploadAttemptStatus.COPYING)
            self.assertEqual(a2.status, UploadAttemptStatus.COPYING)

            # Worker 2 commits; Worker 1's COPYING attempt preserved; late Worker 1 commit fails StaleLeaseError
            res = repo.commit_verified_attachment_versioned(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-2",
                final_key="complaints/CF-2026-0001/images/I1.jpg",
                source_version_id="vid-1",
                destination_version_id="dst-2",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                expected_complaint_version=1,
                staging_sha256=_SHA256,
                staging_size=5000,
                now=after_1us,
            )
            self.assertEqual(res.attempt.status, UploadAttemptStatus.COMMITTED)
            self.assertEqual(repo.get_attempt("CF-2026-0001", "I1", "tok-1").status, UploadAttemptStatus.COPYING)

            with self.assertRaises(StaleLeaseError):
                repo.commit_verified_attachment_versioned(
                    complaint_id="CF-2026-0001",
                    intent_id="I1",
                    lease_token="tok-1",
                    final_key="complaints/CF-2026-0001/images/I1.jpg",
                    source_version_id="vid-1",
                    destination_version_id="dst-1",
                    expected_sha256=_SHA256,
                    expected_mime="image/jpeg",
                    max_byte_size=1_000_000,
                    expected_complaint_version=1,
                    staging_sha256=_SHA256,
                    staging_size=5000,
                    now=after_1us,
                )

    def test_outbox_live_pagination_and_sweeper_ops_in_moto(self) -> None:
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-east-1")
            self._create_table(ddb)
            repo = DynamoDBComplaintRepository(_TABLE_NAME, client=ddb, cursor_secret="moto-secret")
            base = _utc(2026, 2, 1, 10, 0, 0)

            for idx, (iid, offset_hrs) in enumerate((("I1", 5), ("I2", 1), ("I3", 3), ("I4", 2))):
                cid = f"CF-2026-000{idx + 1}"
                repo.create(_make_complaint(cid, submitted_at=base))
                repo.create_intent(replace(
                    _make_intent(complaint_id=cid, intent_id=iid, lease_expires_at=base + timedelta(hours=1)),
                    created_at=base,
                    confirmation_deadline=base + timedelta(minutes=15),
                    recovery_deadline=base + timedelta(hours=2),
                    updated_at=base,
                ))
                repo.begin_copy_attempt(
                    complaint_id=cid,
                    intent_id=iid,
                    lease_token="tok-1",
                    source_version_id="vid-1",
                    final_key=f"complaints/{cid}/images/{iid}.jpg",
                    expected_sha256=_SHA256,
                    expected_mime="image/jpeg",
                    max_byte_size=1_000_000,
                    now=base,
                )
                repo.commit_verified_attachment_versioned(
                    complaint_id=cid,
                    intent_id=iid,
                    lease_token="tok-1",
                    final_key=f"complaints/{cid}/images/{iid}.jpg",
                    source_version_id="vid-1",
                    destination_version_id=f"dst-{iid}",
                    expected_sha256=_SHA256,
                    expected_mime="image/jpeg",
                    max_byte_size=1_000_000,
                    expected_complaint_version=1,
                    staging_sha256=_SHA256,
                    staging_size=1024,
                    now=base + timedelta(hours=offset_hrs),
                )

            # Key-ordered by SK (intent_id) ascending regardless of committed_at
            self.assertEqual([r.intent_id for r in repo.list_pending_outbox_records()], ["I1", "I2", "I3", "I4"])

            page1, c1 = repo.list_pending_outbox_records_paginated(limit=2)
            self.assertEqual([r.intent_id for r in page1], ["I1", "I2"])
            self.assertIsNotNone(c1)

            # Live mutations between pages: delete I2 and insert I5 after cursor
            repo.delete_outbox_record("I2")
            repo.create(_make_complaint("CF-2026-0005", submitted_at=base))
            repo.create_intent(replace(
                _make_intent(complaint_id="CF-2026-0005", intent_id="I5", lease_expires_at=base + timedelta(hours=1)),
                created_at=base,
                confirmation_deadline=base + timedelta(minutes=15),
                recovery_deadline=base + timedelta(hours=2),
                updated_at=base,
            ))
            repo.begin_copy_attempt(
                complaint_id="CF-2026-0005",
                intent_id="I5",
                lease_token="tok-1",
                source_version_id="vid-1",
                final_key="complaints/CF-2026-0005/images/I5.jpg",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                now=base,
            )
            repo.commit_verified_attachment_versioned(
                complaint_id="CF-2026-0005",
                intent_id="I5",
                lease_token="tok-1",
                final_key="complaints/CF-2026-0005/images/I5.jpg",
                source_version_id="vid-1",
                destination_version_id="dst-I5",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                expected_complaint_version=1,
                staging_sha256=_SHA256,
                staging_size=1024,
                now=base,
            )

            seen: list[str] = []
            cur = c1
            while cur is not None:
                pg, cur = repo.list_pending_outbox_records_paginated(limit=2, cursor=cur)
                seen.extend(r.intent_id for r in pg)
            self.assertEqual(seen, ["I3", "I4", "I5"])

            updated = repo.increment_outbox_sweep_attempt("I3", base)
            self.assertEqual(updated.sweep_attempts, 1)

    def test_complaint_crud_upvotes_follow_up_locks_and_3way_gsi1_pagination_in_moto(self) -> None:
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-east-1")
            self._create_table(ddb)
            repo = DynamoDBComplaintRepository(_TABLE_NAME, client=ddb, cursor_secret="moto-secret")
            t0 = _utc(2026, 3, 1, 10, 0, 0)

            # Seed complaints across all 3 statuses with tie timestamps for GSI1 3-way merge
            c1 = _make_complaint("CF-2026-0001", status=ComplaintStatus.PENDING, submitted_at=t0, building="Block A")
            c2 = _make_complaint("CF-2026-0002", status=ComplaintStatus.IN_PROGRESS, submitted_at=t0, building="Block A")
            c3 = _make_complaint("CF-2026-0003", status=ComplaintStatus.RESOLVED, submitted_at=t0 + timedelta(seconds=5), building="Block B")
            c4 = _make_complaint("CF-2026-0004", status=ComplaintStatus.PENDING, submitted_at=t0 - timedelta(seconds=5), building="Block A")
            for c in (c1, c2, c3, c4):
                repo.create(c)

            self.assertEqual(repo.count(), 4)
            # Duplicate create rejected
            with self.assertRaises(ComplaintAlreadyExistsError):
                repo.create(c1)

            # 3-way merge across all statuses sorted (submitted_at, id) descending
            p1, cur1 = repo.list_paginated(limit=2)
            self.assertEqual([c.id for c in p1], ["CF-2026-0003", "CF-2026-0002"])
            self.assertIsNotNone(cur1)
            # Cursor bound to filters
            with self.assertRaises(ValidationError):
                repo.list_paginated(status=ComplaintStatus.PENDING, limit=2, cursor=cur1)
            p2, cur2 = repo.list_paginated(limit=2, cursor=cur1)
            self.assertEqual([c.id for c in p2], ["CF-2026-0001", "CF-2026-0004"])
            self.assertIsNone(cur2)

            # Case-insensitive building filter
            blk_a, blk_cur = repo.list_paginated(building="block a", limit=2)
            self.assertEqual([c.id for c in blk_a], ["CF-2026-0002", "CF-2026-0001"])
            blk_a2, blk_cur2 = repo.list_paginated(building="block a", limit=2, cursor=blk_cur)
            self.assertEqual([c.id for c in blk_a2], ["CF-2026-0004"])
            self.assertIsNone(blk_cur2)

            # Option B idempotent upvotes & empty String Set removal
            up1, ch1 = repo.add_upvote_atomic("CF-2026-0001", "voter-1")
            self.assertTrue(ch1)
            self.assertEqual(up1.version, 2)
            up2, ch2 = repo.add_upvote_atomic("CF-2026-0001", "voter-1")
            self.assertFalse(ch2)
            self.assertEqual(up2.version, 2)
            rm1, ch3 = repo.remove_upvote_atomic("CF-2026-0001", "voter-1")
            self.assertTrue(ch3)
            self.assertEqual(rm1.upvotes, [])
            self.assertEqual(rm1.version, 3)

            # Follow-up lock creation, active uniqueness, resolution lock release, and post-resolution feedback update
            fu1 = _make_complaint(
                "CF-2026-FU01",
                status=ComplaintStatus.PENDING,
                submitted_at=t0 + timedelta(minutes=1),
                is_follow_up=True,
                original_complaint_id="CF-2026-0003",
                follow_up_reason="Issue reappeared the very next morning in the lab.",
            )
            repo.create_follow_up_atomic("CF-2026-0003", fu1)
            self.assertTrue(repo.has_active_follow_up("CF-2026-0003"))

            fu2 = replace(fu1, id="CF-2026-FU02")
            with self.assertRaises(ActiveFollowUpExistsError):
                repo.create_follow_up_atomic("CF-2026-0003", fu2)

            # Resolving follow-up atomically releases lock
            fu1_loaded = repo.get_consistent("CF-2026-FU01")
            assert fu1_loaded is not None
            fu1_loaded.status = ComplaintStatus.RESOLVED
            fu1_loaded.resolved_at = t0 + timedelta(minutes=5)
            fu1_loaded.resolution_remarks = "Replaced faulty capacitor."
            repo.update(fu1_loaded, expected_version=1)
            self.assertEqual(fu1_loaded.version, 2)
            self.assertFalse(repo.has_active_follow_up("CF-2026-0003"))

            # Subsequent update on already-resolved follow-up (e.g. feedback) works with missing lock
            fu1_loaded.feedback = ComplaintFeedback(
                rating=5,
                comment="Works smoothly now.",
                submitted_at=t0 + timedelta(minutes=6),
            )
            repo.update(fu1_loaded, expected_version=2)
            self.assertEqual(repo.get_consistent("CF-2026-FU01").feedback.rating, 5)

            # Now a new follow-up for CF-2026-0003 succeeds again
            repo.create_follow_up_atomic("CF-2026-0003", fu2)
            self.assertEqual([f.id for f in repo.find_by_original_id("CF-2026-0003")], ["CF-2026-FU01", "CF-2026-FU02"])

    def test_worker_concurrent_complaint_update_and_lifecycle_edges_in_moto(self) -> None:
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-east-1")
            s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
            self._create_table(ddb)
            s3.create_bucket(Bucket="campusfix-uploads")
            s3.put_bucket_versioning(
                Bucket="campusfix-uploads",
                VersioningConfiguration={"Status": "Enabled"},
            )
            repo = DynamoDBComplaintRepository(_TABLE_NAME, client=ddb, cursor_secret="moto-secret")
            adapter = RepositoryVersionedUploadStateAdapter(repo, repo)
            storage = Boto3S3StorageService(client=s3)

            now = _utc(2026, 4, 1, 12, 0, 0)
            payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00" + (b"\x00" * 1100) + b"\xff\xd9"
            sha_hex = hashlib.sha256(payload).hexdigest()
            staging_key = "staging/complaints/CF-2026-0001/I1/upload.tmp"
            s3.put_object(
                Bucket="campusfix-uploads",
                Key=staging_key,
                Body=payload,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )

            repo.create(_make_complaint("CF-2026-0001", submitted_at=now))
            repo.create_intent(replace(
                _make_intent(
                    complaint_id="CF-2026-0001",
                    intent_id="I1",
                    status=UploadIntentStatus.CONFIRMING,
                    lease_token="w1",
                    lease_expires_at=now + timedelta(minutes=10),
                    source_version_id=None,
                    expected_sha256=sha_hex,
                ),
                created_at=now,
                upload_expires_at=now + timedelta(minutes=10),
                confirmation_deadline=now + timedelta(minutes=15),
                recovery_deadline=now + timedelta(hours=1),
                updated_at=now,
            ))

            # Bump Complaint.version mid-flight during S3 CopyObject
            def _bump_on_copy(model, **kwargs) -> None:
                if getattr(model, "name", None) == "CopyObject":
                    c = repo.get_consistent("CF-2026-0001")
                    assert c is not None
                    repo.update(replace(c, title="Edited mid-copy"), expected_version=c.version)

            s3.meta.events.register_first("before-call.s3.CopyObject", _bump_on_copy)
            worker = UploadConfirmationWorker(
                s3_storage=storage,
                state_port=adapter,
                source_bucket="campusfix-uploads",
                now_fn=lambda: now + timedelta(seconds=5),
            )
            with self.assertRaises(OptimisticLockError):
                worker.confirm_upload(
                    complaint_id="CF-2026-0001",
                    intent_id="I1",
                    worker_token="w1",
                )
            c_after = repo.get_consistent("CF-2026-0001")
            i_after = repo.get_intent_consistent("CF-2026-0001", "I1")
            a_after = repo.get_attempt("CF-2026-0001", "I1", "w1")
            self.assertEqual(c_after.version, 2)
            self.assertIsNone(c_after.attachment_status)
            self.assertEqual(i_after.status, UploadIntentStatus.CONFIRMING)
            self.assertEqual(a_after.status, UploadAttemptStatus.COPYING)
            self.assertIsNone(repo.get_outbox_record("I1"))

            # Toggle upvote, missing staging attempts, deadline expiry, and follow-up cancellation checks
            toggled1, added = repo.toggle_upvote_atomic("CF-2026-0001", "stu-9")
            self.assertTrue(added)
            self.assertEqual(toggled1.version, 3)
            toggled2, added2 = repo.toggle_upvote_atomic("CF-2026-0001", "stu-9")
            self.assertFalse(added2)
            self.assertEqual(toggled2.upvotes, [])

            self.assertEqual(repo.record_missing_staging_attempt("CF-2026-0001", "I1", 2, now), 1)
            self.assertEqual(repo.get_intent_consistent("CF-2026-0001", "I1").status, UploadIntentStatus.PENDING_UPLOAD)
            self.assertEqual(repo.record_missing_staging_attempt("CF-2026-0001", "I1", 2, now), 2)
            self.assertEqual(repo.get_intent_consistent("CF-2026-0001", "I1").status, UploadIntentStatus.FAILED)

            repo.create_intent(replace(
                _make_intent(
                    complaint_id="CF-2026-0001",
                    intent_id="I2",
                    status=UploadIntentStatus.PENDING_UPLOAD,
                    lease_token=None,
                    lease_expires_at=None,
                    source_version_id=None,
                ),
                created_at=now,
                upload_expires_at=now + timedelta(minutes=5),
                confirmation_deadline=now + timedelta(minutes=1),
                recovery_deadline=now + timedelta(minutes=10),
                updated_at=now,
            ))
            with self.assertRaises(UploadIntentExpiredError):
                repo.claim_intent_lease("CF-2026-0001", "I2", "tok-exp", 60, now + timedelta(minutes=2))
            self.assertEqual(repo.get_intent_consistent("CF-2026-0001", "I2").status, UploadIntentStatus.FAILED)

            # Follow-up eligibility & ownership errors
            bad_fu = _make_complaint(
                "CF-2026-FU99",
                submitted_at=now,
                is_follow_up=True,
                original_complaint_id="MISSING",
                follow_up_reason="At least ten characters reason.",
            )
            with self.assertRaises(ComplaintNotFoundError):
                repo.create_follow_up_atomic("MISSING", bad_fu)
            with self.assertRaises(FollowUpEligibilityError):
                repo.create_follow_up_atomic(
                    "CF-2026-0001",
                    replace(bad_fu, original_complaint_id="CF-2026-0001"),
                )


if __name__ == "__main__":
    unittest.main()
