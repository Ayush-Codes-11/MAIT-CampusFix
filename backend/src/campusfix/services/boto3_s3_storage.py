"""Version-aware Amazon S3 adapter built on pinned boto3/botocore for MAIT CampusFix.

Implements Stage B1 storage operations required by the approved Phase 3.3 Step 2
specification:
- Startup and pre-copy Botocore model introspection (`IfNoneMatch="*"` gate).
- Version-specific source metadata and range/stream verification (`s3:GetObjectVersion`).
- Unversioned latest destination discovery and download presigning (`s3:GetObject`).
- Exact-key paginated `ListObjectVersions` recovery with delete-marker and ambiguity fail-closure.
- Mandatory destination conditional `CopyObject` (`IfNoneMatch="*"`) with strict
  top-level `VersionId` and `CopySourceVersionId` verification.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from typing import Any, TypeGuard

from botocore.exceptions import BotoCoreError, ClientError

from campusfix.domain.rules import (
    MAX_IMAGE_BYTES,
    CampusFixDomainError,
    DestinationConflictError,
    ValidationError,
)
from campusfix.services.s3_capabilities import assert_copy_if_none_match_supported
from campusfix.services.uploads import normalize_sha256

_STREAM_CHUNK_BYTES = 65536
_HEADER_PREVIEW_BYTES = 12


class Boto3S3AdapterError(CampusFixDomainError):
    """Base error for fail-closed Boto3 S3 adapter failures."""


class CopyVerificationError(Boto3S3AdapterError):
    """Raised when CopyObject conditional execution or post-copy verification fails."""

    def __init__(
        self,
        message: str,
        *,
        complaint_id: str | None = None,
        intent_id: str | None = None,
        dest_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.complaint_id = complaint_id
        self.intent_id = intent_id
        self.dest_key = dest_key


class ConditionalCopyPreconditionError(DestinationConflictError):
    """Raised when conditional CopyObject (IfNoneMatch='*') fails with HTTP 412."""


class ConditionalCopyConflictError(Boto3S3AdapterError):
    """Raised when conditional CopyObject fails with HTTP 409 ConditionalRequestConflict."""


class UploadAmbiguousVersionError(Boto3S3AdapterError):
    """Raised when ListObjectVersions cannot unambiguously isolate a single latest version."""


class StagingObjectDeletedError(Boto3S3AdapterError):
    """Raised when the latest entry at a staging key is a DeleteMarker."""


@dataclass(frozen=True)
class VersionedS3ObjectMetadata:
    """Metadata retrieved from S3 HeadObject with optional VersionId and ChecksumSHA256."""

    content_length: int
    sha256_checksum: str | None
    etag: str
    content_type: str
    version_id: str | None


@dataclass(frozen=True)
class StreamedObjectVerification:
    """Cryptographic digest and byte-bounded inspection over a streamed S3 object version."""

    content_length: int
    sha256_checksum: str
    header_bytes: bytes
    version_id: str | None


@dataclass(frozen=True)
class ConditionalCopyResult:
    """Verified metadata returned from a conditional S3 CopyObject operation."""

    destination_version_id: str
    copy_source_version_id: str | None
    etag: str | None = None


def is_valid_version_id(version_id: Any) -> TypeGuard[str]:
    """Return True iff version_id is a non-empty S3 version identifier distinct from 'null'."""
    return isinstance(version_id, str) and bool(version_id.strip()) and version_id != "null"


def hex_sha256_to_base64(checksum_sha256: str) -> str:
    """Convert a 64-char hex or 44-char base64 SHA-256 digest into Base64 for S3 headers."""
    normalized_hex = normalize_sha256(checksum_sha256)
    return base64.b64encode(bytes.fromhex(normalized_hex)).decode("ascii")


def _extract_client_error_details(err: ClientError) -> tuple[str | None, int | None, dict[str, Any]]:
    response = getattr(err, "response", None)
    if not isinstance(response, dict):
        return None, None, {}
    error_code = response.get("Error", {}).get("Code")
    metadata = response.get("ResponseMetadata", {})
    http_status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    headers = metadata.get("HTTPHeaders", {}) if isinstance(metadata, dict) else {}
    return error_code, http_status, headers if isinstance(headers, dict) else {}


def _is_not_found_error(error_code: str | None, http_status: int | None) -> bool:
    return http_status == 404 or error_code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}


def _is_delete_marker_405(http_status: int | None, headers: dict[str, Any]) -> bool:
    if http_status != 405:
        return False
    for header_key, header_value in headers.items():
        if str(header_key).lower() == "x-amz-delete-marker" and str(header_value).lower() == "true":
            return True
    return False


def _validate_version_collection(
    entries: Any,
    *,
    collection_name: str,
    target_key: str,
) -> list[dict[str, Any]]:
    """Validate and exact-key-filter a Versions or DeleteMarkers collection from ListObjectVersions."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise UploadAmbiguousVersionError(
            f"Malformed ListObjectVersions response: '{collection_name}' must be a list."
        )

    matched: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise UploadAmbiguousVersionError(
                f"Malformed entry in ListObjectVersions '{collection_name}'."
            )
        if entry.get("Key") != target_key:
            continue
        if not isinstance(entry.get("IsLatest"), bool):
            raise UploadAmbiguousVersionError(
                f"Malformed {collection_name} entry for '{target_key}': missing boolean IsLatest."
            )
        matched.append(entry)
    return matched


def _close_body(body: Any) -> None:
    close_fn = getattr(body, "close", None)
    if callable(close_fn):
        close_fn()


class Boto3S3StorageService:
    """Production-oriented S3 storage adapter backed by boto3/botocore.

    Enforces SDK `CopyObjectRequest.IfNoneMatch` support before any conditional copy
    and separates unversioned latest operations (`s3:GetObject`) from pinned versioned
    operations (`s3:GetObjectVersion`).
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        region_name: str | None = None,
        verify_capabilities: bool = True,
    ) -> None:
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                region_name=region_name,
                config=Config(signature_version="s3v4"),
            )
        self._client = client
        if verify_capabilities:
            self.assert_capabilities()

    @property
    def client(self) -> Any:
        """Expose underlying boto3 S3 client for Stubber wiring and introspection."""
        return self._client

    def assert_capabilities(self) -> None:
        """Fail closed unless the configured Botocore client supports IfNoneMatch='*'."""
        assert_copy_if_none_match_supported(self._client)

    def generate_presigned_post(
        self,
        bucket: str,
        key: str,
        fields: dict[str, str],
        conditions: list[Any],
        expires_in: int,
    ) -> dict[str, Any]:
        """Generate a presigned POST policy and fields for direct browser staging uploads."""
        return self._client.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=expires_in,
        )

    def create_staging_presigned_post(
        self,
        *,
        bucket: str,
        key: str,
        content_type: str,
        checksum_sha256: str,
        max_byte_size: int,
        expires_in: int,
        min_byte_size: int = 1024,
    ) -> dict[str, Any]:
        """Build Base64 SHA-256 fields/conditions and generate a presigned staging POST."""
        if min_byte_size < 1 or max_byte_size < min_byte_size or max_byte_size > MAX_IMAGE_BYTES:
            raise ValidationError(
                f"Invalid presigned byte range [{min_byte_size}, {max_byte_size}]."
            )
        checksum_base64 = hex_sha256_to_base64(checksum_sha256)
        fields = {
            "Content-Type": content_type,
            "x-amz-checksum-algorithm": "SHA256",
            "x-amz-checksum-sha256": checksum_base64,
        }
        conditions = [
            ["content-length-range", min_byte_size, max_byte_size],
            ["eq", "$key", key],
            ["eq", "$Content-Type", content_type],
            ["eq", "$x-amz-checksum-algorithm", "SHA256"],
            ["eq", "$x-amz-checksum-sha256", checksum_base64],
        ]
        return self.generate_presigned_post(
            bucket=bucket,
            key=key,
            fields=fields,
            conditions=conditions,
            expires_in=expires_in,
        )

    def generate_presigned_get(self, bucket: str, key: str, expires_in: int) -> str:
        """Generate an unversioned presigned GET URL for the current destination key."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    def head_object(
        self,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> VersionedS3ObjectMetadata | None:
        """Inspect S3 object metadata with ChecksumMode='ENABLED'.

        Passes VersionId only when provided (s3:GetObjectVersion); omits VersionId when
        inspecting the current unversioned tip (s3:GetObject).
        """
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "ChecksumMode": "ENABLED",
        }
        if version_id is not None:
            if not is_valid_version_id(version_id):
                raise CopyVerificationError(
                    f"HeadObject called with invalid VersionId: {version_id!r}.",
                    dest_key=key,
                )
            params["VersionId"] = version_id

        try:
            resp = self._client.head_object(**params)
        except ClientError as err:
            error_code, http_status, headers = _extract_client_error_details(err)
            if _is_not_found_error(error_code, http_status):
                return None
            if _is_delete_marker_405(http_status, headers):
                raise StagingObjectDeletedError(
                    f"HeadObject encountered a DeleteMarker at '{key}'."
                ) from err
            raise

        if resp.get("DeleteMarker") is True:
            raise StagingObjectDeletedError(f"HeadObject returned DeleteMarker=True for '{key}'.")

        content_length = resp.get("ContentLength")
        if not isinstance(content_length, int) or isinstance(content_length, bool) or content_length < 0:
            raise CopyVerificationError(
                f"HeadObject response for '{key}' missing valid ContentLength ({content_length!r}).",
                dest_key=key,
            )

        raw_checksum = resp.get("ChecksumSHA256")
        sha256_hex = normalize_sha256(raw_checksum) if raw_checksum is not None else None

        resp_version_id = resp.get("VersionId")
        if version_id is not None and resp_version_id is not None and resp_version_id != version_id:
            raise CopyVerificationError(
                f"HeadObject returned VersionId {resp_version_id!r}, expected {version_id!r}.",
                dest_key=key,
            )

        return VersionedS3ObjectMetadata(
            content_length=content_length,
            sha256_checksum=sha256_hex,
            etag=str(resp.get("ETag", "")),
            content_type=str(resp.get("ContentType", "application/octet-stream")),
            version_id=resp_version_id if isinstance(resp_version_id, str) else None,
        )

    def get_object_range(
        self,
        bucket: str,
        key: str,
        start_byte: int,
        end_byte: int,
        version_id: str | None = None,
    ) -> bytes:
        """Read an inclusive byte slice of an S3 object (optionally pinned to VersionId)."""
        if start_byte < 0 or end_byte < start_byte:
            raise ValidationError(f"Invalid byte range [{start_byte}, {end_byte}].")

        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Range": f"bytes={start_byte}-{end_byte}",
        }
        if version_id is not None:
            if not is_valid_version_id(version_id):
                raise CopyVerificationError(
                    f"GetObject range called with invalid VersionId: {version_id!r}.",
                    dest_key=key,
                )
            params["VersionId"] = version_id

        resp = self._client.get_object(**params)
        body = resp["Body"]
        try:
            resp_version_id = resp.get("VersionId")
            if version_id is not None and resp_version_id is not None and resp_version_id != version_id:
                raise CopyVerificationError(
                    f"GetObject range returned VersionId {resp_version_id!r}, expected {version_id!r}.",
                    dest_key=key,
                )
            return body.read()
        except (BotoCoreError, OSError) as err:
            raise CopyVerificationError(
                f"GetObject range read failed for '{key}': {err}",
                dest_key=key,
            ) from err
        finally:
            _close_body(body)

    def read_object_and_compute_sha256(
        self,
        bucket: str,
        key: str,
        *,
        version_id: str,
        max_byte_size: int,
    ) -> StreamedObjectVerification:
        """Stream a pinned object version, enforce max_byte_size, and compute SHA-256 + magic header."""
        if not is_valid_version_id(version_id):
            raise CopyVerificationError(
                f"Pinned GetObject stream requires a valid VersionId, got {version_id!r}.",
                dest_key=key,
            )
        if max_byte_size < 0:
            raise ValidationError(f"max_byte_size must be non-negative, got {max_byte_size!r}.")

        try:
            resp = self._client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
        except (BotoCoreError, OSError) as err:
            raise CopyVerificationError(
                f"GetObject failed for '{key}'@{version_id}: {err}",
                dest_key=key,
            ) from err

        declared_length = resp.get("ContentLength")
        body = resp["Body"]
        try:
            if declared_length is not None:
                if (
                    isinstance(declared_length, bool)
                    or not isinstance(declared_length, int)
                    or declared_length < 0
                ):
                    raise CopyVerificationError(
                        f"Object '{key}'@{version_id} had invalid ContentLength ({declared_length!r}).",
                        dest_key=key,
                    )
                if declared_length > max_byte_size:
                    raise ValidationError(
                        f"Object '{key}'@{version_id} ContentLength ({declared_length}) exceeds max_byte_size ({max_byte_size})."
                    )

            resp_version_id = resp.get("VersionId")
            if resp_version_id is not None and resp_version_id != version_id:
                raise CopyVerificationError(
                    f"GetObject returned VersionId {resp_version_id!r}, expected {version_id!r}.",
                    dest_key=key,
                )

            hasher = hashlib.sha256()
            header_buf = bytearray()
            total_read = 0
            try:
                while True:
                    chunk = body.read(_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if total_read > max_byte_size:
                        raise ValidationError(
                            f"Streamed object '{key}'@{version_id} exceeded max_byte_size ({max_byte_size})."
                        )
                    if len(header_buf) < _HEADER_PREVIEW_BYTES:
                        need = _HEADER_PREVIEW_BYTES - len(header_buf)
                        header_buf.extend(chunk[:need])
                    hasher.update(chunk)
            except (BotoCoreError, OSError) as err:
                raise CopyVerificationError(
                    f"Streamed read failed for '{key}'@{version_id}: {err}",
                    dest_key=key,
                ) from err

            if declared_length is not None and total_read != declared_length:
                raise ValidationError(
                    f"Streamed object '{key}'@{version_id} truncated (read {total_read} bytes, expected {declared_length})."
                )
        finally:
            _close_body(body)

        return StreamedObjectVerification(
            content_length=total_read,
            sha256_checksum=hasher.hexdigest().lower(),
            header_bytes=bytes(header_buf),
            version_id=resp.get("VersionId") or version_id,
        )

    def list_object_versions_exact(self, bucket: str, key: str) -> str | None:
        """Resolve the single unambiguous latest version of `key` via ListObjectVersions.

        Follows Phase 3.3 Step 2 §10 invariants:
        - Paginates exhaustively using NextKeyMarker and NextVersionIdMarker.
        - Exact-filters both Versions and DeleteMarkers (`entry["Key"] == key`).
        - Returns None only when zero matching entries exist at `key`.
        - Counts `IsLatest == True` across both collections; fails closed if != 1.
        - Fails closed if the latest entry is a DeleteMarker (never revives older versions).
        - Fails closed if multiple data versions exist without an explicit caller VersionId.
        """
        exact_versions: list[dict[str, Any]] = []
        exact_delete_markers: list[dict[str, Any]] = []
        seen_cursors: set[tuple[str, str | None]] = set()
        key_marker: str | None = None
        version_id_marker: str | None = None

        while True:
            request_kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": key}
            if key_marker is not None:
                request_kwargs["KeyMarker"] = key_marker
            if version_id_marker is not None:
                request_kwargs["VersionIdMarker"] = version_id_marker

            resp = self._client.list_object_versions(**request_kwargs)
            if not isinstance(resp, dict):
                raise UploadAmbiguousVersionError("ListObjectVersions returned a non-dict response.")

            exact_versions.extend(
                _validate_version_collection(
                    resp.get("Versions"),
                    collection_name="Versions",
                    target_key=key,
                )
            )
            exact_delete_markers.extend(
                _validate_version_collection(
                    resp.get("DeleteMarkers"),
                    collection_name="DeleteMarkers",
                    target_key=key,
                )
            )

            is_truncated = resp.get("IsTruncated")
            if is_truncated is not None and not isinstance(is_truncated, bool):
                raise UploadAmbiguousVersionError(
                    f"ListObjectVersions returned invalid IsTruncated value: {is_truncated!r}."
                )
            if not is_truncated:
                break

            next_key = resp.get("NextKeyMarker")
            next_vid = resp.get("NextVersionIdMarker")
            if not isinstance(next_key, str) or not next_key:
                raise UploadAmbiguousVersionError(
                    f"ListObjectVersions truncated for '{key}' without NextKeyMarker."
                )
            if next_vid is not None and not isinstance(next_vid, str):
                raise UploadAmbiguousVersionError(
                    f"ListObjectVersions returned invalid NextVersionIdMarker: {next_vid!r}."
                )
            cursor = (next_key, next_vid if next_vid else None)
            if cursor in seen_cursors:
                raise UploadAmbiguousVersionError(
                    f"ListObjectVersions pagination loop detected for '{key}' at {cursor!r}."
                )
            seen_cursors.add(cursor)
            key_marker, version_id_marker = cursor

        if not exact_versions and not exact_delete_markers:
            return None

        latest_versions = [v for v in exact_versions if v["IsLatest"] is True]
        latest_delete_markers = [dm for dm in exact_delete_markers if dm["IsLatest"] is True]
        total_latest = len(latest_versions) + len(latest_delete_markers)
        if total_latest != 1:
            raise UploadAmbiguousVersionError(
                f"Expected exactly 1 IsLatest record for '{key}', found {total_latest}."
            )

        if latest_delete_markers:
            raise StagingObjectDeletedError(
                f"Latest S3 record for staging key '{key}' is a DeleteMarker."
            )

        if len(exact_versions) > 1:
            raise UploadAmbiguousVersionError(
                f"Multiple object versions ({len(exact_versions)}) detected for '{key}' without an explicit VersionId."
            )

        candidate_version_id = latest_versions[0].get("VersionId")
        if not is_valid_version_id(candidate_version_id):
            raise UploadAmbiguousVersionError(
                f"Latest version entry for '{key}' has invalid VersionId: {candidate_version_id!r}."
            )
        return candidate_version_id

    def copy_object_conditional(
        self,
        *,
        source_bucket: str,
        source_key: str,
        source_version_id: str,
        dest_bucket: str,
        dest_key: str,
    ) -> ConditionalCopyResult:
        """Execute conditional CopyObject (IfNoneMatch='*') from pinned staging VersionId.

        Always validates SDK support before sending, never strips IfNoneMatch='*',
        requires top-level valid VersionId, and fails closed on CopySourceVersionId mismatch.
        """
        self.assert_capabilities()

        if not is_valid_version_id(source_version_id):
            raise CopyVerificationError(
                f"Conditional CopyObject requires a valid pinned source VersionId, got {source_version_id!r}.",
                dest_key=dest_key,
            )

        try:
            resp = self._client.copy_object(
                CopySource={
                    "Bucket": source_bucket,
                    "Key": source_key,
                    "VersionId": source_version_id,
                },
                Bucket=dest_bucket,
                Key=dest_key,
                IfNoneMatch="*",
            )
        except ClientError as err:
            error_code, http_status, _ = _extract_client_error_details(err)
            if error_code == "PreconditionFailed" or http_status == 412:
                raise ConditionalCopyPreconditionError(
                    f"Destination object '{dest_key}' already exists in bucket '{dest_bucket}' (HTTP 412)."
                ) from err
            if error_code == "ConditionalRequestConflict" or http_status == 409:
                raise ConditionalCopyConflictError(
                    f"Concurrent conditional CopyObject conflict at '{dest_key}' in bucket '{dest_bucket}' (HTTP 409)."
                ) from err
            raise

        dest_version_id = resp.get("VersionId")
        if not is_valid_version_id(dest_version_id):
            raise CopyVerificationError(
                "CopyObject response did not return a valid top-level destination VersionId. "
                "Bucket versioning may be suspended or response is malformed.",
                dest_key=dest_key,
            )

        copy_source_version_id = resp.get("CopySourceVersionId")
        if copy_source_version_id is not None and copy_source_version_id != source_version_id:
            raise CopyVerificationError(
                f"CopyObject CopySourceVersionId {copy_source_version_id!r} does not match "
                f"pinned source_version_id {source_version_id!r}.",
                dest_key=dest_key,
            )

        copy_result_meta = resp.get("CopyObjectResult")
        etag = copy_result_meta.get("ETag") if isinstance(copy_result_meta, dict) else None
        return ConditionalCopyResult(
            destination_version_id=dest_version_id,
            copy_source_version_id=copy_source_version_id if isinstance(copy_source_version_id, str) else None,
            etag=etag if isinstance(etag, str) else None,
        )

    def delete_object(
        self,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> None:
        """Delete an object (creates a DeleteMarker when version_id is omitted)."""
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if version_id is not None:
            if not is_valid_version_id(version_id):
                raise CopyVerificationError(
                    f"delete_object called with invalid VersionId: {version_id!r}.",
                    dest_key=key,
                )
            params["VersionId"] = version_id
        self._client.delete_object(**params)
