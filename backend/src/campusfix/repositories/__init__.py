"""Repositories package for MAIT CampusFix."""

from .dynamodb import DynamoDBComplaintRepository
from .factory import (
    DEFAULT_AWS_REGION,
    ENV_AWS_REGION,
    ENV_DYNAMODB_TABLE_NAME,
    ENV_PAGINATION_HMAC_SECRET,
    ENV_REPOSITORY_BACKEND,
    REPOSITORY_BACKEND_DYNAMODB,
    REPOSITORY_BACKEND_MEMORY,
    RepositoryConfig,
    create_repository,
    resolve_repository_config,
)
from .interfaces import ComplaintRepository, UploadIntentRepository
from .memory import InMemoryComplaintRepository

__all__ = [
    "ComplaintRepository",
    "DEFAULT_AWS_REGION",
    "DynamoDBComplaintRepository",
    "ENV_AWS_REGION",
    "ENV_DYNAMODB_TABLE_NAME",
    "ENV_PAGINATION_HMAC_SECRET",
    "ENV_REPOSITORY_BACKEND",
    "InMemoryComplaintRepository",
    "REPOSITORY_BACKEND_DYNAMODB",
    "REPOSITORY_BACKEND_MEMORY",
    "RepositoryConfig",
    "UploadIntentRepository",
    "create_repository",
    "resolve_repository_config",
]
