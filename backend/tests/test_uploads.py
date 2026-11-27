"""Unit tests for UploadIntentService and S3 storage abstraction in MAIT CampusFix."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import threading
from typing import Any
import unittest

from campusfix.domain.models import (
    Actor,
    Complaint,
    ComplaintStatus,
    IssueCategory,
    Role,
    UploadIntentStatus,
)
from campusfix.domain.rules import (
    ComplaintNotFoundError,
    DestinationConflictError,
    StaleLeaseError,
    UnauthorizedError,
    UploadFailedError,
    UploadIntentExpiredError,
    UploadIntentNotFoundError,
    UploadNotReadyError,
    ValidationError,
)
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.s3_storage import InMemoryS3StorageService
from campusfix.services.uploads import (
    UploadIntentService,
    normalize_sha256,
    validate_magic_bytes,
)


class TestUploadIntentService(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.s3 = InMemoryS3StorageService(region="ap-southeast-2")
        self.bucket = "test-attachments-bucket"
        self.service = UploadIntentService(
            complaint_repository=self.repo,
            intent_repository=self.repo,
            s3_storage=self.s3,
            bucket_name=self.bucket,
            region="ap-southeast-2",
        )

        self.student = Actor(user_id="std_101", role=Role.STUDENT)
        self.other_student = Actor(user_id="std_102", role=Role.STUDENT)
        self.maintenance = Actor(user_id="maint_01", role=Role.MAINTENANCE)
        self.admin = Actor(user_id="admin_01", role=Role.ADMIN)

        # Create baseline complaint
        self.complaint = Complaint(
            id="cmp_test_001",
            title="Broken window in lab",
            description="Glass cracked in computer lab 3",
            building="Block A",
            floor="2nd Floor",
            room_number="204",
            category=IssueCategory.CIVIL_STRUCTURAL,
            status=ComplaintStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
            submitted_by=self.student.user_id,
            upvotes=[],
            version=1,
        )
        self.repo.save(self.complaint)

    # --- Checksum & Magic Bytes Utility Tests ---

    def test_normalize_sha256_valid_hex(self) -> None:
        raw_hex = "A" * 64
        normalized = normalize_sha256(raw_hex)
        self.assertEqual(normalized, "a" * 64)

    def test_normalize_sha256_valid_base64(self) -> None:
        raw_bytes = b"k" * 32
        import base64
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        normalized = normalize_sha256(b64)
        self.assertEqual(normalized, raw_bytes.hex())

    def test_normalize_sha256_invalid_format(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_sha256("short-hash")
        with self.assertRaises(ValidationError):
            normalize_sha256(12345)  # type: ignore

    def test_validate_magic_bytes(self) -> None:
        # JPEG
        validate_magic_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg")
        with self.assertRaises(ValidationError):
            validate_magic_bytes(b"NOT_A_JPEG_FILE", "image/jpeg")

        # PNG
        validate_magic_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png")
        with self.assertRaises(ValidationError):
            validate_magic_bytes(b"NOT_A_PNG_IMAGE", "image/png")

        # WebP
        validate_magic_bytes(b"RIFF\x20\x00\x00\x00WEBPVP8 ", "image/webp")
        with self.assertRaises(ValidationError):
            validate_magic_bytes(b"RIFF\x20\x00\x00\x00AVI VP8 ", "image/webp")

        # Unsupported
        with self.assertRaises(ValidationError):
            validate_magic_bytes(b"\x00\x00\x00\x00", "image/gif")

    # --- Create Upload Intent Tests ---

    def test_create_upload_intent_success(self) -> None:
        sha256_hex = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=2048,
            checksum_sha256=sha256_hex,
            actor=self.student,
        )

        self.assertIn("intentId", res)
        self.assertIn("uploadUrl", res)
        self.assertIn("fields", res)
        self.assertIn("uploadExpiresAt", res)
        self.assertIn("confirmationDeadline", res)
        self.assertIn(self.bucket, res["uploadUrl"])
        expected_b64 = base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii")
        self.assertEqual(res["fields"]["Content-Type"], "image/jpeg")
        self.assertEqual(res["fields"]["x-amz-checksum-sha256"], expected_b64)

        intent = self.repo.get_intent(self.complaint.id, res["intentId"])
        self.assertIsNotNone(intent)
        self.assertEqual(intent.expected_sha256, sha256_hex)
        self.assertEqual(intent.status, UploadIntentStatus.PENDING_UPLOAD)
        self.assertEqual(intent.owner_id, self.student.user_id)
        self.assertTrue(intent.staging_key.startswith(f"staging/complaints/{self.complaint.id}/"))
        self.assertTrue(intent.final_key.endswith(".jpg"))

    def test_create_upload_intent_unauthorized_non_student(self) -> None:
        sha256_hex = "a" * 64
        with self.assertRaises(UnauthorizedError):
            self.service.create_upload_intent(
                complaint_id=self.complaint.id,
                content_type="image/png",
                byte_size=2048,
                checksum_sha256=sha256_hex,
                actor=self.maintenance,
            )
        with self.assertRaises(UnauthorizedError):
            self.service.create_upload_intent(
                complaint_id=self.complaint.id,
                content_type="image/png",
                byte_size=2048,
                checksum_sha256=sha256_hex,
                actor=self.admin,
            )

    def test_create_upload_intent_not_owner(self) -> None:
        sha256_hex = "a" * 64
        with self.assertRaises(UnauthorizedError):
            self.service.create_upload_intent(
                complaint_id=self.complaint.id,
                content_type="image/png",
                byte_size=2048,
                checksum_sha256=sha256_hex,
                actor=self.other_student,
            )

    def test_create_upload_intent_complaint_not_found(self) -> None:
        sha256_hex = "a" * 64
        with self.assertRaises(ComplaintNotFoundError):
            self.service.create_upload_intent(
                complaint_id="non_existent_cmp",
                content_type="image/jpeg",
                byte_size=2048,
                checksum_sha256=sha256_hex,
                actor=self.student,
            )

    def test_create_upload_intent_resolved_complaint(self) -> None:
        # Move complaint to resolved
        resolved_cmp = Complaint(
            id="cmp_resolved_001",
            title="Resolved issue",
            description="Already fixed",
            building="Block B",
            floor="1st Floor",
            room_number="101",
            category=IssueCategory.ELECTRICAL,
            status=ComplaintStatus.RESOLVED,
            submitted_at=datetime.now(timezone.utc),
            submitted_by=self.student.user_id,
            upvotes=[],
            version=2,
        )
        self.repo.save(resolved_cmp)

        sha256_hex = "a" * 64
        with self.assertRaises(ValidationError):
            self.service.create_upload_intent(
                complaint_id=resolved_cmp.id,
                content_type="image/jpeg",
                byte_size=2048,
                checksum_sha256=sha256_hex,
                actor=self.student,
            )

    def test_create_upload_intent_invalid_mime(self) -> None:
        sha256_hex = "a" * 64
        for bad_mime in ["image/gif", "application/pdf", "text/plain"]:
            with self.assertRaises(ValidationError):
                self.service.create_upload_intent(
                    complaint_id=self.complaint.id,
                    content_type=bad_mime,
                    byte_size=2048,
                    checksum_sha256=sha256_hex,
                    actor=self.student,
                )

    def test_create_upload_intent_invalid_size(self) -> None:
        sha256_hex = "a" * 64
        # Too small (< 1 KB)
        with self.assertRaises(ValidationError):
            self.service.create_upload_intent(
                complaint_id=self.complaint.id,
                content_type="image/png",
                byte_size=500,
                checksum_sha256=sha256_hex,
                actor=self.student,
            )
        # Too large (> 5 MB)
        with self.assertRaises(ValidationError):
            self.service.create_upload_intent(
                complaint_id=self.complaint.id,
                content_type="image/png",
                byte_size=6 * 1024 * 1024,
                checksum_sha256=sha256_hex,
                actor=self.student,
            )

    # --- Confirm Upload Tests ---

    def test_confirm_upload_success_jpeg(self) -> None:
        # Setup valid JPEG payload
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(jpeg_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(jpeg_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        # Simulate user uploading to S3 staging
        self.s3.put_object(self.bucket, intent.staging_key, jpeg_payload, "image/jpeg")

        # Confirm upload
        res = self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        self.assertEqual(res["status"], "VERIFIED")
        self.assertEqual(res["attachmentKey"], intent.final_key)

        # Staging object must be deleted
        self.assertIsNone(self.s3.head_object(self.bucket, intent.staging_key))

        # Final object must exist in S3
        final_meta = self.s3.head_object(self.bucket, intent.final_key)
        self.assertIsNotNone(final_meta)
        self.assertEqual(final_meta.content_length, len(jpeg_payload))

        # Intent must be marked VERIFIED
        updated_intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(updated_intent)
        self.assertEqual(updated_intent.status, UploadIntentStatus.VERIFIED)

        # Complaint must have attachment metadata committed
        updated_cmp = self.repo.get(self.complaint.id)
        self.assertIsNotNone(updated_cmp)
        self.assertEqual(updated_cmp.attachment_key, intent.final_key)
        self.assertEqual(updated_cmp.attachment_status, "VERIFIED")

        # Idempotent re-confirmation returns success
        re_confirm = self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        self.assertEqual(re_confirm["status"], "VERIFIED")
        self.assertEqual(re_confirm["attachmentKey"], intent.final_key)

    def test_confirm_upload_success_png(self) -> None:
        png_payload = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 2000
        sha256_hex = hashlib.sha256(png_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/png",
            byte_size=len(png_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, png_payload, "image/png")
        res = self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        self.assertEqual(res["status"], "VERIFIED")

    def test_confirm_upload_success_webp(self) -> None:
        webp_payload = b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 1500
        sha256_hex = hashlib.sha256(webp_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/webp",
            byte_size=len(webp_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, webp_payload, "image/webp")
        res = self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        self.assertEqual(res["status"], "VERIFIED")

    def test_confirm_upload_not_owner(self) -> None:
        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=2048,
            checksum_sha256="a" * 64,
            actor=self.student,
        )
        with self.assertRaises(UnauthorizedError):
            self.service.confirm_upload(self.complaint.id, intent_res["intentId"], self.other_student)

    def test_confirm_upload_missing_staging_retries_and_exhaustion(self) -> None:
        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=2048,
            checksum_sha256="a" * 64,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]

        # Attempts 1 to 4 should raise UploadNotReadyError
        for i in range(1, 5):
            with self.assertRaises(UploadNotReadyError):
                self.service.confirm_upload(self.complaint.id, intent_id, self.student)
            intent = self.repo.get_intent(self.complaint.id, intent_id)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.confirm_attempts, i)

        # Attempt 5 should raise UploadFailedError and mark intent failed
        with self.assertRaises(UploadFailedError):
            self.service.confirm_upload(self.complaint.id, intent_id, self.student)

        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.status, UploadIntentStatus.FAILED)

        # Subsequent confirm on failed intent raises UploadFailedError immediately
        with self.assertRaises(UploadFailedError):
            self.service.confirm_upload(self.complaint.id, intent_id, self.student)

    def test_confirm_upload_confirmation_deadline_expired(self) -> None:
        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=2048,
            checksum_sha256="a" * 64,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        # Artificially expire confirmation deadline
        expired_intent = intent.from_dict({
            **intent.to_dict(),
            "confirmationDeadline": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        })
        self.repo._intents[(self.complaint.id, intent_id)] = expired_intent

        with self.assertRaises(UploadIntentExpiredError):
            self.service.confirm_upload(self.complaint.id, intent_id, self.student)

        updated_intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(updated_intent)
        self.assertEqual(updated_intent.status, UploadIntentStatus.FAILED)

    def test_confirm_upload_checksum_mismatch(self) -> None:
        declared_hash = "a" * 64
        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=2048,
            checksum_sha256=declared_hash,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        # Upload bytes whose hash does NOT match declared_hash
        actual_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        self.s3.put_object(self.bucket, intent.staging_key, actual_payload, "image/jpeg")

        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        self.assertIn("does not match expected", str(ctx.exception))

        # Verify staging object is removed and intent is marked FAILED
        self.assertIsNone(self.s3.head_object(self.bucket, intent.staging_key))
        updated_intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(updated_intent)
        self.assertEqual(updated_intent.status, UploadIntentStatus.FAILED)

    def test_confirm_upload_magic_bytes_mismatch(self) -> None:
        payload = b"NOT_JPEG_BYTES" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, payload, "image/jpeg")

        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        self.assertIn("magic bytes mismatch", str(ctx.exception))

        # Verify staging object removed and intent marked FAILED
        self.assertIsNone(self.s3.head_object(self.bucket, intent.staging_key))
        updated_intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(updated_intent)
        self.assertEqual(updated_intent.status, UploadIntentStatus.FAILED)

    def test_confirm_upload_stale_lease_token_rejection(self) -> None:
        payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        now = datetime.now(timezone.utc)
        self.repo.claim_intent_lease(self.complaint.id, intent_id, "token_1", 30, now)

        # Trying to commit with token_2 must fail with StaleLeaseError
        with self.assertRaises(StaleLeaseError):
            self.repo.commit_verified_attachment_atomic(
                complaint_id=self.complaint.id,
                intent_id=intent_id,
                lease_token="token_2",
                final_key="dest_key",
                staging_sha256=sha256_hex,
                staging_size=len(payload),
                now=now,
            )

    def test_confirm_upload_destination_conflict(self) -> None:
        payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, payload, "image/jpeg")
        # Put an object at the destination key beforehand
        self.s3.put_object(self.bucket, intent.final_key, b"already_exists", "image/jpeg")

        with self.assertRaises(DestinationConflictError):
            self.service.confirm_upload(self.complaint.id, intent_id, self.student)

    # --- Get Download URL Tests ---

    def test_get_download_url_success(self) -> None:
        # Commit verified attachment to complaint
        cmp_with_att = self.complaint.from_dict({
            **self.complaint.to_dict(),
            "attachmentKey": f"complaints/{self.complaint.id}/images/verified.jpg",
            "attachmentStatus": "VERIFIED",
        })
        self.repo.update(cmp_with_att, expected_version=self.complaint.version)

        # Authorized student can get download URL
        res_std = self.service.get_download_url(self.complaint.id, self.student)
        self.assertIn("downloadUrl", res_std)
        self.assertIn("expiresAt", res_std)
        self.assertIn(self.bucket, res_std["downloadUrl"])

        # Maintenance can also get download URL
        res_maint = self.service.get_download_url(self.complaint.id, self.maintenance)
        self.assertIn("downloadUrl", res_maint)

        # Admin can also get download URL
        res_admin = self.service.get_download_url(self.complaint.id, self.admin)
        self.assertIn("downloadUrl", res_admin)

    def test_get_download_url_unverified_or_missing(self) -> None:
        # Complaint without attachment
        with self.assertRaises(ValidationError):
            self.service.get_download_url(self.complaint.id, self.student)

        # Complaint with unverified attachment
        unverified_cmp = self.complaint.from_dict({
            **self.complaint.to_dict(),
            "attachmentKey": "some/key.jpg",
            "attachmentStatus": "PENDING_UPLOAD",
        })
        self.repo.update(unverified_cmp, expected_version=self.complaint.version)

        with self.assertRaises(ValidationError):
            self.service.get_download_url(self.complaint.id, self.student)

    def test_get_download_url_complaint_not_found(self) -> None:
        with self.assertRaises(ComplaintNotFoundError):
            self.service.get_download_url("non_existent_cmp", self.student)

    # --- Upload-Intent Concurrency & Recovery Tests (Correction C) ---

    def test_active_lease_contention(self) -> None:
        """Test 1: Worker B cannot acquire an unexpired active lease held by Worker A."""
        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=2048,
            checksum_sha256="a" * 64,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        now = datetime.now(timezone.utc)

        # Worker A successfully claims active lease
        token_a = "worker-a-token"
        self.repo.claim_intent_lease(self.complaint.id, intent_id, token_a, 30, now)

        # Worker B attempts to claim the same intent while lease is active
        token_b = "worker-b-token"
        with self.assertRaises(StaleLeaseError) as ctx:
            self.repo.claim_intent_lease(self.complaint.id, intent_id, token_b, 30, now)
        self.assertIn("Another worker holds an active lease", str(ctx.exception))

        # Worker A's lease remains valid and unchanged
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.lease_token, token_a)

    def test_expired_lease_takeover(self) -> None:
        """Test 2: Worker B safely acquires an expired lease; Worker A's old token is rejected."""
        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=2048,
            checksum_sha256="a" * 64,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        now = datetime.now(timezone.utc)

        # Worker A claims lease with 30s duration
        token_a = "worker-a-token"
        self.repo.claim_intent_lease(self.complaint.id, intent_id, token_a, 30, now)

        # Advance fake clock beyond Worker A's lease expiration (+35s)
        later = now + timedelta(seconds=35)

        # Worker B acquires the lease after expiry
        token_b = "worker-b-token"
        self.repo.claim_intent_lease(self.complaint.id, intent_id, token_b, 30, later)

        # Worker B holds active lease
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.lease_token, token_b)

        # Worker A attempts to commit with expired/stale token_a -> must be rejected
        with self.assertRaises(StaleLeaseError):
            self.repo.commit_verified_attachment_atomic(
                complaint_id=self.complaint.id,
                intent_id=intent_id,
                lease_token=token_a,
                final_key="dest_key",
                staging_sha256="a" * 64,
                staging_size=2048,
                now=later,
            )

    def test_simultaneous_confirmation_concurrency(self) -> None:
        """Test 3: Simultaneous confirmation threads do not produce inconsistent or conflicting commits."""
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(jpeg_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(jpeg_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        # Place staging payload
        self.s3.put_object(self.bucket, intent.staging_key, jpeg_payload, "image/jpeg")

        barrier = threading.Barrier(2)
        results: list[dict[str, Any]] = []
        errors: list[Exception] = []

        def worker_task() -> None:
            try:
                barrier.wait(timeout=5)
                res = self.service.confirm_upload(self.complaint.id, intent_id, self.student)
                results.append(res)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker_task)
        t2 = threading.Thread(target=worker_task)

        t1.start()
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        # At least one worker succeeded
        self.assertGreaterEqual(len(results), 1)
        for res in results:
            self.assertEqual(res["status"], "VERIFIED")
            self.assertEqual(res["attachmentKey"], intent.final_key)

        # Any error raised must strictly be StaleLeaseError (loss of race on lease claim)
        for err in errors:
            self.assertIsInstance(err, StaleLeaseError)

        # Final complaint and intent states must be completely consistent
        final_cmp = self.repo.get(self.complaint.id)
        self.assertIsNotNone(final_cmp)
        self.assertEqual(final_cmp.attachment_key, intent.final_key)
        self.assertEqual(final_cmp.attachment_status, "VERIFIED")

        final_intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(final_intent)
        self.assertEqual(final_intent.status, UploadIntentStatus.VERIFIED)

    def test_atomic_commit_failure(self) -> None:
        """Test 4: Error during atomic commit is safely bubbled and avoids partial verified state."""
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(jpeg_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(jpeg_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, jpeg_payload, "image/jpeg")

        # Inject repository failure during atomic commit
        def failing_commit(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Simulated database failure during atomic attachment commit.")

        original_commit = self.repo.commit_verified_attachment_atomic
        self.repo.commit_verified_attachment_atomic = failing_commit  # type: ignore

        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.service.confirm_upload(self.complaint.id, intent_id, self.student)
            self.assertIn("Simulated database failure", str(ctx.exception))
        finally:
            self.repo.commit_verified_attachment_atomic = original_commit

        # Intent must not be marked VERIFIED
        intent_after = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent_after)
        self.assertNotEqual(intent_after.status, UploadIntentStatus.VERIFIED)

    def test_attachment_invisibility_after_commit_failure(self) -> None:
        """Test 5: Explicitly asserts complaint attachment fields remain unset and undownloadable after commit failure."""
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(jpeg_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(jpeg_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, jpeg_payload, "image/jpeg")

        # Inject repository failure during commit
        def failing_commit(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Simulated DB connection drop")

        original_commit = self.repo.commit_verified_attachment_atomic
        self.repo.commit_verified_attachment_atomic = failing_commit  # type: ignore

        try:
            with self.assertRaises(RuntimeError):
                self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        finally:
            self.repo.commit_verified_attachment_atomic = original_commit

        # Complaint attachment fields MUST remain unset (invisible)
        cmp_after = self.repo.get(self.complaint.id)
        self.assertIsNotNone(cmp_after)
        self.assertIsNone(cmp_after.attachment_key)
        self.assertIsNone(cmp_after.attachment_status)

        # Generating a download URL must be strictly rejected
        with self.assertRaises(ValidationError) as dl_ctx:
            self.service.get_download_url(self.complaint.id, self.student)
        self.assertIn("has no verified image attachment", str(dl_ctx.exception))

    def test_staging_cleanup_failure_resilience(self) -> None:
        """Test 6: S3 staging deletion failure does not invalidate commit; safely logged."""
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(jpeg_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(jpeg_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, jpeg_payload, "image/jpeg")

        # Make s3.delete_object raise an exception for staging key
        def failing_delete(bucket: str, key: str) -> None:
            if key == intent.staging_key:
                raise RuntimeError("Simulated S3 timeout during staging cleanup")
            self.s3._objects.pop((bucket, key), None)

        original_delete = self.s3.delete_object
        self.s3.delete_object = failing_delete  # type: ignore

        try:
            with self.assertLogs("campusfix.services.uploads", level="WARNING") as log_capture:
                res = self.service.confirm_upload(self.complaint.id, intent_id, self.student)

            # Confirmation operation still succeeds
            self.assertEqual(res["status"], "VERIFIED")
            self.assertEqual(res["attachmentKey"], intent.final_key)

            # Safe warning was emitted
            self.assertTrue(any("Best-effort deletion of staging object" in msg for msg in log_capture.output))
        finally:
            self.s3.delete_object = original_delete

        # Attachment is securely committed
        cmp_after = self.repo.get(self.complaint.id)
        self.assertIsNotNone(cmp_after)
        self.assertEqual(cmp_after.attachment_status, "VERIFIED")
        self.assertEqual(cmp_after.attachment_key, intent.final_key)

    def test_interrupted_worker_recovery(self) -> None:
        """Test 7: Interrupted worker left in CONFIRMING is recovered by next worker after lease expires."""
        jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1500
        sha256_hex = hashlib.sha256(jpeg_payload).hexdigest()

        intent_res = self.service.create_upload_intent(
            complaint_id=self.complaint.id,
            content_type="image/jpeg",
            byte_size=len(jpeg_payload),
            checksum_sha256=sha256_hex,
            actor=self.student,
        )
        intent_id = intent_res["intentId"]
        intent = self.repo.get_intent(self.complaint.id, intent_id)
        self.assertIsNotNone(intent)

        self.s3.put_object(self.bucket, intent.staging_key, jpeg_payload, "image/jpeg")

        # Worker 1 claimed lease 40 seconds ago (lease expired, duration=30s) but died before completing
        t0 = datetime.now(timezone.utc) - timedelta(seconds=40)
        token_worker_1 = "worker-1-crashed-token"
        self.repo.claim_intent_lease(self.complaint.id, intent_id, token_worker_1, 30, t0)

        # Worker 2 invokes confirm_upload now
        res_worker_2 = self.service.confirm_upload(self.complaint.id, intent_id, self.student)
        self.assertEqual(res_worker_2["status"], "VERIFIED")
        self.assertEqual(res_worker_2["attachmentKey"], intent.final_key)

        # Stale Worker 1 attempts to commit after takeover -> rejected
        with self.assertRaises(StaleLeaseError):
            self.repo.commit_verified_attachment_atomic(
                complaint_id=self.complaint.id,
                intent_id=intent_id,
                lease_token=token_worker_1,
                final_key="stale_key",
                staging_sha256=sha256_hex,
                staging_size=len(jpeg_payload),
                now=datetime.now(timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
