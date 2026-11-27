"""Services package for MAIT CampusFix."""

from .complaints import ComplaintNotFoundError, ComplaintService
from .id_generator import IdGenerator, SequentialYearIdGenerator, UUIDYearIdGenerator
from .s3_storage import InMemoryS3StorageService, S3ObjectMetadata, S3StorageService
from .uploads import UploadIntentService

__all__ = [
    "ComplaintService",
    "ComplaintNotFoundError",
    "IdGenerator",
    "SequentialYearIdGenerator",
    "UUIDYearIdGenerator",
    "S3ObjectMetadata",
    "S3StorageService",
    "InMemoryS3StorageService",
    "UploadIntentService",
]
