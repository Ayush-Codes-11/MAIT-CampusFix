"""RepositoryVersionedUploadStateAdapter — bridges Stage B1 VersionedUploadStatePort
to UploadIntentRepository + ComplaintRepository.

Satisfies the ``VersionedUploadStatePort`` Protocol consumed by
``UploadConfirmationWorker``, delegating each call to the domain repository contracts:
- ``get_intent_consistent``: strongly consistent reads of both intent and complaint to
  compose ``UploadIntentSnapshot`` carrying pre-copy ``expected_complaint_version``.
- ``pin_source_version``: delegates to ``intent_repo.pin_source_version``, then loads
  the current pre-copy ``complaint.version`` via strongly consistent read.
- ``begin_copy_attempt``: delegates to ``intent_repo.begin_copy_attempt`` (which does NOT
  persist ``expected_complaint_version``), verifies ``status == COPYING`` and all six
  immutable attributes on the persisted ``UploadAttempt``, and injects transient
  ``expected_complaint_version`` from the caller's pre-copy snapshot into
  ``CopyAttemptRecord``.
- ``commit_verified_attachment``: delegates directly to
  ``intent_repo.commit_verified_attachment_versioned`` with pre-copy values only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from campusfix.domain.models import (
    UploadAttemptStatus,
    UploadIntent,
    normalize_sha256_hex,
)
from campusfix.domain.rules import (
    ComplaintNotFoundError,
    ImmutableFieldViolationError,
)
from campusfix.repositories.interfaces import ComplaintRepository, UploadIntentRepository
from campusfix.services.boto3_s3_storage import CopyVerificationError
from campusfix.services.upload_confirmation_worker import (
    CopyAttemptRecord,
    UploadIntentSnapshot,
)


def _to_snapshot(intent: UploadIntent, expected_complaint_version: int) -> UploadIntentSnapshot:
    return UploadIntentSnapshot(
        intent_id=intent.intent_id,
        complaint_id=intent.complaint_id,
        owner_id=intent.owner_id,
        staging_key=intent.staging_key,
        final_key=intent.final_key,
        expected_mime=intent.expected_mime,
        max_byte_size=intent.max_byte_size,
        expected_sha256=intent.expected_sha256,
        status=intent.status,
        confirmation_deadline=intent.confirmation_deadline,
        lease_token=intent.lease_token,
        lease_expires_at=intent.lease_expires_at,
        expected_complaint_version=expected_complaint_version,
        source_version_id=intent.source_version_id,
    )


class RepositoryVersionedUploadStateAdapter:
    """Bridges ``VersionedUploadStatePort`` to in-memory or DynamoDB repositories."""

    def __init__(
        self,
        intent_repo: UploadIntentRepository,
        complaint_repo: ComplaintRepository,
    ) -> None:
        self._intent_repo = intent_repo
        self._complaint_repo = complaint_repo

    def get_intent_consistent(
        self,
        complaint_id: str,
        intent_id: str,
    ) -> UploadIntentSnapshot | None:
        """Strongly consistent read of intent + complaint -> ``UploadIntentSnapshot``."""
        intent = self._intent_repo.get_intent_consistent(complaint_id, intent_id)
        if intent is None:
            return None
        complaint = self._complaint_repo.get_consistent(complaint_id)
        if complaint is None:
            return None
        return _to_snapshot(intent, complaint.version)

    def pin_source_version(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        now: datetime,
    ) -> UploadIntentSnapshot:
        """Delegate version pin and return fresh snapshot with pre-copy complaint version."""
        try:
            intent = self._intent_repo.pin_source_version(
                complaint_id=complaint_id,
                intent_id=intent_id,
                lease_token=lease_token,
                source_version_id=source_version_id,
                now=now,
            )
        except ImmutableFieldViolationError as err:
            raise CopyVerificationError(
                str(err),
                complaint_id=complaint_id,
                intent_id=intent_id,
            ) from err

        complaint = self._complaint_repo.get_consistent(complaint_id)
        if complaint is None:
            raise ComplaintNotFoundError(
                f"pin_source_version: complaint '{complaint_id}' was not found."
            )
        return _to_snapshot(intent, complaint.version)

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
    ) -> CopyAttemptRecord:
        """Persist ``UploadAttempt`` without complaint version, verify all immutable fields,
        and return transient ``CopyAttemptRecord`` with ``expected_complaint_version``.
        """
        if (
            isinstance(expected_complaint_version, bool)
            or not isinstance(expected_complaint_version, int)
            or expected_complaint_version < 1
        ):
            raise CopyVerificationError(
                f"begin_copy_attempt requires integer expected_complaint_version >= 1, "
                f"got {expected_complaint_version!r}.",
                complaint_id=complaint_id,
                intent_id=intent_id,
                dest_key=final_key,
            )

        try:
            attempt = self._intent_repo.begin_copy_attempt(
                complaint_id=complaint_id,
                intent_id=intent_id,
                lease_token=lease_token,
                source_version_id=source_version_id,
                final_key=final_key,
                expected_sha256=expected_sha256,
                expected_mime=expected_mime,
                max_byte_size=max_byte_size,
                now=now,
            )
        except ImmutableFieldViolationError as err:
            raise CopyVerificationError(
                str(err),
                complaint_id=complaint_id,
                intent_id=intent_id,
                dest_key=final_key,
            ) from err

        normalised_sha = normalize_sha256_hex(expected_sha256, "expected_sha256")
        mismatches: list[str] = []
        if attempt.complaint_id != complaint_id:
            mismatches.append(f"complaint_id: attempt={attempt.complaint_id!r} vs {complaint_id!r}")
        if attempt.intent_id != intent_id:
            mismatches.append(f"intent_id: attempt={attempt.intent_id!r} vs {intent_id!r}")
        if attempt.lease_token != lease_token:
            mismatches.append(f"lease_token: attempt={attempt.lease_token!r} vs {lease_token!r}")
        if attempt.source_version_id != source_version_id:
            mismatches.append(
                f"source_version_id: attempt={attempt.source_version_id!r} vs {source_version_id!r}"
            )
        if attempt.final_key != final_key:
            mismatches.append(f"final_key: attempt={attempt.final_key!r} vs {final_key!r}")
        if attempt.sha256 != normalised_sha:
            mismatches.append(f"sha256: attempt={attempt.sha256!r} vs {normalised_sha!r}")
        if attempt.content_type != expected_mime:
            mismatches.append(f"content_type: attempt={attempt.content_type!r} vs {expected_mime!r}")
        if attempt.max_byte_size != max_byte_size:
            mismatches.append(f"max_byte_size: attempt={attempt.max_byte_size!r} vs {max_byte_size!r}")
        if attempt.status != UploadAttemptStatus.COPYING:
            mismatches.append(f"status: attempt={attempt.status.value!r} vs 'COPYING'")
        if mismatches:
            raise CopyVerificationError(
                f"begin_copy_attempt adapter: returned UploadAttempt mismatch: {'; '.join(mismatches)}",
                complaint_id=complaint_id,
                intent_id=intent_id,
                dest_key=final_key,
            )

        return CopyAttemptRecord(
            complaint_id=attempt.complaint_id,
            intent_id=attempt.intent_id,
            lease_token=attempt.lease_token,
            source_version_id=attempt.source_version_id,
            final_key=attempt.final_key,
            expected_sha256=attempt.sha256,
            expected_mime=attempt.content_type,
            max_byte_size=attempt.max_byte_size,
            expected_complaint_version=expected_complaint_version,
            status=attempt.status.value,
        )

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
    ) -> Any:
        """Delegate four-action atomic commit to ``intent_repo`` with pre-copy values."""
        try:
            return self._intent_repo.commit_verified_attachment_versioned(
                complaint_id=complaint_id,
                intent_id=intent_id,
                lease_token=lease_token,
                final_key=final_key,
                source_version_id=source_version_id,
                destination_version_id=destination_version_id,
                expected_sha256=expected_sha256,
                expected_mime=expected_mime,
                max_byte_size=max_byte_size,
                expected_complaint_version=expected_complaint_version,
                staging_sha256=staging_sha256,
                staging_size=staging_size,
                now=now,
            )
        except ImmutableFieldViolationError as err:
            raise CopyVerificationError(
                str(err),
                complaint_id=complaint_id,
                intent_id=intent_id,
                dest_key=final_key,
            ) from err
