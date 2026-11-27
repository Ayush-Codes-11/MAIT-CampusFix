"""Moto-backed S3 adapter and worker tests for Stage B1.

Coverage note:
- Uses Moto (`moto==5.2.6`) for stateful versioned bucket behavior: presigned POST
  checksum formatting, `HeadObject` ChecksumMode/VersionId inspection, byte-range and
  streaming SHA-256 reads, exact-key `ListObjectVersions`, conditional `CopyObject`
  top-level `VersionId` capture, unversioned presigned GET, and delete markers.
- Moto does NOT enforce destination `If-None-Match: *` on `CopyObject`; conditional
  412/409 behavior and serialized `If-None-Match: *` wire headers are verified in
  `test_upload_confirmation_worker.py` via Botocore `Stubber` and request hooks.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover - e.g. MSYS2 UCRT64 venv without Moto wheels
    mock_aws = None

from campusfix.domain.models import UploadIntentStatus
from campusfix.domain.rules import ValidationError
from campusfix.services.boto3_s3_storage import (
    Boto3S3StorageService,
    StagingObjectDeletedError,
    UploadAmbiguousVersionError,
)
from campusfix.services.upload_confirmation_worker import (
    CopyAttemptRecord,
    UploadConfirmationWorker,
    UploadIntentSnapshot,
)

_DUMMY_AWS_CREDS = {
    "aws_access_key_id": "test-access-key",
    "aws_secret_access_key": "test-secret-key",
    "region_name": "ap-southeast-2",
}


class _InMemoryVersionedStatePort:
    """Minimal Stage B1 fake state port for Moto end-to-end worker tests."""

    def __init__(self, initial: UploadIntentSnapshot) -> None:
        self._intents: dict[tuple[str, str], UploadIntentSnapshot] = {
            (initial.complaint_id, initial.intent_id): initial
        }
        self._attempts: dict[tuple[str, str, str], CopyAttemptRecord] = {}
        self.commits: list[dict[str, object]] = []

    def get_intent_consistent(
        self,
        complaint_id: str,
        intent_id: str,
    ) -> UploadIntentSnapshot | None:
        return self._intents.get((complaint_id, intent_id))

    def pin_source_version(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        now: datetime,
    ) -> UploadIntentSnapshot:
        current = self._intents[(complaint_id, intent_id)]
        if current.lease_token != lease_token:
            raise AssertionError("Unexpected lease token mismatch during pin_source_version.")
        if current.source_version_id is not None and current.source_version_id != source_version_id:
            raise AssertionError("Attempted to overwrite pinned source_version_id.")
        updated = replace(current, source_version_id=source_version_id)
        self._intents[(complaint_id, intent_id)] = updated
        return updated

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
        attempt_key = (complaint_id, intent_id, lease_token)
        candidate = CopyAttemptRecord(
            complaint_id=complaint_id,
            intent_id=intent_id,
            lease_token=lease_token,
            source_version_id=source_version_id,
            final_key=final_key,
            expected_sha256=expected_sha256,
            expected_mime=expected_mime,
            max_byte_size=max_byte_size,
            expected_complaint_version=expected_complaint_version,
            status="COPYING",
        )
        existing = self._attempts.get(attempt_key)
        if existing is not None:
            if existing != candidate:
                raise AssertionError("Attempt immutable values mismatch on repeat.")
            return existing
        self._attempts[attempt_key] = candidate
        return candidate

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
    ) -> dict[str, object]:
        current = self._intents[(complaint_id, intent_id)]
        updated = replace(current, status=UploadIntentStatus.VERIFIED)
        self._intents[(complaint_id, intent_id)] = updated
        record: dict[str, object] = {
            "complaint_id": complaint_id,
            "intent_id": intent_id,
            "lease_token": lease_token,
            "final_key": final_key,
            "source_version_id": source_version_id,
            "destination_version_id": destination_version_id,
            "expected_sha256": expected_sha256,
            "expected_mime": expected_mime,
            "max_byte_size": max_byte_size,
            "expected_complaint_version": expected_complaint_version,
            "staging_sha256": staging_sha256,
            "staging_size": staging_size,
            "committed_at": now,
        }
        self.commits.append(record)
        return record


def _make_valid_jpeg(size: int = 2048) -> bytes:
    return b"\xff\xd8\xff\xe0" + (b"A" * (size - 4))


def _make_valid_png(size: int = 2560) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + (b"B" * (size - 8))


@unittest.skipIf(mock_aws is None, "moto is not installed in this environment")
class TestBoto3S3StorageWithMoto(unittest.TestCase):
    """Stateful versioned S3 adapter tests executed offline against Moto."""

    BUCKET = "campusfix-attachments-test"
    REGION = "ap-southeast-2"

    def _create_versioned_client(self):
        client = boto3.client(
            "s3",
            config=Config(signature_version="s3v4"),
            **_DUMMY_AWS_CREDS,
        )
        client.create_bucket(
            Bucket=self.BUCKET,
            CreateBucketConfiguration={"LocationConstraint": self.REGION},
        )
        client.put_bucket_versioning(
            Bucket=self.BUCKET,
            VersioningConfiguration={"Status": "Enabled"},
        )
        return client

    def test_presigned_post_base64_checksum_and_head_object_checksum_translation(self) -> None:
        jpeg_bytes = _make_valid_jpeg()
        sha256_hex = hashlib.sha256(jpeg_bytes).hexdigest().lower()
        sha256_b64 = base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii")
        staging_key = "staging/complaints/CF-2026-0001/intent-1/upload.tmp"

        with mock_aws():
            client = self._create_versioned_client()
            adapter = Boto3S3StorageService(client=client)

            post_data = adapter.create_staging_presigned_post(
                bucket=self.BUCKET,
                key=staging_key,
                content_type="image/jpeg",
                checksum_sha256=sha256_hex,
                max_byte_size=4096,
                expires_in=300,
            )
            self.assertEqual(post_data["fields"]["x-amz-checksum-algorithm"], "SHA256")
            self.assertEqual(post_data["fields"]["x-amz-checksum-sha256"], sha256_b64)
            decoded_policy = json.loads(base64.b64decode(post_data["fields"]["policy"]).decode("utf-8"))
            self.assertIn(["eq", "$x-amz-checksum-sha256", sha256_b64], decoded_policy["conditions"])

            put_resp = client.put_object(
                Bucket=self.BUCKET,
                Key=staging_key,
                Body=jpeg_bytes,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )
            v1 = put_resp["VersionId"]
            self.assertTrue(v1 and v1 != "null")

            meta = adapter.head_object(self.BUCKET, staging_key, version_id=v1)
            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.version_id, v1)
            self.assertEqual(meta.content_length, len(jpeg_bytes))
            self.assertEqual(meta.content_type, "image/jpeg")
            self.assertEqual(meta.sha256_checksum, sha256_hex)

            missing = adapter.head_object(self.BUCKET, "staging/missing.tmp")
            self.assertIsNone(missing)

    def test_version_pinned_head_range_and_stream_isolation_across_overwrites(self) -> None:
        jpeg_v1 = _make_valid_jpeg(2048)
        png_v2 = _make_valid_png(3072)
        v1_hex = hashlib.sha256(jpeg_v1).hexdigest().lower()
        staging_key = "staging/complaints/CF-2026-0002/intent-2/upload.tmp"

        with mock_aws():
            client = self._create_versioned_client()
            adapter = Boto3S3StorageService(client=client)

            v1 = client.put_object(
                Bucket=self.BUCKET,
                Key=staging_key,
                Body=jpeg_v1,
                ContentType="image/jpeg",
            )["VersionId"]
            v2 = client.put_object(
                Bucket=self.BUCKET,
                Key=staging_key,
                Body=png_v2,
                ContentType="image/png",
            )["VersionId"]
            self.assertNotEqual(v1, v2)

            # Tip is PNG V2, but pinned operations target exact JPEG V1.
            tip = adapter.head_object(self.BUCKET, staging_key)
            assert tip is not None
            self.assertEqual(tip.version_id, v2)
            self.assertEqual(tip.content_type, "image/png")

            pinned_head = adapter.head_object(self.BUCKET, staging_key, version_id=v1)
            assert pinned_head is not None
            self.assertEqual(pinned_head.version_id, v1)
            self.assertEqual(pinned_head.content_type, "image/jpeg")
            self.assertEqual(pinned_head.content_length, len(jpeg_v1))

            header = adapter.get_object_range(self.BUCKET, staging_key, 0, 11, version_id=v1)
            self.assertEqual(header, jpeg_v1[:12])

            streamed = adapter.read_object_and_compute_sha256(
                self.BUCKET,
                staging_key,
                version_id=v1,
                max_byte_size=4096,
            )
            self.assertEqual(streamed.content_length, len(jpeg_v1))
            self.assertEqual(streamed.sha256_checksum, v1_hex)
            self.assertEqual(streamed.header_bytes, jpeg_v1[:12])
            self.assertEqual(streamed.version_id, v1)

            with self.assertRaises(ValidationError):
                adapter.read_object_and_compute_sha256(
                    self.BUCKET,
                    staging_key,
                    version_id=v1,
                    max_byte_size=1024,
                )

    def test_list_object_versions_exact_handles_empty_sibling_ambiguous_and_deleted(self) -> None:
        target_key = "staging/complaints/CF-2026-0003/intent-3/upload.tmp"
        sibling_key = "staging/complaints/CF-2026-0003/intent-3/upload.tmp.bak"

        with mock_aws():
            client = self._create_versioned_client()
            adapter = Boto3S3StorageService(client=client)

            # Add sibling key noise (multiple versions + delete marker) sharing prefix.
            client.put_object(Bucket=self.BUCKET, Key=sibling_key, Body=b"sib-1")
            client.put_object(Bucket=self.BUCKET, Key=sibling_key, Body=b"sib-2")
            client.delete_object(Bucket=self.BUCKET, Key=sibling_key)

            # 1. No exact key yet -> None
            self.assertIsNone(adapter.list_object_versions_exact(self.BUCKET, target_key))

            # 2. Single version on target key -> returns exact VersionId despite sibling noise
            v1 = client.put_object(
                Bucket=self.BUCKET,
                Key=target_key,
                Body=_make_valid_jpeg(),
                ContentType="image/jpeg",
            )["VersionId"]
            self.assertEqual(adapter.list_object_versions_exact(self.BUCKET, target_key), v1)

            # 3. Overwrite creates second version -> fail closed on ambiguity without explicit VersionId
            client.put_object(
                Bucket=self.BUCKET,
                Key=target_key,
                Body=_make_valid_jpeg(2500),
                ContentType="image/jpeg",
            )
            with self.assertRaises(UploadAmbiguousVersionError):
                adapter.list_object_versions_exact(self.BUCKET, target_key)

            # 4. Delete marker becomes latest -> fail closed (never revive older version)
            client.delete_object(Bucket=self.BUCKET, Key=target_key)
            with self.assertRaises(StagingObjectDeletedError):
                adapter.list_object_versions_exact(self.BUCKET, target_key)

    def test_conditional_copy_captures_top_level_version_id_and_unversioned_presigned_get(self) -> None:
        jpeg_bytes = _make_valid_jpeg()
        staging_key = "staging/complaints/CF-2026-0004/intent-4/upload.tmp"
        final_key = "complaints/CF-2026-0004/images/intent-4.jpg"

        with mock_aws():
            client = self._create_versioned_client()
            adapter = Boto3S3StorageService(client=client)

            src_v1 = client.put_object(
                Bucket=self.BUCKET,
                Key=staging_key,
                Body=jpeg_bytes,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )["VersionId"]

            copy_result = adapter.copy_object_conditional(
                source_bucket=self.BUCKET,
                source_key=staging_key,
                source_version_id=src_v1,
                dest_bucket=self.BUCKET,
                dest_key=final_key,
            )
            self.assertTrue(copy_result.destination_version_id)
            self.assertNotEqual(copy_result.destination_version_id, "null")
            self.assertEqual(copy_result.copy_source_version_id, src_v1)

            dest_head = adapter.head_object(
                self.BUCKET,
                final_key,
                version_id=copy_result.destination_version_id,
            )
            assert dest_head is not None
            self.assertEqual(dest_head.version_id, copy_result.destination_version_id)
            self.assertEqual(dest_head.content_length, len(jpeg_bytes))

            presigned_get = adapter.generate_presigned_get(self.BUCKET, final_key, expires_in=300)
            parsed = urlparse(presigned_get)
            query = parse_qs(parsed.query)
            self.assertIn(final_key, parsed.path)
            self.assertNotIn("versionId", query)

    def test_end_to_end_worker_against_moto_streaming_fallback_and_staging_delete_marker(self) -> None:
        jpeg_bytes = _make_valid_jpeg(2200)
        sha256_hex = hashlib.sha256(jpeg_bytes).hexdigest().lower()
        staging_key = "staging/complaints/CF-2026-0005/intent-5/upload.tmp"
        final_key = "complaints/CF-2026-0005/images/intent-5.jpg"
        now = datetime(2026, 11, 25, 16, 0, 0, tzinfo=timezone.utc)

        with mock_aws():
            client = self._create_versioned_client()
            adapter = Boto3S3StorageService(client=client)

            # Upload WITHOUT ChecksumAlgorithm so HeadObject omits ChecksumSHA256 and exercises
            # full pinned streaming SHA-256 verification before CopyObject.
            src_v1 = client.put_object(
                Bucket=self.BUCKET,
                Key=staging_key,
                Body=jpeg_bytes,
                ContentType="image/jpeg",
            )["VersionId"]

            snapshot = UploadIntentSnapshot(
                intent_id="intent-5",
                complaint_id="CF-2026-0005",
                owner_id="std_1",
                staging_key=staging_key,
                final_key=final_key,
                expected_mime="image/jpeg",
                max_byte_size=4096,
                expected_sha256=sha256_hex,
                status=UploadIntentStatus.CONFIRMING,
                confirmation_deadline=now + timedelta(minutes=15),
                lease_token="lease-abc",
                lease_expires_at=now + timedelta(seconds=30),
                expected_complaint_version=2,
                source_version_id=None,
            )
            state_port = _InMemoryVersionedStatePort(snapshot)
            worker = UploadConfirmationWorker(
                s3_storage=adapter,
                state_port=state_port,
                source_bucket=self.BUCKET,
                now_fn=lambda: now,
            )

            result = worker.confirm_upload(
                complaint_id="CF-2026-0005",
                intent_id="intent-5",
                worker_token="lease-abc",
            )
            self.assertEqual(result.status, "VERIFIED")
            self.assertEqual(result.attachment_key, final_key)
            self.assertEqual(result.source_version_id, src_v1)
            self.assertTrue(result.destination_version_id)
            self.assertEqual(result.staging_sha256, sha256_hex)
            self.assertEqual(result.staging_size, len(jpeg_bytes))
            self.assertEqual(len(state_port.commits), 1)
            self.assertEqual(
                state_port.commits[0]["destination_version_id"],
                result.destination_version_id,
            )

            # Staging key now has a delete marker (unversioned lookup -> None), while noncurrent V1 remains intact.
            self.assertIsNone(adapter.head_object(self.BUCKET, staging_key))
            pinned_head = adapter.head_object(self.BUCKET, staging_key, version_id=src_v1)
            self.assertIsNotNone(pinned_head)
