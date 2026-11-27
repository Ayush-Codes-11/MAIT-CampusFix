"""Focused SDK compatibility and introspection tests for conditional CopyObject."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import unittest

import boto3
import botocore
from botocore.config import Config
import botocore.session

from campusfix.services.s3_capabilities import (
    SdkCapabilityError,
    assert_copy_if_none_match_supported,
)


@dataclass
class _StubShape:
    members: dict[str, Any]
    serialization: dict[str, str] | None = None


class _StubOperationModel:
    def __init__(self, members: dict[str, Any]) -> None:
        self.input_shape = _StubShape(members=members)


class _StubServiceModel:
    def __init__(self, members: dict[str, Any]) -> None:
        self._members = members

    def operation_model(self, name: str) -> _StubOperationModel:
        if name != "CopyObject":
            raise KeyError(name)
        return _StubOperationModel(self._members)


class _StubS3Client:
    def __init__(self, members: dict[str, Any], copy_calls: list[dict[str, Any]]) -> None:
        self._service_model = _StubServiceModel(members)
        self._copy_calls = copy_calls

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        assert_copy_if_none_match_supported(self)
        self._copy_calls.append(kwargs)
        return {}


class TestSdkCompatibility(unittest.TestCase):
    """Offline introspection tests verifying boto3/botocore CopyObject conditional support."""

    def test_installed_sdk_versions_match_pinned_pair(self) -> None:
        self.assertEqual(boto3.__version__, "1.44.11")
        self.assertEqual(botocore.__version__, "1.44.11")

    def test_local_botocore_model_supports_copy_object_if_none_match(self) -> None:
        assert_copy_if_none_match_supported()

        service_model = botocore.session.get_session().get_service_model("s3")
        op_model = service_model.operation_model("CopyObject")
        self.assertIn("IfNoneMatch", op_model.input_shape.members)
        self.assertEqual(
            op_model.input_shape.members["IfNoneMatch"].serialization,
            {"location": "header", "name": "If-None-Match"},
        )

    def test_boto3_client_exposes_if_none_match_and_top_level_version_outputs(self) -> None:
        # Constructing a client with dummy credentials performs zero AWS API calls.
        client = boto3.client(
            "s3",
            region_name="ap-southeast-2",
            aws_access_key_id="dummy-access-key",
            aws_secret_access_key="dummy-secret-key",
            config=Config(signature_version="s3v4"),
        )
        assert_copy_if_none_match_supported(client)

        op_model = client._service_model.operation_model("CopyObject")
        output_members = op_model.output_shape.members
        self.assertIn("VersionId", output_members)
        self.assertIn("CopySourceVersionId", output_members)
        self.assertNotIn("VersionId", output_members["CopyObjectResult"].members)

    def test_boto3_copy_object_validates_and_serializes_if_none_match_offline(self) -> None:
        class _ShortCircuitBeforeSend(Exception):
            pass

        captured_headers: dict[str, Any] = {}
        client = boto3.client(
            "s3",
            region_name="ap-southeast-2",
            aws_access_key_id="dummy-access-key",
            aws_secret_access_key="dummy-secret-key",
            config=Config(signature_version="s3v4"),
        )
        assert_copy_if_none_match_supported(client)

        def _capture_and_abort(request: Any, **kwargs: Any) -> None:
            captured_headers.update(dict(request.headers))
            raise _ShortCircuitBeforeSend()

        # Short-circuits before any HTTP socket/network operation.
        client.meta.events.register_first("before-send.s3.CopyObject", _capture_and_abort)

        with self.assertRaises(_ShortCircuitBeforeSend):
            client.copy_object(
                Bucket="campusfix-attachments-ap-southeast-2",
                Key="complaints/CF-2026-test/images/intent-1.jpg",
                CopySource={
                    "Bucket": "campusfix-attachments-ap-southeast-2",
                    "Key": "staging/complaints/CF-2026-test/intent-1/upload.tmp",
                    "VersionId": "src-version-123",
                },
                IfNoneMatch="*",
            )

        decoded = {
            k: (v.decode("utf-8") if isinstance(v, bytes) else v)
            for k, v in captured_headers.items()
        }
        self.assertEqual(decoded.get("If-None-Match"), "*")
        self.assertEqual(
            decoded.get("x-amz-copy-source"),
            "campusfix-attachments-ap-southeast-2/staging/complaints/CF-2026-test/intent-1/upload.tmp?versionId=src-version-123",
        )

    def test_missing_if_none_match_member_fails_closed_before_copy(self) -> None:
        copy_calls: list[dict[str, Any]] = []
        legacy_client = _StubS3Client(
            members={"Bucket": object(), "Key": object(), "CopySource": object()},
            copy_calls=copy_calls,
        )

        with self.assertRaises(SdkCapabilityError):
            legacy_client.copy_object(
                Bucket="b",
                Key="complaints/c1/images/i1.jpg",
                CopySource={"Bucket": "b", "Key": "staging/k", "VersionId": "src-v1"},
            )

        self.assertEqual(copy_calls, [])

    def test_wrong_if_none_match_header_serialization_fails_closed(self) -> None:
        copy_calls: list[dict[str, Any]] = []
        bad_shape = _StubShape(
            members={},
            serialization={"location": "header", "name": "x-amz-copy-source-if-none-match"},
        )
        bad_client = _StubS3Client(
            members={"Bucket": object(), "Key": object(), "CopySource": object(), "IfNoneMatch": bad_shape},
            copy_calls=copy_calls,
        )

        with self.assertRaises(SdkCapabilityError):
            assert_copy_if_none_match_supported(bad_client)

    def test_missing_service_model_fails_closed(self) -> None:
        with self.assertRaises(SdkCapabilityError):
            assert_copy_if_none_match_supported(object())
