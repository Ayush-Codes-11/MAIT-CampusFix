"""Application composition and dependency wiring for MAIT CampusFix.

Composes domain services and the Stage B2 versioned-upload state adapter around a
single shared repository instance selected via the repository factory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import os
from typing import Any, Final, Mapping

from campusfix.domain.models import Complaint, ValidationError
from campusfix.repositories.dynamodb import DynamoDBComplaintRepository
from campusfix.repositories.factory import (
    AppRepository,
    REPOSITORY_BACKEND_DYNAMODB,
    REPOSITORY_BACKEND_MEMORY,
    RepositoryConfig,
    create_repository,
    resolve_aws_region,
    resolve_repository_config,
)
from campusfix.repositories.interfaces import ComplaintRepository, UploadIntentRepository
from campusfix.services.boto3_s3_storage import Boto3S3StorageService
from campusfix.services.complaints import ComplaintService
from campusfix.services.id_generator import IdGenerator
from campusfix.services.repository_state_adapter import RepositoryVersionedUploadStateAdapter
from campusfix.services.s3_storage import InMemoryS3StorageService, S3StorageService
from campusfix.services.upload_confirmation_worker import UploadConfirmationWorker
from campusfix.services.uploads import UploadIntentService, UploadPresignedStorage

ENV_S3_ATTACHMENTS_BUCKET: Final[str] = "S3_ATTACHMENTS_BUCKET"
DEFAULT_S3_ATTACHMENTS_BUCKET: Final[str] = "campusfix-attachments-ap-southeast-2"

ENV_UPLOAD_BACKEND: Final[str] = "CAMPUSFIX_UPLOAD_BACKEND"
UPLOAD_BACKEND_MEMORY: Final[str] = "memory"
UPLOAD_BACKEND_S3: Final[str] = "s3"
SUPPORTED_UPLOAD_BACKENDS: Final[tuple[str, str]] = (
    UPLOAD_BACKEND_MEMORY,
    UPLOAD_BACKEND_S3,
)


@dataclass(frozen=True)
class UploadConfig:
    """Resolved upload storage/confirmation backend configuration."""

    backend: str
    bucket_name: str
    region_name: str


@dataclass(frozen=True)
class ApplicationContainer:
    """Composed backend collaborators sharing a single repository instance."""

    repository: AppRepository
    complaints_service: ComplaintService
    uploads_service: UploadIntentService
    versioned_upload_state_adapter: RepositoryVersionedUploadStateAdapter
    s3_storage: S3StorageService | UploadPresignedStorage
    upload_bucket: str
    aws_region: str
    repository_config: RepositoryConfig
    upload_backend: str = UPLOAD_BACKEND_MEMORY
    confirmation_worker: UploadConfirmationWorker | None = None


def _resolve_bucket_name(
    bucket_name: str | None,
    env: Mapping[str, str],
    *,
    require_explicit: bool = False,
) -> str:
    if bucket_name is not None:
        if not isinstance(bucket_name, str) or not bucket_name.strip():
            raise ValidationError(
                "Application configuration error: 'bucket_name' must be a non-empty S3 bucket name."
            )
        return bucket_name.strip()

    if ENV_S3_ATTACHMENTS_BUCKET in env:
        raw_env_bucket = env[ENV_S3_ATTACHMENTS_BUCKET]
        if not isinstance(raw_env_bucket, str) or not raw_env_bucket.strip():
            raise ValidationError(
                f"Application configuration error: '{ENV_S3_ATTACHMENTS_BUCKET}' cannot be blank when set."
            )
        return raw_env_bucket.strip()

    if require_explicit:
        raise ValidationError(
            f"Upload backend '{UPLOAD_BACKEND_S3}' requires environment variable "
            f"'{ENV_S3_ATTACHMENTS_BUCKET}' or an explicit 'bucket_name'."
        )

    return DEFAULT_S3_ATTACHMENTS_BUCKET


def resolve_upload_config(
    *,
    upload_backend: str | None = None,
    bucket_name: str | None = None,
    region_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> UploadConfig:
    """Resolve upload backend, S3 bucket, and AWS region without performing I/O."""
    env = os.environ if environ is None else environ

    if upload_backend is not None:
        if not isinstance(upload_backend, str) or not upload_backend.strip():
            raise ValidationError(
                "Upload configuration error: 'upload_backend' cannot be blank."
            )
        selected_backend = upload_backend.strip().lower()
    elif ENV_UPLOAD_BACKEND in env:
        raw_env_backend = env[ENV_UPLOAD_BACKEND]
        if not isinstance(raw_env_backend, str) or not raw_env_backend.strip():
            raise ValidationError(
                f"Upload configuration error: '{ENV_UPLOAD_BACKEND}' cannot be blank when set."
            )
        selected_backend = raw_env_backend.strip().lower()
    else:
        selected_backend = UPLOAD_BACKEND_MEMORY

    if selected_backend not in SUPPORTED_UPLOAD_BACKENDS:
        supported = ", ".join(SUPPORTED_UPLOAD_BACKENDS)
        raise ValidationError(
            f"Unsupported upload backend '{selected_backend}'. Supported backends: {supported}."
        )

    resolved_region = resolve_aws_region(region_name, env)
    resolved_bucket = _resolve_bucket_name(
        bucket_name,
        env,
        require_explicit=(selected_backend == UPLOAD_BACKEND_S3),
    )

    return UploadConfig(
        backend=selected_backend,
        bucket_name=resolved_bucket,
        region_name=resolved_region,
    )


def create_application(
    *,
    repository: AppRepository | None = None,
    backend: str | None = None,
    table_name: str | None = None,
    region_name: str | None = None,
    bucket_name: str | None = None,
    cursor_secret: str | bytes | None = None,
    dynamodb_client: Any | None = None,
    upload_backend: str | None = None,
    s3_storage: Any | None = None,
    s3_client: Any | None = None,
    confirmation_worker: UploadConfirmationWorker | None = None,
    id_generator: IdGenerator | None = None,
    initial_complaints: list[Complaint] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ApplicationContainer:
    """Compose CampusFix services and state adapter around one shared repository instance.

    Defaults to :class:`InMemoryComplaintRepository` and :class:`InMemoryS3StorageService`
    with zero AWS network calls or credential requirements.  Setting
    ``CAMPUSFIX_UPLOAD_BACKEND="s3"`` (or ``upload_backend="s3"``) wires
    :class:`Boto3S3StorageService` and :class:`UploadConfirmationWorker` backed by
    ``RepositoryVersionedUploadStateAdapter`` sharing the exact same repository instance.
    """
    env = os.environ if environ is None else environ
    upload_config = resolve_upload_config(
        upload_backend=upload_backend,
        bucket_name=bucket_name,
        region_name=region_name,
        environ=env,
    )
    resolved_bucket = upload_config.bucket_name

    if repository is not None:
        if not isinstance(repository, ComplaintRepository) or not isinstance(
            repository, UploadIntentRepository
        ):
            raise ValidationError(
                "Supplied 'repository' must implement both ComplaintRepository and UploadIntentRepository."
            )
        resolved_region = resolve_aws_region(region_name, env)
        inferred_backend = (
            REPOSITORY_BACKEND_DYNAMODB
            if isinstance(repository, DynamoDBComplaintRepository)
            else REPOSITORY_BACKEND_MEMORY
        )
        inferred_table = (
            repository._table_name
            if isinstance(repository, DynamoDBComplaintRepository)
            else None
        )
        repo_config = RepositoryConfig(
            backend=inferred_backend,
            table_name=inferred_table,
            region_name=resolved_region,
        )
        shared_repo = repository
    else:
        repo_config = resolve_repository_config(
            backend=backend,
            table_name=table_name,
            region_name=region_name,
            environ=env,
        )
        shared_repo = create_repository(
            backend=backend,
            table_name=table_name,
            region_name=region_name,
            cursor_secret=cursor_secret,
            dynamodb_client=dynamodb_client,
            initial_complaints=initial_complaints,
            environ=env,
        )

    versioned_upload_state_adapter = RepositoryVersionedUploadStateAdapter(
        intent_repo=shared_repo,
        complaint_repo=shared_repo,
    )

    resolved_worker: UploadConfirmationWorker | None
    resolved_s3: S3StorageService | UploadPresignedStorage

    if upload_config.backend == UPLOAD_BACKEND_MEMORY:
        if s3_client is not None:
            raise ValidationError(
                "Application configuration error: 's3_client' requires upload backend 's3'."
            )
        if confirmation_worker is not None:
            raise ValidationError(
                "Application configuration error: 'confirmation_worker' requires upload backend 's3'."
            )
        if s3_storage is not None:
            if isinstance(s3_storage, Boto3S3StorageService) or (
                callable(getattr(s3_storage, "copy_object_conditional", None))
                and not callable(getattr(s3_storage, "copy_object", None))
            ):
                raise ValidationError(
                    "Conditional S3 storage requires upload backend 's3'."
                )
            resolved_s3 = s3_storage
        else:
            resolved_s3 = InMemoryS3StorageService(region=repo_config.region_name)
        resolved_worker = None
    else:
        if s3_storage is not None:
            if s3_client is not None:
                raise ValidationError(
                    "Application configuration error: provide either 's3_storage' or 's3_client', not both."
                )
            required_methods = (
                "list_object_versions_exact",
                "head_object",
                "get_object_range",
                "copy_object_conditional",
                "delete_object",
                "generate_presigned_post",
                "generate_presigned_get",
            )
            if isinstance(s3_storage, InMemoryS3StorageService) or not all(
                callable(getattr(s3_storage, method_name, None))
                for method_name in required_methods
            ):
                raise ValidationError(
                    "Upload backend 's3' requires a conditional S3 storage implementation "
                    "supporting presigned upload/download URLs."
                )
            resolved_s3 = s3_storage
        else:
            resolved_s3 = Boto3S3StorageService(
                client=s3_client,
                region_name=upload_config.region_name,
            )

        if confirmation_worker is not None:
            resolved_worker = confirmation_worker
        else:
            worker_kwargs: dict[str, Any] = {
                "s3_storage": resolved_s3,
                "state_port": versioned_upload_state_adapter,
                "source_bucket": resolved_bucket,
            }
            if now_fn is not None:
                worker_kwargs["now_fn"] = now_fn
            resolved_worker = UploadConfirmationWorker(**worker_kwargs)

    complaints_service = ComplaintService(
        repository=shared_repo,
        id_generator=id_generator,
    )
    uploads_service = UploadIntentService(
        complaint_repository=shared_repo,
        intent_repository=shared_repo,
        s3_storage=resolved_s3,
        bucket_name=resolved_bucket,
        region=repo_config.region_name,
        confirmation_worker=resolved_worker,
        now_fn=now_fn,
    )

    return ApplicationContainer(
        repository=shared_repo,
        complaints_service=complaints_service,
        uploads_service=uploads_service,
        versioned_upload_state_adapter=versioned_upload_state_adapter,
        s3_storage=resolved_s3,
        upload_bucket=resolved_bucket,
        aws_region=repo_config.region_name,
        repository_config=repo_config,
        upload_backend=upload_config.backend,
        confirmation_worker=resolved_worker,
    )
