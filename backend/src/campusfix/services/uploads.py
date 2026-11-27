"""Upload-intent service for secure two-stage S3 image uploads in MAIT CampusFix.

Orchestrates upload authorization, quarantine staging, cryptographic checksum validation,
magic-byte verification, fencing-token lease management, and atomic attachment commits.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import uuid
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from campusfix.domain.models import (
    Actor,
    Complaint,
    ComplaintStatus,
    Role,
    UploadIntent,
    UploadIntentStatus,
)
from campusfix.domain.rules import (
    ALLOWED_IMAGE_MIMES,
    CONFIRMATION_DEADLINE_SECONDS,
    LEASE_DURATION_SECONDS,
    MAX_CONFIRM_RETRIES,
    MAX_IMAGE_BYTES,
    RECOVERY_DEADLINE_SECONDS,
    UPLOAD_PRESIGNED_EXPIRY_SECONDS,
    ComplaintNotFoundError,
    StaleLeaseError,
    UnauthorizedError,
    UploadFailedError,
    UploadIntentExpiredError,
    UploadIntentNotFoundError,
    UploadNotReadyError,
    ValidationError,
)
from campusfix.repositories.interfaces import ComplaintRepository, UploadIntentRepository
from campusfix.services.s3_storage import S3StorageService

if TYPE_CHECKING:
    from campusfix.services.upload_confirmation_worker import UploadConfirmationWorker

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


@runtime_checkable
class UploadPresignedStorage(Protocol):
    """Minimal storage contract required by UploadIntentService directly."""

    def generate_presigned_post(
        self,
        bucket: str,
        key: str,
        fields: dict[str, Any],
        conditions: list[Any],
        expires_in: int = UPLOAD_PRESIGNED_EXPIRY_SECONDS,
    ) -> dict[str, Any]:
        """Generate a scoped presigned POST payload for quarantine staging."""

    def generate_presigned_get(
        self,
        bucket: str,
        key: str,
        expires_in: int = UPLOAD_PRESIGNED_EXPIRY_SECONDS,
    ) -> str:
        """Generate a short-lived presigned GET URL for a verified attachment."""

    def delete_object(
        self,
        bucket: str,
        key: str,
    ) -> None:
        """Delete an object (or current staging version) from S3."""

MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def normalize_sha256(checksum: str) -> str:
    """Normalize a SHA-256 digest to a lowercase 64-character hex string.
    
    Accepts:
    - 64-character hex string.
    - 44-character base64 string.
    
    Raises:
        ValidationError: If the format is invalid.
    """
    if not isinstance(checksum, str):
        raise ValidationError("SHA-256 checksum must be a string.")
    cleaned = checksum.strip()
    if len(cleaned) == 64 and all(c in "0123456789abcdefABCDEF" for c in cleaned):
        return cleaned.lower()
    if len(cleaned) == 44:
        try:
            raw = base64.b64decode(cleaned, validate=True)
            if len(raw) == 32:
                return raw.hex().lower()
        except Exception:
            pass
    raise ValidationError(
        f"Invalid SHA-256 checksum: {checksum!r}. Expected 64-char hex or 44-char base64 string."
    )


def validate_magic_bytes(header_bytes: bytes, mime_type: str) -> None:
    """Validate binary header signatures against expected image MIME type."""
    if not header_bytes or len(header_bytes) < 4:
        raise ValidationError("Uploaded file is too short or empty to verify magic bytes.")

    if mime_type == "image/jpeg":
        # JPEG begins with FF D8 FF
        if not header_bytes.startswith(b"\xff\xd8\xff"):
            raise ValidationError("Invalid JPEG file signature (magic bytes mismatch).")
    elif mime_type == "image/png":
        # PNG begins with 89 50 4E 47 0D 0A 1A 0A
        if not header_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValidationError("Invalid PNG file signature (magic bytes mismatch).")
    elif mime_type == "image/webp":
        # WebP begins with 'RIFF' and has 'WEBP' at bytes 8-11
        if len(header_bytes) < 12 or not header_bytes.startswith(b"RIFF") or header_bytes[8:12] != b"WEBP":
            raise ValidationError("Invalid WebP file signature (magic bytes mismatch).")
    else:
        raise ValidationError(f"Unsupported MIME type for magic byte validation: '{mime_type}'.")


class UploadIntentService:
    """Application service managing S3 image upload authorization and confirmation."""

    def __init__(
        self,
        complaint_repository: ComplaintRepository,
        intent_repository: UploadIntentRepository,
        s3_storage: S3StorageService | UploadPresignedStorage,
        bucket_name: str | None = None,
        region: str | None = None,
        *,
        confirmation_worker: UploadConfirmationWorker | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            confirmation_worker is None
            and callable(getattr(s3_storage, "copy_object_conditional", None))
            and not callable(getattr(s3_storage, "copy_object", None))
        ):
            raise ValidationError(
                "UploadIntentService requires 'confirmation_worker' when configured with conditional S3 storage."
            )
        self._complaint_repo = complaint_repository
        self._intent_repo = intent_repository
        self._s3 = s3_storage
        self._bucket = bucket_name or os.environ.get("S3_ATTACHMENTS_BUCKET", "campusfix-attachments-ap-southeast-2")
        self._region = region or os.environ.get("AWS_REGION", "ap-southeast-2")
        self._confirmation_worker = confirmation_worker
        self._now_fn = now_fn if now_fn is not None else _utc_now

    def _get_complaint(self, complaint_id: str) -> Complaint | None:
        if self._confirmation_worker is not None:
            return self._complaint_repo.get_consistent(complaint_id)
        return self._complaint_repo.get(complaint_id)

    def _get_intent(self, complaint_id: str, intent_id: str) -> UploadIntent | None:
        if self._confirmation_worker is not None:
            return self._intent_repo.get_intent_consistent(complaint_id, intent_id)
        return self._intent_repo.get_intent(complaint_id, intent_id)

    def create_upload_intent(
        self,
        complaint_id: str,
        content_type: str,
        byte_size: int,
        checksum_sha256: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """Request an authorized upload policy to attach an image to an open complaint.
        
        Enforces:
        - Actor must be STUDENT
        - Complaint must exist and belong to the student
        - Complaint must be in 'pending' or 'in-progress' status
        - MIME type in ('image/jpeg', 'image/png', 'image/webp')
        - Byte size between 1 KB and 5 MB
        - SHA-256 checksum format
        """
        if not isinstance(actor, Actor):
            raise UnauthorizedError("Operation requires an authenticated Actor.")
        if actor.role != Role.STUDENT:
            raise UnauthorizedError("Only students can create image upload intents.")

        complaint = self._get_complaint(complaint_id)
        if complaint is None:
            raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")

        if complaint.submitted_by != actor.user_id:
            raise UnauthorizedError(
                f"Cannot upload attachment for complaint '{complaint_id}': caller '{actor.user_id}' is not the owner."
            )

        if complaint.status == ComplaintStatus.RESOLVED:
            raise ValidationError("Cannot upload attachments to a resolved complaint.")

        if content_type not in ALLOWED_IMAGE_MIMES:
            allowed = ", ".join(ALLOWED_IMAGE_MIMES)
            raise ValidationError(f"Invalid Content-Type '{content_type}'. Allowed types are: {allowed}.")

        if not isinstance(byte_size, int) or byte_size < 1024 or byte_size > MAX_IMAGE_BYTES:
            raise ValidationError(
                f"Byte size must be between 1024 bytes (1 KB) and {MAX_IMAGE_BYTES} bytes (5 MB). Got: {byte_size!r}."
            )

        normalized_hash = normalize_sha256(checksum_sha256)
        ext = MIME_TO_EXT[content_type]
        intent_id = str(uuid.uuid4())

        now = self._now_fn()
        upload_expires_at = now + timedelta(seconds=UPLOAD_PRESIGNED_EXPIRY_SECONDS)
        confirmation_deadline = now + timedelta(seconds=CONFIRMATION_DEADLINE_SECONDS)
        recovery_deadline = now + timedelta(seconds=RECOVERY_DEADLINE_SECONDS)
        cleanup_ttl = int((now + timedelta(seconds=86400)).timestamp())

        staging_key = f"staging/complaints/{complaint_id}/{intent_id}/upload.tmp"
        final_key = f"complaints/{complaint_id}/images/{intent_id}{ext}"

        intent = UploadIntent(
            intent_id=intent_id,
            complaint_id=complaint_id,
            owner_id=actor.user_id,
            staging_key=staging_key,
            final_key=final_key,
            expected_mime=content_type,
            max_byte_size=byte_size,
            expected_sha256=normalized_hash,
            status=UploadIntentStatus.PENDING_UPLOAD,
            created_at=now,
            upload_expires_at=upload_expires_at,
            confirmation_deadline=confirmation_deadline,
            recovery_deadline=recovery_deadline,
            cleanup_ttl=cleanup_ttl,
        )

        self._intent_repo.create_intent(intent)

        # Generate presigned POST configuration (S3 requires Base64-encoded SHA-256; internal state remains hex)
        checksum_base64 = base64.b64encode(bytes.fromhex(normalized_hash)).decode("ascii")
        fields = {
            "Content-Type": content_type,
            "x-amz-checksum-algorithm": "SHA256",
            "x-amz-checksum-sha256": checksum_base64,
        }
        conditions = [
            ["content-length-range", 1024, MAX_IMAGE_BYTES],
            ["eq", "$key", staging_key],
            ["eq", "$Content-Type", content_type],
            ["eq", "$x-amz-checksum-algorithm", "SHA256"],
            ["eq", "$x-amz-checksum-sha256", checksum_base64],
        ]

        presigned_post = self._s3.generate_presigned_post(
            bucket=self._bucket,
            key=staging_key,
            fields=fields,
            conditions=conditions,
            expires_in=UPLOAD_PRESIGNED_EXPIRY_SECONDS,
        )

        return {
            "intentId": intent_id,
            "uploadUrl": presigned_post["url"],
            "fields": presigned_post["fields"],
            "uploadExpiresAt": upload_expires_at.isoformat(),
            "confirmationDeadline": confirmation_deadline.isoformat(),
        }

    def _confirm_with_worker(
        self,
        complaint_id: str,
        intent_id: str,
        intent: UploadIntent,
        lease_token: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Execute Stage B2 conditional-copy confirmation via UploadConfirmationWorker."""
        from campusfix.services.upload_confirmation_worker import AlreadyVerifiedIdempotent

        assert self._confirmation_worker is not None
        try:
            result = self._confirmation_worker.confirm_upload(
                complaint_id=complaint_id,
                intent_id=intent_id,
                worker_token=lease_token,
            )
        except AlreadyVerifiedIdempotent as err:
            return {
                "status": "VERIFIED",
                "attachmentKey": err.attachment_key,
            }
        except UploadNotReadyError:
            attempts = self._intent_repo.record_missing_staging_attempt(
                complaint_id, intent_id, MAX_CONFIRM_RETRIES, self._now_fn()
            )
            if attempts >= MAX_CONFIRM_RETRIES:
                raise UploadFailedError(
                    f"Exceeded maximum confirmation retry attempts ({MAX_CONFIRM_RETRIES}) without staging object present."
                )
            raise
        except ValidationError as err:
            self._intent_repo.mark_intent_failed(
                complaint_id, intent_id, str(err), self._now_fn(), lease_token
            )
            try:
                self._s3.delete_object(self._bucket, intent.staging_key)
            except Exception as cleanup_err:
                logger.warning(
                    "Best-effort deletion of rejected staging object '%s' failed: %s",
                    intent.staging_key,
                    cleanup_err,
                )
            raise

        return {
            "status": result.status,
            "attachmentKey": result.attachment_key,
        }

    def confirm_upload(
        self,
        complaint_id: str,
        intent_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """Confirm a staged S3 upload, verify integrity, copy to final storage, and atomically commit."""
        if not isinstance(actor, Actor):
            raise UnauthorizedError("Operation requires an authenticated Actor.")
        if actor.role != Role.STUDENT:
            raise UnauthorizedError("Only students can confirm image uploads.")

        complaint = self._get_complaint(complaint_id)
        if complaint is None:
            raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")

        if complaint.submitted_by != actor.user_id:
            raise UnauthorizedError(
                f"Cannot confirm upload for complaint '{complaint_id}': caller '{actor.user_id}' is not the owner."
            )

        intent = self._get_intent(complaint_id, intent_id)
        if intent is None:
            raise UploadIntentNotFoundError(
                f"Upload intent '{intent_id}' for complaint '{complaint_id}' was not found."
            )

        # Idempotent return if already verified
        if intent.status == UploadIntentStatus.VERIFIED:
            return {
                "status": "VERIFIED",
                "attachmentKey": intent.final_key,
            }

        if intent.status == UploadIntentStatus.FAILED:
            raise UploadFailedError(
                f"Upload intent '{intent_id}' has already failed: {intent.failure_reason or 'unknown failure'}."
            )

        now = self._now_fn()
        if now > intent.confirmation_deadline:
            self._intent_repo.mark_intent_failed(
                complaint_id, intent_id, "Confirmation deadline expired", now
            )
            if self._confirmation_worker is not None:
                try:
                    self._s3.delete_object(self._bucket, intent.staging_key)
                except Exception as cleanup_err:
                    logger.warning(
                        "Best-effort deletion of expired staging object '%s' failed: %s",
                        intent.staging_key,
                        cleanup_err,
                    )
            else:
                self._s3.delete_object(self._bucket, intent.staging_key)
            raise UploadIntentExpiredError(
                f"Upload intent '{intent_id}' confirmation deadline expired at {intent.confirmation_deadline.isoformat()}."
            )

        # Claim or takeover lease with atomic fencing token
        lease_token = str(uuid.uuid4())
        self._intent_repo.claim_intent_lease(
            complaint_id, intent_id, lease_token, LEASE_DURATION_SECONDS, now
        )

        if self._confirmation_worker is not None:
            return self._confirm_with_worker(
                complaint_id=complaint_id,
                intent_id=intent_id,
                intent=intent,
                lease_token=lease_token,
                now=now,
            )

        # Legacy in-memory confirmation flow (default local backend)
        meta = self._s3.head_object(self._bucket, intent.staging_key)
        if meta is None:
            attempts = self._intent_repo.record_missing_staging_attempt(
                complaint_id, intent_id, MAX_CONFIRM_RETRIES, now
            )
            if attempts >= MAX_CONFIRM_RETRIES:
                raise UploadFailedError(
                    f"Exceeded maximum confirmation retry attempts ({MAX_CONFIRM_RETRIES}) without staging object present."
                )
            raise UploadNotReadyError(
                f"Upload not found in staging; upload may still be in progress or failed (attempt {attempts}/{MAX_CONFIRM_RETRIES})."
            )

        # Validate size bounds
        if meta.content_length < 1024 or meta.content_length > intent.max_byte_size:
            self._s3.delete_object(self._bucket, intent.staging_key)
            self._intent_repo.mark_intent_failed(
                complaint_id, intent_id, "Upload size outside permitted bounds", now, lease_token
            )
            raise ValidationError(
                f"Uploaded object size ({meta.content_length} bytes) does not match declared bounds (max: {intent.max_byte_size})."
            )

        # Validate cryptographic SHA-256
        if meta.sha256_checksum.lower() != intent.expected_sha256.lower():
            self._s3.delete_object(self._bucket, intent.staging_key)
            self._intent_repo.mark_intent_failed(
                complaint_id, intent_id, "Cryptographic SHA-256 checksum verification failed", now, lease_token
            )
            raise ValidationError(
                f"Uploaded object SHA-256 '{meta.sha256_checksum}' does not match expected '{intent.expected_sha256}'."
            )

        # Validate binary magic bytes from the first 12 bytes
        header_bytes = self._s3.get_object_range(self._bucket, intent.staging_key, 0, 11)
        try:
            validate_magic_bytes(header_bytes, intent.expected_mime)
        except ValidationError as err:
            self._s3.delete_object(self._bucket, intent.staging_key)
            self._intent_repo.mark_intent_failed(
                complaint_id, intent_id, str(err), now, lease_token
            )
            raise

        # Conditional copy to destination key
        self._s3.copy_object(
            source_bucket=self._bucket,
            source_key=intent.staging_key,
            dest_bucket=self._bucket,
            dest_key=intent.final_key,
            if_none_match="*",
        )

        # Atomic dual-item commit (Complaint metadata + Intent status)
        self._intent_repo.commit_verified_attachment_atomic(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token=lease_token,
            final_key=intent.final_key,
            staging_sha256=meta.sha256_checksum,
            staging_size=meta.content_length,
            now=now,
        )

        # Best-effort staging cleanup (failure does not invalidate commit)
        try:
            self._s3.delete_object(self._bucket, intent.staging_key)
        except Exception as err:
            logger.warning(
                "Best-effort deletion of staging object '%s' failed: %s", intent.staging_key, err
            )

        return {
            "status": "VERIFIED",
            "attachmentKey": intent.final_key,
        }

    def get_download_url(self, complaint_id: str, actor: Actor) -> dict[str, Any]:
        """Generate a short-lived presigned GET URL to view a verified attachment."""
        if not isinstance(actor, Actor):
            raise UnauthorizedError("Operation requires an authenticated Actor.")

        complaint = self._get_complaint(complaint_id)
        if complaint is None:
            raise ComplaintNotFoundError(f"Complaint '{complaint_id}' was not found.")

        if not complaint.attachment_key or complaint.attachment_status != "VERIFIED":
            raise ValidationError(
                f"Complaint '{complaint_id}' has no verified image attachment."
            )

        download_url = self._s3.generate_presigned_get(
            bucket=self._bucket,
            key=complaint.attachment_key,
            expires_in=UPLOAD_PRESIGNED_EXPIRY_SECONDS,
        )
        now = self._now_fn()
        expires_at = now + timedelta(seconds=UPLOAD_PRESIGNED_EXPIRY_SECONDS)

        return {
            "downloadUrl": download_url,
            "expiresAt": expires_at.isoformat(),
        }
