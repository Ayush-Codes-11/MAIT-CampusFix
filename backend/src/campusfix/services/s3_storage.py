"""S3 storage abstraction and in-memory test double for MAIT CampusFix.

Implementation Status & Production Disclaimer:
- The current S3 implementation consists of an abstract interface (S3StorageService)
  and an in-memory simulation (InMemoryS3StorageService).
- boto3 is not installed in the local environment and no real AWS S3 adapter exists yet.
- No real AWS upload operation has been executed or tested.
- All S3 operations (presigned POST/GET, head_object, range reading, conditional copy,
  and deletion) are simulated locally in-memory for testing API and lifecycle contracts.
- A production Boto3S3StorageService will be implemented in a subsequent AWS integration phase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import base64
from dataclasses import dataclass
import hashlib
import threading
from typing import Any

from campusfix.domain.rules import DestinationConflictError, ValidationError


@dataclass(frozen=True)
class S3ObjectMetadata:
    """Metadata retrieved from an S3 object via HeadObject."""
    content_length: int
    sha256_checksum: str  # Lowercase 64-character hex digest
    etag: str
    content_type: str


class S3StorageService(ABC):
    """Abstract interface for Amazon S3 bucket interactions."""

    @abstractmethod
    def generate_presigned_post(
        self,
        bucket: str,
        key: str,
        fields: dict[str, str],
        conditions: list[Any],
        expires_in: int,
    ) -> dict[str, Any]:
        """Generate presigned POST URL and form fields for direct browser upload."""

    @abstractmethod
    def head_object(self, bucket: str, key: str) -> S3ObjectMetadata | None:
        """Inspect object metadata in S3 without fetching object payload.
        
        Returns:
            S3ObjectMetadata if object exists, None if object not found (404 NoSuchKey).
        """

    @abstractmethod
    def get_object_range(self, bucket: str, key: str, start_byte: int, end_byte: int) -> bytes:
        """Read a specific byte slice of an S3 object (e.g. for magic bytes check)."""

    @abstractmethod
    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        dest_bucket: str,
        dest_key: str,
        if_none_match: str | None = None,
        copy_source_if_match: str | None = None,
    ) -> None:
        """Conditionally copy an object from staging to destination key in S3.
        
        Raises:
            DestinationConflictError: If if_none_match is '*' and destination key already exists.
        """

    @abstractmethod
    def delete_object(self, bucket: str, key: str) -> None:
        """Delete an object from S3 (best-effort)."""

    @abstractmethod
    def generate_presigned_get(self, bucket: str, key: str, expires_in: int) -> str:
        """Generate a short-lived presigned GET URL for viewing private attachments."""


@dataclass
class _InMemoryObject:
    data: bytes
    content_type: str
    etag: str
    sha256_hex: str


class InMemoryS3StorageService(S3StorageService):
    """Thread-safe in-memory simulation of S3 storage for tests and local development."""

    def __init__(self, region: str = "ap-southeast-2") -> None:
        self.region = region
        self._objects: dict[tuple[str, str], _InMemoryObject] = {}
        self._lock = threading.RLock()

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Test helper to simulate direct upload of bytes to S3."""
        with self._lock:
            sha256_hex = hashlib.sha256(data).hexdigest()
            md5_hex = hashlib.md5(data).hexdigest()
            self._objects[(bucket, key)] = _InMemoryObject(
                data=data,
                content_type=content_type,
                etag=f'"{md5_hex}"',
                sha256_hex=sha256_hex,
            )

    def generate_presigned_post(
        self,
        bucket: str,
        key: str,
        fields: dict[str, str],
        conditions: list[Any],
        expires_in: int,
    ) -> dict[str, Any]:
        url = f"https://{bucket}.s3.{self.region}.amazonaws.com/"
        merged_fields = dict(fields)
        merged_fields["key"] = key
        merged_fields["policy"] = "mock-base64-policy"
        merged_fields["x-amz-signature"] = "mock-signature"
        return {"url": url, "fields": merged_fields}

    def head_object(self, bucket: str, key: str) -> S3ObjectMetadata | None:
        with self._lock:
            obj = self._objects.get((bucket, key))
            if obj is None:
                return None
            return S3ObjectMetadata(
                content_length=len(obj.data),
                sha256_checksum=obj.sha256_hex,
                etag=obj.etag,
                content_type=obj.content_type,
            )

    def get_object_range(self, bucket: str, key: str, start_byte: int, end_byte: int) -> bytes:
        with self._lock:
            obj = self._objects.get((bucket, key))
            if obj is None:
                raise KeyError(f"NoSuchKey: {bucket}/{key}")
            return obj.data[start_byte : end_byte + 1]

    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        dest_bucket: str,
        dest_key: str,
        if_none_match: str | None = None,
        copy_source_if_match: str | None = None,
    ) -> None:
        with self._lock:
            src = self._objects.get((source_bucket, source_key))
            if src is None:
                raise KeyError(f"Source NoSuchKey: {source_bucket}/{source_key}")

            if copy_source_if_match is not None and src.etag != copy_source_if_match:
                raise DestinationConflictError("Source ETag precondition failed.")

            dest_exists = (dest_bucket, dest_key) in self._objects
            if if_none_match == "*" and dest_exists:
                dest = self._objects[(dest_bucket, dest_key)]
                # If content matches identically, permit idempotency
                if dest.sha256_hex == src.sha256_hex and len(dest.data) == len(src.data):
                    return
                raise DestinationConflictError(
                    f"Destination object '{dest_key}' already exists in bucket '{dest_bucket}'."
                )

            # Perform copy
            self._objects[(dest_bucket, dest_key)] = _InMemoryObject(
                data=src.data,
                content_type=src.content_type,
                etag=src.etag,
                sha256_hex=src.sha256_hex,
            )

    def delete_object(self, bucket: str, key: str) -> None:
        with self._lock:
            self._objects.pop((bucket, key), None)

    def generate_presigned_get(self, bucket: str, key: str, expires_in: int) -> str:
        with self._lock:
            if (bucket, key) not in self._objects:
                pass  # S3 generates presigned URLs without checking object existence
            return f"https://{bucket}.s3.{self.region}.amazonaws.com/{key}?X-Amz-Expires={expires_in}&mock-sig=1"

    def clear(self) -> None:
        """Clear all stored objects."""
        with self._lock:
            self._objects.clear()
