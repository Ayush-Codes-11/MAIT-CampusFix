"""Controlled repository selection factory for MAIT CampusFix.

Selects between :class:`InMemoryComplaintRepository` (default for local dev/tests)
and :class:`DynamoDBComplaintRepository` using application environment configuration
without hardcoding AWS credentials or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Final, Mapping

from campusfix.domain.models import Complaint, ValidationError
from campusfix.repositories.dynamodb import DynamoDBComplaintRepository
from campusfix.repositories.memory import InMemoryComplaintRepository

ENV_REPOSITORY_BACKEND: Final[str] = "CAMPUSFIX_REPOSITORY_BACKEND"
ENV_DYNAMODB_TABLE_NAME: Final[str] = "DYNAMODB_TABLE_NAME"
ENV_AWS_REGION: Final[str] = "AWS_REGION"
ENV_PAGINATION_HMAC_SECRET: Final[str] = "PAGINATION_HMAC_SECRET"

REPOSITORY_BACKEND_MEMORY: Final[str] = "memory"
REPOSITORY_BACKEND_DYNAMODB: Final[str] = "dynamodb"
SUPPORTED_REPOSITORY_BACKENDS: Final[frozenset[str]] = frozenset({
    REPOSITORY_BACKEND_MEMORY,
    REPOSITORY_BACKEND_DYNAMODB,
})
DEFAULT_AWS_REGION: Final[str] = "ap-southeast-2"

AppRepository = InMemoryComplaintRepository | DynamoDBComplaintRepository


@dataclass(frozen=True)
class RepositoryConfig:
    """Validated repository configuration resolved from arguments/environment."""

    backend: str
    table_name: str | None = None
    region_name: str = DEFAULT_AWS_REGION


def resolve_aws_region(
    region_name: str | None,
    env: Mapping[str, str],
) -> str:
    """Resolve and validate AWS region from explicit argument, AWS_REGION, or default."""
    if region_name is not None:
        if not isinstance(region_name, str) or not region_name.strip():
            raise ValidationError(
                "Repository configuration error: 'region_name' must be a non-empty AWS region string."
            )
        return region_name.strip()

    if ENV_AWS_REGION in env:
        raw_env_region = env[ENV_AWS_REGION]
        if not isinstance(raw_env_region, str) or not raw_env_region.strip():
            raise ValidationError(
                f"Repository configuration error: '{ENV_AWS_REGION}' cannot be blank when set."
            )
        return raw_env_region.strip()

    return DEFAULT_AWS_REGION


def resolve_repository_config(
    *,
    backend: str | None = None,
    table_name: str | None = None,
    region_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RepositoryConfig:
    """Resolve and validate repository selection and DynamoDB configuration.

    Rules:
    - Defaults to ``"memory"`` only when both ``backend`` and
      ``CAMPUSFIX_REPOSITORY_BACKEND`` are unset (``None``).
    - Rejects blank/whitespace or unsupported backend identifiers with ``ValidationError``.
    - Requires a non-empty ``table_name`` / ``DYNAMODB_TABLE_NAME`` when
      ``backend == "dynamodb"``.
    - Resolves ``region_name`` from explicit argument, ``AWS_REGION``, or
      ``"ap-southeast-2"`` (matching ``UploadIntentService``), rejecting blank strings.
    """
    env = os.environ if environ is None else environ

    if backend is not None:
        if not isinstance(backend, str) or not backend.strip():
            raise ValidationError(
                "Repository configuration error: 'backend' must be 'memory' or 'dynamodb'."
            )
        resolved_backend = backend.strip().lower()
    elif ENV_REPOSITORY_BACKEND in env:
        raw_env_backend = env[ENV_REPOSITORY_BACKEND]
        if not isinstance(raw_env_backend, str) or not raw_env_backend.strip():
            raise ValidationError(
                f"Repository configuration error: '{ENV_REPOSITORY_BACKEND}' cannot be blank when set."
            )
        resolved_backend = raw_env_backend.strip().lower()
    else:
        resolved_backend = REPOSITORY_BACKEND_MEMORY

    if resolved_backend not in SUPPORTED_REPOSITORY_BACKENDS:
        raise ValidationError(
            f"Unsupported repository backend '{resolved_backend}'. "
            f"Allowed values: {', '.join(sorted(SUPPORTED_REPOSITORY_BACKENDS))}."
        )

    resolved_region = resolve_aws_region(region_name, env)

    if resolved_backend == REPOSITORY_BACKEND_MEMORY:
        return RepositoryConfig(
            backend=REPOSITORY_BACKEND_MEMORY,
            table_name=None,
            region_name=resolved_region,
        )

    raw_table = table_name if table_name is not None else env.get(ENV_DYNAMODB_TABLE_NAME)
    if not isinstance(raw_table, str) or not raw_table.strip():
        raise ValidationError(
            f"Repository configuration error: DynamoDB backend requires non-empty "
            f"'{ENV_DYNAMODB_TABLE_NAME}' (or explicit 'table_name')."
        )

    return RepositoryConfig(
        backend=REPOSITORY_BACKEND_DYNAMODB,
        table_name=raw_table.strip(),
        region_name=resolved_region,
    )


def create_repository(
    *,
    backend: str | None = None,
    table_name: str | None = None,
    region_name: str | None = None,
    cursor_secret: str | bytes | None = None,
    dynamodb_client: Any | None = None,
    initial_complaints: list[Complaint] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppRepository:
    """Instantiate the configured repository backend.

    Returns a single concrete repository instance implementing both
    :class:`ComplaintRepository` and :class:`UploadIntentRepository`.
    """
    env = os.environ if environ is None else environ
    config = resolve_repository_config(
        backend=backend,
        table_name=table_name,
        region_name=region_name,
        environ=env,
    )

    if config.backend == REPOSITORY_BACKEND_MEMORY:
        return InMemoryComplaintRepository(initial_complaints=initial_complaints)

    if initial_complaints is not None:
        raise ValidationError(
            "Repository configuration error: 'initial_complaints' is only supported for the 'memory' backend."
        )

    resolved_secret: str | bytes | None = cursor_secret
    if resolved_secret is None and ENV_PAGINATION_HMAC_SECRET in env:
        raw_secret = env[ENV_PAGINATION_HMAC_SECRET]
        if not isinstance(raw_secret, str) or not raw_secret.strip():
            raise ValidationError(
                f"Repository configuration error: '{ENV_PAGINATION_HMAC_SECRET}' cannot be blank when set."
            )
        resolved_secret = raw_secret.strip()

    assert config.table_name is not None
    return DynamoDBComplaintRepository(
        table_name=config.table_name,
        client=dynamodb_client,
        region_name=config.region_name,
        cursor_secret=resolved_secret,
    )
