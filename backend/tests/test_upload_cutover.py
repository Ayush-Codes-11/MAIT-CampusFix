"""Stage B2 Step 4 upload-service cutover tests (local Stubber and Moto only)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import unittest
from unittest.mock import patch

import boto3
from botocore.config import Config
from botocore.stub import Stubber

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover - optional in MSYS2 test env
    mock_aws = None

from campusfix.api.handler import create_api_handler, create_api_handler_from_env
from campusfix.bootstrap import (
    DEFAULT_S3_ATTACHMENTS_BUCKET,
    ENV_S3_ATTACHMENTS_BUCKET,
    ENV_UPLOAD_BACKEND,
    UPLOAD_BACKEND_MEMORY,
    UPLOAD_BACKEND_S3,
    create_application,
    resolve_upload_config,
)
from campusfix.domain.models import (
    UploadAttemptStatus,
    UploadIntentStatus,
    ValidationError,
)
from campusfix.domain.rules import (
    CONFIRMATION_DEADLINE_SECONDS,
    MAX_CONFIRM_RETRIES,
    ImmutableFieldViolationError,
    StorageThrottleError,
)
from campusfix.repositories.dynamodb import DynamoDBComplaintRepository
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.boto3_s3_storage import (
    Boto3S3StorageService,
    CopyVerificationError,
)
from campusfix.services.s3_capabilities import SdkCapabilityError
from campusfix.services.s3_storage import InMemoryS3StorageService
from campusfix.services.upload_confirmation_worker import UploadConfirmationWorker
from campusfix.services.uploads import UploadIntentService


_DUMMY_AWS_CREDS = {
    "aws_access_key_id": "test",
    "aws_secret_access_key": "test",
    "aws_session_token": "test",
    "region_name": "ap-southeast-2",
}


def _make_valid_jpeg(size: int = 2048, fill: bytes = b"A") -> bytes:
    return b"\xff\xd8\xff\xe0" + (fill * (size - 4))


def _sha256_hex_and_b64(payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).digest()
    return digest.hex().lower(), base64.b64encode(digest).decode("ascii")


def _api_event(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    user_id: str = "student-1",
    role_group: str = "students",
) -> dict:
    return {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": user_id,
                        "token_use": "access",
                        "cognito:groups": [role_group],
                    }
                }
            },
        },
        "rawPath": path,
        "headers": {"origin": "http://localhost:5173"},
        "body": json.dumps(body) if body is not None else None,
    }


def _create_moto_table(ddb_client, table_name: str) -> None:
    ddb_client.create_table(
        TableName=table_name,
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


def _create_moto_versioned_bucket(s3_client, bucket: str, region: str) -> None:
    s3_client.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": region},
    )
    s3_client.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )


class _MutableClock:
    def __init__(self, initial: datetime) -> None:
        self.now = initial

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class TestUploadBackendConfigurationAndWiring(unittest.TestCase):
    """Verify explicit opt-in S3 backend selection while keeping local memory default safe."""

    def test_default_upload_backend_is_memory_with_zero_boto3_calls(self) -> None:
        with patch("boto3.client", side_effect=AssertionError("boto3.client must not be called for memory mode")):
            with patch.dict(os.environ, {}, clear=True):
                app = create_application()

        self.assertEqual(app.upload_backend, UPLOAD_BACKEND_MEMORY)
        self.assertEqual(app.upload_bucket, DEFAULT_S3_ATTACHMENTS_BUCKET)
        self.assertIsInstance(app.repository, InMemoryComplaintRepository)
        self.assertIsInstance(app.s3_storage, InMemoryS3StorageService)
        self.assertIsNone(app.confirmation_worker)
        self.assertIsNone(app.uploads_service._confirmation_worker)
        self.assertIs(app.uploads_service._complaint_repo, app.repository)
        self.assertIs(app.uploads_service._intent_repo, app.repository)
        self.assertIs(app.versioned_upload_state_adapter._complaint_repo, app.repository)
        self.assertIs(app.versioned_upload_state_adapter._intent_repo, app.repository)

    def test_explicit_s3_backend_wires_worker_and_shared_adapter(self) -> None:
        stubbed_s3 = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
        env = {
            ENV_UPLOAD_BACKEND: "S3",
            ENV_S3_ATTACHMENTS_BUCKET: "campusfix-cutover-bucket",
            "AWS_REGION": "ap-southeast-2",
        }
        app = create_application(s3_client=stubbed_s3, environ=env)

        self.assertEqual(app.upload_backend, UPLOAD_BACKEND_S3)
        self.assertEqual(app.upload_bucket, "campusfix-cutover-bucket")
        self.assertEqual(app.aws_region, "ap-southeast-2")
        self.assertIsInstance(app.repository, InMemoryComplaintRepository)
        self.assertIsInstance(app.s3_storage, Boto3S3StorageService)
        self.assertIsInstance(app.confirmation_worker, UploadConfirmationWorker)
        self.assertIs(app.uploads_service._confirmation_worker, app.confirmation_worker)
        self.assertIs(app.uploads_service._s3, app.s3_storage)
        self.assertIn(app.versioned_upload_state_adapter, vars(app.confirmation_worker).values())
        self.assertIs(app.versioned_upload_state_adapter._complaint_repo, app.repository)
        self.assertIs(app.versioned_upload_state_adapter._intent_repo, app.repository)
        self.assertIs(app.complaints_service._repository, app.repository)

    def test_invalid_or_incomplete_upload_backend_config_fails_closed(self) -> None:
        with self.assertRaises(ValidationError):
            resolve_upload_config(environ={ENV_UPLOAD_BACKEND: ""})

        with self.assertRaises(ValidationError):
            resolve_upload_config(environ={ENV_UPLOAD_BACKEND: "   "})

        with self.assertRaises(ValidationError):
            resolve_upload_config(environ={ENV_UPLOAD_BACKEND: "gcs"})

        with self.assertRaises(ValidationError):
            resolve_upload_config(environ={ENV_UPLOAD_BACKEND: UPLOAD_BACKEND_S3})

        with self.assertRaises(ValidationError):
            resolve_upload_config(
                environ={
                    ENV_UPLOAD_BACKEND: UPLOAD_BACKEND_S3,
                    ENV_S3_ATTACHMENTS_BUCKET: "   ",
                }
            )

        stubbed_s3 = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
        with self.assertRaises(ValidationError):
            create_application(s3_client=stubbed_s3, environ={})

        with self.assertRaises(ValidationError):
            create_application(
                s3_storage=Boto3S3StorageService(client=stubbed_s3),
                environ={},
            )

        with self.assertRaises(ValidationError):
            create_application(
                upload_backend=UPLOAD_BACKEND_S3,
                bucket_name="campusfix-cutover-bucket",
                s3_storage=InMemoryS3StorageService(),
                environ={},
            )

        repo = InMemoryComplaintRepository()
        with self.assertRaises(ValidationError):
            UploadIntentService(
                complaint_repository=repo,
                intent_repository=repo,
                s3_storage=Boto3S3StorageService(client=stubbed_s3),
            )


class TestUploadCutoverStubberAndApiErrors(unittest.TestCase):
    """Verify real Botocore HTTP headers, conditional copy retries/412, and Stage B2 API mappings."""

    BUCKET = "campusfix-stubber-bucket"
    REGION = "ap-southeast-2"

    def _new_app_with_stubber(self):
        client = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
        stubber = Stubber(client)
        clock = _MutableClock(datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone.utc))
        env = {
            ENV_UPLOAD_BACKEND: UPLOAD_BACKEND_S3,
            ENV_S3_ATTACHMENTS_BUCKET: self.BUCKET,
            "AWS_REGION": self.REGION,
        }
        app = create_application(s3_client=client, now_fn=clock, environ=env)
        handler = create_api_handler(
            complaints_service=app.complaints_service,
            uploads_service=app.uploads_service,
        )
        return app, handler, stubber, clock

    def _create_complaint_and_intent(self, handler, sha_hex: str, size: int) -> tuple[str, dict]:
        create_res = handler(
            _api_event(
                "POST",
                "/v1/complaints",
                {
                    "title": "Water leak near staircase",
                    "description": "Continuous ceiling drip near first floor landing outside room 108.",
                    "building": "Block A",
                    "floor": "1st",
                    "roomNumber": "108",
                    "category": "Plumbing",
                },
            ),
            None,
        )
        self.assertEqual(create_res["statusCode"], 201)
        cid = json.loads(create_res["body"])["id"]

        intent_res = handler(
            _api_event(
                "POST",
                f"/v1/complaints/{cid}/images/upload-intent",
                {
                    "contentType": "image/jpeg",
                    "byteSize": size,
                    "checksumSha256": sha_hex,
                },
            ),
            None,
        )
        self.assertEqual(intent_res["statusCode"], 200)
        return cid, json.loads(intent_res["body"])

    def test_stubber_happy_path_enforces_if_none_match_and_source_version_pinning(self) -> None:
        payload = _make_valid_jpeg(2048)
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        app, handler, stubber, _ = self._new_app_with_stubber()
        cid, intent_body = self._create_complaint_and_intent(handler, sha_hex, len(payload))
        intent_id = intent_body["intentId"]
        intent = app.repository.get_intent_consistent(cid, intent_id)
        self.assertIsNotNone(intent)
        assert intent is not None

        self.assertEqual(intent_body["fields"]["x-amz-checksum-sha256"], sha_b64)
        self.assertEqual(intent_body["fields"]["x-amz-checksum-algorithm"], "SHA256")

        stubber.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [
                    {
                        "Key": intent.staging_key,
                        "VersionId": "v-stage-1",
                        "IsLatest": True,
                        "Size": len(payload),
                    }
                ],
                "DeleteMarkers": [],
            },
            {"Bucket": self.BUCKET, "Prefix": intent.staging_key},
        )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ChecksumSHA256": sha_b64,
                "ETag": '"etag-stage-1"',
                "VersionId": "v-stage-1",
            },
            {
                "Bucket": self.BUCKET,
                "Key": intent.staging_key,
                "VersionId": "v-stage-1",
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "get_object",
            {"Body": io.BytesIO(payload[:12]), "VersionId": "v-stage-1"},
            {
                "Bucket": self.BUCKET,
                "Key": intent.staging_key,
                "VersionId": "v-stage-1",
                "Range": "bytes=0-11",
            },
        )
        stubber.add_response(
            "copy_object",
            {
                "CopyObjectResult": {"ETag": '"etag-dest-1"'},
                "VersionId": "v-dest-1",
            },
            {
                "Bucket": self.BUCKET,
                "Key": intent.final_key,
                "CopySource": {
                    "Bucket": self.BUCKET,
                    "Key": intent.staging_key,
                    "VersionId": "v-stage-1",
                },
                "IfNoneMatch": "*",
            },
        )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ChecksumSHA256": sha_b64,
                "ETag": '"etag-dest-1"',
                "VersionId": "v-dest-1",
            },
            {
                "Bucket": self.BUCKET,
                "Key": intent.final_key,
                "VersionId": "v-dest-1",
                "ChecksumMode": "ENABLED",
            },
        )
        stubber.add_response(
            "delete_object",
            {"DeleteMarker": False},
            {"Bucket": self.BUCKET, "Key": intent.staging_key},
        )

        with stubber:
            confirm_res = handler(
                _api_event(
                    "POST",
                    f"/v1/complaints/{cid}/images/confirm",
                    {"intentId": intent_id},
                ),
                None,
            )

        stubber.assert_no_pending_responses()
        self.assertEqual(confirm_res["statusCode"], 200)
        self.assertEqual(
            json.loads(confirm_res["body"]),
            {"status": "VERIFIED", "attachmentKey": intent.final_key},
        )

        stored_intent = app.repository.get_intent_consistent(cid, intent_id)
        assert stored_intent is not None
        self.assertEqual(stored_intent.status, UploadIntentStatus.VERIFIED)
        self.assertEqual(stored_intent.source_version_id, "v-stage-1")

        stored_complaint = app.repository.get_consistent(cid)
        assert stored_complaint is not None
        self.assertEqual(stored_complaint.version, 2)
        self.assertEqual(stored_complaint.attachment_key, intent.final_key)
        self.assertEqual(stored_complaint.attachment_status, "VERIFIED")

        dl_res = handler(
            _api_event("GET", f"/v1/complaints/{cid}/images/download-url"),
            None,
        )
        self.assertEqual(dl_res["statusCode"], 200)
        dl_body = json.loads(dl_res["body"])
        self.assertIn(intent.final_key, dl_body["downloadUrl"])

    def test_stubber_409_transient_retry_then_succeeds(self) -> None:
        payload = _make_valid_jpeg(2048)
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)
        app, handler, stubber, _ = self._new_app_with_stubber()
        cid, intent_body = self._create_complaint_and_intent(handler, sha_hex, len(payload))
        intent_id = intent_body["intentId"]
        intent = app.repository.get_intent_consistent(cid, intent_id)
        assert intent is not None

        stubber.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [{"Key": intent.staging_key, "VersionId": "v-s1", "IsLatest": True}],
                "DeleteMarkers": [],
            },
        )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ChecksumSHA256": sha_b64,
                "VersionId": "v-s1",
            },
        )
        stubber.add_response(
            "get_object",
            {"Body": io.BytesIO(payload[:12]), "VersionId": "v-s1"},
        )
        stubber.add_client_error(
            "copy_object",
            service_error_code="ConditionalRequestConflict",
            service_message="Conflicting conditional copy operation",
            http_status_code=409,
        )
        stubber.add_response(
            "copy_object",
            {"CopyObjectResult": {"ETag": '"etag-dest-2"'}, "VersionId": "v-dest-2"},
        )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(payload),
                "ContentType": "image/jpeg",
                "ChecksumSHA256": sha_b64,
                "VersionId": "v-dest-2",
            },
        )
        stubber.add_response("delete_object", {})

        with stubber:
            res = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": intent_id}),
                None,
            )

        self.assertEqual(res["statusCode"], 200)
        attempts = app.repository.list_attempts(cid, intent_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, UploadAttemptStatus.COMMITTED)
        self.assertEqual(attempts[0].destination_version_id, "v-dest-2")

    def test_stubber_412_destination_recovery_vs_mismatch_conflict(self) -> None:
        payload = _make_valid_jpeg(2048)
        sha_hex, sha_b64 = _sha256_hex_and_b64(payload)

        # Case 1: 412 with matching destination object recovers cleanly
        app, handler, stubber, _ = self._new_app_with_stubber()
        cid, intent_body = self._create_complaint_and_intent(handler, sha_hex, len(payload))
        intent_id = intent_body["intentId"]
        intent = app.repository.get_intent_consistent(cid, intent_id)
        assert intent is not None

        stubber.add_response(
            "list_object_versions",
            {"IsTruncated": False, "Versions": [{"Key": intent.staging_key, "VersionId": "v-s1", "IsLatest": True}], "DeleteMarkers": []},
        )
        stubber.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "ChecksumSHA256": sha_b64, "VersionId": "v-s1"},
        )
        stubber.add_response("get_object", {"Body": io.BytesIO(payload[:12]), "VersionId": "v-s1"})
        stubber.add_client_error(
            "copy_object",
            service_error_code="PreconditionFailed",
            service_message="If-None-Match failed",
            http_status_code=412,
        )
        stubber.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "ChecksumSHA256": sha_b64, "VersionId": "v-existing-dest"},
            {"Bucket": self.BUCKET, "Key": intent.final_key, "ChecksumMode": "ENABLED"},
        )
        stubber.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "ChecksumSHA256": sha_b64, "VersionId": "v-existing-dest"},
            {"Bucket": self.BUCKET, "Key": intent.final_key, "VersionId": "v-existing-dest", "ChecksumMode": "ENABLED"},
        )
        stubber.add_response(
            "get_object",
            {"Body": io.BytesIO(payload), "VersionId": "v-existing-dest"},
            {"Bucket": self.BUCKET, "Key": intent.final_key, "VersionId": "v-existing-dest"},
        )
        stubber.add_response("delete_object", {})

        with stubber:
            ok_res = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": intent_id}),
                None,
            )
        self.assertEqual(ok_res["statusCode"], 200)
        self.assertEqual(app.repository.get_consistent(cid).version, 2)

        # Case 2: 412 with mismatched destination digest fails closed -> 409 CONFLICT
        app2, handler2, stubber2, _ = self._new_app_with_stubber()
        cid2, intent_body2 = self._create_complaint_and_intent(handler2, sha_hex, len(payload))
        intent_id2 = intent_body2["intentId"]
        intent2 = app2.repository.get_intent_consistent(cid2, intent_id2)
        assert intent2 is not None
        _, wrong_b64 = _sha256_hex_and_b64(_make_valid_jpeg(2048, fill=b"Z"))

        stubber2.add_response(
            "list_object_versions",
            {"IsTruncated": False, "Versions": [{"Key": intent2.staging_key, "VersionId": "v-s2", "IsLatest": True}], "DeleteMarkers": []},
        )
        stubber2.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "ChecksumSHA256": sha_b64, "VersionId": "v-s2"},
        )
        stubber2.add_response("get_object", {"Body": io.BytesIO(payload[:12]), "VersionId": "v-s2"})
        stubber2.add_client_error(
            "copy_object",
            service_error_code="PreconditionFailed",
            service_message="If-None-Match failed",
            http_status_code=412,
        )
        stubber2.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "ChecksumSHA256": wrong_b64, "VersionId": "v-foreign"},
            {"Bucket": self.BUCKET, "Key": intent2.final_key, "ChecksumMode": "ENABLED"},
        )
        stubber2.add_response(
            "head_object",
            {"ContentLength": len(payload), "ContentType": "image/jpeg", "ChecksumSHA256": wrong_b64, "VersionId": "v-foreign"},
            {"Bucket": self.BUCKET, "Key": intent2.final_key, "VersionId": "v-foreign", "ChecksumMode": "ENABLED"},
        )

        with stubber2:
            conflict_res = handler2(
                _api_event("POST", f"/v1/complaints/{cid2}/images/confirm", {"intentId": intent_id2}),
                None,
            )
        self.assertEqual(conflict_res["statusCode"], 409)
        self.assertEqual(json.loads(conflict_res["body"])["code"], "CONFLICT")
        self.assertEqual(app2.repository.get_consistent(cid2).version, 1)
        self.assertIsNone(app2.repository.get_consistent(cid2).attachment_key)

    def test_stubber_storage_throttle_delete_marker_and_ambiguous_version_http_mappings(self) -> None:
        payload = _make_valid_jpeg(2048)
        sha_hex, _ = _sha256_hex_and_b64(payload)

        # S3 SlowDown -> StorageThrottleError -> 503 SERVICE_UNAVAILABLE
        _, handler, stubber, _ = self._new_app_with_stubber()
        cid, intent_body = self._create_complaint_and_intent(handler, sha_hex, len(payload))
        intent_id = intent_body["intentId"]
        stubber.add_client_error(
            "list_object_versions",
            service_error_code="SlowDown",
            service_message="Secret internal rate hint",
            http_status_code=503,
        )
        with stubber:
            res_503 = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": intent_id}),
                None,
            )
        self.assertEqual(res_503["statusCode"], 503)
        body_503 = json.loads(res_503["body"])
        self.assertEqual(body_503["code"], "SERVICE_UNAVAILABLE")
        self.assertNotIn("Secret internal rate hint", body_503["detail"])

        # S3 DeleteMarker on staging key -> StagingObjectDeletedError -> 400 VALIDATION_ERROR
        app2, handler2, stubber2, _ = self._new_app_with_stubber()
        cid2, intent_body2 = self._create_complaint_and_intent(handler2, sha_hex, len(payload))
        intent_id2 = intent_body2["intentId"]
        intent2 = app2.repository.get_intent_consistent(cid2, intent_id2)
        assert intent2 is not None
        stubber2.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [],
                "DeleteMarkers": [{"Key": intent2.staging_key, "VersionId": "dm-1", "IsLatest": True}],
            },
        )
        with stubber2:
            res_400 = handler2(
                _api_event("POST", f"/v1/complaints/{cid2}/images/confirm", {"intentId": intent_id2}),
                None,
            )
        self.assertEqual(res_400["statusCode"], 400)
        self.assertEqual(json.loads(res_400["body"])["code"], "VALIDATION_ERROR")

        # Ambiguous concurrent staging versions -> UploadAmbiguousVersionError -> 409 CONFLICT
        app3, handler3, stubber3, _ = self._new_app_with_stubber()
        cid3, intent_body3 = self._create_complaint_and_intent(handler3, sha_hex, len(payload))
        intent_id3 = intent_body3["intentId"]
        intent3 = app3.repository.get_intent_consistent(cid3, intent_id3)
        assert intent3 is not None
        stubber3.add_response(
            "list_object_versions",
            {
                "IsTruncated": False,
                "Versions": [
                    {"Key": intent3.staging_key, "VersionId": "v-1", "IsLatest": False},
                    {"Key": intent3.staging_key, "VersionId": "v-2", "IsLatest": True},
                ],
                "DeleteMarkers": [],
            },
        )
        with stubber3:
            res_409 = handler3(
                _api_event("POST", f"/v1/complaints/{cid3}/images/confirm", {"intentId": intent_id3}),
                None,
            )
        self.assertEqual(res_409["statusCode"], 409)
        self.assertEqual(json.loads(res_409["body"])["code"], "CONFLICT")

    def test_handler_maps_immutable_field_and_internal_stage_b2_errors(self) -> None:
        base_app = create_application()

        class _RaisingUploadService:
            def __init__(self, exc: Exception) -> None:
                self._exc = exc

            def confirm_upload(self, **kwargs):
                raise self._exc

        for exc, expected_status, expected_code in (
            (ImmutableFieldViolationError("Attempt attribute mismatch"), 409, "CONFLICT"),
            (StorageThrottleError("raw sdk error detail"), 503, "SERVICE_UNAVAILABLE"),
            (CopyVerificationError("raw verification trace"), 500, "INTERNAL_SERVER_ERROR"),
            (SdkCapabilityError("botocore old version"), 500, "INTERNAL_SERVER_ERROR"),
        ):
            handler = create_api_handler(
                complaints_service=base_app.complaints_service,
                uploads_service=_RaisingUploadService(exc),
            )
            res = handler(
                _api_event("POST", "/v1/complaints/CF-1/images/confirm", {"intentId": "i-1"}),
                None,
            )
            self.assertEqual(res["statusCode"], expected_status)
            body = json.loads(res["body"])
            self.assertEqual(body["code"], expected_code)
            if expected_status >= 500:
                self.assertNotIn("raw", body["detail"])


@unittest.skipIf(mock_aws is None, "moto is not installed in this Python environment")
class TestUploadCutoverMotoDynamoAndS3Integration(unittest.TestCase):
    """Full local Moto DynamoDB + versioned S3 integration across HTTP API v2 routes."""

    TABLE_NAME = "campusfix-cutover-moto"
    BUCKET = "campusfix-cutover-moto-bucket"
    REGION = "ap-southeast-2"

    def _create_moto_stack(self, now_fn: _MutableClock | None = None):
        ddb_client = boto3.client("dynamodb", **_DUMMY_AWS_CREDS)
        s3_client = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
        _create_moto_table(ddb_client, self.TABLE_NAME)
        _create_moto_versioned_bucket(s3_client, self.BUCKET, self.REGION)
        env = {
            "CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb",
            "DYNAMODB_TABLE_NAME": self.TABLE_NAME,
            "AWS_REGION": self.REGION,
            "PAGINATION_HMAC_SECRET": "cutover-hmac-secret",
            ENV_UPLOAD_BACKEND: UPLOAD_BACKEND_S3,
            ENV_S3_ATTACHMENTS_BUCKET: self.BUCKET,
        }
        app = create_application(
            dynamodb_client=ddb_client,
            s3_client=s3_client,
            now_fn=now_fn,
            environ=env,
        )
        handler = create_api_handler(
            complaints_service=app.complaints_service,
            uploads_service=app.uploads_service,
        )
        return app, handler, s3_client, ddb_client

    def test_full_end_to_end_upload_confirm_replay_and_download_with_moto(self) -> None:
        payload = _make_valid_jpeg(4096)
        sha_hex, _ = _sha256_hex_and_b64(payload)

        with mock_aws():
            env = {
                "CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb",
                "DYNAMODB_TABLE_NAME": self.TABLE_NAME,
                "AWS_REGION": self.REGION,
                "AWS_ACCESS_KEY_ID": "test",
                "AWS_SECRET_ACCESS_KEY": "test",
                "AWS_EC2_METADATA_DISABLED": "true",
                "PAGINATION_HMAC_SECRET": "cutover-hmac-secret",
                ENV_UPLOAD_BACKEND: UPLOAD_BACKEND_S3,
                ENV_S3_ATTACHMENTS_BUCKET: self.BUCKET,
            }
            ddb_client = boto3.client("dynamodb", **_DUMMY_AWS_CREDS)
            s3_client = boto3.client("s3", config=Config(signature_version="s3v4"), **_DUMMY_AWS_CREDS)
            _create_moto_table(ddb_client, self.TABLE_NAME)
            _create_moto_versioned_bucket(s3_client, self.BUCKET, self.REGION)

            with patch.dict(os.environ, env, clear=True):
                app = create_application()
                self.assertIsInstance(app.repository, DynamoDBComplaintRepository)
                self.assertIsInstance(app.s3_storage, Boto3S3StorageService)
                self.assertIsInstance(app.confirmation_worker, UploadConfirmationWorker)

                handler = create_api_handler_from_env()

            # 1. Student creates complaint
            create_res = handler(
                _api_event(
                    "POST",
                    "/v1/complaints",
                    {
                        "title": "Projector HDMI fault",
                        "description": "Room 402 overhead projector displays flickering signal.",
                        "building": "Academic Block 4",
                        "floor": "4th",
                        "roomNumber": "402",
                        "category": "IT / Network",
                    },
                ),
                None,
            )
            self.assertEqual(create_res["statusCode"], 201)
            cid = json.loads(create_res["body"])["id"]

            # 2. Student requests upload intent
            intent_res = handler(
                _api_event(
                    "POST",
                    f"/v1/complaints/{cid}/images/upload-intent",
                    {"contentType": "image/jpeg", "byteSize": len(payload), "checksumSha256": sha_hex},
                ),
                None,
            )
            self.assertEqual(intent_res["statusCode"], 200)
            intent_id = json.loads(intent_res["body"])["intentId"]
            intent = app.repository.get_intent_consistent(cid, intent_id)
            assert intent is not None

            # 3. Upload valid staged object to Moto S3
            put_res = s3_client.put_object(
                Bucket=self.BUCKET,
                Key=intent.staging_key,
                Body=payload,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )
            staging_version_id = put_res["VersionId"]

            # 4. Confirm upload
            confirm_res = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": intent_id}),
                None,
            )
            self.assertEqual(confirm_res["statusCode"], 200)
            confirm_body = json.loads(confirm_res["body"])
            self.assertEqual(
                confirm_body,
                {"status": "VERIFIED", "attachmentKey": intent.final_key},
            )

            # Verify DynamoDB persisted state (Intent + Attempt + Outbox + Complaint)
            verified_intent = app.repository.get_intent_consistent(cid, intent_id)
            assert verified_intent is not None
            self.assertEqual(verified_intent.status, UploadIntentStatus.VERIFIED)
            self.assertEqual(verified_intent.source_version_id, staging_version_id)

            complaint = app.repository.get_consistent(cid)
            assert complaint is not None
            self.assertEqual(complaint.version, 2)
            self.assertEqual(complaint.attachment_key, intent.final_key)
            self.assertEqual(complaint.attachment_status, "VERIFIED")

            attempts = app.repository.list_attempts(cid, intent_id)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].status, UploadAttemptStatus.COMMITTED)
            self.assertEqual(attempts[0].source_version_id, staging_version_id)

            outbox_records = app.repository.list_pending_outbox_records()
            self.assertEqual(len(outbox_records), 1)
            outbox_rec = outbox_records[0]
            self.assertEqual(outbox_rec.intent_id, intent_id)
            self.assertEqual(outbox_rec.s3_key, intent.final_key)

            # 5. Replay confirm idempotently (even after outbox cleanup)
            replay_res = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": intent_id}),
                None,
            )
            self.assertEqual(replay_res["statusCode"], 200)
            self.assertEqual(json.loads(replay_res["body"]), confirm_body)

            app.repository.delete_outbox_record(intent_id)
            replay_after_outbox_res = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": intent_id}),
                None,
            )
            self.assertEqual(replay_after_outbox_res["statusCode"], 200)
            self.assertEqual(json.loads(replay_after_outbox_res["body"]), confirm_body)
            self.assertEqual(app.repository.list_pending_outbox_records(), [])
            self.assertEqual(app.repository.get_consistent(cid).version, 2)

            # 6. Authorized download URL works against unversioned final attachmentKey
            dl_res = handler(
                _api_event("GET", f"/v1/complaints/{cid}/images/download-url"),
                None,
            )
            self.assertEqual(dl_res["statusCode"], 200)
            self.assertIn(intent.final_key, json.loads(dl_res["body"])["downloadUrl"])

            dest_head = s3_client.head_object(Bucket=self.BUCKET, Key=intent.final_key)
            self.assertEqual(dest_head["ContentLength"], len(payload))

    def test_missing_staging_bounded_retries_and_bad_payload_cleanup(self) -> None:
        payload = _make_valid_jpeg(2048)
        sha_hex, _ = _sha256_hex_and_b64(payload)

        with mock_aws():
            app, handler, s3_client, _ = self._create_moto_stack()
            create_res = handler(
                _api_event(
                    "POST",
                    "/v1/complaints",
                    {
                        "title": "Bench damaged in lab",
                        "description": "Metal frame broken on workbench row 3 inside physics lab.",
                        "building": "Lab Block",
                        "floor": "2nd",
                        "roomNumber": "204",
                        "category": "Furniture",
                    },
                ),
                None,
            )
            cid = json.loads(create_res["body"])["id"]

            # Missing staging object increments bounded retry count then marks FAILED
            res_i1 = handler(
                _api_event(
                    "POST",
                    f"/v1/complaints/{cid}/images/upload-intent",
                    {"contentType": "image/jpeg", "byteSize": len(payload), "checksumSha256": sha_hex},
                ),
                None,
            )
            i1 = json.loads(res_i1["body"])["intentId"]
            for expected_attempt in range(1, MAX_CONFIRM_RETRIES + 1):
                confirm_missing = handler(
                    _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": i1}),
                    None,
                )
                self.assertEqual(confirm_missing["statusCode"], 400)
                updated_i1 = app.repository.get_intent_consistent(cid, i1)
                assert updated_i1 is not None
                self.assertEqual(updated_i1.confirm_attempts, expected_attempt)
            self.assertEqual(app.repository.get_intent_consistent(cid, i1).status, UploadIntentStatus.FAILED)

            # Bad magic bytes (checksum matches declared value, but payload is plain text)
            bad_bytes = b"NOT_A_JPEG_FILE_" * 128
            bad_hex, _ = _sha256_hex_and_b64(bad_bytes)
            res_i2 = handler(
                _api_event(
                    "POST",
                    f"/v1/complaints/{cid}/images/upload-intent",
                    {"contentType": "image/jpeg", "byteSize": len(bad_bytes), "checksumSha256": bad_hex},
                ),
                None,
            )
            i2 = json.loads(res_i2["body"])["intentId"]
            intent2 = app.repository.get_intent_consistent(cid, i2)
            assert intent2 is not None
            s3_client.put_object(
                Bucket=self.BUCKET,
                Key=intent2.staging_key,
                Body=bad_bytes,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )
            bad_confirm = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": i2}),
                None,
            )
            self.assertEqual(bad_confirm["statusCode"], 400)
            self.assertEqual(app.repository.get_intent_consistent(cid, i2).status, UploadIntentStatus.FAILED)
            self.assertIsNone(app.s3_storage.head_object(self.BUCKET, intent2.staging_key))

    def test_expired_deadline_and_concurrent_complaint_occ_conflict(self) -> None:
        payload = _make_valid_jpeg(2048)
        sha_hex, _ = _sha256_hex_and_b64(payload)
        clock = _MutableClock(datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc))

        with mock_aws():
            app, handler, s3_client, _ = self._create_moto_stack(now_fn=clock)
            create_res = handler(
                _api_event(
                    "POST",
                    "/v1/complaints",
                    {
                        "title": "AC vent rattling loudly",
                        "description": "Heavy vibration from overhead split AC unit in seminar hall.",
                        "building": "Main Block",
                        "floor": "3rd",
                        "roomNumber": "301",
                        "category": "Electrical",
                    },
                ),
                None,
            )
            cid = json.loads(create_res["body"])["id"]

            # Expired deadline fails cleanly -> 400 + intent FAILED
            exp_intent_res = handler(
                _api_event(
                    "POST",
                    f"/v1/complaints/{cid}/images/upload-intent",
                    {"contentType": "image/jpeg", "byteSize": len(payload), "checksumSha256": sha_hex},
                ),
                None,
            )
            exp_intent_id = json.loads(exp_intent_res["body"])["intentId"]
            exp_intent = app.repository.get_intent_consistent(cid, exp_intent_id)
            assert exp_intent is not None
            s3_client.put_object(
                Bucket=self.BUCKET,
                Key=exp_intent.staging_key,
                Body=payload,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )
            clock.advance(CONFIRMATION_DEADLINE_SECONDS + 5)
            exp_confirm = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": exp_intent_id}),
                None,
            )
            self.assertEqual(exp_confirm["statusCode"], 400)
            self.assertEqual(
                app.repository.get_intent_consistent(cid, exp_intent_id).status,
                UploadIntentStatus.FAILED,
            )

            # Concurrent Complaint version update between worker read and commit -> 409 CONFLICT
            occ_intent_res = handler(
                _api_event(
                    "POST",
                    f"/v1/complaints/{cid}/images/upload-intent",
                    {"contentType": "image/jpeg", "byteSize": len(payload), "checksumSha256": sha_hex},
                ),
                None,
            )
            occ_intent_id = json.loads(occ_intent_res["body"])["intentId"]
            occ_intent = app.repository.get_intent_consistent(cid, occ_intent_id)
            assert occ_intent is not None
            s3_client.put_object(
                Bucket=self.BUCKET,
                Key=occ_intent.staging_key,
                Body=payload,
                ContentType="image/jpeg",
                ChecksumAlgorithm="SHA256",
            )

            original_begin = app.repository.begin_copy_attempt
            injected_concurrent_update = False

            def _begin_with_concurrent_complaint_update(*args, **kwargs):
                nonlocal injected_concurrent_update
                attempt = original_begin(*args, **kwargs)
                if not injected_concurrent_update:
                    injected_concurrent_update = True
                    patch_res = handler(
                        _api_event(
                            "PATCH",
                            f"/v1/complaints/{cid}/status",
                            {
                                "status": "in-progress",
                                "remarks": "Technician assigned concurrently",
                                "expectedVersion": 1,
                            },
                            user_id="maint-1",
                            role_group="maintenance",
                        ),
                        None,
                    )
                    self.assertEqual(patch_res["statusCode"], 200)
                return attempt

            with patch.object(
                app.repository,
                "begin_copy_attempt",
                side_effect=_begin_with_concurrent_complaint_update,
            ):
                occ_res = handler(
                    _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": occ_intent_id}),
                    None,
                )

            self.assertEqual(occ_res["statusCode"], 409)
            self.assertEqual(json.loads(occ_res["body"])["code"], "CONFLICT")
            latest_complaint = app.repository.get_consistent(cid)
            assert latest_complaint is not None
            self.assertEqual(latest_complaint.version, 2)
            self.assertIsNone(latest_complaint.attachment_key)
            self.assertEqual(app.repository.list_pending_outbox_records(), [])


if __name__ == "__main__":
    unittest.main()
