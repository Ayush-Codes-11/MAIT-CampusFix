"""Version-pinned conditional-copy upload confirmation worker (Stage B1).

Orchestrates the Phase 3.3 Step 2 §2.2a/§2.3/§2.4/§10 conditional-copy workflow behind
narrow, typed Protocols (`ConditionalS3StoragePort` and `VersionedUploadStatePort`)
so persistence (DynamoDB transactions/attempts/outbox) stays decoupled until Stage B2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import random
import time
from typing import Any, Callable, Protocol

from botocore.exceptions import ClientError

from campusfix.domain.models import UploadIntentStatus
from campusfix.domain.rules import (
    MAX_IMAGE_BYTES,
    DestinationConflictError,
    StaleLeaseError,
    UploadFailedError,
    UploadIntentExpiredError,
    UploadIntentNotFoundError,
    UploadNotReadyError,
    ValidationError,
)
from campusfix.services.boto3_s3_storage import (
    Boto3S3AdapterError,
    ConditionalCopyConflictError,
    ConditionalCopyPreconditionError,
    ConditionalCopyResult,
    CopyVerificationError,
    StagingObjectDeletedError,
    StreamedObjectVerification,
    UploadAmbiguousVersionError,
    VersionedS3ObjectMetadata,
    is_valid_version_id,
)
from campusfix.services.uploads import normalize_sha256, validate_magic_bytes

logger = logging.getLogger(__name__)

MAX_CONDITIONAL_COPY_CONFLICT_RETRIES = 3
BASE_CONFLICT_BACKOFF_SECONDS = 0.2


class AlreadyVerifiedIdempotent(Exception):
    """Control-flow signal raised when an intent has already been committed transactionally."""

    def __init__(self, attachment_key: str) -> None:
        super().__init__(f"Upload intent already verified at '{attachment_key}'.")
        self.attachment_key = attachment_key


class UploadRecoveryError(Boto3S3AdapterError):
    """Raised when version recovery or consistency checks fail safely without committing."""


@dataclass(frozen=True)
class UploadIntentSnapshot:
    """Typed worker snapshot of an UploadIntent record and Complaint OCC version."""

    intent_id: str
    complaint_id: str
    owner_id: str
    staging_key: str
    final_key: str
    expected_mime: str
    max_byte_size: int
    expected_sha256: str
    status: UploadIntentStatus
    confirmation_deadline: datetime
    lease_token: str | None
    lease_expires_at: datetime | None
    expected_complaint_version: int
    source_version_id: str | None = None


@dataclass(frozen=True)
class CopyAttemptRecord:
    """Immutable pre-copy Attempt(COPYING) record matching Phase 3.3 Step 2 §6."""

    complaint_id: str
    intent_id: str
    lease_token: str
    source_version_id: str
    final_key: str
    expected_sha256: str
    expected_mime: str
    max_byte_size: int
    expected_complaint_version: int
    status: str = "COPYING"


@dataclass(frozen=True)
class ConditionalCopyConfirmationResult:
    """Verified outcome ready for Stage B2 transactional persistence."""

    status: str
    attachment_key: str
    source_version_id: str
    destination_version_id: str
    staging_sha256: str
    staging_size: int


class ConditionalS3StoragePort(Protocol):
    """Narrow S3 storage port consumed by `UploadConfirmationWorker`."""

    def assert_capabilities(self) -> None: ...

    def list_object_versions_exact(self, bucket: str, key: str) -> str | None: ...

    def head_object(
        self,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> VersionedS3ObjectMetadata | None: ...

    def get_object_range(
        self,
        bucket: str,
        key: str,
        start_byte: int,
        end_byte: int,
        version_id: str | None = None,
    ) -> bytes: ...

    def read_object_and_compute_sha256(
        self,
        bucket: str,
        key: str,
        *,
        version_id: str,
        max_byte_size: int,
    ) -> StreamedObjectVerification: ...

    def copy_object_conditional(
        self,
        *,
        source_bucket: str,
        source_key: str,
        source_version_id: str,
        dest_bucket: str,
        dest_key: str,
    ) -> ConditionalCopyResult: ...

    def delete_object(
        self,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> None: ...


class VersionedUploadStatePort(Protocol):
    """Narrow state/persistence port consumed by `UploadConfirmationWorker`."""

    def get_intent_consistent(
        self,
        complaint_id: str,
        intent_id: str,
    ) -> UploadIntentSnapshot | None: ...

    def pin_source_version(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        now: datetime,
    ) -> UploadIntentSnapshot: ...

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
        expected_complaint_version: int,
        now: datetime,
    ) -> CopyAttemptRecord: ...

    def commit_verified_attachment(
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
    ) -> Any: ...


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UploadConfirmationWorker:
    """Executes version-pinned S3 validation, conditional CopyObject, and 409/412 recovery."""

    def __init__(
        self,
        *,
        s3_storage: ConditionalS3StoragePort,
        state_port: VersionedUploadStatePort,
        source_bucket: str,
        dest_bucket: str | None = None,
        max_conflict_retries: int = MAX_CONDITIONAL_COPY_CONFLICT_RETRIES,
        base_conflict_backoff_seconds: float = BASE_CONFLICT_BACKOFF_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
        now_fn: Callable[[], datetime] = _default_utc_now,
    ) -> None:
        if max_conflict_retries < 0:
            raise ValueError("max_conflict_retries must be non-negative.")
        if base_conflict_backoff_seconds < 0:
            raise ValueError("base_conflict_backoff_seconds must be non-negative.")
        self._s3 = s3_storage
        self._state = state_port
        self._source_bucket = source_bucket
        self._dest_bucket = dest_bucket or source_bucket
        self._max_conflict_retries = max_conflict_retries
        self._base_backoff = base_conflict_backoff_seconds
        self._sleep = sleep_fn
        self._jitter = jitter_fn
        self._now = now_fn

    def confirm_upload(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        worker_token: str,
        source_version_id: str | None = None,
    ) -> ConditionalCopyConfirmationResult:
        """Verify staging object at pinned version, conditionally copy to final key, and commit."""
        # Step 0: Fail closed before any copy path if SDK lacks IfNoneMatch support.
        self._s3.assert_capabilities()

        # Step 1: Strongly consistent lease & status validation.
        intent = self._require_active_confirming_intent(
            complaint_id=complaint_id,
            intent_id=intent_id,
            worker_token=worker_token,
            check_deadline=True,
        )

        # Step 2: Resolve & immutably pin source_version_id.
        pinned_intent, pinned_source_version_id = self._resolve_and_pin_source_version(
            intent=intent,
            worker_token=worker_token,
            explicit_source_version_id=source_version_id,
        )

        # Step 3: Validate the exact pinned staging object version (bounds, MIME, SHA-256, magic bytes).
        staging_size, staging_sha256 = self._validate_pinned_source(
            intent=pinned_intent,
            source_version_id=pinned_source_version_id,
        )

        # Step 4: Unconditionally record/verify Attempt(status="COPYING") before CopyObject.
        normalized_expected_sha256 = normalize_sha256(pinned_intent.expected_sha256)
        attempt = self._state.begin_copy_attempt(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token=worker_token,
            source_version_id=pinned_source_version_id,
            final_key=pinned_intent.final_key,
            expected_sha256=normalized_expected_sha256,
            expected_mime=pinned_intent.expected_mime,
            max_byte_size=pinned_intent.max_byte_size,
            expected_complaint_version=pinned_intent.expected_complaint_version,
            now=self._now(),
        )
        self._validate_copy_attempt_record(
            attempt=attempt,
            intent=pinned_intent,
            lease_token=worker_token,
            source_version_id=pinned_source_version_id,
            expected_sha256=normalized_expected_sha256,
        )

        # Step 5: Mandatory conditional CopyObject (IfNoneMatch='*') with bounded 409 retries and 412 recovery.
        destination_version_id = self._execute_conditional_copy_with_recovery(
            intent=pinned_intent,
            worker_token=worker_token,
            source_version_id=pinned_source_version_id,
            verified_staging_size=staging_size,
            verified_staging_sha256=staging_sha256,
        )

        # Step 6: Persist verified attachment through state port using exact pre-copy immutable values.
        self._state.commit_verified_attachment(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token=worker_token,
            final_key=pinned_intent.final_key,
            source_version_id=pinned_source_version_id,
            destination_version_id=destination_version_id,
            expected_sha256=normalized_expected_sha256,
            expected_mime=pinned_intent.expected_mime,
            max_byte_size=pinned_intent.max_byte_size,
            expected_complaint_version=pinned_intent.expected_complaint_version,
            staging_sha256=staging_sha256,
            staging_size=staging_size,
            now=self._now(),
        )

        # Best-effort staging delete creates a delete marker without deleting noncurrent versions.
        try:
            self._s3.delete_object(self._source_bucket, pinned_intent.staging_key)
        except Exception as err:
            logger.warning(
                "Best-effort staging cleanup failed for '%s': %s",
                pinned_intent.staging_key,
                err,
            )

        return ConditionalCopyConfirmationResult(
            status="VERIFIED",
            attachment_key=pinned_intent.final_key,
            source_version_id=pinned_source_version_id,
            destination_version_id=destination_version_id,
            staging_sha256=staging_sha256,
            staging_size=staging_size,
        )

    def _require_active_confirming_intent(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        worker_token: str,
        check_deadline: bool,
    ) -> UploadIntentSnapshot:
        fresh = self._state.get_intent_consistent(complaint_id, intent_id)
        if fresh is None:
            raise UploadIntentNotFoundError(
                f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
            )
        if fresh.status == UploadIntentStatus.VERIFIED:
            raise AlreadyVerifiedIdempotent(attachment_key=fresh.final_key)

        now = self._now()
        if (
            not worker_token
            or fresh.lease_token != worker_token
            or fresh.lease_expires_at is None
            or now >= fresh.lease_expires_at
        ):
            raise StaleLeaseError(
                f"Worker lease token for intent '{intent_id}' is missing, stale, or expired."
            )

        if fresh.status != UploadIntentStatus.CONFIRMING:
            raise UploadFailedError(
                f"Upload intent '{intent_id}' is in unexpected state '{fresh.status.value}'."
            )

        if (
            isinstance(fresh.expected_complaint_version, bool)
            or not isinstance(fresh.expected_complaint_version, int)
            or fresh.expected_complaint_version < 1
        ):
            raise CopyVerificationError(
                f"Upload intent '{intent_id}' missing valid expected_complaint_version: "
                f"{fresh.expected_complaint_version!r}.",
                complaint_id=complaint_id,
                intent_id=intent_id,
                dest_key=fresh.final_key,
            )

        if check_deadline and now > fresh.confirmation_deadline:
            raise UploadIntentExpiredError(
                f"Upload intent '{intent_id}' confirmation deadline expired at "
                f"{fresh.confirmation_deadline.isoformat()}."
            )
        return fresh

    def _assert_immutable_intent_unchanged(
        self,
        *,
        expected: UploadIntentSnapshot,
        fresh: UploadIntentSnapshot,
        expected_source_version_id: str,
    ) -> None:
        """Fail closed if consistent re-read shows drift in immutable intent/attempt attributes."""
        drifts: list[str] = []
        if fresh.source_version_id != expected_source_version_id:
            drifts.append(
                f"source_version_id ({fresh.source_version_id!r} != {expected_source_version_id!r})"
            )
        if fresh.final_key != expected.final_key:
            drifts.append(f"final_key ({fresh.final_key!r} != {expected.final_key!r})")
        if normalize_sha256(fresh.expected_sha256) != normalize_sha256(expected.expected_sha256):
            drifts.append(
                f"expected_sha256 ({fresh.expected_sha256!r} != {expected.expected_sha256!r})"
            )
        if fresh.expected_mime != expected.expected_mime:
            drifts.append(f"expected_mime ({fresh.expected_mime!r} != {expected.expected_mime!r})")
        if fresh.max_byte_size != expected.max_byte_size:
            drifts.append(f"max_byte_size ({fresh.max_byte_size!r} != {expected.max_byte_size!r})")

        if drifts:
            raise CopyVerificationError(
                "UploadIntent immutable attributes changed during confirmation: "
                + "; ".join(drifts),
                complaint_id=expected.complaint_id,
                intent_id=expected.intent_id,
                dest_key=expected.final_key,
            )

    def _validate_copy_attempt_record(
        self,
        *,
        attempt: CopyAttemptRecord,
        intent: UploadIntentSnapshot,
        lease_token: str,
        source_version_id: str,
        expected_sha256: str,
    ) -> None:
        if not isinstance(attempt, CopyAttemptRecord):
            raise CopyVerificationError(
                f"begin_copy_attempt returned invalid attempt type: {type(attempt).__name__}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )
        if attempt.status != "COPYING":
            raise CopyVerificationError(
                f"CopyAttempt status must be 'COPYING' before CopyObject, got {attempt.status!r}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )
        if (
            attempt.complaint_id != intent.complaint_id
            or attempt.intent_id != intent.intent_id
            or attempt.lease_token != lease_token
            or attempt.source_version_id != source_version_id
            or attempt.final_key != intent.final_key
            or normalize_sha256(attempt.expected_sha256) != expected_sha256
            or attempt.expected_mime != intent.expected_mime
            or attempt.max_byte_size != intent.max_byte_size
            or attempt.expected_complaint_version != intent.expected_complaint_version
        ):
            raise CopyVerificationError(
                "CopyAttempt immutable attributes do not match verified UploadIntent snapshot.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )

    def _resolve_and_pin_source_version(
        self,
        *,
        intent: UploadIntentSnapshot,
        worker_token: str,
        explicit_source_version_id: str | None,
    ) -> tuple[UploadIntentSnapshot, str]:
        if explicit_source_version_id is not None and not is_valid_version_id(explicit_source_version_id):
            raise CopyVerificationError(
                f"Supplied source_version_id is invalid: {explicit_source_version_id!r}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )

        existing_pin = intent.source_version_id
        if existing_pin is not None:
            if not is_valid_version_id(existing_pin):
                raise CopyVerificationError(
                    f"Stored pinned source_version_id is invalid: {existing_pin!r}.",
                    complaint_id=intent.complaint_id,
                    intent_id=intent.intent_id,
                    dest_key=intent.final_key,
                )
            if explicit_source_version_id is not None and explicit_source_version_id != existing_pin:
                raise CopyVerificationError(
                    f"Provided source_version_id {explicit_source_version_id!r} conflicts with immutable "
                    f"pinned source_version_id {existing_pin!r}.",
                    complaint_id=intent.complaint_id,
                    intent_id=intent.intent_id,
                    dest_key=intent.final_key,
                )
            return intent, existing_pin

        candidate_version = explicit_source_version_id
        if candidate_version is None:
            candidate_version = self._s3.list_object_versions_exact(
                self._source_bucket,
                intent.staging_key,
            )
            if candidate_version is None:
                raise UploadNotReadyError(
                    f"Staging object '{intent.staging_key}' is not present in S3 yet."
                )

        if not is_valid_version_id(candidate_version):
            raise UploadAmbiguousVersionError(
                f"Resolved staging version is invalid: {candidate_version!r}."
            )

        pinned = self._state.pin_source_version(
            complaint_id=intent.complaint_id,
            intent_id=intent.intent_id,
            lease_token=worker_token,
            source_version_id=candidate_version,
            now=self._now(),
        )
        if pinned.source_version_id != candidate_version:
            raise CopyVerificationError(
                f"Pinned source_version_id mismatch after state update: expected {candidate_version!r}, "
                f"got {pinned.source_version_id!r}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )
        return pinned, candidate_version

    def _validate_pinned_source(
        self,
        *,
        intent: UploadIntentSnapshot,
        source_version_id: str,
    ) -> tuple[int, str]:
        src_head = self._s3.head_object(
            self._source_bucket,
            intent.staging_key,
            version_id=source_version_id,
        )
        if src_head is None:
            raise UploadNotReadyError(
                f"Pinned staging object '{intent.staging_key}'@{source_version_id} was not found."
            )
        if src_head.version_id is not None and src_head.version_id != source_version_id:
            raise CopyVerificationError(
                f"Source HeadObject returned VersionId {src_head.version_id!r}, expected {source_version_id!r}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )

        effective_max = min(intent.max_byte_size, MAX_IMAGE_BYTES)
        if src_head.content_length < 1024 or src_head.content_length > effective_max:
            raise ValidationError(
                f"Staging object size ({src_head.content_length} bytes) outside permitted bounds "
                f"[1024, {effective_max}]."
            )

        if src_head.content_type != intent.expected_mime:
            raise ValidationError(
                f"Staging Content-Type {src_head.content_type!r} does not match expected {intent.expected_mime!r}."
            )

        expected_sha256 = normalize_sha256(intent.expected_sha256)
        if src_head.sha256_checksum is not None:
            staging_sha256 = src_head.sha256_checksum.lower()
            if staging_sha256 != expected_sha256:
                raise ValidationError(
                    f"Staging SHA-256 '{staging_sha256}' does not match expected '{expected_sha256}'."
                )
            header_bytes = self._s3.get_object_range(
                self._source_bucket,
                intent.staging_key,
                0,
                11,
                version_id=source_version_id,
            )
            validate_magic_bytes(header_bytes, intent.expected_mime)
            return src_head.content_length, staging_sha256

        # Fail closed cleanly when HeadObject lacks ChecksumSHA256 by streaming pinned version.
        streamed = self._s3.read_object_and_compute_sha256(
            self._source_bucket,
            intent.staging_key,
            version_id=source_version_id,
            max_byte_size=effective_max,
        )
        if streamed.content_length != src_head.content_length:
            raise ValidationError(
                f"Streamed staging size ({streamed.content_length}) mismatch against ContentLength ({src_head.content_length})."
            )
        if streamed.sha256_checksum.lower() != expected_sha256:
            raise ValidationError(
                f"Streamed staging SHA-256 '{streamed.sha256_checksum}' does not match expected '{expected_sha256}'."
            )
        validate_magic_bytes(streamed.header_bytes, intent.expected_mime)
        return src_head.content_length, streamed.sha256_checksum.lower()

    def _execute_conditional_copy_with_recovery(
        self,
        *,
        intent: UploadIntentSnapshot,
        worker_token: str,
        source_version_id: str,
        verified_staging_size: int,
        verified_staging_sha256: str,
    ) -> str:
        for conflict_attempt in range(self._max_conflict_retries + 1):
            # Re-verify SDK gate before every CopyObject invocation.
            self._s3.assert_capabilities()
            try:
                copy_res = self._s3.copy_object_conditional(
                    source_bucket=self._source_bucket,
                    source_key=intent.staging_key,
                    source_version_id=source_version_id,
                    dest_bucket=self._dest_bucket,
                    dest_key=intent.final_key,
                )
            except ConditionalCopyPreconditionError:
                return self._recover_and_verify_existing_destination_412(
                    intent=intent,
                    worker_token=worker_token,
                    source_version_id=source_version_id,
                    verified_staging_size=verified_staging_size,
                    verified_staging_sha256=verified_staging_sha256,
                )
            except ConditionalCopyConflictError as err:
                fresh_intent = self._require_active_confirming_intent(
                    complaint_id=intent.complaint_id,
                    intent_id=intent.intent_id,
                    worker_token=worker_token,
                    check_deadline=False,
                )
                self._assert_immutable_intent_unchanged(
                    expected=intent,
                    fresh=fresh_intent,
                    expected_source_version_id=source_version_id,
                )
                if conflict_attempt >= self._max_conflict_retries:
                    raise CopyVerificationError(
                        f"CopyObject aborted after {self._max_conflict_retries} retries on "
                        "HTTP 409 ConditionalRequestConflict.",
                        complaint_id=intent.complaint_id,
                        intent_id=intent.intent_id,
                        dest_key=intent.final_key,
                    ) from err

                remaining_lease = (fresh_intent.lease_expires_at - self._now()).total_seconds()
                delay = min(
                    self._base_backoff * (2**conflict_attempt),
                    max(0.0, remaining_lease * 0.25),
                )
                if delay > 0:
                    jitter = self._jitter(0.0, min(0.05, delay))
                    self._sleep(delay + max(0.0, jitter))
                continue

            return self._verify_copied_destination(
                copy_res=copy_res,
                intent=intent,
                source_version_id=source_version_id,
                verified_staging_size=verified_staging_size,
            )

        raise CopyVerificationError(
            "CopyObject retry loop exited without producing a verified destination VersionId.",
            complaint_id=intent.complaint_id,
            intent_id=intent.intent_id,
            dest_key=intent.final_key,
        )

    def _verify_copied_destination(
        self,
        *,
        copy_res: ConditionalCopyResult,
        intent: UploadIntentSnapshot,
        source_version_id: str,
        verified_staging_size: int,
    ) -> str:
        destination_version_id = copy_res.destination_version_id
        if not is_valid_version_id(destination_version_id):
            raise CopyVerificationError(
                f"Invalid destination VersionId returned by CopyObject: {destination_version_id!r}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )

        if copy_res.copy_source_version_id is not None and copy_res.copy_source_version_id != source_version_id:
            raise CopyVerificationError(
                f"CopyObject CopySourceVersionId {copy_res.copy_source_version_id!r} differs from "
                f"pinned source_version_id {source_version_id!r}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )

        dest_head = self._s3.head_object(
            self._dest_bucket,
            intent.final_key,
            version_id=destination_version_id,
        )
        if dest_head is None:
            raise CopyVerificationError(
                f"Copied destination '{intent.final_key}'@{destination_version_id} missing on HeadObject.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )
        if dest_head.version_id is not None and dest_head.version_id != destination_version_id:
            raise CopyVerificationError(
                f"Destination HeadObject returned VersionId {dest_head.version_id!r}, "
                f"expected {destination_version_id!r}.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )
        if (
            dest_head.content_length != verified_staging_size
            or dest_head.content_length < 1024
            or dest_head.content_length > intent.max_byte_size
        ):
            raise CopyVerificationError(
                f"Destination '{intent.final_key}'@{destination_version_id} ContentLength "
                f"({dest_head.content_length}) does not match verified staging size ({verified_staging_size}) or bounds.",
                complaint_id=intent.complaint_id,
                intent_id=intent.intent_id,
                dest_key=intent.final_key,
            )

        return destination_version_id

    def _recover_and_verify_existing_destination_412(
        self,
        *,
        intent: UploadIntentSnapshot,
        worker_token: str,
        source_version_id: str,
        verified_staging_size: int,
        verified_staging_sha256: str,
    ) -> str:
        """Execute fail-closed §2.4 HTTP 412 recovery over existing destination object."""
        fresh_intent = self._require_active_confirming_intent(
            complaint_id=intent.complaint_id,
            intent_id=intent.intent_id,
            worker_token=worker_token,
            check_deadline=False,
        )
        self._assert_immutable_intent_unchanged(
            expected=intent,
            fresh=fresh_intent,
            expected_source_version_id=source_version_id,
        )

        # Use validated pinned intent values consistently for S3 inspection and commit.
        pinned = intent

        try:
            tip_head = self._s3.head_object(self._dest_bucket, pinned.final_key, version_id=None)
        except (ClientError, StagingObjectDeletedError, CopyVerificationError) as err:
            raise DestinationConflictError(
                f"Destination '{pinned.final_key}' returned 412 on CopyObject but could not be inspected: {err}"
            ) from err

        if tip_head is None or not is_valid_version_id(tip_head.version_id):
            raise DestinationConflictError(
                f"Existing destination '{pinned.final_key}' missing valid VersionId during 412 recovery."
            )

        existing_version_id = tip_head.version_id

        try:
            ver_head = self._s3.head_object(
                self._dest_bucket,
                pinned.final_key,
                version_id=existing_version_id,
            )
        except (ClientError, StagingObjectDeletedError, CopyVerificationError) as err:
            raise DestinationConflictError(
                f"412 recovery versioned HeadObject failed for '{pinned.final_key}'@{existing_version_id}: {err}"
            ) from err

        if ver_head is None or ver_head.version_id != existing_version_id:
            actual_vid = ver_head.version_id if ver_head is not None else None
            raise DestinationConflictError(
                f"412 recovery VersionId mismatch: expected {existing_version_id!r}, got {actual_vid!r}."
            )

        if (
            ver_head.content_length != verified_staging_size
            or ver_head.content_length < 1024
            or ver_head.content_length > pinned.max_byte_size
        ):
            raise DestinationConflictError(
                f"412 recovery size mismatch at '{pinned.final_key}'@{existing_version_id}: "
                f"got {ver_head.content_length}, expected {verified_staging_size}."
            )

        if ver_head.content_type != pinned.expected_mime:
            raise DestinationConflictError(
                f"412 recovery MIME mismatch at '{pinned.final_key}'@{existing_version_id}: "
                f"got {ver_head.content_type!r}, expected {pinned.expected_mime!r}."
            )

        expected_sha256 = normalize_sha256(pinned.expected_sha256)
        staging_sha256_lower = verified_staging_sha256.lower()
        if ver_head.sha256_checksum is not None:
            head_sha = ver_head.sha256_checksum.lower()
            if head_sha != expected_sha256 or head_sha != staging_sha256_lower:
                raise DestinationConflictError(
                    f"412 recovery ChecksumSHA256 mismatch at '{pinned.final_key}'@{existing_version_id}."
                )

        # Full cryptographic byte parity & magic-byte validation on pinned existing_version_id.
        try:
            streamed = self._s3.read_object_and_compute_sha256(
                self._dest_bucket,
                pinned.final_key,
                version_id=existing_version_id,
                max_byte_size=pinned.max_byte_size,
            )
            if streamed.content_length != verified_staging_size:
                raise DestinationConflictError("412 recovery streamed size mismatch.")
            computed_sha = streamed.sha256_checksum.lower()
            if computed_sha != staging_sha256_lower or computed_sha != expected_sha256:
                raise DestinationConflictError(
                    f"412 recovery SHA-256 mismatch at '{pinned.final_key}'@{existing_version_id}: "
                    f"computed={computed_sha}, expected={expected_sha256}."
                )
            validate_magic_bytes(streamed.header_bytes, pinned.expected_mime)
        except ValidationError as err:
            raise DestinationConflictError(
                f"412 recovery validation failed at '{pinned.final_key}'@{existing_version_id}: {err}"
            ) from err

        return existing_version_id
