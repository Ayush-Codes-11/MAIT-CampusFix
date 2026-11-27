"""Botocore Stubber and controlled-response tests for conditional-copy worker Stage B1."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
from typing import Any
import unittest

import boto3
from botocore.config import Config
from botocore.exceptions import IncompleteReadError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from campusfix.domain.models import UploadIntentStatus
from campusfix.domain.rules import (
    DestinationConflictError,
    StaleLeaseError,
    UploadNotReadyError,
    ValidationError,
)
from campusfix.services.boto3_s3_storage import (
    Boto3S3StorageService,
    CopyVerificationError,
    UploadAmbiguousVersionError,
)
from campusfix.services.s3_capabilities import SdkCapabilityError
from campusfix.services.upload_confirmation_worker import (
    AlreadyVerifiedIdempotent,
    CopyAttemptRecord,
    UploadConfirmationWorker,
    UploadIntentSnapshot,
)

_DUMMY_AWS_CREDS = {
    "aws_access_key_id": "test-access-key",
    "aws_secret_access_key": "test-secret-key",
    "region_name": "ap-southeast-2",
}


def _make_jpeg(size: int = 2048, fill: bytes = b"J") -> bytes:
    return b"\xff\xd8\xff\xe0" + (fill * (size - 4))


def _make_png(size: int = 2048) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + (b"P" * (size - 8))


def _sha256_hex_and_b64(data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).digest()
    return digest.hex().lower(), base64.b64encode(digest).decode("ascii")


def _streaming_body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


class _SequenceStatePort:
    """Controlled state port supporting deterministic lease changes and Attempt tracking."""

    def __init__(self, initial: UploadIntentSnapshot) -> None:
        self._current: UploadIntentSnapshot | None = initial
        self._read_overrides: list[UploadIntentSnapshot | None] = []
        self._attempts: dict[tuple[str, str, str], CopyAttemptRecord] = {}
        self.attempt_override: CopyAttemptRecord | None = None
        self.consistent_reads = 0
        self.pin_calls: list[str] = []
        self.attempt_calls: list[CopyAttemptRecord] = []
        self.commits: list[dict[str, Any]] = []

    def queue_consistent_reads(self, *snapshots: UploadIntentSnapshot | None) -> None:
        self._read_overrides.extend(snapshots)

    def get_intent_consistent(
        self,
        complaint_id: str,
        intent_id: str,
    ) -> UploadIntentSnapshot | None:
        self.consistent_reads += 1
        if self._read_overrides:
            return self._read_overrides.pop(0)
        if self._current and (
            self._current.complaint_id == complaint_id and self._current.intent_id == intent_id
        ):
            return self._current
        return None

    def pin_source_version(
        self,
        *,
        complaint_id: str,
        intent_id: str,
        lease_token: str,
        source_version_id: str,
        now: datetime,
    ) -> UploadIntentSnapshot:
        assert self._current is not None
        self.pin_calls.append(source_version_id)
        self._current = replace(self._current, source_version_id=source_version_id)
        return self._current

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
        if self.attempt_override is not None:
            self.attempt_calls.append(self.attempt_override)
            return self.attempt_override

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
                raise CopyVerificationError(
                    "Existing CopyAttempt record immutable fields do not match."
                )
            self.attempt_calls.append(existing)
            return existing

        self._attempts[attempt_key] = candidate
        self.attempt_calls.append(candidate)
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
    ) -> dict[str, Any]:
        payload = {
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
            "now": now,
        }
        self.commits.append(payload)
        return payload


def _attach_copy_header_spy(client: Any) -> list[dict[str, str]]:
    """Capture serialized HTTP headers emitted by Botocore for CopyObject requests."""
    captured: list[dict[str, str]] = []

    def _on_before_call(model: Any, params: dict[str, Any], **kwargs: Any) -> None:
        if getattr(model, "name", None) != "CopyObject":
            return None
        headers = params.get("headers", {})
        captured.append(
            {
                k: (v.decode("utf-8") if isinstance(v, bytes) else str(v))
                for k, v in headers.items()
            }
        )
        return None

    client.meta.events.register_first("before-call.*.*", _on_before_call)
    return captured


class _FakeLegacyModel:
    def __init__(self, members: dict[str, Any]) -> None:
        self._members = members

    def operation_model(self, name: str) -> Any:
        if name != "CopyObject":
            raise KeyError(name)
        return type("Op", (), {"input_shape": type("Shape", (), {"members": self._members})()})()


class TestConditionalCopyWorkerWithStubber(unittest.TestCase):
    """Focused Stubber and controlled-response tests for Stage B1 conditional-copy worker."""

    BUCKET = "campusfix-attachments-stub"
    COMPLAINT_ID = "CF-2026-0100"
    INTENT_ID = "intent-100"
    STAGING_KEY = "staging/complaints/CF-2026-0100/intent-100/upload.tmp"
    FINAL_KEY = "complaints/CF-2026-0100/images/intent-100.jpg"
    LEASE_TOKEN = "worker-lease-1"
    SRC_VID = "src-version-abc123"
    DEST_VID = "dest-version-xyz789"
    NOW = datetime(2026, 11, 25, 16, 0, 0, tzinfo=timezone.utc)

    def _new_stubbed_s3(self) -> tuple[Any, Stubber, Boto3S3StorageService]:
        client = boto3.client(
            "s3",
            config=Config(signature_version="s3v4"),
            **_DUMMY_AWS_CREDS,
        )
        stubber = Stubber(client)
        adapter = Boto3S3StorageService(client=client)
        return client, stubber, adapter

    def _make_snapshot(
        self,
        *,
        sha256_hex: str,
        max_byte_size: int = 4096,
        source_version_id: str | None = SRC_VID,
        status: UploadIntentStatus = UploadIntentStatus.CONFIRMING,
        lease_token: str | None = LEASE_TOKEN,
        lease_delta_seconds: int = 30,
        expected_complaint_version: int = 3,
    ) -> UploadIntentSnapshot:
        return UploadIntentSnapshot(
            intent_id=self.INTENT_ID,
            complaint_id=self.COMPLAINT_ID,
            owner_id="std_100",
            staging_key=self.STAGING_KEY,
            final_key=self.FINAL_KEY,
            expected_mime="image/jpeg",
            max_byte_size=max_byte_size,
            expected_sha256=sha256_hex,
            status=status,
            confirmation_deadline=self.NOW + timedelta(minutes=15),
            lease_token=lease_token,
            lease_expires_at=self.NOW + timedelta(seconds=lease_delta_seconds),
            expected_complaint_version=expected_complaint_version,
            source_version_id=source_version_id,
        )

    def _stub_valid_source_checks(
        self,
        stubber: Stubber,
        payload: bytes,
        sha256_b64: str | None,
        *,
        version_id: str = SRC_VID,
        content_type: str = "image/jpeg",
    ) -> None:
        head_resp: dict[str, Any] = {
            "ContentLength": len(payload),
            "ContentType": content_type,
            "ETag": '"etag-src"',
            "VersionId": version_id,
        }
        if sha256_b64 is not None:
            head_resp["ChecksumSHA256"] = sha256_b64

        stubber.add_response(
            "head_object",
            head_resp,
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.STAGING_KEY,
                "VersionId": version_id,
                "ChecksumMode": "ENABLED",
            },
        )
        if sha256_b64 is not None:
            stubber.add_response(
                "get_object",
                {"Body": _streaming_body(payload[:12]), "VersionId": version_id},
                expected_params={
                    "Bucket": self.BUCKET,
                    "Key": self.STAGING_KEY,
                    "VersionId": version_id,
                    "Range": "bytes=0-11",
                },
            )
        else:
            stubber.add_response(
                "get_object",
                {
                    "Body": _streaming_body(payload),
                    "ContentLength": len(payload),
                    "VersionId": version_id,
                },
                expected_params={
                    "Bucket": self.BUCKET,
                    "Key": self.STAGING_KEY,
                    "VersionId": version_id,
                },
            )

    def test_stubber_wire_headers_and_versioned_destination_head_object(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        client, stubber, adapter = self._new_stubbed_s3()
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))

        captured_wire_headers = _attach_copy_header_spy(client)

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        stubber.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest"'},
                "CopySourceVersionId": self.SRC_VID,
                "VersionId": self.DEST_VID,
            },
            expected_params={
                "CopySource": {
                    "Bucket": self.BUCKET,
                    "Key": self.STAGING_KEY,
                    "VersionId": self.SRC_VID,
                },
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "IfNoneMatch": "*",
            },
        )
        # Proves destination HeadObject explicitly passes captured top-level VersionId.
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-dest"',
                "VersionId": self.DEST_VID,
            },
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "VersionId": self.DEST_VID,
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "delete_object",
            {"DeleteMarker": True, "VersionId": "dm-1"},
            expected_params={"Bucket": self.BUCKET, "Key": self.STAGING_KEY},
        )

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )

        with stubber:
            result = worker.confirm_upload(
                complaint_id=self.COMPLAINT_ID,
                intent_id=self.INTENT_ID,
                worker_token=self.LEASE_TOKEN,
            )

        stubber.assert_no_pending_responses()
        self.assertEqual(len(captured_wire_headers), 1)
        self.assertEqual(captured_wire_headers[0].get("If-None-Match"), "*")
        self.assertIn(f"versionId={self.SRC_VID}", captured_wire_headers[0].get("x-amz-copy-source", ""))
        self.assertEqual(result.destination_version_id, self.DEST_VID)
        self.assertEqual(state.pin_calls, [])
        self.assertEqual(len(state.attempt_calls), 1)
        self.assertEqual(state.attempt_calls[0].expected_complaint_version, 3)
        self.assertEqual(state.attempt_calls[0].status, "COPYING")
        self.assertEqual(len(state.commits), 1)
        self.assertEqual(state.commits[0]["destination_version_id"], self.DEST_VID)
        self.assertEqual(state.commits[0]["expected_sha256"], sha_hex)
        self.assertEqual(state.commits[0]["expected_mime"], "image/jpeg")
        self.assertEqual(state.commits[0]["max_byte_size"], 4096)
        self.assertEqual(state.commits[0]["expected_complaint_version"], 3)

    def test_missing_sdk_if_none_match_fails_closed_before_copy(self) -> None:
        payload = _make_jpeg()
        sha_hex, _ = _sha256_hex_and_b64(payload)

        class _LegacyClient:
            _service_model = _FakeLegacyModel({"Bucket": object(), "Key": object(), "CopySource": object()})

            def copy_object(self, **kwargs: Any) -> dict[str, Any]:
                raise AssertionError("copy_object must NEVER be called when SDK lacks IfNoneMatch.")

        # Constructor fail-closed check
        with self.assertRaises(SdkCapabilityError):
            Boto3S3StorageService(client=_LegacyClient())

        # Call-time worker fail-closed check (even if constructed with verify_capabilities=False)
        adapter = Boto3S3StorageService(client=_LegacyClient(), verify_capabilities=False)
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))
        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with self.assertRaises(SdkCapabilityError):
            worker.confirm_upload(
                complaint_id=self.COMPLAINT_ID,
                intent_id=self.INTENT_ID,
                worker_token=self.LEASE_TOKEN,
            )
        self.assertEqual(state.commits, [])

    def test_absent_empty_or_null_copy_version_id_fails_closed_without_commit(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)

        for bad_copy_resp in (
            {"CopyObjectResult": {"ETag": '"etag-dest"'}, "CopySourceVersionId": self.SRC_VID},
            {"CopyObjectResult": {"ETag": '"etag-dest"'}, "CopySourceVersionId": self.SRC_VID, "VersionId": "null"},
            {"CopyObjectResult": {"ETag": '"etag-dest"'}, "CopySourceVersionId": self.SRC_VID, "VersionId": ""},
        ):
            _, stubber, adapter = self._new_stubbed_s3()
            state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))
            self._stub_valid_source_checks(stubber, payload, sha_b64)
            stubber.add_response("copy_object", bad_copy_resp)

            worker = UploadConfirmationWorker(
                s3_storage=adapter,
                state_port=state,
                source_bucket=self.BUCKET,
                now_fn=lambda: self.NOW,
            )
            with stubber:
                with self.assertRaises(CopyVerificationError):
                    worker.confirm_upload(
                        complaint_id=self.COMPLAINT_ID,
                        intent_id=self.INTENT_ID,
                        worker_token=self.LEASE_TOKEN,
                    )
            stubber.assert_no_pending_responses()
            self.assertEqual(state.commits, [])

    def test_copy_source_version_id_mismatch_fails_closed_without_commit(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        _, stubber, adapter = self._new_stubbed_s3()
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        stubber.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest"'},
                "CopySourceVersionId": "unexpected-different-source-version",
                "VersionId": self.DEST_VID,
            },
        )

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(CopyVerificationError):
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber.assert_no_pending_responses()
        self.assertEqual(state.commits, [])

    def test_destination_head_size_or_version_mismatch_fails_closed_without_commit(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)

        for head_override in (
            {"ContentLength": len(payload) - 10, "ContentType": "image/jpeg", "VersionId": self.DEST_VID},
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "VersionId": "other-dest-v"},
        ):
            _, stubber, adapter = self._new_stubbed_s3()
            state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))
            self._stub_valid_source_checks(stubber, payload, sha_b64)
            stubber.add_response(
                "copy_object",
                {
                    "CopyObjectResult": {"ETag": '"etag-dest"'},
                    "CopySourceVersionId": self.SRC_VID,
                    "VersionId": self.DEST_VID,
                },
            )
            stubber.add_response(
                "head_object",
                head_override,
                expected_params={
                    "Bucket": self.BUCKET,
                    "Key": self.FINAL_KEY,
                    "VersionId": self.DEST_VID,
                    "ChecksumMode": "ENABLED",
                },
            )

            worker = UploadConfirmationWorker(
                s3_storage=adapter,
                state_port=state,
                source_bucket=self.BUCKET,
                now_fn=lambda: self.NOW,
            )
            with stubber:
                with self.assertRaises(CopyVerificationError):
                    worker.confirm_upload(
                        complaint_id=self.COMPLAINT_ID,
                        intent_id=self.INTENT_ID,
                        worker_token=self.LEASE_TOKEN,
                    )
            stubber.assert_no_pending_responses()
            self.assertEqual(state.commits, [])

    def test_409_retries_preserve_if_none_match_recheck_lease_and_succeed(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        client, stubber, adapter = self._new_stubbed_s3()
        snapshot = self._make_snapshot(sha256_hex=sha_hex, lease_delta_seconds=30)
        state = _SequenceStatePort(snapshot)

        captured_wire_headers = _attach_copy_header_spy(client)

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        for _ in range(2):
            stubber.add_client_error(
                "copy_object",
                service_error_code="ConditionalRequestConflict",
                service_message="Concurrent conditional write in progress.",
                http_status_code=409,
                expected_params={
                    "CopySource": {"Bucket": self.BUCKET, "Key": self.STAGING_KEY, "VersionId": self.SRC_VID},
                    "Bucket": self.BUCKET,
                    "Key": self.FINAL_KEY,
                    "IfNoneMatch": "*",
                },
            )
        stubber.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest"'},
                "CopySourceVersionId": self.SRC_VID,
                "VersionId": self.DEST_VID,
            },
            expected_params={
                "CopySource": {"Bucket": self.BUCKET, "Key": self.STAGING_KEY, "VersionId": self.SRC_VID},
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "IfNoneMatch": "*",
            },
        )
        stubber.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "VersionId": self.DEST_VID},
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "VersionId": self.DEST_VID,
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response("delete_object", {})

        recorded_sleeps: list[float] = []
        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            sleep_fn=recorded_sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
            now_fn=lambda: self.NOW,
        )

        with stubber:
            res = worker.confirm_upload(
                complaint_id=self.COMPLAINT_ID,
                intent_id=self.INTENT_ID,
                worker_token=self.LEASE_TOKEN,
            )

        stubber.assert_no_pending_responses()
        self.assertEqual([h.get("If-None-Match") for h in captured_wire_headers], ["*", "*", "*"])
        self.assertEqual(recorded_sleeps, [0.2, 0.4])
        # Initial consistent read + 2 consistent lease re-checks after each 409.
        self.assertEqual(state.consistent_reads, 3)
        self.assertEqual(res.destination_version_id, self.DEST_VID)
        self.assertEqual(len(state.commits), 1)

    def test_409_lease_lost_or_expired_aborts_without_retry_or_commit(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)

        # Case 1: lease stolen by another worker on consistent re-read after 409.
        _, stubber, adapter = self._new_stubbed_s3()
        initial = self._make_snapshot(sha256_hex=sha_hex)
        stolen = replace(initial, lease_token="other-worker-token")
        state = _SequenceStatePort(initial)
        state.queue_consistent_reads(initial, stolen)

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        stubber.add_client_error(
            "copy_object",
            service_error_code="ConditionalRequestConflict",
            http_status_code=409,
        )

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            sleep_fn=lambda _d: self.fail("Must not sleep after lease loss"),
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(StaleLeaseError):
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber.assert_no_pending_responses()
        self.assertEqual(state.commits, [])

        # Case 2: lease expired on consistent re-read after 409.
        _, stubber2, adapter2 = self._new_stubbed_s3()
        expired = replace(initial, lease_expires_at=self.NOW - timedelta(seconds=1))
        state2 = _SequenceStatePort(initial)
        state2.queue_consistent_reads(initial, expired)

        self._stub_valid_source_checks(stubber2, payload, sha_b64)
        stubber2.add_client_error(
            "copy_object",
            service_error_code="ConditionalRequestConflict",
            http_status_code=409,
        )
        worker2 = UploadConfirmationWorker(
            s3_storage=adapter2,
            state_port=state2,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber2:
            with self.assertRaises(StaleLeaseError):
                worker2.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber2.assert_no_pending_responses()
        self.assertEqual(state2.commits, [])

    def test_409_retry_exhaustion_fails_closed_preserving_if_none_match_and_no_commit(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        client, stubber, adapter = self._new_stubbed_s3()
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex, lease_delta_seconds=30))

        captured_wire_headers = _attach_copy_header_spy(client)

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        for _ in range(4):  # 1 initial + MAX_CONDITIONAL_COPY_CONFLICT_RETRIES (3) = 4 attempts
            stubber.add_client_error(
                "copy_object",
                service_error_code="ConditionalRequestConflict",
                http_status_code=409,
                expected_params={
                    "CopySource": {"Bucket": self.BUCKET, "Key": self.STAGING_KEY, "VersionId": self.SRC_VID},
                    "Bucket": self.BUCKET,
                    "Key": self.FINAL_KEY,
                    "IfNoneMatch": "*",
                },
            )

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            sleep_fn=lambda _d: None,
            jitter_fn=lambda _lo, _hi: 0.0,
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(CopyVerificationError):
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber.assert_no_pending_responses()
        self.assertEqual([h.get("If-None-Match") for h in captured_wire_headers], ["*", "*", "*", "*"])
        self.assertEqual(state.commits, [])

    def test_409_followed_by_412_recovers_and_commits_verified_existing_object(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        existing_vid = "existing-dest-v1"
        _, stubber, adapter = self._new_stubbed_s3()
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        stubber.add_client_error(
            "copy_object",
            service_error_code="ConditionalRequestConflict",
            http_status_code=409,
        )
        stubber.add_client_error(
            "copy_object",
            service_error_code="PreconditionFailed",
            http_status_code=412,
        )
        # 412 recovery: unversioned HEAD discovers existing_version_id, then versioned HEAD & full stream GET.
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-dest"',
                "ChecksumSHA256": sha_b64,
                "VersionId": existing_vid,
            },
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ETag": '"etag-dest"',
                "ChecksumSHA256": sha_b64,
                "VersionId": existing_vid,
            },
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "VersionId": existing_vid,
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "get_object",
            {
                "Body": _streaming_body(payload),
                "ContentLength": len(payload),
                "VersionId": existing_vid,
            },
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "VersionId": existing_vid,
            },
        )
        stubber.add_response("delete_object", {})

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            sleep_fn=lambda _d: None,
            jitter_fn=lambda _lo, _hi: 0.0,
            now_fn=lambda: self.NOW,
        )
        with stubber:
            res = worker.confirm_upload(
                complaint_id=self.COMPLAINT_ID,
                intent_id=self.INTENT_ID,
                worker_token=self.LEASE_TOKEN,
            )

        stubber.assert_no_pending_responses()
        self.assertEqual(res.destination_version_id, existing_vid)
        self.assertEqual(len(state.commits), 1)
        self.assertEqual(state.commits[0]["destination_version_id"], existing_vid)

    def test_412_recovery_rejects_corrupt_or_mismatched_existing_destination(self) -> None:
        source_payload = _make_jpeg(fill=b"A")
        src_hex, src_b64 = _sha256_hex_and_b64(source_payload)

        # Case A: Same size & MIME & even forged metadata, but streamed bytes hash differs -> fails closed.
        tampered_payload = _make_jpeg(fill=b"X")
        existing_vid = "corrupt-dest-v1"
        _, stubber, adapter = self._new_stubbed_s3()
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=src_hex))

        self._stub_valid_source_checks(stubber, source_payload, src_b64)
        stubber.add_client_error("copy_object", service_error_code="PreconditionFailed", http_status_code=412)
        stubber.add_response(
            "head_object",
            {"ContentLength": len(source_payload), "ContentType": "image/jpeg", "VersionId": existing_vid},
            expected_params={"Bucket": self.BUCKET, "Key": self.FINAL_KEY, "ChecksumMode": "ENABLED"},
        )
        stubber.add_response(
            "head_object",
            {"ContentLength": len(source_payload), "ContentType": "image/jpeg", "VersionId": existing_vid},
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "VersionId": existing_vid,
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "get_object",
            {"Body": _streaming_body(tampered_payload), "ContentLength": len(tampered_payload), "VersionId": existing_vid},
            expected_params={"Bucket": self.BUCKET, "Key": self.FINAL_KEY, "VersionId": existing_vid},
        )

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(DestinationConflictError):
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber.assert_no_pending_responses()
        self.assertEqual(state.commits, [])

        # Case B: Missing destination (404 / DeleteMarker race after 412) -> DestinationConflictError.
        _, stubber2, adapter2 = self._new_stubbed_s3()
        state2 = _SequenceStatePort(self._make_snapshot(sha256_hex=src_hex))
        self._stub_valid_source_checks(stubber2, source_payload, src_b64)
        stubber2.add_client_error("copy_object", service_error_code="PreconditionFailed", http_status_code=412)
        stubber2.add_client_error("head_object", service_error_code="404", http_status_code=404)

        worker2 = UploadConfirmationWorker(
            s3_storage=adapter2,
            state_port=state2,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber2:
            with self.assertRaises(DestinationConflictError):
                worker2.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber2.assert_no_pending_responses()
        self.assertEqual(state2.commits, [])

        # Case C: Destination existing object is wrong MIME (PNG instead of JPEG) -> DestinationConflictError.
        png_dest = _make_png()
        _, stubber3, adapter3 = self._new_stubbed_s3()
        state3 = _SequenceStatePort(self._make_snapshot(sha256_hex=src_hex))
        self._stub_valid_source_checks(stubber3, source_payload, src_b64)
        stubber3.add_client_error("copy_object", service_error_code="PreconditionFailed", http_status_code=412)
        stubber3.add_response(
            "head_object",
            {"ContentLength": len(png_dest), "ContentType": "image/png", "VersionId": existing_vid},
            expected_params={"Bucket": self.BUCKET, "Key": self.FINAL_KEY, "ChecksumMode": "ENABLED"},
        )
        stubber3.add_response(
            "head_object",
            {"ContentLength": len(png_dest), "ContentType": "image/png", "VersionId": existing_vid},
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.FINAL_KEY,
                "VersionId": existing_vid,
                "ChecksumMode": "ENABLED",
            },
        )
        worker3 = UploadConfirmationWorker(
            s3_storage=adapter3,
            state_port=state3,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber3:
            with self.assertRaises(DestinationConflictError):
                worker3.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber3.assert_no_pending_responses()
        self.assertEqual(state3.commits, [])

    def test_412_recovery_handles_stale_lease_and_already_verified_idempotency(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)

        # Already VERIFIED on 412 consistent re-read -> AlreadyVerifiedIdempotent (no commit).
        _, stubber, adapter = self._new_stubbed_s3()
        initial = self._make_snapshot(sha256_hex=sha_hex)
        verified = replace(initial, status=UploadIntentStatus.VERIFIED)
        state = _SequenceStatePort(initial)
        state.queue_consistent_reads(initial, verified)

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        stubber.add_client_error("copy_object", service_error_code="PreconditionFailed", http_status_code=412)

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(AlreadyVerifiedIdempotent) as ctx:
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        self.assertEqual(ctx.exception.attachment_key, self.FINAL_KEY)
        stubber.assert_no_pending_responses()
        self.assertEqual(state.commits, [])

        # Stale lease token on 412 consistent re-read -> StaleLeaseError (no commit).
        _, stubber2, adapter2 = self._new_stubbed_s3()
        stale = replace(initial, lease_token="newer-worker")
        state2 = _SequenceStatePort(initial)
        state2.queue_consistent_reads(initial, stale)
        self._stub_valid_source_checks(stubber2, payload, sha_b64)
        stubber2.add_client_error("copy_object", service_error_code="PreconditionFailed", http_status_code=412)

        worker2 = UploadConfirmationWorker(
            s3_storage=adapter2,
            state_port=state2,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber2:
            with self.assertRaises(StaleLeaseError):
                worker2.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber2.assert_no_pending_responses()
        self.assertEqual(state2.commits, [])

    def test_list_object_versions_pagination_and_malformed_or_zero_latest_fail_closed(self) -> None:
        _, stubber, adapter = self._new_stubbed_s3()
        sibling = f"{self.STAGING_KEY}.part"

        # Page 1: only sibling key + truncation with both NextKeyMarker and NextVersionIdMarker.
        stubber.add_response(
            "list_object_versions",
            {
                "IsTruncated": True,
                "NextKeyMarker": self.STAGING_KEY,
                "NextVersionIdMarker": "cursor-vid-1",
                "Versions": [{"Key": sibling, "VersionId": "sib-v1", "IsLatest": True}],
                "DeleteMarkers": [],
            },
            expected_params={"Bucket": self.BUCKET, "Prefix": self.STAGING_KEY},
        )
        # Page 2: target exact version arrives on subsequent page.
        stubber.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [{"Key": self.STAGING_KEY, "VersionId": self.SRC_VID, "IsLatest": True}],
                "DeleteMarkers": [],
            },
            expected_params={
                "Bucket": self.BUCKET,
                "Prefix": self.STAGING_KEY,
                "KeyMarker": self.STAGING_KEY,
                "VersionIdMarker": "cursor-vid-1",
            },
        )
        with stubber:
            resolved = adapter.list_object_versions_exact(self.BUCKET, self.STAGING_KEY)
        self.assertEqual(resolved, self.SRC_VID)
        stubber.assert_no_pending_responses()

        # Multiple IsLatest=True across Versions + DeleteMarkers -> UploadAmbiguousVersionError
        _, stubber2, adapter2 = self._new_stubbed_s3()
        stubber2.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [{"Key": self.STAGING_KEY, "VersionId": "v1", "IsLatest": True}],
                "DeleteMarkers": [{"Key": self.STAGING_KEY, "VersionId": "dm1", "IsLatest": True}],
            },
        )
        with stubber2:
            with self.assertRaises(UploadAmbiguousVersionError):
                adapter2.list_object_versions_exact(self.BUCKET, self.STAGING_KEY)

        # Truncated page without advancing NextKeyMarker -> fail closed
        _, stubber3, adapter3 = self._new_stubbed_s3()
        stubber3.add_response(
            "list_object_versions",
            {
                "IsTruncated": True,
                "Versions": [{"Key": self.STAGING_KEY, "VersionId": "v1", "IsLatest": True}],
            },
        )
        with stubber3:
            with self.assertRaises(UploadAmbiguousVersionError):
                adapter3.list_object_versions_exact(self.BUCKET, self.STAGING_KEY)

    def test_source_absent_checksum_uses_stream_verification_and_rejects_tampered_bytes(self) -> None:
        expected_jpeg = _make_jpeg(fill=b"E")
        expected_hex, _ = _sha256_hex_and_b64(expected_jpeg)
        actual_wrong_jpeg = _make_jpeg(fill=b"Z")

        _, stubber, adapter = self._new_stubbed_s3()
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=expected_hex))
        # HeadObject omits ChecksumSHA256; streamed GetObject(VersionId=...) detects wrong digest.
        self._stub_valid_source_checks(stubber, actual_wrong_jpeg, sha256_b64=None)

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(ValidationError):
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber.assert_no_pending_responses()
        self.assertEqual(state.commits, [])

    def test_unpinned_missing_staging_raises_upload_not_ready_and_cleanup_error_is_nonfatal(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)

        # Unpinned empty S3 staging key -> UploadNotReadyError
        _, stubber_empty, adapter_empty = self._new_stubbed_s3()
        state_empty = _SequenceStatePort(
            self._make_snapshot(sha256_hex=sha_hex, source_version_id=None)
        )
        stubber_empty.add_response(
            "list_object_versions",
            {"IsTruncated": False, "Versions": [], "DeleteMarkers": []},
        )
        worker_empty = UploadConfirmationWorker(
            s3_storage=adapter_empty,
            state_port=state_empty,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber_empty:
            with self.assertRaises(UploadNotReadyError):
                worker_empty.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        self.assertEqual(state_empty.commits, [])

        # Best-effort staging DeleteObject failure after commit does NOT break confirmation result.
        _, stubber_del, adapter_del = self._new_stubbed_s3()
        state_del = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))
        self._stub_valid_source_checks(stubber_del, payload, sha_b64)
        stubber_del.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest"'},
                "CopySourceVersionId": self.SRC_VID,
                "VersionId": self.DEST_VID,
            },
        )
        stubber_del.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "VersionId": self.DEST_VID},
        )
        stubber_del.add_client_error("delete_object", service_error_code="AccessDenied", http_status_code=403)

        worker_del = UploadConfirmationWorker(
            s3_storage=adapter_del,
            state_port=state_del,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber_del:
            res = worker_del.confirm_upload(
                complaint_id=self.COMPLAINT_ID,
                intent_id=self.INTENT_ID,
                worker_token=self.LEASE_TOKEN,
            )
        stubber_del.assert_no_pending_responses()
        self.assertEqual(res.status, "VERIFIED")
        self.assertEqual(len(state_del.commits), 1)

    def test_conflicting_explicit_source_version_id_fails_closed(self) -> None:
        payload = _make_jpeg()
        sha_hex, _ = _sha256_hex_and_b64(payload)
        _, stubber, adapter = self._new_stubbed_s3()
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex, source_version_id="pinned-v1"))

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(CopyVerificationError):
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                    source_version_id="different-v2",
                )
        stubber.assert_no_pending_responses()
        self.assertEqual(state.attempt_calls, [])
        self.assertEqual(state.commits, [])

    def test_source_version_drift_during_409_retry_fails_closed(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        _, stubber, adapter = self._new_stubbed_s3()
        initial = self._make_snapshot(sha256_hex=sha_hex)
        drifted = replace(initial, source_version_id="mutated-src-version")
        state = _SequenceStatePort(initial)
        state.queue_consistent_reads(initial, drifted)

        self._stub_valid_source_checks(stubber, payload, sha_b64)
        stubber.add_client_error(
            "copy_object",
            service_error_code="ConditionalRequestConflict",
            http_status_code=409,
        )

        worker = UploadConfirmationWorker(
            s3_storage=adapter,
            state_port=state,
            source_bucket=self.BUCKET,
            sleep_fn=lambda _d: self.fail("Must not sleep or retry after source_version_id drift"),
            now_fn=lambda: self.NOW,
        )
        with stubber:
            with self.assertRaises(CopyVerificationError):
                worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stubber.assert_no_pending_responses()
        self.assertEqual(len(state.attempt_calls), 1)
        self.assertEqual(state.commits, [])

    def test_immutable_field_drift_during_412_recovery_fails_closed(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        initial = self._make_snapshot(sha256_hex=sha_hex)

        drift_cases = (
            replace(initial, source_version_id="drifted-src-v"),
            replace(initial, final_key="complaints/CF-2026-0100/images/hijacked.jpg"),
            replace(initial, expected_sha256="f" * 64),
            replace(initial, expected_mime="image/png"),
            replace(initial, max_byte_size=2048),
        )
        for drifted in drift_cases:
            _, stubber, adapter = self._new_stubbed_s3()
            state = _SequenceStatePort(initial)
            state.queue_consistent_reads(initial, drifted)

            self._stub_valid_source_checks(stubber, payload, sha_b64)
            stubber.add_client_error(
                "copy_object",
                service_error_code="PreconditionFailed",
                http_status_code=412,
            )

            worker = UploadConfirmationWorker(
                s3_storage=adapter,
                state_port=state,
                source_bucket=self.BUCKET,
                now_fn=lambda: self.NOW,
            )
            with stubber:
                with self.assertRaises(CopyVerificationError):
                    worker.confirm_upload(
                        complaint_id=self.COMPLAINT_ID,
                        intent_id=self.INTENT_ID,
                        worker_token=self.LEASE_TOKEN,
                    )
            # Stubber queued zero recovery head_object calls, proving S3 recovery never started.
            stubber.assert_no_pending_responses()
            self.assertEqual(state.commits, [])

    def test_list_object_versions_zero_latest_malformed_and_empty_version_marker(self) -> None:
        # 1. Zero IsLatest=True entries -> UploadAmbiguousVersionError
        _, stub_zero, adapter_zero = self._new_stubbed_s3()
        stub_zero.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [{"Key": self.STAGING_KEY, "VersionId": "v1", "IsLatest": False}],
                "DeleteMarkers": [],
            },
        )
        with stub_zero:
            with self.assertRaises(UploadAmbiguousVersionError):
                adapter_zero.list_object_versions_exact(self.BUCKET, self.STAGING_KEY)

        # 2. Malformed IsLatest (non-bool / missing) -> UploadAmbiguousVersionError
        _, stub_bad_bool, adapter_bad_bool = self._new_stubbed_s3()
        stub_bad_bool.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [{"Key": self.STAGING_KEY, "VersionId": "v1"}],
                "DeleteMarkers": [],
            },
        )
        with stub_bad_bool:
            with self.assertRaises(UploadAmbiguousVersionError):
                adapter_bad_bool.list_object_versions_exact(self.BUCKET, self.STAGING_KEY)

        # 3. Non-list Versions / DeleteMarkers returned by malformed client -> UploadAmbiguousVersionError
        class _MalformedClient:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
                return self._payload

        for bad_payload in (
            {"IsTruncated": False, "Versions": "not-a-list"},
            {"IsTruncated": False, "Versions": [], "DeleteMarkers": {"bad": True}},
            {"IsTruncated": False, "Versions": [{"Key": self.STAGING_KEY, "VersionId": "v1", "IsLatest": "true"}]},
        ):
            bad_adapter = Boto3S3StorageService(client=_MalformedClient(bad_payload), verify_capabilities=False)
            with self.assertRaises(UploadAmbiguousVersionError):
                bad_adapter.list_object_versions_exact(self.BUCKET, self.STAGING_KEY)

        # 4. Empty NextVersionIdMarker treated as absent (VersionIdMarker omitted on next page)
        _, stub_empty_marker, adapter_empty_marker = self._new_stubbed_s3()
        stub_empty_marker.add_response(
            "list_object_versions",
            {
                "IsTruncated": True,
                "NextKeyMarker": self.STAGING_KEY,
                "NextVersionIdMarker": "",
                "Versions": [{"Key": f"{self.STAGING_KEY}.other", "VersionId": "s1", "IsLatest": True}],
                "DeleteMarkers": [],
            },
            expected_params={"Bucket": self.BUCKET, "Prefix": self.STAGING_KEY},
        )
        stub_empty_marker.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [{"Key": self.STAGING_KEY, "VersionId": self.SRC_VID, "IsLatest": True}],
                "DeleteMarkers": [],
            },
            expected_params={"Bucket": self.BUCKET, "Prefix": self.STAGING_KEY, "KeyMarker": self.STAGING_KEY},
        )
        with stub_empty_marker:
            self.assertEqual(
                adapter_empty_marker.list_object_versions_exact(self.BUCKET, self.STAGING_KEY),
                self.SRC_VID,
            )
        stub_empty_marker.assert_no_pending_responses()

    def test_streaming_hash_truncated_stream_midstream_errors_and_body_closure(self) -> None:
        payload = _make_jpeg()
        sha_hex, _ = _sha256_hex_and_b64(payload)

        class _TrackingBody:
            def __init__(self, chunks: list[bytes], raise_on_read: Exception | None = None) -> None:
                self._chunks = list(chunks)
                self._raise = raise_on_read
                self.closed = False

            def read(self, _amt: int | None = None) -> bytes:
                if self._chunks:
                    return self._chunks.pop(0)
                if self._raise is not None:
                    raise self._raise
                return b""

            def close(self) -> None:
                self.closed = True

        # 1. Successful read closes body.
        good_body = _TrackingBody([payload[:1024], payload[1024:]])
        class _StreamClient:
            def __init__(self, body: _TrackingBody, content_length: int) -> None:
                self._body = body
                self._len = content_length

            def get_object(self, **kwargs: Any) -> dict[str, Any]:
                return {"Body": self._body, "ContentLength": self._len, "VersionId": kwargs["VersionId"]}

        good_adapter = Boto3S3StorageService(
            client=_StreamClient(good_body, len(payload)),
            verify_capabilities=False,
        )
        streamed = good_adapter.read_object_and_compute_sha256(
            self.BUCKET,
            self.STAGING_KEY,
            version_id=self.SRC_VID,
            max_byte_size=4096,
        )
        self.assertEqual(streamed.sha256_checksum, sha_hex)
        self.assertTrue(good_body.closed)

        # 2. Truncated stream (ends before declared ContentLength) -> ValidationError & body closed; worker aborts.
        trunc_body = _TrackingBody([payload[:512]])
        trunc_adapter = Boto3S3StorageService(
            client=_StreamClient(trunc_body, len(payload)),
            verify_capabilities=False,
        )
        with self.assertRaises(ValidationError):
            trunc_adapter.read_object_and_compute_sha256(
                self.BUCKET,
                self.STAGING_KEY,
                version_id=self.SRC_VID,
                max_byte_size=4096,
            )
        self.assertTrue(trunc_body.closed)

        # Also verify worker aborts without copying/committing on truncated source stream.
        _, stub_trunc, worker_trunc_adapter = self._new_stubbed_s3()
        state_trunc = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))
        stub_trunc.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "VersionId": self.SRC_VID},
        )
        stub_trunc.add_response(
            "get_object",
            {"Body": _streaming_body(payload[:512]), "ContentLength": len(payload), "VersionId": self.SRC_VID},
        )
        trunc_worker = UploadConfirmationWorker(
            s3_storage=worker_trunc_adapter,
            state_port=state_trunc,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stub_trunc:
            with self.assertRaises(ValidationError):
                trunc_worker.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        self.assertEqual(state_trunc.attempt_calls, [])
        self.assertEqual(state_trunc.commits, [])

        # 3. Mid-stream IOError and BotoCoreError (IncompleteReadError) -> CopyVerificationError & body closed.
        for stream_err in (
            IOError("socket reset"),
            IncompleteReadError(actual_bytes=512, expected_bytes=len(payload)),
        ):
            err_body = _TrackingBody([payload[:512]], raise_on_read=stream_err)
            err_adapter = Boto3S3StorageService(
                client=_StreamClient(err_body, len(payload)),
                verify_capabilities=False,
            )
            with self.assertRaises(CopyVerificationError) as ctx:
                err_adapter.read_object_and_compute_sha256(
                    self.BUCKET,
                    self.STAGING_KEY,
                    version_id=self.SRC_VID,
                    max_byte_size=4096,
                )
            self.assertIs(ctx.exception.__cause__, stream_err)
            self.assertTrue(err_body.closed)

    def test_get_object_range_version_mismatch_fails_and_omitted_version_succeeds(self) -> None:
        payload = _make_jpeg()

        # Mismatched VersionId -> CopyVerificationError & closes Body
        _, stub_mismatch, adapter_mismatch = self._new_stubbed_s3()
        stub_mismatch.add_response(
            "get_object",
            {"Body": _streaming_body(payload[:12]), "VersionId": "wrong-version-id"},
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.STAGING_KEY,
                "Range": "bytes=0-11",
                "VersionId": self.SRC_VID,
            },
        )
        with stub_mismatch:
            with self.assertRaises(CopyVerificationError):
                adapter_mismatch.get_object_range(
                    self.BUCKET,
                    self.STAGING_KEY,
                    0,
                    11,
                    version_id=self.SRC_VID,
                )
        stub_mismatch.assert_no_pending_responses()

        # Omitted VersionId in response preserves compatibility
        _, stub_omitted, adapter_omitted = self._new_stubbed_s3()
        stub_omitted.add_response(
            "get_object",
            {"Body": _streaming_body(payload[:12])},
            expected_params={
                "Bucket": self.BUCKET,
                "Key": self.STAGING_KEY,
                "Range": "bytes=0-11",
                "VersionId": self.SRC_VID,
            },
        )
        with stub_omitted:
            header = adapter_omitted.get_object_range(
                self.BUCKET,
                self.STAGING_KEY,
                0,
                11,
                version_id=self.SRC_VID,
            )
        self.assertEqual(header, payload[:12])
        stub_omitted.assert_no_pending_responses()

    def test_begin_copy_attempt_order_idempotency_and_immutable_mismatch_rejection(self) -> None:
        payload = _make_jpeg()
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex, expected_complaint_version=7))

        # Idempotent identical repeat succeeds; immutable change fails closed.
        first = state.begin_copy_attempt(
            complaint_id=self.COMPLAINT_ID,
            intent_id=self.INTENT_ID,
            lease_token=self.LEASE_TOKEN,
            source_version_id=self.SRC_VID,
            final_key=self.FINAL_KEY,
            expected_sha256=sha_hex,
            expected_mime="image/jpeg",
            max_byte_size=4096,
            expected_complaint_version=7,
            now=self.NOW,
        )
        second = state.begin_copy_attempt(
            complaint_id=self.COMPLAINT_ID,
            intent_id=self.INTENT_ID,
            lease_token=self.LEASE_TOKEN,
            source_version_id=self.SRC_VID,
            final_key=self.FINAL_KEY,
            expected_sha256=sha_hex,
            expected_mime="image/jpeg",
            max_byte_size=4096,
            expected_complaint_version=7,
            now=self.NOW,
        )
        self.assertEqual(first, second)
        with self.assertRaises(CopyVerificationError):
            state.begin_copy_attempt(
                complaint_id=self.COMPLAINT_ID,
                intent_id=self.INTENT_ID,
                lease_token=self.LEASE_TOKEN,
                source_version_id="other-src-v",
                final_key=self.FINAL_KEY,
                expected_sha256=sha_hex,
                expected_mime="image/jpeg",
                max_byte_size=4096,
                expected_complaint_version=7,
                now=self.NOW,
            )

        # Worker fails closed BEFORE CopyObject if attempt status is not COPYING.
        _, stub_bad_attempt, adapter_bad_attempt = self._new_stubbed_s3()
        bad_attempt_state = _SequenceStatePort(self._make_snapshot(sha256_hex=sha_hex))
        bad_attempt_state.attempt_override = CopyAttemptRecord(
            complaint_id=self.COMPLAINT_ID,
            intent_id=self.INTENT_ID,
            lease_token=self.LEASE_TOKEN,
            source_version_id=self.SRC_VID,
            final_key=self.FINAL_KEY,
            expected_sha256=sha_hex,
            expected_mime="image/jpeg",
            max_byte_size=4096,
            expected_complaint_version=3,
            status="COMMITTED",
        )
        self._stub_valid_source_checks(stub_bad_attempt, payload, sha_b64)
        worker_bad = UploadConfirmationWorker(
            s3_storage=adapter_bad_attempt,
            state_port=bad_attempt_state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stub_bad_attempt:
            with self.assertRaises(CopyVerificationError):
                worker_bad.confirm_upload(
                    complaint_id=self.COMPLAINT_ID,
                    intent_id=self.INTENT_ID,
                    worker_token=self.LEASE_TOKEN,
                )
        stub_bad_attempt.assert_no_pending_responses()
        self.assertEqual(bad_attempt_state.commits, [])

        # Confirm event ordering: begin_copy_attempt fires AFTER source validation and BEFORE CopyObject,
        # even when source_version_id was already pinned.
        _, stub_order, adapter_order = self._new_stubbed_s3()
        order_log: list[str] = []

        class _OrderTrackingState(_SequenceStatePort):
            def begin_copy_attempt(self, **kwargs: Any) -> CopyAttemptRecord:
                order_log.append("begin_copy_attempt")
                return super().begin_copy_attempt(**kwargs)

            def commit_verified_attachment(self, **kwargs: Any) -> dict[str, Any]:
                order_log.append("commit_verified_attachment")
                return super().commit_verified_attachment(**kwargs)

        adapter_order.client.meta.events.register_first(
            "before-call.*.*",
            lambda model, **kw: order_log.append(f"s3:{model.name}") and None,
        )
        order_state = _OrderTrackingState(
            self._make_snapshot(sha256_hex=sha_hex, expected_complaint_version=9)
        )
        self._stub_valid_source_checks(stub_order, payload, sha_b64)
        stub_order.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest"'},
                "CopySourceVersionId": self.SRC_VID,
                "VersionId": self.DEST_VID,
            },
        )
        stub_order.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "VersionId": self.DEST_VID},
        )
        stub_order.add_response("delete_object", {})
        order_worker = UploadConfirmationWorker(
            s3_storage=adapter_order,
            state_port=order_state,
            source_bucket=self.BUCKET,
            now_fn=lambda: self.NOW,
        )
        with stub_order:
            order_worker.confirm_upload(
                complaint_id=self.COMPLAINT_ID,
                intent_id=self.INTENT_ID,
                worker_token=self.LEASE_TOKEN,
            )
        self.assertEqual(
            order_log,
            [
                "s3:HeadObject",
                "s3:GetObject",
                "begin_copy_attempt",
                "s3:CopyObject",
                "s3:HeadObject",
                "commit_verified_attachment",
                "s3:DeleteObject",
            ],
        )
        self.assertEqual(order_state.commits[0]["expected_complaint_version"], 9)
