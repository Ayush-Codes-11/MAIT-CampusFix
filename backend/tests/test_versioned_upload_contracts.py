"""Stage B2 Step 1 contract tests: domain records, repository contracts, and adapter.

Covers:
- format_canonical_utc_timestamp (27-char UTC format, conversion, microsecond preservation, naive/invalid rejection)
- UploadIntent new fields (source_version_id, updated_at, intent_pk/intent_sk)
- UploadAttempt / TagOutboxRecord / AtomicAttachmentCommitResult domain validation & keys
- InMemoryComplaintRepository Stage B2 contracts:
  * get_consistent / get_intent_consistent
  * pin_source_version (lease-fenced, now >= lease_expires_at boundary, idempotency, immutability)
  * begin_copy_attempt (lease-fenced, intent & attempt immutability, CW-2 prior attempt retention)
  * get_attempt / list_attempts
  * commit_verified_attachment_versioned (Actions 1-4 conditions, idempotent replay, zero partial mutation)
  * Outbox CRUD (get, sorted list + limit, increment_sweep_attempt, delete)
- RepositoryVersionedUploadStateAdapter (port wiring, UploadAttempt verification without persisting
  expected_complaint_version, ImmutableFieldViolationError -> CopyVerificationError translation,
  pre-copy complaint.version capture even if complaint updates concurrently)
- UploadConfirmationWorker wired via adapter to InMemoryComplaintRepository + Botocore Stubber
  (unpinned source discovery + commit, and CW-2 lease takeover + 412 recovery preserving audit trail)
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import unittest

import boto3
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import Stubber

from campusfix.domain.models import (
    AtomicAttachmentCommitResult,
    Complaint,
    ComplaintStatus,
    IssueCategory,
    TagOutboxRecord,
    UploadAttempt,
    UploadAttemptStatus,
    UploadIntent,
    UploadIntentStatus,
    ValidationError,
    format_canonical_utc_timestamp,
)
from campusfix.domain.rules import (
    ComplaintNotFoundError,
    ImmutableFieldViolationError,
    OptimisticLockError,
    StaleLeaseError,
    UploadFailedError,
    UploadIntentNotFoundError,
)
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.boto3_s3_storage import (
    Boto3S3StorageService,
    CopyVerificationError,
)
from campusfix.services.repository_state_adapter import RepositoryVersionedUploadStateAdapter
from campusfix.services.upload_confirmation_worker import (
    CopyAttemptRecord,
    UploadConfirmationWorker,
)

_DUMMY_AWS_CREDS = {
    "aws_access_key_id": "test-access-key",
    "aws_secret_access_key": "test-secret-key",
    "region_name": "ap-southeast-2",
}

_SHA256 = "a" * 64


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


def _make_jpeg(size: int = 2048, fill: bytes = b"J") -> bytes:
    return b"\xff\xd8\xff\xe0" + (fill * (size - 4))


def _sha256_hex_and_b64(data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).digest()
    return digest.hex().lower(), base64.b64encode(digest).decode("ascii")


def _make_complaint(cid: str = "CF-2026-0001", version: int = 1) -> Complaint:
    return Complaint(
        id=cid,
        title="Broken lab ceiling fan",
        description="A sufficiently long description for the domain model validation.",
        building="Block A",
        floor="1",
        room_number="101",
        category=IssueCategory.ELECTRICAL,
        status=ComplaintStatus.PENDING,
        submitted_at=_utc(2026, 1, 1),
        submitted_by="student-1",
        version=version,
    )


def _make_intent(
    complaint_id: str = "CF-2026-0001",
    intent_id: str = "I1",
    status: UploadIntentStatus = UploadIntentStatus.PENDING_UPLOAD,
    lease_token: str | None = None,
    lease_expires_at: datetime | None = None,
    source_version_id: str | None = None,
    expected_sha256: str = _SHA256,
    expected_mime: str = "image/jpeg",
    max_byte_size: int = 1_000_000,
) -> UploadIntent:
    now = _utc(2026, 1, 1)
    return UploadIntent(
        intent_id=intent_id,
        complaint_id=complaint_id,
        owner_id="student-1",
        staging_key=f"staging/complaints/{complaint_id}/{intent_id}/upload.tmp",
        final_key=f"complaints/{complaint_id}/images/{intent_id}.jpg",
        expected_mime=expected_mime,
        max_byte_size=max_byte_size,
        expected_sha256=expected_sha256,
        status=status,
        created_at=now,
        upload_expires_at=now + timedelta(minutes=5),
        confirmation_deadline=now + timedelta(minutes=20),
        recovery_deadline=now + timedelta(hours=2),
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        source_version_id=source_version_id,
    )


def _seed_complaint_and_intent(
    repo: InMemoryComplaintRepository,
    complaint_id: str = "CF-2026-0001",
    intent_id: str = "I1",
    intent_status: UploadIntentStatus = UploadIntentStatus.CONFIRMING,
    lease_token: str | None = "tok-1",
    lease_expires_at: datetime | None = None,
    source_version_id: str | None = None,
    expected_sha256: str = _SHA256,
    expected_mime: str = "image/jpeg",
    max_byte_size: int = 1_000_000,
    complaint_version: int = 1,
) -> tuple[Complaint, UploadIntent]:
    if lease_expires_at is None:
        lease_expires_at = _utc(2026, 1, 2)
    complaint = _make_complaint(complaint_id, version=complaint_version)
    repo.seed(complaint)
    intent = _make_intent(
        complaint_id=complaint_id,
        intent_id=intent_id,
        status=intent_status,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        source_version_id=source_version_id,
        expected_sha256=expected_sha256,
        expected_mime=expected_mime,
        max_byte_size=max_byte_size,
    )
    repo.create_intent(intent)
    return complaint, intent


# ===========================================================================
# 1. Canonical UTC timestamp formatting
# ===========================================================================

class TestFormatCanonicalUtcTimestamp(unittest.TestCase):

    def test_produces_27_char_string_with_microseconds(self) -> None:
        dt = _utc(2026, 6, 15, 12, 30, 45, 123456)
        result = format_canonical_utc_timestamp(dt)
        self.assertEqual(len(result), 27)
        self.assertEqual(result, "2026-06-15T12:30:45.123456Z")

    def test_converts_non_utc_offset_to_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime(2026, 1, 1, 10, 30, 0, 500000, tzinfo=ist)
        result = format_canonical_utc_timestamp(dt_ist)
        self.assertEqual(len(result), 27)
        self.assertEqual(result, "2026-01-01T05:00:00.500000Z")

    def test_naive_datetime_and_non_datetime_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            format_canonical_utc_timestamp(datetime(2026, 1, 1, 12, 0, 0))
        with self.assertRaises(ValidationError):
            format_canonical_utc_timestamp("2026-01-01T00:00:00Z")  # type: ignore[arg-type]

    def test_zero_and_max_microseconds_preserved(self) -> None:
        self.assertEqual(
            format_canonical_utc_timestamp(_utc(2026, 1, 1, 0, 0, 0, 0)),
            "2026-01-01T00:00:00.000000Z",
        )
        self.assertEqual(
            format_canonical_utc_timestamp(_utc(2026, 12, 31, 23, 59, 59, 999999)),
            "2026-12-31T23:59:59.999999Z",
        )


# ===========================================================================
# 2. UploadIntent new fields and DynamoDB key builders
# ===========================================================================

class TestUploadIntentContracts(unittest.TestCase):

    def test_intent_pk_and_sk_match_spec(self) -> None:
        self.assertEqual(UploadIntent.intent_pk("CF-2026-abc"), "UPLOAD_INTENT#CF-2026-abc")
        self.assertEqual(UploadIntent.intent_sk("uuid-1"), "INTENT#uuid-1")

    def test_version_and_updated_at_roundtrip(self) -> None:
        intent = _make_intent(source_version_id="vid-123")
        intent.updated_at = _utc(2026, 6, 1, 10, 0, 0)
        d = intent.to_dict()
        self.assertEqual(d["sourceVersionId"], "vid-123")
        self.assertNotIn("destinationVersionId", d)
        self.assertFalse(hasattr(intent, "destination_version_id"))
        restored = UploadIntent.from_dict(d)
        self.assertEqual(restored.source_version_id, "vid-123")
        self.assertEqual(restored.updated_at, _utc(2026, 6, 1, 10, 0, 0))

    def test_legacy_dict_without_new_fields_deserializes(self) -> None:
        intent = _make_intent()
        legacy = intent.to_dict()
        legacy.pop("sourceVersionId")
        legacy.pop("updatedAt")
        restored = UploadIntent.from_dict(legacy)
        self.assertIsNone(restored.source_version_id)
        self.assertIsNone(restored.updated_at)

    def test_invalid_source_version_id_rejected(self) -> None:
        base = _make_intent().to_dict()
        for bad in ("", "   ", "null", 123):
            bad_src = {**base, "sourceVersionId": bad}
            with self.assertRaises(ValidationError):
                UploadIntent.from_dict(bad_src)

        with self.assertRaises(ValidationError):
            _make_intent(source_version_id="null")


# ===========================================================================
# 3. UploadAttempt / TagOutboxRecord / AtomicAttachmentCommitResult
# ===========================================================================

class TestDomainRecords(unittest.TestCase):

    def _make_attempt(self, **overrides) -> UploadAttempt:
        defaults = dict(
            complaint_id="CF-2026-0001",
            intent_id="I1",
            lease_token="tok-1",
            source_version_id="vid-1",
            sha256=_SHA256,
            content_type="image/jpeg",
            max_byte_size=1_000_000,
            final_key="complaints/CF-2026-0001/images/I1.jpg",
            status=UploadAttemptStatus.COPYING,
            created_at=_utc(2026, 1, 1),
            updated_at=_utc(2026, 1, 1),
        )
        defaults.update(overrides)
        return UploadAttempt(**defaults)

    def test_upload_attempt_spec_fields_exclude_complaint_version(self) -> None:
        attempt = self._make_attempt()
        self.assertFalse(hasattr(attempt, "expected_complaint_version"))
        self.assertEqual(UploadAttempt.attempt_pk("CF-2026-0001", "I1"), "ATTEMPT#CF-2026-0001#I1")
        self.assertEqual(UploadAttempt.attempt_sk("tok-1"), "LEASE#tok-1")

    def test_upload_attempt_validation(self) -> None:
        attempt = self._make_attempt(sha256="A" * 64)
        self.assertEqual(attempt.sha256, "a" * 64)

        for kw in (
            {"complaint_id": ""},
            {"lease_token": " "},
            {"source_version_id": "null"},
            {"destination_version_id": "null"},
            {"sha256": "a" * 63},
            {"sha256": "g" * 64},
            {"max_byte_size": -1},
            {"max_byte_size": True},
            {"status": "COPYING"},
            {"created_at": datetime(2026, 1, 1)},  # naive
        ):
            with self.assertRaises(ValidationError):
                self._make_attempt(**kw)

    def test_tag_outbox_record_keys_and_validation(self) -> None:
        rec = TagOutboxRecord(
            intent_id="I1",
            complaint_id="CF-2026-0001",
            s3_key="complaints/CF-2026-0001/images/I1.jpg",
            destination_version_id="dst-1",
            committed_at=_utc(2026, 1, 1),
        )
        self.assertEqual(rec.sweep_attempts, 0)
        self.assertEqual(TagOutboxRecord.outbox_pk(), "OUTBOX#TAG_PROMOTION")
        self.assertEqual(TagOutboxRecord.outbox_sk("I1"), "INTENT#I1")

        for kw in (
            {"intent_id": ""},
            {"destination_version_id": "null"},
            {"sweep_attempts": -1},
            {"sweep_attempts": True},
            {"committed_at": datetime(2026, 1, 1)},
        ):
            base = dict(
                intent_id="I1",
                complaint_id="CF-2026-0001",
                s3_key="k",
                destination_version_id="dst-1",
                committed_at=_utc(2026, 1, 1),
            )
            base.update(kw)
            with self.assertRaises(ValidationError):
                TagOutboxRecord(**base)

    def test_atomic_commit_result_is_frozen(self) -> None:
        c = _make_complaint()
        i = _make_intent()
        a = self._make_attempt(status=UploadAttemptStatus.COMMITTED, destination_version_id="dst-1")
        o = TagOutboxRecord(
            intent_id="I1",
            complaint_id="CF-2026-0001",
            s3_key="k",
            destination_version_id="dst-1",
            committed_at=_utc(),
        )
        res = AtomicAttachmentCommitResult(complaint=c, intent=i, attempt=a, outbox=o)
        self.assertIs(res.complaint, c)
        with self.assertRaises(AttributeError):
            res.complaint = c  # type: ignore[misc]


# ===========================================================================
# 4. Strongly-consistent reads & pin_source_version
# ===========================================================================

class TestConsistentReadsAndPinSourceVersion(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.now = _utc(2026, 1, 1, 12, 0, 0)
        self.lease_expires = _utc(2026, 1, 1, 13, 0, 0)
        _seed_complaint_and_intent(
            self.repo,
            lease_token="tok-1",
            lease_expires_at=self.lease_expires,
        )

    def test_get_consistent_and_get_intent_consistent_return_clones(self) -> None:
        c1 = self.repo.get_consistent("CF-2026-0001")
        assert c1 is not None
        c1.title = "Mutated caller copy"
        self.assertEqual(self.repo.get("CF-2026-0001").title, "Broken lab ceiling fan")
        self.assertIsNone(self.repo.get_consistent("MISSING"))

        i1 = self.repo.get_intent_consistent("CF-2026-0001", "I1")
        assert i1 is not None
        i1.source_version_id = "mutated"
        self.assertIsNone(self.repo.get_intent("CF-2026-0001", "I1").source_version_id)
        self.assertIsNone(self.repo.get_intent_consistent("CF-2026-0001", "MISSING"))

    def test_pin_idempotent_immutable_and_lease_fenced(self) -> None:
        pinned = self.repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-abc", self.now)
        self.assertEqual(pinned.source_version_id, "vid-abc")
        self.assertEqual(pinned.updated_at, self.now)

        # Idempotent same version
        again = self.repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-abc", self.now + timedelta(seconds=5))
        self.assertEqual(again.source_version_id, "vid-abc")
        self.assertEqual(again.updated_at, self.now)

        # Conflicting version rejected
        with self.assertRaises(ImmutableFieldViolationError):
            self.repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-XYZ", self.now)

        # Decision 2 microsecond boundaries:
        # -1us is permitted for pin_source_version, exact lease_expires_at and +1us reject
        just_before = self.lease_expires - timedelta(microseconds=1)
        still_ok = self.repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-abc", just_before)
        self.assertEqual(still_ok.source_version_id, "vid-abc")
        with self.assertRaises(StaleLeaseError):
            self.repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-abc", self.lease_expires)
        with self.assertRaises(StaleLeaseError):
            self.repo.pin_source_version(
                "CF-2026-0001", "I1", "tok-1", "vid-abc", self.lease_expires + timedelta(microseconds=1)
            )

        with self.assertRaises(StaleLeaseError):
            self.repo.pin_source_version("CF-2026-0001", "I1", "WRONG", "vid-abc", self.now)
        with self.assertRaises(StaleLeaseError):
            self.repo.pin_source_version("CF-2026-0001", "I1", "", "vid-abc", self.now)

    def test_claim_intent_lease_takeover_exact_microsecond_boundary(self) -> None:
        lease_exp = _utc(2026, 1, 1, 0, 10, 0)
        _seed_complaint_and_intent(
            self.repo,
            complaint_id="CF-2026-0002",
            intent_id="I2",
            lease_token="tok-1",
            lease_expires_at=lease_exp,
        )
        with self.assertRaises(StaleLeaseError):
            self.repo.claim_intent_lease("CF-2026-0002", "I2", "tok-2", 60, lease_exp)
        taken = self.repo.claim_intent_lease(
            "CF-2026-0002", "I2", "tok-2", 60, lease_exp + timedelta(microseconds=1)
        )
        self.assertEqual(taken.lease_token, "tok-2")

    def test_pin_rejects_bad_inputs_and_non_confirming_status(self) -> None:
        for bad_vid in ("", "  ", "null"):
            with self.assertRaises(ValidationError):
                self.repo.pin_source_version("CF-2026-0001", "I1", "tok-1", bad_vid, self.now)

        with self.assertRaises(ValidationError):
            self.repo.pin_source_version(
                "CF-2026-0001", "I1", "tok-1", "vid-1", datetime(2026, 1, 1)
            )
        with self.assertRaises(UploadIntentNotFoundError):
            self.repo.pin_source_version("CF-2026-0001", "MISSING", "tok-1", "vid-1", self.now)

        self.repo.mark_intent_failed("CF-2026-0001", "I1", "aborted", self.now)
        with self.assertRaises(UploadFailedError):
            self.repo.pin_source_version("CF-2026-0001", "I1", "tok-1", "vid-1", self.now)


# ===========================================================================
# 5. begin_copy_attempt, get_attempt, list_attempts, CW-2 audit trail
# ===========================================================================

class TestBeginCopyAttemptAndCW2(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.now = _utc(2026, 1, 1, 0, 5, 0)
        self.lease_expires = _utc(2026, 1, 1, 0, 10, 0)
        _seed_complaint_and_intent(
            self.repo,
            lease_token="tok-1",
            lease_expires_at=self.lease_expires,
            source_version_id="vid-1",
        )

    def _begin(self, **overrides) -> UploadAttempt:
        defaults = dict(
            complaint_id="CF-2026-0001",
            intent_id="I1",
            lease_token="tok-1",
            source_version_id="vid-1",
            final_key="complaints/CF-2026-0001/images/I1.jpg",
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            now=self.now,
        )
        defaults.update(overrides)
        return self.repo.begin_copy_attempt(**defaults)

    def test_create_idempotency_and_mismatches(self) -> None:
        a1 = self._begin()
        self.assertEqual(a1.status, UploadAttemptStatus.COPYING)
        self.assertEqual(self._begin(), a1)

        # Normalizes base64 or uppercase hex consistently
        b64_sha = base64.b64encode(bytes.fromhex(_SHA256)).decode("ascii")
        self.assertEqual(self._begin(expected_sha256=b64_sha), a1)

        # Immutable mismatch vs Intent rejected even on first attempt or repeat
        for bad_kw in (
            {"source_version_id": "vid-other"},
            {"final_key": "complaints/CF-2026-0001/images/other.jpg"},
            {"expected_sha256": "b" * 64},
            {"expected_mime": "image/png"},
            {"max_byte_size": 999},
        ):
            with self.assertRaises(ImmutableFieldViolationError):
                self._begin(**bad_kw)

        # Lease fencing (Decision 2: -1us succeeds, exact lease_expires_at & +1us rejected)
        a_edge = self._begin(now=self.lease_expires - timedelta(microseconds=1))
        self.assertEqual(a_edge, a1)
        with self.assertRaises(StaleLeaseError):
            self._begin(now=self.lease_expires)
        with self.assertRaises(StaleLeaseError):
            self._begin(now=self.lease_expires + timedelta(microseconds=1))
        with self.assertRaises(StaleLeaseError):
            self._begin(lease_token="WRONG")

    def test_cw2_lease_takeover_preserves_prior_copying_attempt(self) -> None:
        # Worker 1 starts CopyObject attempt under tok-1 and crashes.
        a1 = self._begin(lease_token="tok-1")
        self.assertEqual(a1.status, UploadAttemptStatus.COPYING)

        # Lease expires; Worker 2 claims tok-2 and begins its own attempt.
        later = self.lease_expires + timedelta(seconds=10)
        claimed = self.repo.claim_intent_lease("CF-2026-0001", "I1", "tok-2", 60, later)
        self.assertEqual(claimed.lease_token, "tok-2")
        self.assertEqual(claimed.source_version_id, "vid-1")

        a2 = self._begin(lease_token="tok-2", now=later)
        self.assertEqual(a2.status, UploadAttemptStatus.COPYING)

        attempts = self.repo.list_attempts("CF-2026-0001", "I1")
        self.assertEqual([a.lease_token for a in attempts], ["tok-1", "tok-2"])
        self.assertTrue(all(a.status == UploadAttemptStatus.COPYING for a in attempts))

        # Worker 2 commits; Worker 1's COPYING audit record remains untouched; tok-1 fenced out.
        res = self.repo.commit_verified_attachment_versioned(
            complaint_id="CF-2026-0001",
            intent_id="I1",
            lease_token="tok-2",
            final_key="complaints/CF-2026-0001/images/I1.jpg",
            source_version_id="vid-1",
            destination_version_id="dst-vid-1",
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            expected_complaint_version=1,
            staging_sha256=_SHA256,
            staging_size=50_000,
            now=later,
        )
        self.assertEqual(res.attempt.lease_token, "tok-2")
        self.assertEqual(res.attempt.status, UploadAttemptStatus.COMMITTED)
        self.assertEqual(self.repo.get_attempt("CF-2026-0001", "I1", "tok-1").status, UploadAttemptStatus.COPYING)
        self.assertEqual(self.repo.get_attempt("CF-2026-0001", "I1", "tok-2").status, UploadAttemptStatus.COMMITTED)

        with self.assertRaises(StaleLeaseError):
            self.repo.commit_verified_attachment_versioned(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-1",
                final_key="complaints/CF-2026-0001/images/I1.jpg",
                source_version_id="vid-1",
                destination_version_id="dst-vid-1",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                expected_complaint_version=2,
                staging_sha256=_SHA256,
                staging_size=50_000,
                now=later,
            )


# ===========================================================================
# 6. commit_verified_attachment_versioned — 4-action conditions & rollback
# ===========================================================================

class TestCommitVerifiedAttachmentVersioned(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.now = _utc(2026, 1, 1, 0, 5, 0)
        self.lease_expires = _utc(2026, 1, 1, 0, 10, 0)
        _seed_complaint_and_intent(
            self.repo,
            lease_token="tok-1",
            lease_expires_at=self.lease_expires,
            source_version_id="vid-1",
        )
        self.repo.begin_copy_attempt(
            complaint_id="CF-2026-0001",
            intent_id="I1",
            lease_token="tok-1",
            source_version_id="vid-1",
            final_key="complaints/CF-2026-0001/images/I1.jpg",
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            now=self.now,
        )

    def _commit(self, **overrides) -> AtomicAttachmentCommitResult:
        defaults = dict(
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
            now=self.now,
        )
        defaults.update(overrides)
        return self.repo.commit_verified_attachment_versioned(**defaults)

    def _assert_unmutated(self) -> None:
        c = self.repo.get("CF-2026-0001")
        i = self.repo.get_intent("CF-2026-0001", "I1")
        a = self.repo.get_attempt("CF-2026-0001", "I1", "tok-1")
        self.assertEqual(c.version, 1)
        self.assertIsNone(c.attachment_status)
        self.assertEqual(i.status, UploadIntentStatus.CONFIRMING)
        self.assertEqual(a.status, UploadAttemptStatus.COPYING)
        self.assertIsNone(self.repo.get_outbox_record("I1"))

    def test_happy_path_and_exact_idempotent_replay(self) -> None:
        res = self._commit()
        self.assertEqual(res.complaint.attachment_status, "VERIFIED")
        self.assertEqual(res.complaint.attachment_key, "complaints/CF-2026-0001/images/I1.jpg")
        self.assertEqual(res.complaint.version, 2)
        self.assertEqual(res.intent.status, UploadIntentStatus.VERIFIED)
        self.assertFalse(hasattr(res.intent, "destination_version_id"))
        self.assertEqual(res.attempt.status, UploadAttemptStatus.COMMITTED)
        self.assertEqual(res.attempt.destination_version_id, "dst-vid-1")
        self.assertEqual(res.outbox.intent_id, "I1")
        self.assertEqual(res.outbox.destination_version_id, "dst-vid-1")

        # Retrying the same call with pre-copy expected_complaint_version=1 is idempotent (Spec §5.2).
        replay = self._commit()
        self.assertEqual(replay.complaint.version, 2)
        self.assertEqual(replay.attempt.destination_version_id, "dst-vid-1")
        self.assertEqual(replay.outbox.destination_version_id, "dst-vid-1")

        # CW-5: after outbox sweeper deletes the outbox record, an exact retry remains idempotent
        # with outbox=None, whereas a conflicting destination_version_id still fails.
        self.repo.delete_outbox_record("I1")
        self.assertIsNone(self.repo.get_outbox_record("I1"))
        swept_replay = self._commit()
        self.assertEqual(swept_replay.complaint.version, 2)
        self.assertEqual(swept_replay.attempt.destination_version_id, "dst-vid-1")
        self.assertIsNone(swept_replay.outbox)
        with self.assertRaises(OptimisticLockError):
            self._commit(destination_version_id="dst-OTHER")
        with self.assertRaises(ImmutableFieldViolationError):
            self._commit(expected_complaint_version=2, destination_version_id="dst-OTHER")

    def test_action1_conditions_fail_without_partial_mutation(self) -> None:
        with self.assertRaises(ComplaintNotFoundError):
            self._commit(complaint_id="MISSING")

        with self.assertRaises(OptimisticLockError):
            self._commit(expected_complaint_version=99)
        self._assert_unmutated()

        # Simulate complaint already verified by a different upload at same version
        c = self.repo.get("CF-2026-0001")
        c.attachment_status = "VERIFIED"
        c.attachment_key = "complaints/CF-2026-0001/images/OTHER.jpg"
        self.repo._storage["CF-2026-0001"] = c
        with self.assertRaises(ImmutableFieldViolationError):
            self._commit(expected_complaint_version=1)

    def test_action2_conditions_fail_without_partial_mutation(self) -> None:
        with self.assertRaises(StaleLeaseError):
            self._commit(lease_token="WRONG-TOKEN")
        self._assert_unmutated()

        for kw in (
            {"source_version_id": "vid-WRONG"},
            {"final_key": "complaints/CF-2026-0001/images/HIJACK.jpg"},
            {"expected_sha256": "b" * 64, "staging_sha256": "b" * 64},
            {"expected_mime": "image/png"},
            {"max_byte_size": 999, "staging_size": 500},
        ):
            with self.assertRaises(ImmutableFieldViolationError):
                self._commit(**kw)
            self._assert_unmutated()

    def test_action3_conditions_fail_without_partial_mutation(self) -> None:
        # Attempt immutable fields mutated in store -> Action 3 catches each and leaves Complaint/Intent intact
        for field_name, bad_val in (
            ("source_version_id", "vid-tampered"),
            ("final_key", "complaints/CF-2026-0001/images/tampered.jpg"),
            ("sha256", "b" * 64),
            ("content_type", "image/png"),
            ("max_byte_size", 555_555),
        ):
            repo_t = InMemoryComplaintRepository()
            _seed_complaint_and_intent(
                repo_t,
                lease_token="tok-1",
                lease_expires_at=self.lease_expires,
                source_version_id="vid-1",
            )
            repo_t.begin_copy_attempt(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-1",
                source_version_id="vid-1",
                final_key="complaints/CF-2026-0001/images/I1.jpg",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                now=self.now,
            )
            setattr(repo_t._attempts[("CF-2026-0001", "I1", "tok-1")], field_name, bad_val)
            with self.assertRaises(ImmutableFieldViolationError):
                repo_t.commit_verified_attachment_versioned(
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
                    now=self.now,
                )
            self.assertEqual(repo_t.get("CF-2026-0001").version, 1)
            self.assertIsNone(repo_t.get("CF-2026-0001").attachment_status)
            self.assertEqual(repo_t.get_intent("CF-2026-0001", "I1").status, UploadIntentStatus.CONFIRMING)

        # Attempt mutated to COMMITTED before commit -> Action 3 rejects and Complaint/Intent unchanged
        self.repo._attempts[("CF-2026-0001", "I1", "tok-1")].status = UploadAttemptStatus.COMMITTED
        with self.assertRaises(ImmutableFieldViolationError):
            self._commit()
        c = self.repo.get("CF-2026-0001")
        i = self.repo.get_intent("CF-2026-0001", "I1")
        self.assertEqual(c.version, 1)
        self.assertIsNone(c.attachment_status)
        self.assertEqual(i.status, UploadIntentStatus.CONFIRMING)

        # Attempt missing -> UploadIntentNotFoundError and Complaint/Intent unchanged
        self.repo._attempts.pop(("CF-2026-0001", "I1", "tok-1"))
        with self.assertRaises(UploadIntentNotFoundError):
            self._commit()
        self.assertEqual(self.repo.get("CF-2026-0001").version, 1)

    def test_action4_duplicate_outbox_blocks_commit_without_partial_mutation(self) -> None:
        self.repo._outbox["I1"] = TagOutboxRecord(
            intent_id="I1",
            complaint_id="CF-2026-0001",
            s3_key="complaints/CF-2026-0001/images/I1.jpg",
            destination_version_id="dst-existing",
            committed_at=self.now,
        )
        with self.assertRaises(ImmutableFieldViolationError):
            self._commit()
        c = self.repo.get("CF-2026-0001")
        i = self.repo.get_intent("CF-2026-0001", "I1")
        a = self.repo.get_attempt("CF-2026-0001", "I1", "tok-1")
        self.assertEqual(c.version, 1)
        self.assertIsNone(c.attachment_status)
        self.assertEqual(i.status, UploadIntentStatus.CONFIRMING)
        self.assertEqual(a.status, UploadAttemptStatus.COPYING)


# ===========================================================================
# 7. Outbox CRUD
# ===========================================================================

class TestOutboxCRUD(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.now = _utc(2026, 1, 1, 12, 0, 0)
        self.lease_expires = _utc(2026, 1, 1, 13, 0, 0)

    def _full_commit(self, intent_id: str, complaint_id: str, committed_at: datetime) -> None:
        _seed_complaint_and_intent(
            self.repo,
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token="tok-1",
            lease_expires_at=self.lease_expires,
            source_version_id="vid-1",
        )
        final_key = f"complaints/{complaint_id}/images/{intent_id}.jpg"
        self.repo.begin_copy_attempt(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token="tok-1",
            source_version_id="vid-1",
            final_key=final_key,
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            now=self.now,
        )
        self.repo.commit_verified_attachment_versioned(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token="tok-1",
            final_key=final_key,
            source_version_id="vid-1",
            destination_version_id=f"dst-{intent_id}",
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            expected_complaint_version=1,
            staging_sha256=_SHA256,
            staging_size=50_000,
            now=committed_at,
        )

    def test_list_sorted_by_key_limit_increment_and_delete(self) -> None:
        # Decision 3: sorted deterministically by SK (INTENT#{intent_id}) ascending,
        # NOT committed_at, so DynamoDB and InMemory share identical key-based order.
        self._full_commit("I1", "CF-2026-0001", _utc(2026, 1, 1, 10, 0, 0))
        self._full_commit("I2", "CF-2026-0002", _utc(2026, 1, 1, 8, 0, 0))
        self._full_commit("I3", "CF-2026-0003", _utc(2026, 1, 1, 9, 0, 0))

        ordered = self.repo.list_pending_outbox_records()
        self.assertEqual([r.intent_id for r in ordered], ["I1", "I2", "I3"])

        limited = self.repo.list_pending_outbox_records(limit=2)
        self.assertEqual([r.intent_id for r in limited], ["I1", "I2"])

        with self.assertRaises(ValidationError):
            self.repo.list_pending_outbox_records(limit=0)

        rec = self.repo.increment_outbox_sweep_attempt("I2", self.now)
        self.assertEqual(rec.sweep_attempts, 1)
        self.assertEqual(self.repo.increment_outbox_sweep_attempt("I2", self.now).sweep_attempts, 2)

        with self.assertRaises(UploadIntentNotFoundError):
            self.repo.increment_outbox_sweep_attempt("MISSING", self.now)

        self.repo.delete_outbox_record("I2")
        self.assertIsNone(self.repo.get_outbox_record("I2"))
        self.repo.delete_outbox_record("MISSING")

    def test_list_pending_outbox_records_paginated_live_keyset(self) -> None:
        self._full_commit("I1", "CF-2026-0001", _utc(2026, 1, 1, 10, 0, 0))
        self._full_commit("I2", "CF-2026-0002", _utc(2026, 1, 1, 8, 0, 0))
        self._full_commit("I3", "CF-2026-0003", _utc(2026, 1, 1, 9, 0, 0))

        page1, cursor1 = self.repo.list_pending_outbox_records_paginated(limit=2)
        self.assertEqual([r.intent_id for r in page1], ["I1", "I2"])
        self.assertIsNotNone(cursor1)

        # Concurrent delete of page1's last item + insert after cursor (live pagination)
        self.repo.delete_outbox_record("I2")
        self._full_commit("I4", "CF-2026-0004", _utc(2026, 1, 1, 7, 0, 0))

        page2, cursor2 = self.repo.list_pending_outbox_records_paginated(limit=2, cursor=cursor1)
        self.assertEqual([r.intent_id for r in page2], ["I3", "I4"])
        self.assertIsNone(cursor2)

        with self.assertRaises(ValidationError):
            self.repo.list_pending_outbox_records_paginated(limit=0)
        with self.assertRaises(ValidationError):
            self.repo.list_pending_outbox_records_paginated(cursor="bad-cursor")


# ===========================================================================
# 8. RepositoryVersionedUploadStateAdapter & Worker integration with Stubber
# ===========================================================================

class TestRepositoryStateAdapterAndWorkerIntegration(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.adapter = RepositoryVersionedUploadStateAdapter(self.repo, self.repo)
        self.now = _utc(2026, 1, 1, 0, 5, 0)
        self.lease_expires = _utc(2026, 1, 1, 0, 10, 0)

    def test_adapter_translates_immutable_errors_to_copy_verification_error(self) -> None:
        _seed_complaint_and_intent(
            self.repo,
            lease_token="tok-1",
            lease_expires_at=self.lease_expires,
            source_version_id="vid-1",
        )
        with self.assertRaises(CopyVerificationError):
            self.adapter.pin_source_version(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-1",
                source_version_id="vid-OTHER",
                now=self.now,
            )

        with self.assertRaises(CopyVerificationError):
            self.adapter.begin_copy_attempt(
                complaint_id="CF-2026-0001",
                intent_id="I1",
                lease_token="tok-1",
                source_version_id="vid-1",
                final_key="complaints/CF-2026-0001/images/WRONG.jpg",
                expected_sha256=_SHA256,
                expected_mime="image/jpeg",
                max_byte_size=1_000_000,
                expected_complaint_version=1,
                now=self.now,
            )

        # Persisted UploadAttempt must NOT contain expected_complaint_version,
        # while transient CopyAttemptRecord returned by adapter DOES contain it.
        copy_attempt = self.adapter.begin_copy_attempt(
            complaint_id="CF-2026-0001",
            intent_id="I1",
            lease_token="tok-1",
            source_version_id="vid-1",
            final_key="complaints/CF-2026-0001/images/I1.jpg",
            expected_sha256=_SHA256,
            expected_mime="image/jpeg",
            max_byte_size=1_000_000,
            expected_complaint_version=1,
            now=self.now,
        )
        self.assertIsInstance(copy_attempt, CopyAttemptRecord)
        self.assertEqual(copy_attempt.expected_complaint_version, 1)
        persisted = self.repo.get_attempt("CF-2026-0001", "I1", "tok-1")
        assert persisted is not None
        self.assertFalse(hasattr(persisted, "expected_complaint_version"))

    def test_worker_full_flow_via_adapter_and_stubber_preserves_complaint_occ(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        complaint_id = "CF-2026-0001"
        intent_id = "I1"
        staging_key = f"staging/complaints/{complaint_id}/{intent_id}/upload.tmp"
        final_key = f"complaints/{complaint_id}/images/{intent_id}.jpg"
        src_vid = "src-vid-001"
        dst_vid = "dst-vid-001"

        _seed_complaint_and_intent(
            self.repo,
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token="worker-tok",
            lease_expires_at=self.lease_expires,
            source_version_id=None,  # tests unpinned discovery -> pin -> copy -> commit
            expected_sha256=sha_hex,
            expected_mime="image/jpeg",
            max_byte_size=4096,
            complaint_version=1,
        )
        # Bump complaint version to 3 before worker runs so non-trivial OCC version is exercised.
        c = self.repo.get(complaint_id)
        assert c is not None
        self.repo.update(replace(c, title="Updated Lab Fan Title"), expected_version=1)
        self.repo.update(replace(self.repo.get(complaint_id), building="Block B"), expected_version=2)
        self.assertEqual(self.repo.get_consistent(complaint_id).version, 3)

        client = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        stubber.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [{"Key": staging_key, "VersionId": src_vid, "IsLatest": True}],
                "DeleteMarkers": [],
            },
            expected_params={"Bucket": "campusfix-bucket", "Prefix": staging_key},
        )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-src"',
                "ChecksumSHA256": sha_b64,
                "VersionId": src_vid,
            },
            expected_params={
                "Bucket": "campusfix-bucket",
                "Key": staging_key,
                "VersionId": src_vid,
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(payload[:12]), 12), "VersionId": src_vid},
            expected_params={
                "Bucket": "campusfix-bucket",
                "Key": staging_key,
                "VersionId": src_vid,
                "Range": "bytes=0-11",
            },
        )
        stubber.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest"'},
                "CopySourceVersionId": src_vid,
                "VersionId": dst_vid,
            },
            expected_params={
                "CopySource": {"Bucket": "campusfix-bucket", "Key": staging_key, "VersionId": src_vid},
                "Bucket": "campusfix-bucket",
                "Key": final_key,
                "IfNoneMatch": "*",
            },
        )
        stubber.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "VersionId": dst_vid},
            expected_params={
                "Bucket": "campusfix-bucket",
                "Key": final_key,
                "VersionId": dst_vid,
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "delete_object",
            {"DeleteMarker": True, "VersionId": "dm-1"},
            expected_params={"Bucket": "campusfix-bucket", "Key": staging_key},
        )

        storage = Boto3S3StorageService(client=client)
        worker = UploadConfirmationWorker(
            s3_storage=storage,
            state_port=self.adapter,
            source_bucket="campusfix-bucket",
            now_fn=lambda: self.now,
        )

        with stubber:
            res = worker.confirm_upload(
                complaint_id=complaint_id,
                intent_id=intent_id,
                worker_token="worker-tok",
            )

        stubber.assert_no_pending_responses()
        self.assertEqual(res.status, "VERIFIED")
        self.assertEqual(res.source_version_id, src_vid)
        self.assertEqual(res.destination_version_id, dst_vid)

        committed_complaint = self.repo.get(complaint_id)
        committed_intent = self.repo.get_intent(complaint_id, intent_id)
        committed_attempt = self.repo.get_attempt(complaint_id, intent_id, "worker-tok")
        outbox = self.repo.get_outbox_record(intent_id)
        self.assertEqual(committed_complaint.version, 4)
        self.assertEqual(committed_complaint.attachment_status, "VERIFIED")
        self.assertEqual(committed_complaint.attachment_key, final_key)
        self.assertEqual(committed_intent.status, UploadIntentStatus.VERIFIED)
        self.assertEqual(committed_intent.source_version_id, src_vid)
        self.assertFalse(hasattr(committed_intent, "destination_version_id"))
        self.assertEqual(committed_attempt.status, UploadAttemptStatus.COMMITTED)
        self.assertEqual(committed_attempt.destination_version_id, dst_vid)
        self.assertIsNotNone(outbox)
        self.assertEqual(outbox.destination_version_id, dst_vid)

    def test_worker_cw2_412_recovery_via_adapter_keeps_prior_attempt(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        complaint_id = "CF-2026-0001"
        intent_id = "I1"
        staging_key = f"staging/complaints/{complaint_id}/{intent_id}/upload.tmp"
        final_key = f"complaints/{complaint_id}/images/{intent_id}.jpg"
        src_vid = "src-vid-001"
        existing_dst_vid = "dst-v1-cw2"

        _seed_complaint_and_intent(
            self.repo,
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token="worker-A",
            lease_expires_at=self.lease_expires,
            source_version_id=src_vid,
            expected_sha256=sha_hex,
            expected_mime="image/jpeg",
            max_byte_size=4096,
        )
        # Worker A recorded COPYING before crashing post-CopyObject
        self.repo.begin_copy_attempt(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token="worker-A",
            source_version_id=src_vid,
            final_key=final_key,
            expected_sha256=sha_hex,
            expected_mime="image/jpeg",
            max_byte_size=4096,
            now=self.now,
        )

        # Worker B takes over expired lease
        later = self.lease_expires + timedelta(seconds=10)
        self.repo.claim_intent_lease(complaint_id, intent_id, "worker-B", 60, later)

        client = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-src"',
                "ChecksumSHA256": sha_b64,
                "VersionId": src_vid,
            },
        )
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(payload[:12]), 12), "VersionId": src_vid},
        )
        stubber.add_client_error("copy_object", service_error_code="PreconditionFailed", http_status_code=412)
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-dest"',
                "ChecksumSHA256": sha_b64,
                "VersionId": existing_dst_vid,
            },
        )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-dest"',
                "ChecksumSHA256": sha_b64,
                "VersionId": existing_dst_vid,
            },
        )
        stubber.add_response(
            "get_object",
            {
                "Body": StreamingBody(io.BytesIO(payload), len(payload)),
                "ContentLength": len(payload),
                "VersionId": existing_dst_vid,
            },
        )
        stubber.add_response("delete_object", {})

        worker = UploadConfirmationWorker(
            s3_storage=Boto3S3StorageService(client=client),
            state_port=self.adapter,
            source_bucket="campusfix-bucket",
            now_fn=lambda: later,
        )
        with stubber:
            res = worker.confirm_upload(
                complaint_id=complaint_id,
                intent_id=intent_id,
                worker_token="worker-B",
            )

        stubber.assert_no_pending_responses()
        self.assertEqual(res.destination_version_id, existing_dst_vid)
        attempts = self.repo.list_attempts(complaint_id, intent_id)
        self.assertEqual([(a.lease_token, a.status) for a in attempts], [
            ("worker-A", UploadAttemptStatus.COPYING),
            ("worker-B", UploadAttemptStatus.COMMITTED),
        ])

    def test_worker_concurrent_complaint_update_fails_closed_without_post_copy_reconstruct(self) -> None:
        """If Complaint.version advances concurrently during CopyObject, commit must pass the
        pre-copy captured version (1) and fail with OptimisticLockError rather than reading post-copy.
        """
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        complaint_id = "CF-2026-0001"
        intent_id = "I1"
        staging_key = f"staging/complaints/{complaint_id}/{intent_id}/upload.tmp"
        final_key = f"complaints/{complaint_id}/images/{intent_id}.jpg"
        src_vid = "src-vid-001"
        dst_vid = "dst-vid-001"

        _seed_complaint_and_intent(
            self.repo,
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token="worker-tok",
            lease_expires_at=self.lease_expires,
            source_version_id=src_vid,
            expected_sha256=sha_hex,
            expected_mime="image/jpeg",
            max_byte_size=4096,
            complaint_version=1,
        )

        client = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-src"',
                "ChecksumSHA256": sha_b64,
                "VersionId": src_vid,
            },
        )
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(payload[:12]), 12), "VersionId": src_vid},
        )
        stubber.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest"'},
                "CopySourceVersionId": src_vid,
                "VersionId": dst_vid,
            },
        )
        stubber.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "VersionId": dst_vid},
        )

        def _bump_complaint_version_on_copy(model, **kwargs) -> None:
            if getattr(model, "name", None) == "CopyObject":
                cur = self.repo.get(complaint_id)
                assert cur is not None
                self.repo.update(replace(cur, title="Concurrent staff edit"), expected_version=cur.version)

        client.meta.events.register_first("before-call.*.*", _bump_complaint_version_on_copy)

        worker = UploadConfirmationWorker(
            s3_storage=Boto3S3StorageService(client=client),
            state_port=self.adapter,
            source_bucket="campusfix-bucket",
            now_fn=lambda: self.now,
        )
        with stubber:
            with self.assertRaises(OptimisticLockError):
                worker.confirm_upload(
                    complaint_id=complaint_id,
                    intent_id=intent_id,
                    worker_token="worker-tok",
                )

        stubber.assert_no_pending_responses()
        c_after = self.repo.get(complaint_id)
        i_after = self.repo.get_intent(complaint_id, intent_id)
        a_after = self.repo.get_attempt(complaint_id, intent_id, "worker-tok")
        self.assertEqual(c_after.version, 2)
        self.assertIsNone(c_after.attachment_status)
        self.assertEqual(i_after.status, UploadIntentStatus.CONFIRMING)
        self.assertEqual(a_after.status, UploadAttemptStatus.COPYING)
        self.assertIsNone(self.repo.get_outbox_record(intent_id))


if __name__ == "__main__":
    unittest.main()
