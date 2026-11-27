"""Tests for repository selection, application composition, and shared-instance wiring."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

from campusfix.api.handler import create_api_handler_from_env
from campusfix.bootstrap import (
    ApplicationContainer,
    DEFAULT_S3_ATTACHMENTS_BUCKET,
    create_application,
)
from campusfix.domain.models import Actor, Complaint, ComplaintStatus, IssueCategory, Role, ValidationError
from campusfix.repositories import (
    DEFAULT_AWS_REGION,
    DynamoDBComplaintRepository,
    InMemoryComplaintRepository,
    create_repository,
    resolve_repository_config,
)
from campusfix.repositories.interfaces import ComplaintRepository

try:
    from moto import mock_aws  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    mock_aws = None


_DUMMY_CREDS = {
    "region_name": "ap-southeast-2",
    "aws_access_key_id": "test",
    "aws_secret_access_key": "test",
}


def _api_event(method: str, path: str, body: dict | None = None, user_id: str = "stu-1") -> dict:
    return {
        "version": "2.0",
        "rawPath": path,
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"sub": user_id, "token_use": "access", "cognito:groups": ["students"]}}},
        },
        "body": json.dumps(body) if body is not None else "",
        "isBase64Encoded": False,
    }


def _valid_png_with_sha() -> tuple[bytes, str]:
    payload = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 1040)
    digest_hex = hashlib.sha256(payload).hexdigest()
    return payload, digest_hex


class TestRepositoryFactoryAndBootstrap(unittest.TestCase):

    def test_defaults_to_in_memory_without_aws_calls(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("campusfix.repositories.dynamodb.boto3.client") as mock_boto_client:
                repo = create_repository()
                self.assertIsInstance(repo, InMemoryComplaintRepository)
                mock_boto_client.assert_not_called()

                app = create_application()
                self.assertIsInstance(app, ApplicationContainer)
                self.assertIsInstance(app.repository, InMemoryComplaintRepository)
                self.assertEqual(app.repository_config.backend, "memory")
                self.assertIsNone(app.repository_config.table_name)
                self.assertEqual(app.aws_region, DEFAULT_AWS_REGION)
                self.assertEqual(app.upload_bucket, DEFAULT_S3_ATTACHMENTS_BUCKET)
                mock_boto_client.assert_not_called()

    def test_explicit_memory_backend_case_insensitive_and_initial_complaints(self) -> None:
        seed = Complaint(
            id="CF-2026-SEED01",
            title="Seeded issue",
            description="Water tap dripping continuously in Block B.",
            building="Block B",
            floor="1st",
            room_number="102",
            category=IssueCategory.PLUMBING,
            status=ComplaintStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
            submitted_by="stu-1",
            version=1,
        )
        repo = create_repository(
            environ={"CAMPUSFIX_REPOSITORY_BACKEND": " MEMORY "},
            initial_complaints=[seed],
        )
        self.assertIsInstance(repo, InMemoryComplaintRepository)
        self.assertIsNotNone(repo.get("CF-2026-SEED01"))

    def test_dynamodb_selection_uses_env_config_and_region_without_hardcoded_secrets(self) -> None:
        env = {
            "CAMPUSFIX_REPOSITORY_BACKEND": "DynamoDB",
            "DYNAMODB_TABLE_NAME": "campusfix-complaints-staging",
            "AWS_REGION": "us-east-1",
            "PAGINATION_HMAC_SECRET": "test-hmac-secret",
            "S3_ATTACHMENTS_BUCKET": "campusfix-bucket-staging",
        }
        fake_client = object()
        with patch("campusfix.repositories.dynamodb.boto3.client", return_value=fake_client) as mock_client:
            app = create_application(environ=env)
            mock_client.assert_called_once_with("dynamodb", region_name="us-east-1")

        self.assertIsInstance(app.repository, DynamoDBComplaintRepository)
        self.assertEqual(app.repository_config.backend, "dynamodb")
        self.assertEqual(app.repository_config.table_name, "campusfix-complaints-staging")
        self.assertEqual(app.repository_config.region_name, "us-east-1")
        self.assertEqual(app.aws_region, "us-east-1")
        self.assertEqual(app.upload_bucket, "campusfix-bucket-staging")
        self.assertEqual(app.uploads_service._bucket, "campusfix-bucket-staging")
        self.assertEqual(app.uploads_service._region, "us-east-1")

    def test_dynamodb_region_defaults_to_ap_southeast_2_matching_upload_service(self) -> None:
        env = {
            "CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb",
            "DYNAMODB_TABLE_NAME": "campusfix-complaints",
        }
        cfg = resolve_repository_config(environ=env)
        self.assertEqual(cfg.backend, "dynamodb")
        self.assertEqual(cfg.table_name, "campusfix-complaints")
        self.assertEqual(cfg.region_name, "ap-southeast-2")

    def test_shared_instance_wiring_for_both_memory_and_dynamodb(self) -> None:
        mem_app = create_application(environ={})
        self.assertIs(mem_app.complaints_service._repository, mem_app.repository)
        self.assertIs(mem_app.uploads_service._complaint_repo, mem_app.repository)
        self.assertIs(mem_app.uploads_service._intent_repo, mem_app.repository)
        self.assertIs(mem_app.versioned_upload_state_adapter._complaint_repo, mem_app.repository)
        self.assertIs(mem_app.versioned_upload_state_adapter._intent_repo, mem_app.repository)

        ddb_client = boto3.client("dynamodb", **_DUMMY_CREDS)
        Stubber(ddb_client)
        ddb_app = create_application(
            dynamodb_client=ddb_client,
            environ={
                "CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb",
                "DYNAMODB_TABLE_NAME": "campusfix-complaints",
                "AWS_REGION": "ap-southeast-2",
            },
        )
        self.assertIsInstance(ddb_app.repository, DynamoDBComplaintRepository)
        self.assertIs(ddb_app.complaints_service._repository, ddb_app.repository)
        self.assertIs(ddb_app.uploads_service._complaint_repo, ddb_app.repository)
        self.assertIs(ddb_app.uploads_service._intent_repo, ddb_app.repository)
        self.assertIs(ddb_app.versioned_upload_state_adapter._complaint_repo, ddb_app.repository)
        self.assertIs(ddb_app.versioned_upload_state_adapter._intent_repo, ddb_app.repository)

    def test_missing_and_invalid_configuration_fails_closed(self) -> None:
        invalid_envs = [
            {"CAMPUSFIX_REPOSITORY_BACKEND": ""},
            {"CAMPUSFIX_REPOSITORY_BACKEND": "   "},
            {"CAMPUSFIX_REPOSITORY_BACKEND": "postgres"},
            {"CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb"},
            {"CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb", "DYNAMODB_TABLE_NAME": ""},
            {"CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb", "DYNAMODB_TABLE_NAME": "   "},
            {"CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb", "DYNAMODB_TABLE_NAME": "tbl", "AWS_REGION": "  "},
            {
                "CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb",
                "DYNAMODB_TABLE_NAME": "tbl",
                "PAGINATION_HMAC_SECRET": "   ",
            },
            {"S3_ATTACHMENTS_BUCKET": "  "},
        ]
        for bad_env in invalid_envs:
            with self.subTest(bad_env=bad_env):
                with self.assertRaises(ValidationError):
                    create_application(environ=bad_env)

        with self.assertRaises(ValidationError):
            create_repository(backend="", environ={})
        with self.assertRaises(ValidationError):
            create_repository(backend="dynamodb", table_name="tbl", initial_complaints=[], environ={})
        with self.assertRaises(ValidationError):
            create_application(region_name="  ", environ={})
        with self.assertRaises(ValidationError):
            create_application(bucket_name="", environ={})

        # Custom repository must satisfy both ComplaintRepository and UploadIntentRepository
        class _ComplaintOnlyRepo(ComplaintRepository):
            def get(self, complaint_id: str): return None
            def get_consistent(self, complaint_id: str): return None
            def create(self, complaint): pass
            def update(self, complaint, expected_version: int): pass
            def seed(self, complaint): pass
            def save(self, complaint): pass
            def create_follow_up_atomic(self, original_complaint_id: str, follow_up): return follow_up
            def toggle_upvote_atomic(self, complaint_id: str, student_id: str): raise NotImplementedError
            def add_upvote_atomic(self, complaint_id: str, student_id: str): raise NotImplementedError
            def remove_upvote_atomic(self, complaint_id: str, student_id: str): raise NotImplementedError
            def list_all(self, status=None, building=None, category=None): return []
            def list_paginated(self, status=None, building=None, category=None, *, limit=20, cursor=None):
                return [], None
            def find_by_original_id(self, original_complaint_id: str): return []
            def has_active_follow_up(self, original_complaint_id: str): return False
            def count(self): return 0

        with self.assertRaises(ValidationError):
            create_application(repository=_ComplaintOnlyRepo())  # type: ignore[arg-type]

    def test_in_memory_services_and_state_adapter_share_state_and_legacy_commit_path(self) -> None:
        app = create_application(environ={})
        actor = Actor.student("stu-1")
        created = app.complaints_service.create_complaint(
            title="Broken AC fan",
            description="Air conditioner making buzzing sound in lecture hall 2.",
            building="Block A",
            floor="1st",
            room_number="101",
            category=IssueCategory.ELECTRICAL,
            actor=actor,
        )

        payload, sha_hex = _valid_png_with_sha()
        intent_res = app.uploads_service.create_upload_intent(
            complaint_id=created.id,
            content_type="image/png",
            byte_size=len(payload),
            checksum_sha256=sha_hex,
            actor=actor,
        )
        intent_id = intent_res["intentId"]

        # Shared repository exposes the created complaint and intent to the Stage B2 adapter
        snapshot = app.versioned_upload_state_adapter.get_intent_consistent(created.id, intent_id)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.expected_complaint_version, 1)

        # Legacy UploadIntentService confirm path remains intact
        app.s3_storage.put_object(
            bucket=app.upload_bucket,
            key=intent_res["fields"]["key"],
            data=payload,
            content_type="image/png",
        )
        confirm_res = app.uploads_service.confirm_upload(created.id, intent_id, actor)
        self.assertEqual(confirm_res["status"], "VERIFIED")

        updated_c = app.complaints_service.get_complaint_or_raise(created.id)
        self.assertEqual(updated_c.version, 2)
        self.assertEqual(updated_c.attachment_status, "VERIFIED")

    def test_create_api_handler_from_env_uses_in_memory_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            handler = create_api_handler_from_env()
        create_resp = handler(
            _api_event(
                "POST",
                "/v1/complaints",
                {
                    "title": "Projector flicker",
                    "description": "Classroom projector turns off every few minutes.",
                    "building": "Main Block",
                    "floor": "2nd",
                    "roomNumber": "205",
                    "category": "IT / Network",
                },
            ),
            None,
        )
        self.assertEqual(create_resp["statusCode"], 201)
        created_id = json.loads(create_resp["body"])["id"]

        get_resp = handler(_api_event("GET", f"/v1/complaints/{created_id}"), None)
        self.assertEqual(get_resp["statusCode"], 200)
        self.assertEqual(json.loads(get_resp["body"])["id"], created_id)


@unittest.skipIf(mock_aws is None, "moto is not installed in this Python environment")
class TestAppBootstrapMotoDynamoDBIntegration(unittest.TestCase):

    def _create_table(self, ddb_client, table_name: str) -> None:
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

    def test_dynamodb_selected_via_env_shares_instance_and_serves_api_requests(self) -> None:
        table_name = "campusfix-complaints-integration"
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
            self._create_table(ddb, table_name)

            env = {
                "CAMPUSFIX_REPOSITORY_BACKEND": "dynamodb",
                "DYNAMODB_TABLE_NAME": table_name,
                "AWS_REGION": "ap-southeast-2",
                "PAGINATION_HMAC_SECRET": "integration-hmac-secret",
                "S3_ATTACHMENTS_BUCKET": "campusfix-attachments-ap-southeast-2",
            }
            with patch.dict(os.environ, env, clear=True):
                app = create_application()
                self.assertIsInstance(app.repository, DynamoDBComplaintRepository)
                self.assertIs(app.complaints_service._repository, app.repository)
                self.assertIs(app.uploads_service._complaint_repo, app.repository)
                self.assertIs(app.uploads_service._intent_repo, app.repository)
                self.assertIs(app.versioned_upload_state_adapter._complaint_repo, app.repository)
                self.assertIs(app.versioned_upload_state_adapter._intent_repo, app.repository)

                handler = create_api_handler_from_env(s3_storage=app.s3_storage)

            create_resp = handler(
                _api_event(
                    "POST",
                    "/v1/complaints",
                    {
                        "title": "Corridor light failure",
                        "description": "Third floor hallway lights remain off throughout evening labs.",
                        "building": "Block C",
                        "floor": "3rd",
                        "roomNumber": "312",
                        "category": "Electrical",
                    },
                ),
                None,
            )
            self.assertEqual(create_resp["statusCode"], 201)
            cid = json.loads(create_resp["body"])["id"]

            # Shared adapter & uploads service observe complaint persisted through API handler
            payload, sha_hex = _valid_png_with_sha()
            intent_resp = handler(
                _api_event(
                    "POST",
                    f"/v1/complaints/{cid}/images/upload-intent",
                    {
                        "contentType": "image/png",
                        "byteSize": len(payload),
                        "checksumSha256": sha_hex,
                    },
                ),
                None,
            )
            self.assertEqual(intent_resp["statusCode"], 200)
            intent_body = json.loads(intent_resp["body"])
            intent_id = intent_body["intentId"]

            snapshot = app.versioned_upload_state_adapter.get_intent_consistent(cid, intent_id)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.expected_complaint_version, 1)

            # Confirm through legacy upload path via API handler
            app.s3_storage.put_object(
                bucket=app.upload_bucket,
                key=intent_body["fields"]["key"],
                data=payload,
                content_type="image/png",
            )
            confirm_resp = handler(
                _api_event("POST", f"/v1/complaints/{cid}/images/confirm", {"intentId": intent_id}),
                None,
            )
            self.assertEqual(confirm_resp["statusCode"], 200)

            get_resp = handler(_api_event("GET", f"/v1/complaints/{cid}"), None)
            self.assertEqual(get_resp["statusCode"], 200)
            dto = json.loads(get_resp["body"])
            self.assertEqual(dto["attachmentStatus"], "VERIFIED")
            self.assertEqual(dto["version"], 2)


if __name__ == "__main__":
    unittest.main()
