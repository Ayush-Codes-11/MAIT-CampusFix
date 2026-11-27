"""DynamoDB repository adapter for complaints, upload intents, copy attempts, and outbox records.

Implements both :class:`ComplaintRepository` and :class:`UploadIntentRepository`
against a single DynamoDB table using low-level ``boto3.client("dynamodb")``
operations so all transactional writes, condition expressions, and strongly
consistent reads map 1-to-1 to the approved Phase 3.3 architecture and
``phase-3.3-step2-final-spec.md``.

Single-table key layout:
    - Complaint:
        PK = ``COMPLAINT#{complaint_id}``, SK = ``META``
        GSI1PK = ``STATUS#{status}``, GSI1SK = ``SUBMITTED_AT#{27-char-UTC}#ID#{complaint_id}``
        GSI2PK = ``ORIG#{original_complaint_id}``, GSI2SK = ``SUBMITTED_AT#{27-char-UTC}#ID#{complaint_id}``
    - Active follow-up uniqueness lock:
        PK = ``COMPLAINT#{original_complaint_id}#ACTIVE_FOLLOW_UP``, SK = ``LOCK``
    - Upload intent:
        PK = ``UPLOAD_INTENT#{complaint_id}``, SK = ``INTENT#{intent_id}``
    - Upload copy attempt:
        PK = ``ATTEMPT#{complaint_id}#{intent_id}``, SK = ``LEASE#{lease_token}``
    - Tag-promotion outbox record:
        PK = ``OUTBOX#TAG_PROMOTION``, SK = ``INTENT#{intent_id}``
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import os
import random
import secrets
import time
from typing import Any, Callable, Sequence

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from campusfix.domain.models import (
    AtomicAttachmentCommitResult,
    Complaint,
    ComplaintFeedback,
    ComplaintStatus,
    IssueCategory,
    StatusHistoryEntry,
    TagOutboxRecord,
    UploadAttempt,
    UploadAttemptStatus,
    UploadIntent,
    UploadIntentStatus,
    ValidationError,
    format_canonical_utc_timestamp,
    normalize_sha256_hex,
    parse_utc_iso_datetime,
)
from campusfix.domain.rules import (
    ActiveFollowUpExistsError,
    ComplaintAlreadyExistsError,
    ComplaintNotFoundError,
    FollowUpEligibilityError,
    ImmutableFieldViolationError,
    InvalidFollowUpReasonError,
    OptimisticLockError,
    StaleLeaseError,
    StorageThrottleError,
    UnauthorizedError,
    UploadFailedError,
    UploadIntentExpiredError,
    UploadIntentNotFoundError,
)
from .interfaces import ComplaintRepository, UploadIntentRepository


_TRANSIENT_THROTTLE_CODES = frozenset({
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
    "InternalServerError",
})

_TRANSIENT_CONFLICT_CODES = frozenset({
    "TransactionConflictException",
    "TransactionConflict",
})

_DEFAULT_MAX_RETRIES = 4
_DEFAULT_BASE_DELAY_SECONDS = 0.05
_PAGINATION_SECRET_ENV = "PAGINATION_HMAC_SECRET"


def _validate_version_id(version_id: Any, field_name: str, caller: str) -> str:
    """Validate and trim a non-empty S3 VersionId (rejecting literal 'null')."""
    if not isinstance(version_id, str) or version_id.strip() in ("", "null"):
        raise ValidationError(
            f"{caller}: '{field_name}' must be a non-empty valid S3 VersionId, got {version_id!r}."
        )
    return version_id.strip()


def _require_non_empty_str(value: Any, field_name: str, caller: str) -> str:
    """Validate and trim a non-empty string field."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{caller}: '{field_name}' must be a non-empty string.")
    return value.strip()


def _validate_positive_int(value: Any, field_name: str, caller: str, *, minimum: int = 1) -> int:
    """Validate a non-boolean integer >= minimum."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(
            f"{caller}: '{field_name}' must be an int >= {minimum}, got {value!r}."
        )
    return value


def _b64url_encode(data: bytes) -> str:
    """Encode bytes into unpadded URL-safe base64 ASCII string."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(token: str) -> bytes:
    """Decode unpadded or padded URL-safe base64 ASCII string."""
    if not isinstance(token, str) or not token:
        raise ValueError("Empty base64url segment")
    padded = token + "=" * ((4 - len(token) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _normalize_decoded_value(value: Any) -> Any:
    """Recursively convert boto3 TypeDeserializer Decimals to ints where appropriate."""
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {k: _normalize_decoded_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_decoded_value(v) for v in value]
    if isinstance(value, set):
        return {_normalize_decoded_value(v) for v in value}
    return value


class DynamoDBComplaintRepository(ComplaintRepository, UploadIntentRepository):
    """Production DynamoDB adapter implementing Complaint and UploadIntent persistence.

    Uses strongly consistent reads (``ConsistentRead=True``) prior to copy and
    during ``TransactionCanceledException`` disambiguation so idempotent replays
    are strictly distinguished from partial, fenced, or conflicting state.
    """

    def __init__(
        self,
        table_name: str,
        *,
        client: Any | None = None,
        region_name: str | None = None,
        cursor_secret: str | bytes | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValidationError("DynamoDBComplaintRepository: 'table_name' must be a non-empty string.")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValidationError("DynamoDBComplaintRepository: 'max_retries' must be an int >= 0.")
        if isinstance(base_delay_seconds, bool) or not isinstance(base_delay_seconds, (int, float)) or base_delay_seconds < 0:
            raise ValidationError("DynamoDBComplaintRepository: 'base_delay_seconds' must be >= 0.")

        self._table_name = table_name.strip()
        self._client = client if client is not None else boto3.client("dynamodb", region_name=region_name)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()
        self._max_retries = max_retries
        self._base_delay_seconds = float(base_delay_seconds)
        self._sleep = sleep_fn
        self._jitter = jitter_fn

        if cursor_secret is not None:
            raw_secret = cursor_secret.encode("utf-8") if isinstance(cursor_secret, str) else bytes(cursor_secret)
            if not raw_secret:
                raise ValidationError("DynamoDBComplaintRepository: 'cursor_secret' cannot be empty.")
            self._cursor_secret = raw_secret
        else:
            env_secret = os.environ.get(_PAGINATION_SECRET_ENV, "").strip()
            self._cursor_secret = env_secret.encode("utf-8") if env_secret else secrets.token_bytes(32)

    # -------------------------------------------------------------------------
    # DynamoDB serialization, keys, cursors & retry helpers
    # -------------------------------------------------------------------------

    def _to_av(self, python_item: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Serialize a flat/nested Python dictionary to DynamoDB AttributeValue map."""
        return {k: self._serializer.serialize(v) for k, v in python_item.items() if v is not None}

    def _from_av(self, av_item: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Deserialize a DynamoDB AttributeValue map to normalized Python types."""
        decoded = {k: self._deserializer.deserialize(v) for k, v in av_item.items()}
        return _normalize_decoded_value(decoded)

    @staticmethod
    def _complaint_key(complaint_id: str) -> dict[str, dict[str, str]]:
        return {"PK": {"S": f"COMPLAINT#{complaint_id}"}, "SK": {"S": "META"}}

    @staticmethod
    def _follow_up_lock_key(original_complaint_id: str) -> dict[str, dict[str, str]]:
        return {
            "PK": {"S": f"COMPLAINT#{original_complaint_id}#ACTIVE_FOLLOW_UP"},
            "SK": {"S": "LOCK"},
        }

    @staticmethod
    def _intent_key(complaint_id: str, intent_id: str) -> dict[str, dict[str, str]]:
        return {
            "PK": {"S": UploadIntent.intent_pk(complaint_id)},
            "SK": {"S": UploadIntent.intent_sk(intent_id)},
        }

    @staticmethod
    def _attempt_key(complaint_id: str, intent_id: str, lease_token: str) -> dict[str, dict[str, str]]:
        return {
            "PK": {"S": UploadAttempt.attempt_pk(complaint_id, intent_id)},
            "SK": {"S": UploadAttempt.attempt_sk(lease_token)},
        }

    @staticmethod
    def _outbox_key(intent_id: str) -> dict[str, dict[str, str]]:
        return {
            "PK": {"S": TagOutboxRecord.outbox_pk()},
            "SK": {"S": TagOutboxRecord.outbox_sk(intent_id)},
        }

    @staticmethod
    def _extract_error_code(err: ClientError) -> str:
        return str(err.response.get("Error", {}).get("Code", ""))

    @staticmethod
    def _extract_cancellation_reasons(err: ClientError) -> list[dict[str, Any]]:
        reasons = err.response.get("CancellationReasons")
        return list(reasons) if isinstance(reasons, list) else []

    @classmethod
    def _is_retryable_client_error(cls, err: ClientError) -> tuple[bool, bool]:
        """Return ``(should_retry, is_throttle_like)`` for a Botocore ClientError."""
        code = cls._extract_error_code(err)
        if code in _TRANSIENT_THROTTLE_CODES:
            return True, True
        if code in _TRANSIENT_CONFLICT_CODES:
            return True, True
        if code == "TransactionCanceledException":
            reasons = cls._extract_cancellation_reasons(err)
            non_none = [r.get("Code") for r in reasons if r.get("Code") not in (None, "None", "")]
            if non_none and all(c in _TRANSIENT_CONFLICT_CODES for c in non_none):
                return True, True
        return False, False

    def _call_with_retry(self, operation_name: str, fn: Callable[[], Any]) -> Any:
        """Execute a DynamoDB SDK call with bounded jittered exponential backoff on transient failures."""
        attempt = 0
        while True:
            try:
                return fn()
            except ClientError as err:
                retryable, is_throttle = self._is_retryable_client_error(err)
                if not retryable:
                    raise
                if attempt >= self._max_retries:
                    code = self._extract_error_code(err)
                    if is_throttle:
                        raise StorageThrottleError(
                            f"{operation_name}: DynamoDB transient failure '{code}' after {attempt + 1} attempts."
                        ) from err
                    raise
                upper = self._base_delay_seconds * (2 ** attempt)
                delay = self._jitter(0.0, upper) if upper > 0 else 0.0
                if delay > 0:
                    self._sleep(delay)
                attempt += 1

    def _get_item_consistent(self, key: dict[str, dict[str, str]], *, consistent: bool = True) -> dict[str, Any] | None:
        """Fetch a single item by primary key and return decoded dict or None."""
        resp = self._call_with_retry(
            "get_item",
            lambda: self._client.get_item(
                TableName=self._table_name,
                Key=key,
                ConsistentRead=consistent,
            ),
        )
        raw = resp.get("Item")
        return self._from_av(raw) if raw is not None else None

    def _batch_get_consistent(
        self,
        keys: Sequence[dict[str, dict[str, str]]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Fetch up to 100 items with ConsistentRead=True in one logical batch, retrying UnprocessedKeys."""
        pending = list(keys)
        results: dict[tuple[str, str], dict[str, Any]] = {}
        attempt = 0
        while pending:
            request_items = {
                self._table_name: {
                    "Keys": pending,
                    "ConsistentRead": True,
                }
            }
            resp = self._call_with_retry(
                "batch_get_item",
                lambda: self._client.batch_get_item(RequestItems=request_items),
            )
            items = resp.get("Responses", {}).get(self._table_name, [])
            for raw in items:
                decoded = self._from_av(raw)
                pk = str(decoded.get("PK", ""))
                sk = str(decoded.get("SK", ""))
                results[(pk, sk)] = decoded

            unprocessed = resp.get("UnprocessedKeys", {}).get(self._table_name, {}).get("Keys", [])
            if not unprocessed:
                break
            if attempt >= self._max_retries:
                raise StorageThrottleError(
                    "batch_get_item: DynamoDB UnprocessedKeys remained after bounded retries."
                )
            upper = self._base_delay_seconds * (2 ** attempt)
            delay = self._jitter(0.0, upper) if upper > 0 else 0.0
            if delay > 0:
                self._sleep(delay)
            pending = list(unprocessed)
            attempt += 1
        return results

    def _sign_cursor(self, payload: dict[str, Any]) -> str:
        """Serialize and HMAC-SHA256 sign a pagination cursor payload."""
        raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = _b64url_encode(raw_json)
        sig = hmac.new(self._cursor_secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        return f"{payload_b64}.{_b64url_encode(sig)}"

    def _verify_cursor(self, cursor: str, *, expected_scope: str) -> dict[str, Any]:
        """Verify HMAC signature and structure of a pagination cursor."""
        if not isinstance(cursor, str) or "." not in cursor:
            raise ValidationError(f"Invalid pagination cursor: {cursor!r}")
        payload_b64, sig_b64 = cursor.split(".", 1)
        if not payload_b64 or not sig_b64 or "." in sig_b64:
            raise ValidationError(f"Invalid pagination cursor: {cursor!r}")
        expected_sig = hmac.new(
            self._cursor_secret,
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
        try:
            supplied_sig = _b64url_decode(sig_b64)
            if not hmac.compare_digest(expected_sig, supplied_sig):
                raise ValidationError(f"Invalid pagination cursor signature: {cursor!r}")
            payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        except ValidationError:
            raise
        except Exception as err:
            raise ValidationError(f"Invalid pagination cursor: {cursor!r}") from err

        if not isinstance(payload, dict) or payload.get("scope") != expected_scope:
            raise ValidationError(f"Invalid pagination cursor scope for {expected_scope!r}.")
        return payload

    # -------------------------------------------------------------------------
    # Entity mappers (Domain <-> DynamoDB Item dict)
    # -------------------------------------------------------------------------

    @staticmethod
    def _complaint_gsi1_sk(complaint: Complaint) -> str:
        ts = format_canonical_utc_timestamp(complaint.submitted_at)
        return f"SUBMITTED_AT#{ts}#ID#{complaint.id}"

    @classmethod
    def _complaint_to_item(cls, complaint: Complaint, *, version_override: int | None = None) -> dict[str, Any]:
        """Convert a Complaint domain aggregate into a DynamoDB table item dict."""
        submitted_ts = format_canonical_utc_timestamp(complaint.submitted_at)
        gsi1_sk = f"SUBMITTED_AT#{submitted_ts}#ID#{complaint.id}"
        version = version_override if version_override is not None else complaint.version

        item: dict[str, Any] = {
            "PK": f"COMPLAINT#{complaint.id}",
            "SK": "META",
            "GSI1PK": f"STATUS#{complaint.status.value}",
            "GSI1SK": gsi1_sk,
            "entity_type": "COMPLAINT",
            "id": complaint.id,
            "title": complaint.title,
            "description": complaint.description,
            "building": complaint.building,
            "buildingLower": complaint.building.strip().lower(),
            "floor": complaint.floor,
            "roomNumber": complaint.room_number,
            "category": complaint.category.value,
            "status": complaint.status.value,
            "submittedAt": submitted_ts,
            "submittedBy": complaint.submitted_by,
            "overdueThresholdDays": int(complaint.overdue_threshold_days),
            "statusHistory": [
                {
                    "from": h.from_status.value,
                    "to": h.to_status.value,
                    "changedAt": format_canonical_utc_timestamp(h.changed_at),
                    "changedBy": h.changed_by,
                    **(
                        {"remarks": h.remarks}
                        if h.remarks is not None
                        else {}
                    ),
                }
                for h in complaint.status_history
            ],
            "isFollowUp": bool(complaint.is_follow_up),
            "version": int(version),
        }
        if complaint.resolved_at is not None:
            item["resolvedAt"] = format_canonical_utc_timestamp(complaint.resolved_at)
        if complaint.image_base64 is not None:
            item["imageBase64"] = complaint.image_base64
        if complaint.attachment_key is not None:
            item["attachmentKey"] = complaint.attachment_key
        if complaint.attachment_status is not None:
            item["attachmentStatus"] = complaint.attachment_status
        # Empty String Sets are invalid in DynamoDB; omit upvotes when empty.
        clean_upvotes = {u.strip() for u in complaint.upvotes if isinstance(u, str) and u.strip()}
        if clean_upvotes:
            item["upvotes"] = clean_upvotes
        if complaint.assigned_staff is not None:
            item["assignedStaff"] = complaint.assigned_staff
        if complaint.resolution_remarks is not None:
            item["resolutionRemarks"] = complaint.resolution_remarks
        if complaint.feedback is not None:
            fb: dict[str, Any] = {
                "rating": int(complaint.feedback.rating),
                "submittedAt": format_canonical_utc_timestamp(complaint.feedback.submitted_at),
            }
            if complaint.feedback.comment is not None:
                fb["comment"] = complaint.feedback.comment
            if complaint.feedback.updated_at is not None:
                fb["updatedAt"] = format_canonical_utc_timestamp(complaint.feedback.updated_at)
            item["feedback"] = fb
        if complaint.original_complaint_id is not None:
            item["originalComplaintId"] = complaint.original_complaint_id
            item["GSI2PK"] = f"ORIG#{complaint.original_complaint_id}"
            item["GSI2SK"] = gsi1_sk
        if complaint.follow_up_reason is not None:
            item["followUpReason"] = complaint.follow_up_reason
        return item

    @staticmethod
    def _complaint_from_item(item: dict[str, Any]) -> Complaint:
        """Convert a decoded DynamoDB item dict into a Complaint domain object."""
        raw_upvotes = item.get("upvotes")
        upvotes = sorted(str(u) for u in raw_upvotes) if isinstance(raw_upvotes, (set, list, tuple)) else []

        raw_history = item.get("statusHistory") or []
        history: list[StatusHistoryEntry] = [
            StatusHistoryEntry(
                from_status=ComplaintStatus(entry.get("from", entry.get("status"))),
                to_status=ComplaintStatus(entry.get("to", entry.get("status"))),
                changed_at=parse_utc_iso_datetime(entry["changedAt"], "changedAt"),
                changed_by=entry["changedBy"],
                remarks=entry.get("remarks"),
            )
            for entry in raw_history
        ]

        raw_feedback = item.get("feedback")
        feedback: ComplaintFeedback | None = None
        if isinstance(raw_feedback, dict):
            updated_fb_raw = raw_feedback.get("updatedAt")
            feedback = ComplaintFeedback(
                rating=int(raw_feedback["rating"]),
                comment=raw_feedback.get("comment"),
                submitted_at=parse_utc_iso_datetime(raw_feedback["submittedAt"], "feedback.submittedAt"),
                updated_at=(
                    parse_utc_iso_datetime(updated_fb_raw, "feedback.updatedAt")
                    if updated_fb_raw is not None
                    else None
                ),
            )

        resolved_raw = item.get("resolvedAt")
        resolved_at = parse_utc_iso_datetime(resolved_raw, "resolvedAt") if resolved_raw is not None else None

        return Complaint(
            id=str(item["id"]),
            title=str(item["title"]),
            description=str(item["description"]),
            building=str(item["building"]),
            floor=str(item["floor"]),
            room_number=str(item["roomNumber"]),
            category=IssueCategory(item["category"]),
            status=ComplaintStatus(item["status"]),
            submitted_at=parse_utc_iso_datetime(item["submittedAt"], "submittedAt"),
            submitted_by=str(item["submittedBy"]),
            overdue_threshold_days=int(item.get("overdueThresholdDays", 7)),
            resolved_at=resolved_at,
            image_base64=item.get("imageBase64"),
            attachment_key=item.get("attachmentKey"),
            attachment_status=item.get("attachmentStatus"),
            upvotes=upvotes,
            assigned_staff=item.get("assignedStaff"),
            resolution_remarks=item.get("resolutionRemarks"),
            status_history=history,
            feedback=feedback,
            is_follow_up=bool(item.get("isFollowUp", False)),
            original_complaint_id=item.get("originalComplaintId"),
            follow_up_reason=item.get("followUpReason"),
            version=int(item.get("version", 1)),
        )

    @staticmethod
    def _intent_to_item(intent: UploadIntent) -> dict[str, Any]:
        """Convert UploadIntent into DynamoDB item dict using canonical ``sha256`` attribute."""
        normalised_sha = normalize_sha256_hex(intent.expected_sha256, "expected_sha256")
        item: dict[str, Any] = {
            "PK": UploadIntent.intent_pk(intent.complaint_id),
            "SK": UploadIntent.intent_sk(intent.intent_id),
            "entity_type": "UPLOAD_INTENT",
            "complaint_id": intent.complaint_id,
            "intent_id": intent.intent_id,
            "owner_id": intent.owner_id,
            "status": intent.status.value,
            "staging_key": intent.staging_key,
            "final_key": intent.final_key,
            # Canonical attribute name 'sha256' per phase-3.3-step2-final-spec.md §7.
            "sha256": normalised_sha,
            "expected_mime": intent.expected_mime,
            "max_byte_size": int(intent.max_byte_size),
            "confirm_attempts": int(intent.confirm_attempts),
            "created_at": format_canonical_utc_timestamp(intent.created_at),
            "upload_expires_at": format_canonical_utc_timestamp(intent.upload_expires_at),
            "confirmation_deadline": format_canonical_utc_timestamp(intent.confirmation_deadline),
            "recovery_deadline": format_canonical_utc_timestamp(intent.recovery_deadline),
        }
        if intent.lease_token is not None:
            item["lease_token"] = intent.lease_token
        if intent.lease_expires_at is not None:
            item["lease_expires_at"] = format_canonical_utc_timestamp(intent.lease_expires_at)
        if intent.completed_at is not None:
            item["completed_at"] = format_canonical_utc_timestamp(intent.completed_at)
        if intent.updated_at is not None:
            item["updated_at"] = format_canonical_utc_timestamp(intent.updated_at)
        if intent.failure_reason is not None:
            item["failure_reason"] = intent.failure_reason
        if intent.staging_sha256 is not None:
            item["staging_sha256"] = normalize_sha256_hex(intent.staging_sha256, "staging_sha256")
        if intent.staging_size is not None:
            item["staging_size"] = int(intent.staging_size)
        if intent.source_version_id is not None:
            item["source_version_id"] = intent.source_version_id
        if intent.cleanup_ttl is not None:
            item["cleanup_ttl"] = int(intent.cleanup_ttl)
        return item

    @staticmethod
    def _intent_from_item(item: dict[str, Any]) -> UploadIntent:
        """Convert decoded DynamoDB item dict to UploadIntent."""
        sha_val = item.get("sha256")
        if sha_val is None:
            sha_val = item.get("expected_sha256")
        lease_exp_raw = item.get("lease_expires_at")
        completed_raw = item.get("completed_at")
        updated_raw = item.get("updated_at")
        return UploadIntent(
            intent_id=str(item["intent_id"]),
            complaint_id=str(item["complaint_id"]),
            owner_id=str(item["owner_id"]),
            status=UploadIntentStatus(item["status"]),
            staging_key=str(item["staging_key"]),
            final_key=str(item["final_key"]),
            expected_sha256=str(sha_val),
            expected_mime=str(item["expected_mime"]),
            max_byte_size=int(item["max_byte_size"]),
            created_at=parse_utc_iso_datetime(item["created_at"], "created_at"),
            upload_expires_at=parse_utc_iso_datetime(item["upload_expires_at"], "upload_expires_at"),
            confirmation_deadline=parse_utc_iso_datetime(
                item["confirmation_deadline"], "confirmation_deadline"
            ),
            recovery_deadline=parse_utc_iso_datetime(
                item["recovery_deadline"], "recovery_deadline"
            ),
            lease_token=item.get("lease_token"),
            lease_expires_at=(
                parse_utc_iso_datetime(lease_exp_raw, "lease_expires_at")
                if lease_exp_raw is not None
                else None
            ),
            confirm_attempts=int(item.get("confirm_attempts", 0)),
            completed_at=(
                parse_utc_iso_datetime(completed_raw, "completed_at")
                if completed_raw is not None
                else None
            ),
            failure_reason=item.get("failure_reason"),
            staging_sha256=item.get("staging_sha256"),
            staging_size=(
                int(item["staging_size"]) if item.get("staging_size") is not None else None
            ),
            source_version_id=item.get("source_version_id"),
            updated_at=(
                parse_utc_iso_datetime(updated_raw, "updated_at")
                if updated_raw is not None
                else None
            ),
            cleanup_ttl=(
                int(item["cleanup_ttl"]) if item.get("cleanup_ttl") is not None else None
            ),
        )

    @staticmethod
    def _attempt_to_item(attempt: UploadAttempt) -> dict[str, Any]:
        """Convert UploadAttempt into DynamoDB item dict (Spec §6.1, no expected_complaint_version)."""
        item: dict[str, Any] = {
            "PK": UploadAttempt.attempt_pk(attempt.complaint_id, attempt.intent_id),
            "SK": UploadAttempt.attempt_sk(attempt.lease_token),
            "entity_type": "UPLOAD_ATTEMPT",
            "complaint_id": attempt.complaint_id,
            "intent_id": attempt.intent_id,
            "lease_token": attempt.lease_token,
            "status": attempt.status.value,
            "source_version_id": attempt.source_version_id,
            "final_key": attempt.final_key,
            "sha256": normalize_sha256_hex(attempt.sha256, "sha256"),
            "content_type": attempt.content_type,
            "max_byte_size": int(attempt.max_byte_size),
            "created_at": format_canonical_utc_timestamp(attempt.created_at),
            "updated_at": format_canonical_utc_timestamp(attempt.updated_at),
        }
        if attempt.destination_version_id is not None:
            item["destination_version_id"] = attempt.destination_version_id
        return item

    @staticmethod
    def _attempt_from_item(item: dict[str, Any]) -> UploadAttempt:
        """Convert decoded DynamoDB item dict into UploadAttempt."""
        return UploadAttempt(
            complaint_id=str(item["complaint_id"]),
            intent_id=str(item["intent_id"]),
            lease_token=str(item["lease_token"]),
            status=UploadAttemptStatus(item["status"]),
            source_version_id=str(item["source_version_id"]),
            final_key=str(item["final_key"]),
            sha256=str(item["sha256"]),
            content_type=str(item["content_type"]),
            max_byte_size=int(item["max_byte_size"]),
            created_at=parse_utc_iso_datetime(item["created_at"], "created_at"),
            updated_at=parse_utc_iso_datetime(item["updated_at"], "updated_at"),
            destination_version_id=item.get("destination_version_id"),
        )

    @staticmethod
    def _outbox_to_item(record: TagOutboxRecord) -> dict[str, Any]:
        """Convert TagOutboxRecord into DynamoDB item dict (Spec §6.2)."""
        return {
            "PK": TagOutboxRecord.outbox_pk(),
            "SK": TagOutboxRecord.outbox_sk(record.intent_id),
            "entity_type": "TAG_OUTBOX",
            "intent_id": record.intent_id,
            "complaint_id": record.complaint_id,
            "s3_key": record.s3_key,
            "destination_version_id": record.destination_version_id,
            "committed_at": format_canonical_utc_timestamp(record.committed_at),
            "sweep_attempts": int(record.sweep_attempts),
        }

    @staticmethod
    def _outbox_from_item(item: dict[str, Any]) -> TagOutboxRecord:
        """Convert decoded DynamoDB item dict into TagOutboxRecord."""
        return TagOutboxRecord(
            intent_id=str(item["intent_id"]),
            complaint_id=str(item["complaint_id"]),
            s3_key=str(item["s3_key"]),
            destination_version_id=str(item["destination_version_id"]),
            committed_at=parse_utc_iso_datetime(item["committed_at"], "committed_at"),
            sweep_attempts=int(item.get("sweep_attempts", 0)),
        )

    # -------------------------------------------------------------------------
    # Shared immutable mismatch checks (parity with InMemoryComplaintRepository)
    # -------------------------------------------------------------------------

    @staticmethod
    def _collect_intent_immutable_mismatches(
        intent: UploadIntent,
        *,
        source_version_id: str,
        final_key: str,
        normalised_sha256: str,
        expected_mime: str,
        max_byte_size: int,
    ) -> list[str]:
        mismatches: list[str] = []
        if intent.source_version_id != source_version_id:
            mismatches.append(f"source_version_id ({intent.source_version_id!r} != {source_version_id!r})")
        if intent.final_key != final_key:
            mismatches.append(f"final_key ({intent.final_key!r} != {final_key!r})")
        try:
            stored_sha = normalize_sha256_hex(intent.expected_sha256, "expected_sha256")
        except ValidationError:
            stored_sha = intent.expected_sha256
        if stored_sha != normalised_sha256:
            mismatches.append(f"sha256 ({stored_sha!r} != {normalised_sha256!r})")
        if intent.expected_mime != expected_mime:
            mismatches.append(f"expected_mime ({intent.expected_mime!r} != {expected_mime!r})")
        if intent.max_byte_size != max_byte_size:
            mismatches.append(f"max_byte_size ({intent.max_byte_size!r} != {max_byte_size!r})")
        return mismatches

    @staticmethod
    def _collect_attempt_immutable_mismatches(
        attempt: UploadAttempt,
        *,
        lease_token: str,
        source_version_id: str,
        final_key: str,
        normalised_sha256: str,
        expected_mime: str,
        max_byte_size: int,
    ) -> list[str]:
        mismatches: list[str] = []
        if attempt.lease_token != lease_token:
            mismatches.append(f"lease_token ({attempt.lease_token!r} != {lease_token!r})")
        if attempt.source_version_id != source_version_id:
            mismatches.append(f"source_version_id ({attempt.source_version_id!r} != {source_version_id!r})")
        if attempt.final_key != final_key:
            mismatches.append(f"final_key ({attempt.final_key!r} != {final_key!r})")
        if attempt.sha256 != normalised_sha256:
            mismatches.append(f"sha256 ({attempt.sha256!r} != {normalised_sha256!r})")
        if attempt.content_type != expected_mime:
            mismatches.append(f"content_type ({attempt.content_type!r} != {expected_mime!r})")
        if attempt.max_byte_size != max_byte_size:
            mismatches.append(f"max_byte_size ({attempt.max_byte_size!r} != {max_byte_size!r})")
        return mismatches

    # -------------------------------------------------------------------------
    # ComplaintRepository implementation
    # -------------------------------------------------------------------------

    def get(self, complaint_id: str) -> Complaint | None:
        raw = self._get_item_consistent(self._complaint_key(complaint_id), consistent=False)
        return self._complaint_from_item(raw) if raw is not None else None

    def get_consistent(self, complaint_id: str) -> Complaint | None:
        raw = self._get_item_consistent(self._complaint_key(complaint_id), consistent=True)
        return self._complaint_from_item(raw) if raw is not None else None

    def create(self, complaint: Complaint) -> None:
        item = self._to_av(self._complaint_to_item(complaint))
        try:
            self._call_with_retry(
                "put_item",
                lambda: self._client.put_item(
                    TableName=self._table_name,
                    Item=item,
                    ConditionExpression="attribute_not_exists(PK)",
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) == "ConditionalCheckFailedException":
                raise ComplaintAlreadyExistsError(
                    f"Complaint with ID '{complaint.id}' already exists in storage."
                ) from err
            raise

    def seed(self, complaint: Complaint) -> None:
        item = self._to_av(self._complaint_to_item(complaint))
        try:
            self._call_with_retry(
                "put_item",
                lambda: self._client.put_item(
                    TableName=self._table_name,
                    Item=item,
                    ConditionExpression="attribute_not_exists(PK)",
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) == "ConditionalCheckFailedException":
                raise ComplaintAlreadyExistsError(
                    f"seed() rejected duplicate ID '{complaint.id}'. Each fixture ID must be unique."
                ) from err
            raise

    def save(self, complaint: Complaint) -> None:
        """Deprecated unrestricted save. Overwriting existing complaints is forbidden."""
        item = self._to_av(self._complaint_to_item(complaint))
        try:
            self._call_with_retry(
                "put_item",
                lambda: self._client.put_item(
                    TableName=self._table_name,
                    Item=item,
                    ConditionExpression="attribute_not_exists(PK)",
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) == "ConditionalCheckFailedException":
                raise ValidationError(
                    f"Unrestricted overwrite of existing complaint '{complaint.id}' via save() is forbidden. "
                    "Use update() with mandatory expected_version."
                ) from err
            raise

    def update(self, complaint: Complaint, expected_version: int) -> None:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError(
                f"expected_version must be a mandatory positive integer >= 1 (not boolean or None). "
                f"Received: {expected_version!r} (type: {type(expected_version).__name__})"
            )

        new_version = expected_version + 1
        item = self._to_av(self._complaint_to_item(complaint, version_override=new_version))

        # Check whether an active follow-up lock for this follow-up ID should be deleted atomically
        # when transitioning the follow-up to RESOLVED. Checking lock state first allows updates on
        # already-RESOLVED follow-ups (e.g., feedback submission) after the lock has been released.
        delete_lock_key: dict[str, dict[str, str]] | None = None
        if (
            complaint.is_follow_up
            and complaint.status == ComplaintStatus.RESOLVED
            and complaint.original_complaint_id
        ):
            lock_item = self._get_item_consistent(
                self._follow_up_lock_key(complaint.original_complaint_id),
                consistent=True,
            )
            if lock_item is not None and lock_item.get("active_follow_up_id") == complaint.id:
                delete_lock_key = self._follow_up_lock_key(complaint.original_complaint_id)

        if delete_lock_key is not None:
            try:
                self._call_with_retry(
                    "transact_write_items",
                    lambda: self._client.transact_write_items(
                        TransactItems=[
                            {
                                "Put": {
                                    "TableName": self._table_name,
                                    "Item": item,
                                    "ConditionExpression": "attribute_exists(PK) AND #ver = :expected_version",
                                    "ExpressionAttributeNames": {"#ver": "version"},
                                    "ExpressionAttributeValues": {":expected_version": {"N": str(expected_version)}},
                                }
                            },
                            {
                                "Delete": {
                                    "TableName": self._table_name,
                                    "Key": delete_lock_key,
                                    "ConditionExpression": "attribute_exists(PK) AND active_follow_up_id = :fid",
                                    "ExpressionAttributeValues": {":fid": {"S": complaint.id}},
                                }
                            },
                        ]
                    ),
                )
                complaint.version = new_version
                return
            except ClientError as err:
                if self._extract_error_code(err) != "TransactionCanceledException":
                    raise
                reasons = self._extract_cancellation_reasons(err)
                stored = self.get_consistent(complaint.id)
                if stored is None:
                    raise ComplaintNotFoundError(
                        f"Complaint with ID '{complaint.id}' was not found for update."
                    ) from err
                if stored.version != expected_version:
                    raise OptimisticLockError(
                        f"Version conflict updating complaint '{complaint.id}': "
                        f"expected version {expected_version}, but found {stored.version}."
                    ) from err
                # Reason 1 failed because lock disappeared concurrently without another complaint write;
                # fall through to the plain conditional PutItem if complaint version still matches.
                if not reasons or reasons[0].get("Code") not in (None, "None", ""):
                    raise OptimisticLockError(
                        f"Version conflict updating complaint '{complaint.id}': transaction cancelled ({reasons!r})."
                    ) from err

        try:
            self._call_with_retry(
                "put_item",
                lambda: self._client.put_item(
                    TableName=self._table_name,
                    Item=item,
                    ConditionExpression="attribute_exists(PK) AND #ver = :expected_version",
                    ExpressionAttributeNames={"#ver": "version"},
                    ExpressionAttributeValues={":expected_version": {"N": str(expected_version)}},
                ),
            )
            complaint.version = new_version
        except ClientError as err:
            if self._extract_error_code(err) != "ConditionalCheckFailedException":
                raise
            stored = self.get_consistent(complaint.id)
            if stored is None:
                raise ComplaintNotFoundError(
                    f"Complaint with ID '{complaint.id}' was not found for update."
                ) from err
            raise OptimisticLockError(
                f"Version conflict updating complaint '{complaint.id}': "
                f"expected version {expected_version}, but found {stored.version}."
            ) from err

    def create_follow_up_atomic(
        self,
        original_complaint_id: str,
        follow_up: Complaint,
    ) -> Complaint:
        if follow_up.id == original_complaint_id:
            raise ValidationError(
                f"Follow-up child ID '{follow_up.id}' cannot be identical to parent ID."
            )
        if not isinstance(follow_up.is_follow_up, bool) or not follow_up.is_follow_up:
            raise ValidationError("Follow-up complaint must have is_follow_up=True.")
        if follow_up.original_complaint_id != original_complaint_id:
            raise ValidationError(
                f"Follow-up original_complaint_id ('{follow_up.original_complaint_id}') "
                f"must match parent ID ('{original_complaint_id}')."
            )
        if follow_up.status != ComplaintStatus.PENDING:
            raise ValidationError(
                f"Follow-up complaint must start in 'pending' status, got '{follow_up.status.value}'."
            )
        if (
            not follow_up.follow_up_reason
            or not isinstance(follow_up.follow_up_reason, str)
            or len(follow_up.follow_up_reason.strip()) < 10
        ):
            raise InvalidFollowUpReasonError(
                "Follow-up complaint must have a valid follow-up reason of at least 10 characters."
            )
        if follow_up.upvotes and len(follow_up.upvotes) > 0:
            raise ValidationError("Follow-up complaint must start with zero upvotes.")
        if follow_up.feedback is not None:
            raise ValidationError("Follow-up complaint cannot inherit or contain feedback upon creation.")

        child_item = self._to_av(self._complaint_to_item(follow_up))
        lock_item = self._to_av({
            "PK": f"COMPLAINT#{original_complaint_id}#ACTIVE_FOLLOW_UP",
            "SK": "LOCK",
            "entity_type": "FOLLOW_UP_LOCK",
            "original_complaint_id": original_complaint_id,
            "active_follow_up_id": follow_up.id,
            "created_at": format_canonical_utc_timestamp(follow_up.submitted_at),
        })

        try:
            self._call_with_retry(
                "transact_write_items",
                lambda: self._client.transact_write_items(
                    TransactItems=[
                        {
                            "ConditionCheck": {
                                "TableName": self._table_name,
                                "Key": self._complaint_key(original_complaint_id),
                                "ConditionExpression": (
                                    "attribute_exists(PK) AND #status = :resolved AND submittedBy = :submitter"
                                ),
                                "ExpressionAttributeNames": {"#status": "status"},
                                "ExpressionAttributeValues": {
                                    ":resolved": {"S": ComplaintStatus.RESOLVED.value},
                                    ":submitter": {"S": follow_up.submitted_by},
                                },
                            }
                        },
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": child_item,
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": lock_item,
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                    ]
                ),
            )
            created = self.get_consistent(follow_up.id)
            return created if created is not None else self._complaint_from_item(self._from_av(child_item))
        except ClientError as err:
            if self._extract_error_code(err) != "TransactionCanceledException":
                raise
            reasons = self._extract_cancellation_reasons(err)
            fetched = self._batch_get_consistent([
                self._complaint_key(original_complaint_id),
                self._complaint_key(follow_up.id),
                self._follow_up_lock_key(original_complaint_id),
            ])
            parent_raw = fetched.get((f"COMPLAINT#{original_complaint_id}", "META"))
            if parent_raw is None:
                raise ComplaintNotFoundError(
                    f"Original complaint '{original_complaint_id}' was not found."
                ) from err
            parent = self._complaint_from_item(parent_raw)
            if parent.status != ComplaintStatus.RESOLVED:
                raise FollowUpEligibilityError(
                    f"Cannot create a follow-up for complaint '{original_complaint_id}' because "
                    f"its status is '{parent.status.value}'. Follow-ups are permitted only for Resolved complaints."
                ) from err
            if parent.submitted_by != follow_up.submitted_by:
                raise UnauthorizedError(
                    f"Follow-up submitter '{follow_up.submitted_by}' does not match "
                    f"original complaint owner '{parent.submitted_by}'."
                ) from err

            child_raw = fetched.get((f"COMPLAINT#{follow_up.id}", "META"))
            if child_raw is not None:
                raise ComplaintAlreadyExistsError(
                    f"Follow-up complaint with ID '{follow_up.id}' already exists."
                ) from err

            lock_raw = fetched.get((f"COMPLAINT#{original_complaint_id}#ACTIVE_FOLLOW_UP", "LOCK"))
            if lock_raw is not None or (len(reasons) >= 3 and reasons[2].get("Code") == "ConditionalCheckFailed"):
                raise ActiveFollowUpExistsError(
                    f"An active (unresolved) follow-up already exists for complaint '{original_complaint_id}'."
                ) from err

            raise ActiveFollowUpExistsError(
                f"Follow-up creation cancelled for complaint '{original_complaint_id}' ({reasons!r})."
            ) from err

    def toggle_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        clean_id = _require_non_empty_str(student_id, "student_id", "toggle_upvote_atomic")
        for _attempt in range(self._max_retries + 3):
            current = self.get_consistent(complaint_id)
            if current is None:
                raise ComplaintNotFoundError(
                    f"Complaint '{complaint_id}' was not found for upvote."
                )
            currently_present = clean_id in current.upvotes
            try:
                if currently_present:
                    resp = self._call_with_retry(
                        "update_item",
                        lambda: self._client.update_item(
                            TableName=self._table_name,
                            Key=self._complaint_key(complaint_id),
                            UpdateExpression="DELETE upvotes :sid_set ADD #ver :one",
                            ConditionExpression="attribute_exists(PK) AND contains(upvotes, :sid)",
                            ExpressionAttributeNames={"#ver": "version"},
                            ExpressionAttributeValues={
                                ":sid_set": {"SS": [clean_id]},
                                ":sid": {"S": clean_id},
                                ":one": {"N": "1"},
                            },
                            ReturnValues="ALL_NEW",
                        ),
                    )
                    return self._complaint_from_item(self._from_av(resp["Attributes"])), False

                resp = self._call_with_retry(
                    "update_item",
                    lambda: self._client.update_item(
                        TableName=self._table_name,
                        Key=self._complaint_key(complaint_id),
                        UpdateExpression="ADD upvotes :sid_set, #ver :one",
                        ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(upvotes) OR NOT contains(upvotes, :sid))",
                        ExpressionAttributeNames={"#ver": "version"},
                        ExpressionAttributeValues={
                            ":sid_set": {"SS": [clean_id]},
                            ":sid": {"S": clean_id},
                            ":one": {"N": "1"},
                        },
                        ReturnValues="ALL_NEW",
                    ),
                )
                return self._complaint_from_item(self._from_av(resp["Attributes"])), True
            except ClientError as err:
                if self._extract_error_code(err) == "ConditionalCheckFailedException":
                    continue
                raise
        raise StorageThrottleError(
            f"toggle_upvote_atomic: concurrent contention toggling upvote on '{complaint_id}'."
        )

    def add_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        clean_id = _require_non_empty_str(student_id, "student_id", "add_upvote_atomic")
        for _attempt in range(self._max_retries + 3):
            try:
                resp = self._call_with_retry(
                    "update_item",
                    lambda: self._client.update_item(
                        TableName=self._table_name,
                        Key=self._complaint_key(complaint_id),
                        UpdateExpression="ADD upvotes :sid_set, #ver :one",
                        ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(upvotes) OR NOT contains(upvotes, :sid))",
                        ExpressionAttributeNames={"#ver": "version"},
                        ExpressionAttributeValues={
                            ":sid_set": {"SS": [clean_id]},
                            ":sid": {"S": clean_id},
                            ":one": {"N": "1"},
                        },
                        ReturnValues="ALL_NEW",
                    ),
                )
                return self._complaint_from_item(self._from_av(resp["Attributes"])), True
            except ClientError as err:
                if self._extract_error_code(err) != "ConditionalCheckFailedException":
                    raise
                current = self.get_consistent(complaint_id)
                if current is None:
                    raise ComplaintNotFoundError(
                        f"Complaint '{complaint_id}' was not found for upvote."
                    ) from err
                if clean_id in current.upvotes:
                    return current, False
        raise StorageThrottleError(
            f"add_upvote_atomic: concurrent contention adding upvote on '{complaint_id}'."
        )

    def remove_upvote_atomic(
        self,
        complaint_id: str,
        student_id: str,
    ) -> tuple[Complaint, bool]:
        clean_id = _require_non_empty_str(student_id, "student_id", "remove_upvote_atomic")
        for _attempt in range(self._max_retries + 3):
            try:
                resp = self._call_with_retry(
                    "update_item",
                    lambda: self._client.update_item(
                        TableName=self._table_name,
                        Key=self._complaint_key(complaint_id),
                        UpdateExpression="DELETE upvotes :sid_set ADD #ver :one",
                        ConditionExpression="attribute_exists(PK) AND attribute_exists(upvotes) AND contains(upvotes, :sid)",
                        ExpressionAttributeNames={"#ver": "version"},
                        ExpressionAttributeValues={
                            ":sid_set": {"SS": [clean_id]},
                            ":sid": {"S": clean_id},
                            ":one": {"N": "1"},
                        },
                        ReturnValues="ALL_NEW",
                    ),
                )
                return self._complaint_from_item(self._from_av(resp["Attributes"])), True
            except ClientError as err:
                if self._extract_error_code(err) != "ConditionalCheckFailedException":
                    raise
                current = self.get_consistent(complaint_id)
                if current is None:
                    raise ComplaintNotFoundError(
                        f"Complaint '{complaint_id}' was not found for upvote."
                    ) from err
                if clean_id not in current.upvotes:
                    return current, False
        raise StorageThrottleError(
            f"remove_upvote_atomic: concurrent contention removing upvote on '{complaint_id}'."
        )

    @staticmethod
    def _matches_complaint_filters(
        complaint: Complaint,
        building_lower: str | None,
        category: IssueCategory | None,
    ) -> bool:
        if building_lower is not None and complaint.building.strip().lower() != building_lower:
            return False
        if category is not None and complaint.category != category:
            return False
        return True

    def _query_gsi1_batch(
        self,
        status: ComplaintStatus,
        *,
        limit: int,
        exclusive_start_key: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list[Complaint], dict[str, dict[str, Any]] | None]:
        """Execute a single descending Query on GSI1 for one status partition."""
        kwargs: dict[str, Any] = {
            "TableName": self._table_name,
            "IndexName": "GSI1",
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"STATUS#{status.value}"}},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if exclusive_start_key is not None:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = self._call_with_retry("query", lambda: self._client.query(**kwargs))
        items = [self._complaint_from_item(self._from_av(raw)) for raw in resp.get("Items", [])]
        return items, resp.get("LastEvaluatedKey")

    def list_all(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
    ) -> list[Complaint]:
        building_lower = building.strip().lower() if isinstance(building, str) and building.strip() else None
        statuses = [status] if status is not None else [
            ComplaintStatus.PENDING,
            ComplaintStatus.IN_PROGRESS,
            ComplaintStatus.RESOLVED,
        ]
        results: list[Complaint] = []
        for s in statuses:
            lek: dict[str, dict[str, Any]] | None = None
            while True:
                batch, lek = self._query_gsi1_batch(s, limit=100, exclusive_start_key=lek)
                for c in batch:
                    if self._matches_complaint_filters(c, building_lower, category):
                        results.append(c)
                if lek is None:
                    break
        results.sort(key=lambda c: (c.submitted_at, c.id), reverse=True)
        return results

    def list_paginated(
        self,
        status: ComplaintStatus | None = None,
        building: str | None = None,
        category: IssueCategory | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Complaint], str | None]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 100:
            raise ValidationError(f"Pagination limit must be between 1 and 100, got {limit}.")

        building_lower = building.strip().lower() if isinstance(building, str) and building.strip() else None
        filter_binding = {
            "status": status.value if status is not None else None,
            "building": building_lower,
            "category": category.value if category is not None else None,
        }

        target_statuses = (
            [status]
            if status is not None
            else [ComplaintStatus.PENDING, ComplaintStatus.IN_PROGRESS, ComplaintStatus.RESOLVED]
        )

        # Safe keyset watermarks per stream: raw key-value dict representing the last
        # safely skipped/emitted item's GSI1 key so unemitted buffered items are never skipped.
        watermarks: dict[str, dict[str, str] | None] = {s.value: None for s in target_statuses}
        exhausted: dict[str, bool] = {s.value: False for s in target_statuses}

        if cursor is not None:
            payload = self._verify_cursor(cursor, expected_scope="complaints")
            if payload.get("filters") != filter_binding:
                raise ValidationError("Pagination cursor does not match the active filter parameters.")
            saved_watermarks = payload.get("watermarks")
            saved_exhausted = payload.get("exhausted")
            if not isinstance(saved_watermarks, dict) or not isinstance(saved_exhausted, dict):
                raise ValidationError("Invalid complaint pagination cursor state.")
            for s in target_statuses:
                wm = saved_watermarks.get(s.value)
                if wm is not None:
                    if not isinstance(wm, dict) or set(wm.keys()) != {"PK", "SK", "GSI1PK", "GSI1SK"}:
                        raise ValidationError("Invalid complaint pagination watermark.")
                    watermarks[s.value] = {k: str(v) for k, v in wm.items()}
                exhausted[s.value] = bool(saved_exhausted.get(s.value, False))

        # Per-stream matching item buffers plus raw query start pointer beyond current watermark.
        buffers: dict[str, list[Complaint]] = {s.value: [] for s in target_statuses}
        scan_cursor: dict[str, dict[str, dict[str, Any]] | None] = {
            s.value: (
                {k: {"S": v} for k, v in watermarks[s.value].items()}
                if watermarks[s.value] is not None
                else None
            )
            for s in target_statuses
        }
        has_filters = building_lower is not None or category is not None
        fetch_size = 100 if has_filters else min(limit + 1, 100)

        def _gsi1_item_key(c: Complaint) -> dict[str, str]:
            return {
                "PK": f"COMPLAINT#{c.id}",
                "SK": "META",
                "GSI1PK": f"STATUS#{c.status.value}",
                "GSI1SK": self._complaint_gsi1_sk(c),
            }

        def _ensure_head(s: ComplaintStatus) -> None:
            key = s.value
            while not buffers[key] and not exhausted[key]:
                batch, lek = self._query_gsi1_batch(
                    s,
                    limit=fetch_size,
                    exclusive_start_key=scan_cursor[key],
                )
                scan_cursor[key] = lek
                for c in batch:
                    if self._matches_complaint_filters(c, building_lower, category):
                        buffers[key].append(c)
                    elif not buffers[key]:
                        # All items preceding the first buffered match failed filters, so watermark can safely advance.
                        watermarks[key] = _gsi1_item_key(c)
                if lek is None:
                    exhausted[key] = True

        emitted: list[Complaint] = []
        while len(emitted) < limit:
            for s in target_statuses:
                _ensure_head(s)
            candidates = [(s, buffers[s.value][0]) for s in target_statuses if buffers[s.value]]
            if not candidates:
                break
            best_status, best_item = max(
                candidates,
                key=lambda pair: (pair[1].submitted_at, pair[1].id),
            )
            buffers[best_status.value].pop(0)
            emitted.append(best_item)
            watermarks[best_status.value] = _gsi1_item_key(best_item)

        # Look ahead by 1 item across streams so next_cursor is None when exact boundary is exhausted.
        for s in target_statuses:
            _ensure_head(s)
        has_more = any(bool(buffers[s.value]) for s in target_statuses)

        next_cursor: str | None = None
        if has_more and emitted:
            cursor_exhausted = {
                s.value: (exhausted[s.value] and not buffers[s.value])
                for s in target_statuses
            }
            next_cursor = self._sign_cursor({
                "v": 1,
                "scope": "complaints",
                "filters": filter_binding,
                "watermarks": watermarks,
                "exhausted": cursor_exhausted,
            })

        return emitted, next_cursor

    def find_by_original_id(self, original_complaint_id: str) -> list[Complaint]:
        results: list[Complaint] = []
        lek: dict[str, dict[str, Any]] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "IndexName": "GSI2",
                "KeyConditionExpression": "GSI2PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": f"ORIG#{original_complaint_id}"}},
                "ScanIndexForward": True,
            }
            if lek is not None:
                kwargs["ExclusiveStartKey"] = lek
            resp = self._call_with_retry("query", lambda: self._client.query(**kwargs))
            for raw in resp.get("Items", []):
                results.append(self._complaint_from_item(self._from_av(raw)))
            lek = resp.get("LastEvaluatedKey")
            if lek is None:
                break
        results.sort(key=lambda c: c.submitted_at)
        return results

    def has_active_follow_up(self, original_complaint_id: str) -> bool:
        lock_item = self._get_item_consistent(
            self._follow_up_lock_key(original_complaint_id),
            consistent=True,
        )
        if lock_item is not None:
            return True
        return any(c.status != ComplaintStatus.RESOLVED for c in self.find_by_original_id(original_complaint_id))

    def count(self) -> int:
        total = 0
        lek: dict[str, dict[str, Any]] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "Select": "COUNT",
                "FilterExpression": "entity_type = :ct",
                "ExpressionAttributeValues": {":ct": {"S": "COMPLAINT"}},
            }
            if lek is not None:
                kwargs["ExclusiveStartKey"] = lek
            resp = self._call_with_retry("scan", lambda: self._client.scan(**kwargs))
            total += int(resp.get("Count", 0))
            lek = resp.get("LastEvaluatedKey")
            if lek is None:
                break
        return total

    # -------------------------------------------------------------------------
    # UploadIntentRepository: base lifecycle & strongly consistent reads
    # -------------------------------------------------------------------------

    def create_intent(self, intent: UploadIntent) -> None:
        item = self._to_av(self._intent_to_item(intent))
        try:
            self._call_with_retry(
                "transact_write_items",
                lambda: self._client.transact_write_items(
                    TransactItems=[
                        {
                            "ConditionCheck": {
                                "TableName": self._table_name,
                                "Key": self._complaint_key(intent.complaint_id),
                                "ConditionExpression": "attribute_exists(PK)",
                            }
                        },
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": item,
                            }
                        },
                    ]
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) == "TransactionCanceledException":
                reasons = self._extract_cancellation_reasons(err)
                if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                    raise ComplaintNotFoundError(
                        f"Complaint '{intent.complaint_id}' was not found for upload intent."
                    ) from err
            raise

    def get_intent(self, complaint_id: str, intent_id: str) -> UploadIntent | None:
        raw = self._get_item_consistent(self._intent_key(complaint_id, intent_id), consistent=False)
        return self._intent_from_item(raw) if raw is not None else None

    def get_intent_consistent(self, complaint_id: str, intent_id: str) -> UploadIntent | None:
        raw = self._get_item_consistent(self._intent_key(complaint_id, intent_id), consistent=True)
        return self._intent_from_item(raw) if raw is not None else None

    def _expire_intent_if_needed(
        self,
        complaint_id: str,
        intent_id: str,
        *,
        expected_status: UploadIntentStatus,
        reason: str,
        now_iso: str,
    ) -> None:
        """Conditionally mark an expired PENDING_UPLOAD or CONFIRMING intent as FAILED."""
        try:
            self._call_with_retry(
                "update_item",
                lambda: self._client.update_item(
                    TableName=self._table_name,
                    Key=self._intent_key(complaint_id, intent_id),
                    UpdateExpression=(
                        "SET #status = :failed, failure_reason = :reason, "
                        "completed_at = :now_iso, updated_at = :now_iso"
                    ),
                    ConditionExpression="attribute_exists(PK) AND #status = :expected_status",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":failed": {"S": UploadIntentStatus.FAILED.value},
                        ":reason": {"S": reason},
                        ":now_iso": {"S": now_iso},
                        ":expected_status": {"S": expected_status.value},
                    },
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) != "ConditionalCheckFailedException":
                raise

    def claim_intent_lease(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        lease_duration_seconds: int,
        now: datetime,
    ) -> UploadIntent:
        parsed_now = parse_utc_iso_datetime(now, "now")
        now_iso = format_canonical_utc_timestamp(parsed_now)
        new_expires_iso = format_canonical_utc_timestamp(
            parsed_now + timedelta(seconds=lease_duration_seconds)
        )

        # Preserve legacy takeover equality boundary (Decision 2):
        # another worker can take over only when lease_expires_at < now
        # (now <= lease_expires_at keeps existing holder active).
        for _ in range(self._max_retries + 3):
            try:
                resp = self._call_with_retry(
                    "update_item",
                    lambda: self._client.update_item(
                        TableName=self._table_name,
                        Key=self._intent_key(complaint_id, intent_id),
                        UpdateExpression=(
                            "SET #status = :confirming, lease_token = :lease_token, "
                            "lease_expires_at = :new_expires, updated_at = :now_iso"
                        ),
                        ConditionExpression=(
                            "attribute_exists(PK) AND ("
                            "(#status = :pending AND :now_iso <= confirmation_deadline) OR "
                            "(#status = :confirming AND :now_iso <= recovery_deadline AND ("
                            "lease_token = :lease_token OR attribute_not_exists(lease_expires_at) "
                            "OR lease_expires_at < :now_iso))"
                            ")"
                        ),
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={
                            ":confirming": {"S": UploadIntentStatus.CONFIRMING.value},
                            ":pending": {"S": UploadIntentStatus.PENDING_UPLOAD.value},
                            ":lease_token": {"S": lease_token},
                            ":new_expires": {"S": new_expires_iso},
                            ":now_iso": {"S": now_iso},
                        },
                        ReturnValues="ALL_NEW",
                    ),
                )
                return self._intent_from_item(self._from_av(resp["Attributes"]))
            except ClientError as err:
                if self._extract_error_code(err) != "ConditionalCheckFailedException":
                    raise
                intent = self.get_intent_consistent(complaint_id, intent_id)
                if intent is None:
                    raise UploadIntentNotFoundError(
                        f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                    ) from err
                if intent.status == UploadIntentStatus.VERIFIED:
                    return intent
                if intent.status == UploadIntentStatus.FAILED:
                    raise UploadFailedError(
                        f"Upload intent '{intent_id}' has already failed: "
                        f"{intent.failure_reason or 'unknown reason'}."
                    ) from err
                if intent.status == UploadIntentStatus.PENDING_UPLOAD:
                    if parsed_now > intent.confirmation_deadline:
                        self._expire_intent_if_needed(
                            complaint_id,
                            intent_id,
                            expected_status=UploadIntentStatus.PENDING_UPLOAD,
                            reason="Confirmation deadline expired",
                            now_iso=now_iso,
                        )
                        raise UploadIntentExpiredError(
                            f"Upload intent '{intent_id}' confirmation deadline expired at "
                            f"{intent.confirmation_deadline.isoformat()}."
                        ) from err
                    # Racing worker just transitioned PENDING_UPLOAD -> CONFIRMING; retry loop evaluates cleanly.
                    continue
                if intent.status == UploadIntentStatus.CONFIRMING:
                    if parsed_now > intent.recovery_deadline:
                        self._expire_intent_if_needed(
                            complaint_id,
                            intent_id,
                            expected_status=UploadIntentStatus.CONFIRMING,
                            reason="Recovery deadline expired",
                            now_iso=now_iso,
                        )
                        raise UploadIntentExpiredError(
                            f"Upload intent '{intent_id}' recovery deadline expired at "
                            f"{intent.recovery_deadline.isoformat()}."
                        ) from err
                    if (
                        intent.lease_expires_at is not None
                        and parsed_now <= intent.lease_expires_at
                        and intent.lease_token != lease_token
                    ):
                        raise StaleLeaseError(
                            f"Another worker holds an active lease on intent '{intent_id}' until "
                            f"{intent.lease_expires_at.isoformat()}."
                        ) from err
                    continue
        raise StorageThrottleError(
            f"claim_intent_lease: concurrent contention claiming lease on intent '{intent_id}'."
        )

    def commit_verified_attachment_atomic(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        final_key: str,
        staging_sha256: str,
        staging_size: int,
        now: datetime,
    ) -> Complaint:
        """Legacy atomic attachment commit (complaint + intent without attempt/outbox).

        Preserved untouched in contract for legacy ``uploads.py`` callers.
        """
        parsed_now = parse_utc_iso_datetime(now, "now")
        now_iso = format_canonical_utc_timestamp(parsed_now)
        try:
            self._call_with_retry(
                "transact_write_items",
                lambda: self._client.transact_write_items(
                    TransactItems=[
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": self._complaint_key(complaint_id),
                                "UpdateExpression": (
                                    "SET attachmentKey = :final_key, attachmentStatus = :verified ADD #ver :one"
                                ),
                                "ConditionExpression": "attribute_exists(PK)",
                                "ExpressionAttributeNames": {"#ver": "version"},
                                "ExpressionAttributeValues": {
                                    ":final_key": {"S": final_key},
                                    ":verified": {"S": "VERIFIED"},
                                    ":one": {"N": "1"},
                                },
                            }
                        },
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": self._intent_key(complaint_id, intent_id),
                                "UpdateExpression": (
                                    "SET #status = :verified, completed_at = :now_iso, "
                                    "staging_sha256 = :staging_sha256, staging_size = :staging_size, "
                                    "updated_at = :now_iso"
                                ),
                                "ConditionExpression": (
                                    "attribute_exists(PK) AND lease_token = :lease_token AND #status = :confirming"
                                ),
                                "ExpressionAttributeNames": {"#status": "status"},
                                "ExpressionAttributeValues": {
                                    ":verified": {"S": UploadIntentStatus.VERIFIED.value},
                                    ":now_iso": {"S": now_iso},
                                    ":staging_sha256": {"S": staging_sha256},
                                    ":staging_size": {"N": str(int(staging_size))},
                                    ":lease_token": {"S": lease_token},
                                    ":confirming": {"S": UploadIntentStatus.CONFIRMING.value},
                                },
                            }
                        },
                    ]
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) != "TransactionCanceledException":
                raise
            fetched = self._batch_get_consistent([
                self._complaint_key(complaint_id),
                self._intent_key(complaint_id, intent_id),
            ])
            intent_raw = fetched.get((UploadIntent.intent_pk(complaint_id), UploadIntent.intent_sk(intent_id)))
            if intent_raw is None:
                raise UploadIntentNotFoundError(
                    f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                ) from err
            intent = self._intent_from_item(intent_raw)
            if intent.lease_token != lease_token:
                raise StaleLeaseError(
                    f"Worker lease token mismatch: expected '{intent.lease_token}', "
                    f"got '{lease_token}'. Worker has been fenced out."
                ) from err
            complaint_raw = fetched.get((f"COMPLAINT#{complaint_id}", "META"))
            if intent.status == UploadIntentStatus.VERIFIED:
                if complaint_raw is None:
                    raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.") from err
                return self._complaint_from_item(complaint_raw)
            if intent.status != UploadIntentStatus.CONFIRMING:
                raise UploadFailedError(
                    f"Cannot commit upload intent '{intent_id}' in status '{intent.status.value}'."
                ) from err
            if complaint_raw is None:
                raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.") from err
            raise

        committed = self.get_consistent(complaint_id)
        if committed is None:
            raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")
        return committed

    def mark_intent_failed(
        self,
        complaint_id: str,
        intent_id: str,
        reason: str,
        now: datetime,
        lease_token: str | None = None,
    ) -> None:
        parsed_now = parse_utc_iso_datetime(now, "now")
        now_iso = format_canonical_utc_timestamp(parsed_now)
        try:
            self._call_with_retry(
                "update_item",
                lambda: self._client.update_item(
                    TableName=self._table_name,
                    Key=self._intent_key(complaint_id, intent_id),
                    UpdateExpression=(
                        "SET #status = :failed, failure_reason = :reason, "
                        "completed_at = :now_iso, updated_at = :now_iso"
                    ),
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":failed": {"S": UploadIntentStatus.FAILED.value},
                        ":reason": {"S": reason},
                        ":now_iso": {"S": now_iso},
                    },
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) == "ConditionalCheckFailedException":
                return
            raise

    def record_missing_staging_attempt(
        self,
        complaint_id: str,
        intent_id: str,
        max_attempts: int,
        now: datetime,
    ) -> int:
        parsed_now = parse_utc_iso_datetime(now, "now")
        now_iso = format_canonical_utc_timestamp(parsed_now)
        try:
            resp = self._call_with_retry(
                "update_item",
                lambda: self._client.update_item(
                    TableName=self._table_name,
                    Key=self._intent_key(complaint_id, intent_id),
                    UpdateExpression=(
                        "ADD confirm_attempts :one "
                        "SET updated_at = :now_iso "
                        "REMOVE lease_token, lease_expires_at"
                    ),
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeValues={
                        ":one": {"N": "1"},
                        ":now_iso": {"S": now_iso},
                    },
                    ReturnValues="ALL_NEW",
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) == "ConditionalCheckFailedException":
                raise UploadIntentNotFoundError(f"Upload intent '{intent_id}' was not found.") from err
            raise

        updated = self._from_av(resp["Attributes"])
        attempts = int(updated["confirm_attempts"])
        next_status = (
            UploadIntentStatus.FAILED.value
            if attempts >= max_attempts
            else UploadIntentStatus.PENDING_UPLOAD.value
        )
        expr_values: dict[str, dict[str, str]] = {":next_status": {"S": next_status}}
        update_expr = "SET #status = :next_status"
        if attempts >= max_attempts:
            update_expr += ", failure_reason = :reason, completed_at = :now_iso"
            expr_values[":reason"] = {"S": "Exceeded maximum confirmation retry attempts without staging object present"}
            expr_values[":now_iso"] = {"S": now_iso}
        try:
            self._call_with_retry(
                "update_item",
                lambda: self._client.update_item(
                    TableName=self._table_name,
                    Key=self._intent_key(complaint_id, intent_id),
                    UpdateExpression=update_expr,
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues=expr_values,
                ),
            )
        except ClientError as err:
            if self._extract_error_code(err) == "ConditionalCheckFailedException":
                raise UploadIntentNotFoundError(f"Upload intent '{intent_id}' was not found.") from err
            raise
        return attempts

    # -------------------------------------------------------------------------
    # Stage B2: Version pinning & atomic 2-item CopyAttempt initialization
    # -------------------------------------------------------------------------

    def pin_source_version(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        now: datetime,
    ) -> UploadIntent:
        cleaned_vid = _validate_version_id(source_version_id, "source_version_id", "pin_source_version")
        cleaned_lease = _require_non_empty_str(lease_token, "lease_token", "pin_source_version")
        parsed_now = parse_utc_iso_datetime(now, "now")
        now_iso = format_canonical_utc_timestamp(parsed_now)

        # Decision 2: new pin_source_version rejects at now >= lease_expires_at (:now_iso < lease_expires_at fails).
        try:
            resp = self._call_with_retry(
                "update_item",
                lambda: self._client.update_item(
                    TableName=self._table_name,
                    Key=self._intent_key(complaint_id, intent_id),
                    UpdateExpression="SET source_version_id = :src_vid, updated_at = :now_iso",
                    ConditionExpression=(
                        "attribute_exists(PK) AND #status = :confirming "
                        "AND lease_token = :lease_token "
                        "AND :now_iso < lease_expires_at "
                        "AND (attribute_not_exists(source_version_id) OR source_version_id = :src_vid)"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":confirming": {"S": UploadIntentStatus.CONFIRMING.value},
                        ":lease_token": {"S": cleaned_lease},
                        ":now_iso": {"S": now_iso},
                        ":src_vid": {"S": cleaned_vid},
                    },
                    ReturnValues="ALL_NEW",
                ),
            )
            return self._intent_from_item(self._from_av(resp["Attributes"]))
        except ClientError as err:
            if self._extract_error_code(err) != "ConditionalCheckFailedException":
                raise
            intent = self.get_intent_consistent(complaint_id, intent_id)
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"pin_source_version: upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                ) from err
            if intent.status == UploadIntentStatus.FAILED:
                raise UploadFailedError(
                    f"pin_source_version: upload intent '{intent_id}' has failed: "
                    f"{intent.failure_reason or 'unknown reason'}."
                ) from err
            if (
                intent.status != UploadIntentStatus.CONFIRMING
                or intent.lease_token != cleaned_lease
                or intent.lease_expires_at is None
                or parsed_now >= intent.lease_expires_at
            ):
                raise StaleLeaseError(
                    f"pin_source_version: lease fencing failed for intent '{intent_id}'."
                ) from err
            if intent.source_version_id is not None and intent.source_version_id != cleaned_vid:
                raise ImmutableFieldViolationError(
                    f"pin_source_version: source_version_id is already pinned to "
                    f"{intent.source_version_id!r}, cannot overwrite with {cleaned_vid!r}."
                ) from err
            raise StaleLeaseError(
                f"pin_source_version: condition check failed for intent '{intent_id}'."
            ) from err

    def begin_copy_attempt(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        final_key: str,
        expected_sha256: str,
        expected_mime: str,
        max_byte_size: int,
        now: datetime,
    ) -> UploadAttempt:
        cleaned_src_vid = _validate_version_id(source_version_id, "source_version_id", "begin_copy_attempt")
        cleaned_lease = _require_non_empty_str(lease_token, "lease_token", "begin_copy_attempt")
        cleaned_final_key = _require_non_empty_str(final_key, "final_key", "begin_copy_attempt")
        cleaned_mime = _require_non_empty_str(expected_mime, "expected_mime", "begin_copy_attempt")
        normalised_sha = normalize_sha256_hex(expected_sha256, "expected_sha256")
        valid_max_bytes = _validate_positive_int(max_byte_size, "max_byte_size", "begin_copy_attempt")
        parsed_now = parse_utc_iso_datetime(now, "now")
        now_iso = format_canonical_utc_timestamp(parsed_now)

        new_attempt = UploadAttempt(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token=cleaned_lease,
            status=UploadAttemptStatus.COPYING,
            source_version_id=cleaned_src_vid,
            final_key=cleaned_final_key,
            sha256=normalised_sha,
            content_type=cleaned_mime,
            max_byte_size=valid_max_bytes,
            created_at=parsed_now,
            updated_at=parsed_now,
        )
        attempt_item = self._to_av(self._attempt_to_item(new_attempt))

        # Mandatory correction: eliminate TOCTOU race by using an atomic 2-item
        # TransactWriteItems (ConditionCheck on UploadIntent + Put on UploadAttempt).
        try:
            self._call_with_retry(
                "transact_write_items",
                lambda: self._client.transact_write_items(
                    TransactItems=[
                        {
                            "ConditionCheck": {
                                "TableName": self._table_name,
                                "Key": self._intent_key(complaint_id, intent_id),
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
                                    ":confirming": {"S": UploadIntentStatus.CONFIRMING.value},
                                    ":lease_token": {"S": cleaned_lease},
                                    ":now_iso": {"S": now_iso},
                                    ":src_vid": {"S": cleaned_src_vid},
                                    ":sha256": {"S": normalised_sha},
                                    ":expected_mime": {"S": cleaned_mime},
                                    ":max_byte_size": {"N": str(valid_max_bytes)},
                                    ":final_key": {"S": cleaned_final_key},
                                },
                            }
                        },
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": attempt_item,
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                    ]
                ),
            )
            return new_attempt
        except ClientError as err:
            if self._extract_error_code(err) != "TransactionCanceledException":
                raise
            reasons = self._extract_cancellation_reasons(err)
            fetched = self._batch_get_consistent([
                self._intent_key(complaint_id, intent_id),
                self._attempt_key(complaint_id, intent_id, cleaned_lease),
            ])
            intent_raw = fetched.get(
                (UploadIntent.intent_pk(complaint_id), UploadIntent.intent_sk(intent_id))
            )
            if intent_raw is None:
                raise UploadIntentNotFoundError(
                    f"begin_copy_attempt: upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                ) from err
            intent = self._intent_from_item(intent_raw)
            if intent.status == UploadIntentStatus.FAILED:
                raise UploadFailedError(
                    f"begin_copy_attempt: upload intent '{intent_id}' has failed: "
                    f"{intent.failure_reason or 'unknown reason'}."
                ) from err
            if (
                intent.status != UploadIntentStatus.CONFIRMING
                or intent.lease_token != cleaned_lease
                or intent.lease_expires_at is None
                or parsed_now >= intent.lease_expires_at
            ):
                raise StaleLeaseError(
                    f"begin_copy_attempt: lease fencing failed for intent '{intent_id}'."
                ) from err
            if intent.source_version_id is None:
                raise ImmutableFieldViolationError(
                    "begin_copy_attempt: source_version_id is not pinned on UploadIntent before copy."
                ) from err
            intent_mismatches = self._collect_intent_immutable_mismatches(
                intent,
                source_version_id=cleaned_src_vid,
                final_key=cleaned_final_key,
                normalised_sha256=normalised_sha,
                expected_mime=cleaned_mime,
                max_byte_size=valid_max_bytes,
            )
            if intent_mismatches:
                raise ImmutableFieldViolationError(
                    f"begin_copy_attempt: UploadIntent immutable field mismatch: {'; '.join(intent_mismatches)}"
                ) from err

            attempt_raw = fetched.get(
                (
                    UploadAttempt.attempt_pk(complaint_id, intent_id),
                    UploadAttempt.attempt_sk(cleaned_lease),
                )
            )
            if attempt_raw is not None:
                existing_attempt = self._attempt_from_item(attempt_raw)
                if existing_attempt.status != UploadAttemptStatus.COPYING:
                    raise ImmutableFieldViolationError(
                        f"begin_copy_attempt: existing attempt for lease '{cleaned_lease}' has terminal "
                        f"status='{existing_attempt.status.value}', expected COPYING."
                    ) from err
                attempt_mismatches = self._collect_attempt_immutable_mismatches(
                    existing_attempt,
                    lease_token=cleaned_lease,
                    source_version_id=cleaned_src_vid,
                    final_key=cleaned_final_key,
                    normalised_sha256=normalised_sha,
                    expected_mime=cleaned_mime,
                    max_byte_size=valid_max_bytes,
                )
                if attempt_mismatches:
                    raise ImmutableFieldViolationError(
                        f"begin_copy_attempt: attempt immutable field mismatch for lease '{cleaned_lease}': "
                        f"{'; '.join(attempt_mismatches)}"
                    ) from err
                # Valid idempotent replay under active unexpired CONFIRMING lease.
                return existing_attempt

            raise StaleLeaseError(
                f"begin_copy_attempt: transaction cancelled for intent '{intent_id}' ({reasons!r})."
            ) from err

    def get_attempt(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
    ) -> UploadAttempt | None:
        raw = self._get_item_consistent(
            self._attempt_key(complaint_id, intent_id, lease_token),
            consistent=True,
        )
        return self._attempt_from_item(raw) if raw is not None else None

    def list_attempts(
        self,
        complaint_id: str,
        intent_id: str,
    ) -> list[UploadAttempt]:
        pk = UploadAttempt.attempt_pk(complaint_id, intent_id)
        results: list[UploadAttempt] = []
        lek: dict[str, dict[str, Any]] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": pk}},
                "ConsistentRead": True,
                "ScanIndexForward": True,
            }
            if lek is not None:
                kwargs["ExclusiveStartKey"] = lek
            resp = self._call_with_retry("query", lambda: self._client.query(**kwargs))
            for raw in resp.get("Items", []):
                results.append(self._attempt_from_item(self._from_av(raw)))
            lek = resp.get("LastEvaluatedKey")
            if lek is None:
                break
        results.sort(key=lambda a: (a.created_at, a.lease_token))
        return results

    # -------------------------------------------------------------------------
    # Stage B2: 4-Action Atomic Attachment Commit (Spec §7 + Decision 1)
    # -------------------------------------------------------------------------

    def _fetch_commit_entities(
        self,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
    ) -> tuple[Complaint | None, UploadIntent | None, UploadAttempt | None, TagOutboxRecord | None]:
        """Fetch all 4 commit entities via strongly consistent BatchGetItem."""
        fetched = self._batch_get_consistent([
            self._complaint_key(complaint_id),
            self._intent_key(complaint_id, intent_id),
            self._attempt_key(complaint_id, intent_id, lease_token),
            self._outbox_key(intent_id),
        ])
        comp_raw = fetched.get((f"COMPLAINT#{complaint_id}", "META"))
        intent_raw = fetched.get((UploadIntent.intent_pk(complaint_id), UploadIntent.intent_sk(intent_id)))
        attempt_raw = fetched.get(
            (UploadAttempt.attempt_pk(complaint_id, intent_id), UploadAttempt.attempt_sk(lease_token))
        )
        outbox_raw = fetched.get((TagOutboxRecord.outbox_pk(), TagOutboxRecord.outbox_sk(intent_id)))
        return (
            self._complaint_from_item(comp_raw) if comp_raw is not None else None,
            self._intent_from_item(intent_raw) if intent_raw is not None else None,
            self._attempt_from_item(attempt_raw) if attempt_raw is not None else None,
            self._outbox_from_item(outbox_raw) if outbox_raw is not None else None,
        )

    def _is_matching_commit_replay(
        self,
        *,
        complaint: Complaint | None,
        intent: UploadIntent | None,
        attempt: UploadAttempt | None,
        existing_outbox: TagOutboxRecord | None,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        final_key: str,
        cleaned_src_vid: str,
        cleaned_dst_vid: str,
        normalised_expected_sha: str,
        expected_mime: str,
        max_byte_size: int,
        expected_complaint_version: int,
    ) -> bool:
        """Return True ONLY when committed entities independently verify exact commit replay."""
        return (
            complaint is not None
            and complaint.attachment_status == "VERIFIED"
            and complaint.version in (expected_complaint_version, expected_complaint_version + 1)
            and complaint.attachment_key == final_key
            and intent is not None
            and intent.status == UploadIntentStatus.VERIFIED
            and intent.lease_token == lease_token
            and not self._collect_intent_immutable_mismatches(
                intent,
                source_version_id=cleaned_src_vid,
                final_key=final_key,
                normalised_sha256=normalised_expected_sha,
                expected_mime=expected_mime,
                max_byte_size=max_byte_size,
            )
            and attempt is not None
            and attempt.status == UploadAttemptStatus.COMMITTED
            and attempt.destination_version_id == cleaned_dst_vid
            and not self._collect_attempt_immutable_mismatches(
                attempt,
                lease_token=lease_token,
                source_version_id=cleaned_src_vid,
                final_key=final_key,
                normalised_sha256=normalised_expected_sha,
                expected_mime=expected_mime,
                max_byte_size=max_byte_size,
            )
            and (
                existing_outbox is None
                or (
                    existing_outbox.complaint_id == complaint_id
                    and existing_outbox.intent_id == intent_id
                    and existing_outbox.s3_key == final_key
                    and existing_outbox.destination_version_id == cleaned_dst_vid
                )
            )
        )

    def commit_verified_attachment_versioned(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        final_key: str,
        source_version_id: str,
        destination_version_id: str,
        expected_sha256: str,
        expected_mime: str,
        max_byte_size: int,
        expected_complaint_version: int,
        staging_sha256: str,
        staging_size: int,
        now: datetime,
    ) -> AtomicAttachmentCommitResult:
        cleaned_src_vid = _validate_version_id(
            source_version_id, "source_version_id", "commit_verified_attachment_versioned"
        )
        cleaned_dst_vid = _validate_version_id(
            destination_version_id, "destination_version_id", "commit_verified_attachment_versioned"
        )
        cleaned_lease = _require_non_empty_str(
            lease_token, "lease_token", "commit_verified_attachment_versioned"
        )
        cleaned_final_key = _require_non_empty_str(
            final_key, "final_key", "commit_verified_attachment_versioned"
        )
        cleaned_mime = _require_non_empty_str(
            expected_mime, "expected_mime", "commit_verified_attachment_versioned"
        )
        valid_expected_ver = _validate_positive_int(
            expected_complaint_version,
            "expected_complaint_version",
            "commit_verified_attachment_versioned",
        )
        valid_max_bytes = _validate_positive_int(
            max_byte_size, "max_byte_size", "commit_verified_attachment_versioned"
        )
        valid_staging_size = _validate_positive_int(
            staging_size, "staging_size", "commit_verified_attachment_versioned", minimum=0
        )
        normalised_expected_sha = normalize_sha256_hex(expected_sha256, "expected_sha256")
        normalised_staging_sha = normalize_sha256_hex(staging_sha256, "staging_sha256")
        parsed_now = parse_utc_iso_datetime(now, "now")
        now_iso = format_canonical_utc_timestamp(parsed_now)

        outbox_record = TagOutboxRecord(
            intent_id=intent_id,
            complaint_id=complaint_id,
            s3_key=cleaned_final_key,
            destination_version_id=cleaned_dst_vid,
            committed_at=parsed_now,
        )
        outbox_item = self._to_av(self._outbox_to_item(outbox_record))

        transact_items = [
            # Action 1: Update Complaint (OCC on version + attachment_status != VERIFIED)
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": self._complaint_key(complaint_id),
                    "UpdateExpression": (
                        "SET attachmentKey = :final_key, attachmentStatus = :verified ADD #ver :one"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #ver = :expected_complaint_version "
                        "AND (attribute_not_exists(attachmentStatus) OR attachmentStatus <> :verified)"
                    ),
                    "ExpressionAttributeNames": {"#ver": "version"},
                    "ExpressionAttributeValues": {
                        ":final_key": {"S": cleaned_final_key},
                        ":verified": {"S": "VERIFIED"},
                        ":one": {"N": "1"},
                        ":expected_complaint_version": {"N": str(valid_expected_ver)},
                    },
                }
            },
            # Action 2: Update UploadIntent (CONFIRMING -> VERIFIED, canonical sha256 attribute)
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": self._intent_key(complaint_id, intent_id),
                    "UpdateExpression": (
                        "SET #status = :verified, completed_at = :now_iso, "
                        "staging_sha256 = :staging_sha256, staging_size = :staging_size, "
                        "updated_at = :now_iso"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #status = :confirming "
                        "AND lease_token = :lease_token "
                        "AND source_version_id = :src_vid "
                        "AND sha256 = :expected_sha256 "
                        "AND expected_mime = :expected_mime "
                        "AND max_byte_size = :max_byte_size "
                        "AND final_key = :final_key"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":verified": {"S": UploadIntentStatus.VERIFIED.value},
                        ":now_iso": {"S": now_iso},
                        ":staging_sha256": {"S": normalised_staging_sha},
                        ":staging_size": {"N": str(valid_staging_size)},
                        ":confirming": {"S": UploadIntentStatus.CONFIRMING.value},
                        ":lease_token": {"S": cleaned_lease},
                        ":src_vid": {"S": cleaned_src_vid},
                        ":expected_sha256": {"S": normalised_expected_sha},
                        ":expected_mime": {"S": cleaned_mime},
                        ":max_byte_size": {"N": str(valid_max_bytes)},
                        ":final_key": {"S": cleaned_final_key},
                    },
                }
            },
            # Action 3: Update UploadAttempt (COPYING -> COMMITTED + 6 immutable checks)
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": self._attempt_key(complaint_id, intent_id, cleaned_lease),
                    "UpdateExpression": (
                        "SET #status = :committed, destination_version_id = :dst_vid, "
                        "updated_at = :now_iso"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #status = :copying "
                        "AND lease_token = :lease_token "
                        "AND source_version_id = :src_vid "
                        "AND final_key = :final_key "
                        "AND sha256 = :expected_sha256 "
                        "AND content_type = :expected_mime "
                        "AND max_byte_size = :max_byte_size"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":committed": {"S": UploadAttemptStatus.COMMITTED.value},
                        ":dst_vid": {"S": cleaned_dst_vid},
                        ":now_iso": {"S": now_iso},
                        ":copying": {"S": UploadAttemptStatus.COPYING.value},
                        ":lease_token": {"S": cleaned_lease},
                        ":src_vid": {"S": cleaned_src_vid},
                        ":final_key": {"S": cleaned_final_key},
                        ":expected_sha256": {"S": normalised_expected_sha},
                        ":expected_mime": {"S": cleaned_mime},
                        ":max_byte_size": {"N": str(valid_max_bytes)},
                    },
                }
            },
            # Action 4: Create Outbox record once (attribute_not_exists(PK))
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": outbox_item,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]

        try:
            self._call_with_retry(
                "transact_write_items",
                lambda: self._client.transact_write_items(TransactItems=transact_items),
            )
            complaint, intent, attempt, stored_outbox = self._fetch_commit_entities(
                complaint_id, intent_id, cleaned_lease
            )
            if complaint is None:
                raise ComplaintNotFoundError(
                    f"commit_versioned: complaint '{complaint_id}' missing immediately after commit."
                )
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"commit_versioned: intent '{intent_id}' missing immediately after commit."
                )
            if attempt is None:
                raise UploadIntentNotFoundError(
                    f"commit_versioned: attempt ({complaint_id}, {intent_id}, {cleaned_lease}) missing after commit."
                )
            return AtomicAttachmentCommitResult(
                complaint=complaint,
                intent=intent,
                attempt=attempt,
                outbox=stored_outbox if stored_outbox is not None else outbox_record,
            )
        except ClientError as err:
            if self._extract_error_code(err) != "TransactionCanceledException":
                raise
            reasons = self._extract_cancellation_reasons(err)
            complaint, intent, attempt, existing_outbox = self._fetch_commit_entities(
                complaint_id, intent_id, cleaned_lease
            )

            # 1. Verify genuine idempotent replay strictly across committed entities.
            if self._is_matching_commit_replay(
                complaint=complaint,
                intent=intent,
                attempt=attempt,
                existing_outbox=existing_outbox,
                complaint_id=complaint_id,
                intent_id=intent_id,
                lease_token=cleaned_lease,
                final_key=cleaned_final_key,
                cleaned_src_vid=cleaned_src_vid,
                cleaned_dst_vid=cleaned_dst_vid,
                normalised_expected_sha=normalised_expected_sha,
                expected_mime=cleaned_mime,
                max_byte_size=valid_max_bytes,
                expected_complaint_version=valid_expected_ver,
            ):
                assert complaint is not None and intent is not None and attempt is not None
                return AtomicAttachmentCommitResult(
                    complaint=complaint,
                    intent=intent,
                    attempt=attempt,
                    outbox=existing_outbox,
                )

            # 2. Deterministic error precedence matching InMemoryComplaintRepository.
            if complaint is None:
                raise ComplaintNotFoundError(
                    f"commit_versioned: complaint '{complaint_id}' was not found."
                ) from err
            if intent is None:
                raise UploadIntentNotFoundError(
                    f"commit_versioned: upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
                ) from err
            if intent.status == UploadIntentStatus.FAILED:
                raise UploadFailedError(
                    f"commit_versioned: upload intent '{intent_id}' has failed: "
                    f"{intent.failure_reason or 'unknown reason'}."
                ) from err
            if intent.lease_token != cleaned_lease:
                raise StaleLeaseError(
                    f"commit_versioned: lease token mismatch for intent '{intent_id}'. Worker fenced out."
                ) from err

            if complaint.version != valid_expected_ver:
                raise OptimisticLockError(
                    f"commit_versioned: complaint '{complaint_id}' version conflict: "
                    f"expected {valid_expected_ver}, stored {complaint.version}."
                ) from err
            if complaint.attachment_status == "VERIFIED":
                raise ImmutableFieldViolationError(
                    f"commit_versioned: complaint '{complaint_id}' attachment_status is already 'VERIFIED'."
                ) from err

            intent_mismatches = self._collect_intent_immutable_mismatches(
                intent,
                source_version_id=cleaned_src_vid,
                final_key=cleaned_final_key,
                normalised_sha256=normalised_expected_sha,
                expected_mime=cleaned_mime,
                max_byte_size=valid_max_bytes,
            )
            if intent_mismatches:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: UploadIntent immutable field mismatch: {'; '.join(intent_mismatches)}"
                ) from err
            if intent.status == UploadIntentStatus.VERIFIED:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: intent '{intent_id}' is already VERIFIED with conflicting commit state."
                ) from err
            if intent.status != UploadIntentStatus.CONFIRMING:
                raise StaleLeaseError(
                    f"commit_versioned: intent '{intent_id}' status is '{intent.status.value}', expected CONFIRMING."
                ) from err

            if attempt is None:
                raise UploadIntentNotFoundError(
                    f"commit_versioned: UploadAttempt not found for ({complaint_id}, {intent_id}, {cleaned_lease})."
                ) from err
            if attempt.status != UploadAttemptStatus.COPYING:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: attempt status='{attempt.status.value}', expected COPYING."
                ) from err
            attempt_mismatches = self._collect_attempt_immutable_mismatches(
                attempt,
                lease_token=cleaned_lease,
                source_version_id=cleaned_src_vid,
                final_key=cleaned_final_key,
                normalised_sha256=normalised_expected_sha,
                expected_mime=cleaned_mime,
                max_byte_size=valid_max_bytes,
            )
            if attempt_mismatches:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: UploadAttempt immutable field mismatch: {'; '.join(attempt_mismatches)}"
                ) from err

            if existing_outbox is not None:
                raise ImmutableFieldViolationError(
                    f"commit_versioned: outbox record already exists for intent '{intent_id}'. Duplicate commit."
                ) from err

            raise OptimisticLockError(
                f"commit_versioned: DynamoDB transaction cancelled for intent '{intent_id}' ({reasons!r})."
            ) from err

    # -------------------------------------------------------------------------
    # Stage B2: Outbox CRUD (key-based SK = INTENT#{intent_id} ascending)
    # -------------------------------------------------------------------------

    def get_outbox_record(self, intent_id: str) -> TagOutboxRecord | None:
        raw = self._get_item_consistent(self._outbox_key(intent_id), consistent=True)
        return self._outbox_from_item(raw) if raw is not None else None

    def list_pending_outbox_records(self, *, limit: int = 100) -> list[TagOutboxRecord]:
        _validate_positive_int(limit, "limit", "list_pending_outbox_records")
        collected: list[TagOutboxRecord] = []
        lek: dict[str, dict[str, Any]] | None = None
        while len(collected) < limit:
            remaining = limit - len(collected)
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": TagOutboxRecord.outbox_pk()}},
                "ConsistentRead": True,
                "ScanIndexForward": True,
                "Limit": remaining,
            }
            if lek is not None:
                kwargs["ExclusiveStartKey"] = lek
            resp = self._call_with_retry("query", lambda: self._client.query(**kwargs))
            for raw in resp.get("Items", []):
                collected.append(self._outbox_from_item(self._from_av(raw)))
                if len(collected) >= limit:
                    break
            lek = resp.get("LastEvaluatedKey")
            if lek is None:
                break
        return collected[:limit]

    def list_pending_outbox_records_paginated(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[TagOutboxRecord], str | None]:
        _validate_positive_int(limit, "limit", "list_pending_outbox_records_paginated")
        exclusive_start_key: dict[str, dict[str, str]] | None = None
        if cursor is not None:
            payload = self._verify_cursor(cursor, expected_scope="outbox")
            sk = payload.get("sk")
            if (
                payload.get("pk") != TagOutboxRecord.outbox_pk()
                or not isinstance(sk, str)
                or not sk.startswith("INTENT#")
                or len(sk) <= len("INTENT#")
            ):
                raise ValidationError("Invalid outbox pagination cursor key payload.")
            exclusive_start_key = {
                "PK": {"S": TagOutboxRecord.outbox_pk()},
                "SK": {"S": sk},
            }

        kwargs: dict[str, Any] = {
            "TableName": self._table_name,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": TagOutboxRecord.outbox_pk()}},
            "ConsistentRead": True,
            "ScanIndexForward": True,
            "Limit": limit,
        }
        if exclusive_start_key is not None:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        resp = self._call_with_retry("query", lambda: self._client.query(**kwargs))
        records = [self._outbox_from_item(self._from_av(raw)) for raw in resp.get("Items", [])]

        lek = resp.get("LastEvaluatedKey")
        next_cursor: str | None = None
        if isinstance(lek, dict):
            lek_sk = lek.get("SK", {}).get("S")
            if isinstance(lek_sk, str) and lek_sk.startswith("INTENT#"):
                next_cursor = self._sign_cursor({
                    "v": 1,
                    "scope": "outbox",
                    "pk": TagOutboxRecord.outbox_pk(),
                    "sk": lek_sk,
                })
        return records, next_cursor

    def increment_outbox_sweep_attempt(self, intent_id: str, now: datetime) -> TagOutboxRecord:
        parse_utc_iso_datetime(now, "now")
        try:
            resp = self._call_with_retry(
                "update_item",
                lambda: self._client.update_item(
                    TableName=self._table_name,
                    Key=self._outbox_key(intent_id),
                    UpdateExpression="ADD sweep_attempts :one",
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeValues={":one": {"N": "1"}},
                    ReturnValues="ALL_NEW",
                ),
            )
            return self._outbox_from_item(self._from_av(resp["Attributes"]))
        except ClientError as err:
            if self._extract_error_code(err) == "ConditionalCheckFailedException":
                raise UploadIntentNotFoundError(
                    f"increment_outbox_sweep_attempt: no outbox record for intent '{intent_id}'."
                ) from err
            raise

    def delete_outbox_record(self, intent_id: str) -> None:
        self._call_with_retry(
            "delete_item",
            lambda: self._client.delete_item(
                TableName=self._table_name,
                Key=self._outbox_key(intent_id),
            ),
        )
