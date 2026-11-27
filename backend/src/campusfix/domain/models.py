"""Domain models for MAIT CampusFix.

These models define the core business entities independent of any
persistence mechanism (in-memory, DynamoDB, PostgreSQL, etc.).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# -----------------------------------------------------------------------------
# Base Domain Exceptions (defined here so models can raise them on deserialization)
# -----------------------------------------------------------------------------

class CampusFixDomainError(Exception):
    """Base exception for all domain rule violations in MAIT CampusFix."""


class ValidationError(CampusFixDomainError, ValueError):
    """Raised when input or persisted parameters fail business validation rules."""


def parse_utc_iso_datetime(val: Any, field_name: str) -> datetime:
    """Parse and validate that val is a timezone-aware ISO datetime strictly in UTC.
    
    Raises:
        ValidationError: If val is missing, naive, not in UTC, or not a valid ISO timestamp.
    """
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned:
            raise ValidationError(f"'{field_name}' datetime string cannot be empty or whitespace-only.")
        try:
            dt = datetime.fromisoformat(cleaned)
        except (ValueError, TypeError) as err:
            raise ValidationError(f"Invalid ISO datetime for '{field_name}': {val!r}") from err
    elif isinstance(val, datetime):
        dt = val
    else:
        raise ValidationError(f"'{field_name}' must be a datetime or ISO string, got {type(val).__name__}.")

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValidationError(
            f"'{field_name}' must be a timezone-aware datetime in UTC, but received naive datetime: {val!r}"
        )

    if dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValidationError(
            f"'{field_name}' must be in UTC timezone (offset +00:00), but received offset: {dt.utcoffset()}."
        )

    return dt


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------

class Role(str, Enum):
    """User roles in the MAIT CampusFix system.
    
    - STUDENT: Campus student. Can file complaints, upvote, submit feedback,
      and raise follow-ups for their own complaints.
    - MAINTENANCE: Maintenance staff technician/supervisor. Can update status,
      assign technicians, and add resolution remarks.
    - ADMIN: Estate / College administrative authority. Full operational access.
    """
    STUDENT = "STUDENT"
    MAINTENANCE = "MAINTENANCE"
    ADMIN = "ADMIN"


@dataclass(frozen=True)
class Actor:
    """Authenticated principal representing the verified caller.
    
    Security & Architecture Note:
    ------------------------------
    In production with AWS Cognito / API Gateway:
    - This Actor MUST be constructed in the API/HTTP middleware by decoding
      and validating the Cognito JWT Access/ID Token (signature, exp, aud, iss).
    - `user_id` should be extracted from the `sub` claim or verified institutional identity.
    - `role` should be mapped from Cognito User Pool Groups (e.g. `cognito:groups`
      containing 'students', 'maintenance', or 'admins').
    - Client requests must NEVER pass `actor_id` or `role` in the request body
      to claim privileges.
    """
    user_id: str
    role: Role

    def __post_init__(self) -> None:
        if not self.user_id or not self.user_id.strip():
            raise ValidationError("Actor user_id cannot be empty or whitespace-only.")
        if not isinstance(self.role, Role):
            raise ValidationError(f"Actor role must be an instance of Role enum. Received: {self.role}")

    @property
    def is_student(self) -> bool:
        return self.role == Role.STUDENT

    @property
    def is_maintenance(self) -> bool:
        return self.role == Role.MAINTENANCE

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_staff(self) -> bool:
        """True if actor has operational staff privileges (Maintenance or Admin)."""
        return self.role in (Role.MAINTENANCE, Role.ADMIN)

    @classmethod
    def student(cls, user_id: str) -> Actor:
        """Convenience factory for student actors."""
        return cls(user_id=user_id.strip(), role=Role.STUDENT)

    @classmethod
    def maintenance(cls, user_id: str) -> Actor:
        """Convenience factory for maintenance staff actors."""
        return cls(user_id=user_id.strip(), role=Role.MAINTENANCE)

    @classmethod
    def admin(cls, user_id: str) -> Actor:
        """Convenience factory for administrator actors."""
        return cls(user_id=user_id.strip(), role=Role.ADMIN)


class ComplaintStatus(str, Enum):
    """Lifecycle statuses for a complaint in MAIT CampusFix.
    
    Transitions are strictly forward-only:
    PENDING -> IN_PROGRESS -> RESOLVED (terminal)
    """
    PENDING = "pending"
    IN_PROGRESS = "in-progress"
    RESOLVED = "resolved"


class IssueCategory(str, Enum):
    """Categories of campus maintenance issues."""
    ELECTRICAL = "Electrical"
    PLUMBING = "Plumbing"
    CIVIL_STRUCTURAL = "Civil / Structural"
    HOUSEKEEPING = "Housekeeping"
    IT_NETWORK = "IT / Network"
    FURNITURE = "Furniture"
    FIRE_SAFETY = "Fire Safety"
    OTHER = "Other"


@dataclass(frozen=True)
class StatusHistoryEntry:
    """An immutable audit trail entry recording a single status transition."""
    from_status: ComplaintStatus
    to_status: ComplaintStatus
    changed_at: datetime
    changed_by: str  # Authenticated staff or admin identifier
    remarks: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to a dictionary matching frontend expectations."""
        return {
            "from": self.from_status.value,
            "to": self.to_status.value,
            "changedAt": self.changed_at.isoformat(),
            "changedBy": self.changed_by,
            "remarks": self.remarks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StatusHistoryEntry:
        """Construct entry from a dictionary with strict validation."""
        if not isinstance(data, dict):
            raise ValidationError(f"StatusHistoryEntry data must be a dictionary, got {type(data).__name__}.")

        from_raw = data.get("from")
        to_raw = data.get("to")
        try:
            from_status = ComplaintStatus(from_raw) if isinstance(from_raw, str) else from_raw
            to_status = ComplaintStatus(to_raw) if isinstance(to_raw, str) else to_raw
            if not isinstance(from_status, ComplaintStatus) or not isinstance(to_status, ComplaintStatus):
                raise ValueError()
        except (ValueError, KeyError):
            raise ValidationError(f"Invalid status values in history entry: from={from_raw!r}, to={to_raw!r}.")

        # Validate transition matrix
        from campusfix.domain.rules import can_transition
        if not can_transition(from_status, to_status):
            raise ValidationError(
                f"Invalid status transition in history entry: cannot transition from '{from_status.value}' to '{to_status.value}'."
            )

        changed_by = data.get("changedBy")
        if not changed_by or not isinstance(changed_by, str) or not changed_by.strip():
            raise ValidationError("StatusHistoryEntry changedBy must be a non-empty string.")

        changed_at = parse_utc_iso_datetime(data.get("changedAt"), "StatusHistoryEntry changedAt")

        remarks = data.get("remarks")
        if to_status == ComplaintStatus.RESOLVED:
            if not remarks or not isinstance(remarks, str) or not remarks.strip():
                raise ValidationError("StatusHistoryEntry marking complaint 'resolved' must have non-empty remarks.")
        elif remarks is not None and not isinstance(remarks, str):
            raise ValidationError("StatusHistoryEntry remarks must be a string or None.")

        return cls(
            from_status=from_status,
            to_status=to_status,
            changed_at=changed_at,
            changed_by=changed_by.strip(),
            remarks=remarks.strip() if remarks else None,
        )


@dataclass
class ComplaintFeedback:
    """Student feedback and quality rating on a resolved complaint."""
    rating: int  # 1 to 5 stars
    submitted_at: datetime
    comment: str | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert feedback to a dictionary matching frontend expectations."""
        result: dict[str, Any] = {
            "rating": self.rating,
            "submittedAt": self.submitted_at.isoformat(),
            "comment": self.comment,
        }
        if self.updated_at:
            result["updatedAt"] = self.updated_at.isoformat()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComplaintFeedback:
        """Construct feedback from a dictionary with strict validation."""
        if not isinstance(data, dict):
            raise ValidationError(f"ComplaintFeedback data must be a dictionary, got {type(data).__name__}.")

        raw_rating = data.get("rating")
        if isinstance(raw_rating, bool) or not isinstance(raw_rating, int):
            raise ValidationError(f"Feedback rating must be an integer (not boolean or float), got {type(raw_rating).__name__}.")
        if raw_rating < 1 or raw_rating > 5:
            raise ValidationError(f"Feedback rating must be an integer between 1 and 5. Received: {raw_rating}.")

        sub_at = parse_utc_iso_datetime(data.get("submittedAt"), "Feedback submittedAt")

        raw_updated_at = data.get("updatedAt")
        if raw_updated_at is not None:
            up_at = parse_utc_iso_datetime(raw_updated_at, "Feedback updatedAt")
            if up_at < sub_at:
                raise ValidationError(
                    f"Feedback updatedAt ({up_at.isoformat()}) cannot be earlier than submittedAt ({sub_at.isoformat()})."
                )
        else:
            up_at = None

        comment = data.get("comment")
        if comment is not None and not isinstance(comment, str):
            raise ValidationError("Feedback comment must be a string or None.")

        return cls(
            rating=raw_rating,
            submitted_at=sub_at,
            comment=comment.strip() if comment else None,
            updated_at=up_at,
        )


class UploadIntentStatus(str, Enum):
    """Lifecycle statuses for an S3 upload intent."""
    PENDING_UPLOAD = "PENDING_UPLOAD"
    CONFIRMING = "CONFIRMING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class UploadAttemptStatus(str, Enum):
    """Lifecycle statuses for a single CopyObject attempt (Spec §6.1)."""
    COPYING = "COPYING"
    COMMITTED = "COMMITTED"
    ABANDONED = "ABANDONED"


def format_canonical_utc_timestamp(dt: datetime) -> str:
    """Produce a fixed 27-char UTC timestamp for DynamoDB sort keys.

    Format: ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (exactly 27 ASCII chars, microseconds always present).
    Use ONLY for DynamoDB sort keys; API DTOs should continue using ``.isoformat()``.

    Raises:
        ValidationError: If ``dt`` is not a timezone-aware ``datetime``.
    """
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValidationError(
            f"format_canonical_utc_timestamp requires a timezone-aware UTC datetime, got {dt!r}."
        )
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class UploadIntent:
    """Represents a two-stage S3 image upload authorization and confirmation intent."""
    intent_id: str
    complaint_id: str
    owner_id: str
    staging_key: str
    final_key: str
    expected_mime: str
    max_byte_size: int
    expected_sha256: str
    status: UploadIntentStatus
    created_at: datetime
    upload_expires_at: datetime
    confirmation_deadline: datetime
    recovery_deadline: datetime
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    confirm_attempts: int = 0
    staging_sha256: str | None = None
    staging_size: int | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    cleanup_ttl: int | None = None
    # Stage B2: pinned source S3 version and mutation timestamp
    source_version_id: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.intent_id or not self.intent_id.strip():
            raise ValidationError("UploadIntent 'intent_id' cannot be empty.")
        if not self.complaint_id or not self.complaint_id.strip():
            raise ValidationError("UploadIntent 'complaint_id' cannot be empty.")
        if not self.owner_id or not self.owner_id.strip():
            raise ValidationError("UploadIntent 'owner_id' cannot be empty.")
        if not isinstance(self.status, UploadIntentStatus):
            raise ValidationError(f"UploadIntent status must be UploadIntentStatus enum, got {self.status!r}.")
        if self.source_version_id is not None:
            if not isinstance(self.source_version_id, str) or self.source_version_id.strip() in ("", "null"):
                raise ValidationError(
                    f"UploadIntent 'source_version_id' must be a non-empty valid VersionId or None, "
                    f"got {self.source_version_id!r}."
                )
        if self.updated_at is not None:
            parse_utc_iso_datetime(self.updated_at, "updated_at")

    @staticmethod
    def intent_pk(complaint_id: str) -> str:
        """DynamoDB Partition Key for this intent (Spec §4.2)."""
        return f"UPLOAD_INTENT#{complaint_id}"

    @staticmethod
    def intent_sk(intent_id: str) -> str:
        """DynamoDB Sort Key for this intent (Spec §4.2)."""
        return f"INTENT#{intent_id}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize intent to dictionary."""
        return {
            "intentId": self.intent_id,
            "complaintId": self.complaint_id,
            "ownerId": self.owner_id,
            "stagingKey": self.staging_key,
            "finalKey": self.final_key,
            "expectedMime": self.expected_mime,
            "maxByteSize": self.max_byte_size,
            "expectedSha256": self.expected_sha256,
            "status": self.status.value,
            "createdAt": self.created_at.isoformat(),
            "uploadExpiresAt": self.upload_expires_at.isoformat(),
            "confirmationDeadline": self.confirmation_deadline.isoformat(),
            "recoveryDeadline": self.recovery_deadline.isoformat(),
            "leaseToken": self.lease_token,
            "leaseExpiresAt": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "confirmAttempts": self.confirm_attempts,
            "stagingSha256": self.staging_sha256,
            "stagingSize": self.staging_size,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "failureReason": self.failure_reason,
            "cleanupTtl": self.cleanup_ttl,
            "sourceVersionId": self.source_version_id,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UploadIntent:
        """Deserialize intent from dictionary with strict validation."""
        if not isinstance(data, dict):
            raise ValidationError(f"UploadIntent data must be a dictionary, got {type(data).__name__}.")

        intent_id = data.get("intentId", data.get("intent_id"))
        if not intent_id or not isinstance(intent_id, str) or not intent_id.strip():
            raise ValidationError("UploadIntent 'intentId' must be a non-empty string.")

        complaint_id = data.get("complaintId", data.get("complaint_id"))
        if not complaint_id or not isinstance(complaint_id, str) or not complaint_id.strip():
            raise ValidationError("UploadIntent 'complaintId' must be a non-empty string.")

        owner_id = data.get("ownerId", data.get("owner_id"))
        if not owner_id or not isinstance(owner_id, str) or not owner_id.strip():
            raise ValidationError("UploadIntent 'ownerId' must be a non-empty string.")

        staging_key = data.get("stagingKey", data.get("staging_key"))
        if not staging_key or not isinstance(staging_key, str) or not staging_key.strip():
            raise ValidationError("UploadIntent 'stagingKey' must be a non-empty string.")

        final_key = data.get("finalKey", data.get("final_key"))
        if not final_key or not isinstance(final_key, str) or not final_key.strip():
            raise ValidationError("UploadIntent 'finalKey' must be a non-empty string.")

        expected_mime = data.get("expectedMime", data.get("expected_mime"))
        if not expected_mime or not isinstance(expected_mime, str) or not expected_mime.strip():
            raise ValidationError("UploadIntent 'expectedMime' must be a non-empty string.")

        max_byte_size = data.get("maxByteSize", data.get("max_byte_size"))
        if isinstance(max_byte_size, bool) or not isinstance(max_byte_size, int) or max_byte_size <= 0:
            raise ValidationError(f"UploadIntent 'maxByteSize' must be a positive integer, got {max_byte_size!r}.")

        expected_sha256 = data.get("expectedSha256", data.get("expected_sha256"))
        if not expected_sha256 or not isinstance(expected_sha256, str) or not expected_sha256.strip():
            raise ValidationError("UploadIntent 'expectedSha256' must be a non-empty string.")

        raw_status = data.get("status")
        try:
            status = UploadIntentStatus(raw_status)
        except (ValueError, TypeError) as err:
            raise ValidationError(f"Invalid UploadIntent status: {raw_status!r}") from err

        created_at = parse_utc_iso_datetime(data.get("createdAt", data.get("created_at")), "createdAt")
        upload_expires_at = parse_utc_iso_datetime(data.get("uploadExpiresAt", data.get("upload_expires_at")), "uploadExpiresAt")
        confirmation_deadline = parse_utc_iso_datetime(data.get("confirmationDeadline", data.get("confirmation_deadline")), "confirmationDeadline")
        recovery_deadline = parse_utc_iso_datetime(data.get("recoveryDeadline", data.get("recovery_deadline")), "recoveryDeadline")

        raw_lease_exp = data.get("leaseExpiresAt", data.get("lease_expires_at"))
        lease_expires_at = parse_utc_iso_datetime(raw_lease_exp, "leaseExpiresAt") if raw_lease_exp else None

        raw_comp_at = data.get("completedAt", data.get("completed_at"))
        completed_at = parse_utc_iso_datetime(raw_comp_at, "completedAt") if raw_comp_at else None

        # Stage B2 fields
        raw_src_vid = data.get("sourceVersionId", data.get("source_version_id"))
        if raw_src_vid is not None:
            if not isinstance(raw_src_vid, str) or raw_src_vid.strip() in ("", "null"):
                raise ValidationError(
                    f"UploadIntent 'sourceVersionId' must be a non-empty string or None, got {raw_src_vid!r}."
                )
            source_version_id: str | None = raw_src_vid.strip()
        else:
            source_version_id = None

        raw_upd_at = data.get("updatedAt", data.get("updated_at"))
        updated_at = parse_utc_iso_datetime(raw_upd_at, "updatedAt") if raw_upd_at else None

        return cls(
            intent_id=intent_id.strip(),
            complaint_id=complaint_id.strip(),
            owner_id=owner_id.strip(),
            staging_key=staging_key.strip(),
            final_key=final_key.strip(),
            expected_mime=expected_mime.strip(),
            max_byte_size=max_byte_size,
            expected_sha256=expected_sha256.strip(),
            status=status,
            created_at=created_at,
            upload_expires_at=upload_expires_at,
            confirmation_deadline=confirmation_deadline,
            recovery_deadline=recovery_deadline,
            lease_token=data.get("leaseToken", data.get("lease_token")),
            lease_expires_at=lease_expires_at,
            confirm_attempts=data.get("confirmAttempts", data.get("confirm_attempts", 0)),
            staging_sha256=data.get("stagingSha256", data.get("staging_sha256")),
            staging_size=data.get("stagingSize", data.get("staging_size")),
            completed_at=completed_at,
            failure_reason=data.get("failureReason", data.get("failure_reason")),
            cleanup_ttl=data.get("cleanupTtl", data.get("cleanup_ttl")),
            source_version_id=source_version_id,
            updated_at=updated_at,
        )


@dataclass
class Complaint:
    """Core domain model representing a campus maintenance complaint."""
    id: str
    title: str
    description: str
    building: str
    floor: str
    room_number: str
    category: IssueCategory
    status: ComplaintStatus
    submitted_at: datetime
    submitted_by: str  # Verified student identifier
    overdue_threshold_days: int = 7
    resolved_at: datetime | None = None
    image_base64: str | None = None
    attachment_key: str | None = None
    attachment_status: str | None = None
    upvotes: list[str] = field(default_factory=list)
    assigned_staff: str | None = None
    resolution_remarks: str | None = None
    status_history: list[StatusHistoryEntry] = field(default_factory=list)
    feedback: ComplaintFeedback | None = None
    is_follow_up: bool = False
    original_complaint_id: str | None = None
    follow_up_reason: str | None = None
    version: int = 1  # For optimistic concurrency in DynamoDB / document stores

    @property
    def is_resolved(self) -> bool:
        """Check if complaint is in the terminal resolved state."""
        return self.status == ComplaintStatus.RESOLVED

    def to_dict(self) -> dict[str, Any]:
        """Serialize complaint to a dictionary structure.
        
        Fields mirror the TypeScript Complaint interface in src/types/index.ts.
        """
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "building": self.building,
            "floor": self.floor,
            "roomNumber": self.room_number,
            "category": self.category.value,
            "status": self.status.value,
            "submittedAt": self.submitted_at.isoformat(),
            "resolvedAt": self.resolved_at.isoformat() if self.resolved_at else None,
            "imageBase64": self.image_base64,
            "attachmentKey": self.attachment_key,
            "attachmentStatus": self.attachment_status,
            "upvotes": list(self.upvotes),
            "assignedStaff": self.assigned_staff,
            "resolutionRemarks": self.resolution_remarks,
            "submittedBy": self.submitted_by,
            "overdueThresholdDays": self.overdue_threshold_days,
            "statusHistory": [h.to_dict() for h in self.status_history],
            "feedback": self.feedback.to_dict() if self.feedback else None,
            "isFollowUp": self.is_follow_up,
            "originalComplaintId": self.original_complaint_id,
            "followUpReason": self.follow_up_reason,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Complaint:
        """Deserialize complaint from a dictionary with thorough domain validation."""
        if not isinstance(data, dict):
            raise ValidationError(f"Complaint data must be a dictionary, got {type(data).__name__}.")

        # Required identifiers and core text fields
        cid = data.get("id")
        if not cid or not isinstance(cid, str) or not cid.strip():
            raise ValidationError("Complaint 'id' must be a non-empty string.")

        title = data.get("title")
        if not title or not isinstance(title, str) or len(title.strip()) < 3:
            raise ValidationError("Complaint 'title' must be a string of at least 3 characters.")

        description = data.get("description")
        if not description or not isinstance(description, str) or len(description.strip()) < 20:
            raise ValidationError("Complaint 'description' must be a string of at least 20 characters.")

        building = data.get("building")
        if not building or not isinstance(building, str) or not building.strip():
            raise ValidationError("Complaint 'building' must be a non-empty string.")

        floor = data.get("floor")
        if not floor or not isinstance(floor, str) or not floor.strip():
            raise ValidationError("Complaint 'floor' must be a non-empty string.")

        room_number = data.get("roomNumber", data.get("room_number"))
        if not room_number or not isinstance(room_number, str) or not room_number.strip():
            raise ValidationError("Complaint 'roomNumber' must be a non-empty string.")

        submitted_by = data.get("submittedBy", data.get("submitted_by"))
        if not submitted_by or not isinstance(submitted_by, str) or not submitted_by.strip():
            raise ValidationError("Complaint 'submittedBy' must be a non-empty string.")

        # Category and status enums
        raw_category = data.get("category")
        if isinstance(raw_category, IssueCategory):
            category = raw_category
        elif isinstance(raw_category, str):
            try:
                category = IssueCategory(raw_category)
            except ValueError:
                raise ValidationError(f"Invalid IssueCategory: '{raw_category}'.")
        else:
            raise ValidationError(f"Invalid category type: {type(raw_category).__name__}.")

        raw_status = data.get("status")
        if isinstance(raw_status, ComplaintStatus):
            status = raw_status
        elif isinstance(raw_status, str):
            try:
                status = ComplaintStatus(raw_status)
            except ValueError:
                raise ValidationError(f"Invalid ComplaintStatus: '{raw_status}'.")
        else:
            raise ValidationError(f"Invalid status type: {type(raw_status).__name__}.")

        # Version validation
        raw_version = data.get("version", 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
            raise ValidationError(f"Complaint 'version' must be a positive integer >= 1. Received: {raw_version}.")

        # Overdue threshold validation
        raw_threshold = data.get("overdueThresholdDays", data.get("overdue_threshold_days", 7))
        if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, int) or raw_threshold < 1 or raw_threshold > 30:
            raise ValidationError(f"Complaint 'overdueThresholdDays' must be an integer between 1 and 30. Received: {raw_threshold}.")
        if category == IssueCategory.FIRE_SAFETY and raw_threshold != 3:
            raise ValidationError(f"Fire Safety complaints have a non-negotiable 3-day SLA. Found invalid persisted value: {raw_threshold}.")

        # Datetime fields
        submitted_at = parse_utc_iso_datetime(data.get("submittedAt"), "Complaint submittedAt")

        raw_resolved_at = data.get("resolvedAt")
        if raw_resolved_at is not None:
            resolved_at = parse_utc_iso_datetime(raw_resolved_at, "Complaint resolvedAt")
            if resolved_at < submitted_at:
                raise ValidationError(
                    f"Complaint resolvedAt ({resolved_at.isoformat()}) cannot be earlier than submittedAt ({submitted_at.isoformat()})."
                )
        else:
            resolved_at = None

        resolution_remarks = data.get("resolutionRemarks", data.get("resolution_remarks"))
        if status == ComplaintStatus.RESOLVED:
            if not resolved_at:
                raise ValidationError("Resolved complaint must have a valid 'resolvedAt' timestamp.")
            if not resolution_remarks or not isinstance(resolution_remarks, str) or not resolution_remarks.strip():
                raise ValidationError("Resolved complaint must have non-empty 'resolutionRemarks'.")
        else:
            if resolved_at is not None:
                raise ValidationError(f"Non-resolved complaint (status '{status.value}') cannot have 'resolvedAt'.")

        # Staff assignment validation
        raw_staff = data.get("assignedStaff", data.get("assigned_staff"))
        if raw_staff is not None:
            from campusfix.domain.rules import validate_staff_name
            assigned_staff = validate_staff_name(raw_staff)
        else:
            assigned_staff = None

        # Image attachment validation
        raw_image = data.get("imageBase64", data.get("image_base64"))
        if raw_image is not None:
            from campusfix.domain.rules import validate_image_base64
            image_base64 = validate_image_base64(raw_image)
        else:
            image_base64 = None

        # Feedback validation & lifecycle consistency
        feedback_data = data.get("feedback")
        feedback = ComplaintFeedback.from_dict(feedback_data) if feedback_data else None
        if feedback is not None:
            if status != ComplaintStatus.RESOLVED:
                raise ValidationError(
                    f"Complaint in status '{status.value}' cannot contain feedback. "
                    "Feedback is permitted only on Resolved complaints."
                )
            if resolved_at and feedback.submitted_at < resolved_at:
                raise ValidationError(
                    f"Feedback submittedAt ({feedback.submitted_at.isoformat()}) cannot be earlier than "
                    f"complaint resolvedAt ({resolved_at.isoformat()})."
                )

        # Status History
        raw_history = data.get("statusHistory", [])
        if not isinstance(raw_history, list):
            raise ValidationError("Complaint 'statusHistory' must be a list.")
        status_history = [
            h if isinstance(h, StatusHistoryEntry) else StatusHistoryEntry.from_dict(h)
            for h in raw_history
        ]
        # Validate chronological order and state-chain continuity
        for i in range(len(status_history) - 1):
            if status_history[i].changed_at > status_history[i + 1].changed_at:
                raise ValidationError(
                    f"Status history entries must be in chronological order. Entry {i} ({status_history[i].changed_at.isoformat()}) "
                    f"is later than entry {i+1} ({status_history[i+1].changed_at.isoformat()})."
                )
            if status_history[i].to_status != status_history[i + 1].from_status:
                raise ValidationError(
                    f"Status history is discontinuous: entry {i} ends at "
                    f"'{status_history[i].to_status.value}' but entry {i + 1} starts from "
                    f"'{status_history[i + 1].from_status.value}'. "
                    "Each entry's to_status must equal the next entry's from_status."
                )
        if status == ComplaintStatus.PENDING and len(status_history) > 0:
            raise ValidationError("Complaint in 'pending' status cannot have status transition history entries.")
        if status_history and status_history[-1].to_status != status:
            raise ValidationError(
                f"Last status history entry to_status ('{status_history[-1].to_status.value}') "
                f"does not match current complaint status ('{status.value}')."
            )

        # Follow-up fields & structural relationship validation
        raw_is_fu = data.get("isFollowUp", data.get("is_follow_up"))
        if raw_is_fu is None:
            is_follow_up = False
        elif isinstance(raw_is_fu, bool):
            is_follow_up = raw_is_fu
        else:
            raise ValidationError(f"Complaint 'isFollowUp' must be a boolean (True or False), got {type(raw_is_fu).__name__}.")

        orig_id = data.get("originalComplaintId", data.get("original_complaint_id"))
        fu_reason = data.get("followUpReason", data.get("follow_up_reason"))

        if is_follow_up:
            if not orig_id or not isinstance(orig_id, str) or not orig_id.strip():
                raise ValidationError("Follow-up complaint must specify a non-empty 'originalComplaintId'.")
            clean_orig_id = orig_id.strip()
            if clean_orig_id == cid.strip():
                raise ValidationError(f"Follow-up complaint cannot reference itself as 'originalComplaintId' ('{clean_orig_id}').")
            if not fu_reason or not isinstance(fu_reason, str) or len(fu_reason.strip()) < 10:
                raise ValidationError("Follow-up complaint must have 'followUpReason' of at least 10 characters.")
            clean_fu_reason = fu_reason.strip()
        else:
            if orig_id is not None:
                raise ValidationError(
                    f"Non-follow-up complaint cannot specify 'originalComplaintId' "
                    f"(got {orig_id!r}). Set 'isFollowUp' to True or omit the field entirely."
                )
            if fu_reason is not None:
                raise ValidationError(
                    f"Non-follow-up complaint cannot specify 'followUpReason' "
                    f"(got {fu_reason!r}). Set 'isFollowUp' to True or omit the field entirely."
                )
            clean_orig_id = None
            clean_fu_reason = None

        # Attachment fields
        att_key = data.get("attachmentKey", data.get("attachment_key"))
        if att_key is not None and (not isinstance(att_key, str) or not att_key.strip()):
            raise ValidationError("Complaint 'attachmentKey' must be a non-empty string or None.")
        clean_att_key = att_key.strip() if att_key else None

        att_status = data.get("attachmentStatus", data.get("attachment_status"))
        if att_status is not None and (not isinstance(att_status, str) or not att_status.strip()):
            raise ValidationError("Complaint 'attachmentStatus' must be a non-empty string or None.")
        clean_att_status = att_status.strip() if att_status else None

        # Upvotes
        raw_upvotes = data.get("upvotes", [])
        if not isinstance(raw_upvotes, list) or not all(isinstance(u, str) and u.strip() for u in raw_upvotes):
            raise ValidationError("Complaint 'upvotes' must be a list of non-empty student ID strings.")

        return cls(
            id=cid.strip(),
            title=title.strip(),
            description=description.strip(),
            building=building.strip(),
            floor=floor.strip(),
            room_number=room_number.strip(),
            category=category,
            status=status,
            submitted_at=submitted_at,
            submitted_by=submitted_by.strip(),
            overdue_threshold_days=raw_threshold,
            resolved_at=resolved_at,
            image_base64=image_base64,
            attachment_key=clean_att_key,
            attachment_status=clean_att_status,
            upvotes=list(raw_upvotes),
            assigned_staff=assigned_staff,
            resolution_remarks=resolution_remarks.strip() if resolution_remarks else None,
            status_history=status_history,
            feedback=feedback,
            is_follow_up=is_follow_up,
            original_complaint_id=clean_orig_id,
            follow_up_reason=clean_fu_reason,
            version=raw_version,
        )


# -----------------------------------------------------------------------------
# Stage B2: Versioned Upload Persistence Domain Records
# -----------------------------------------------------------------------------

def normalize_sha256_hex(value: str, field_name: str = "sha256") -> str:
    """Normalize a 64-char hex or 44-char base64 SHA-256 checksum to lowercase 64-char hex."""
    if not isinstance(value, str):
        raise ValidationError(f"'{field_name}' must be a string, got {type(value).__name__}.")
    cleaned = value.strip()
    if len(cleaned) == 64 and all(c in "0123456789abcdefABCDEF" for c in cleaned):
        return cleaned.lower()
    if len(cleaned) == 44:
        try:
            raw = base64.b64decode(cleaned, validate=True)
            if len(raw) == 32:
                return raw.hex().lower()
        except Exception:
            pass
    raise ValidationError(
        f"'{field_name}' must be a valid 64-char hex or 44-char base64 SHA-256 string, got {value!r}."
    )


def _validate_sha256_hex(value: str, field_name: str) -> str:
    """Validate and return a normalised 64-char lowercase hex SHA-256 string."""
    if not isinstance(value, str):
        raise ValidationError(f"'{field_name}' must be a string, got {type(value).__name__}.")
    normalised = value.strip().lower()
    if len(normalised) != 64:
        raise ValidationError(
            f"'{field_name}' must be exactly 64 hex characters, got {len(normalised)}."
        )
    if not all(c in "0123456789abcdef" for c in normalised):
        raise ValidationError(f"'{field_name}' must be a valid lowercase hex string.")
    return normalised


@dataclass
class UploadAttempt:
    """Persisted record for a single CopyObject attempt (Spec §6.1).

    Immutable attributes (set at creation, never changed):
        complaint_id, intent_id, lease_token, source_version_id,
        sha256, content_type, max_byte_size, final_key

    Mutable attributes: status, destination_version_id, updated_at.

    DynamoDB keys:
        PK = "ATTEMPT#{complaint_id}#{intent_id}"
        SK = "LEASE#{lease_token}"
    """
    complaint_id: str
    intent_id: str
    lease_token: str
    source_version_id: str
    sha256: str              # normalised hex; equals expected_sha256
    content_type: str        # equals expected_mime
    max_byte_size: int
    final_key: str
    status: UploadAttemptStatus
    created_at: datetime
    updated_at: datetime
    destination_version_id: str | None = None

    def __post_init__(self) -> None:
        for fname, fval in (
            ("complaint_id", self.complaint_id),
            ("intent_id", self.intent_id),
            ("lease_token", self.lease_token),
            ("source_version_id", self.source_version_id),
            ("content_type", self.content_type),
            ("final_key", self.final_key),
        ):
            if not isinstance(fval, str) or not fval.strip():
                raise ValidationError(f"UploadAttempt '{fname}' must be a non-empty string.")
        self.sha256 = _validate_sha256_hex(self.sha256, "sha256")
        if isinstance(self.max_byte_size, bool) or not isinstance(self.max_byte_size, int) or self.max_byte_size < 0:
            raise ValidationError(
                f"UploadAttempt 'max_byte_size' must be a non-negative integer, got {self.max_byte_size!r}."
            )
        if not isinstance(self.status, UploadAttemptStatus):
            raise ValidationError(
                f"UploadAttempt 'status' must be UploadAttemptStatus enum, got {self.status!r}."
            )
        if self.source_version_id.strip() == "null":
            raise ValidationError("UploadAttempt 'source_version_id' cannot be 'null'.")
        if self.destination_version_id is not None:
            if not isinstance(self.destination_version_id, str) or self.destination_version_id.strip() in ("", "null"):
                raise ValidationError(
                    f"UploadAttempt 'destination_version_id' must be a non-empty valid VersionId or None, "
                    f"got {self.destination_version_id!r}."
                )
        self.created_at = parse_utc_iso_datetime(self.created_at, "created_at")
        self.updated_at = parse_utc_iso_datetime(self.updated_at, "updated_at")

    @staticmethod
    def attempt_pk(complaint_id: str, intent_id: str) -> str:
        """DynamoDB PK for this record."""
        return f"ATTEMPT#{complaint_id}#{intent_id}"

    @staticmethod
    def attempt_sk(lease_token: str) -> str:
        """DynamoDB SK for this record."""
        return f"LEASE#{lease_token}"


@dataclass
class TagOutboxRecord:
    """Outbox record queued after a successful atomic commit for tag promotion (Spec §11, §7 Action 4).

    DynamoDB keys:
        PK = "OUTBOX#TAG_PROMOTION"
        SK = "INTENT#{intent_id}"
    """
    intent_id: str
    complaint_id: str
    s3_key: str              # = final_key at commit time
    destination_version_id: str
    committed_at: datetime
    sweep_attempts: int = 0

    def __post_init__(self) -> None:
        for fname, fval in (
            ("intent_id", self.intent_id),
            ("complaint_id", self.complaint_id),
            ("s3_key", self.s3_key),
            ("destination_version_id", self.destination_version_id),
        ):
            if not isinstance(fval, str) or not fval.strip():
                raise ValidationError(f"TagOutboxRecord '{fname}' must be a non-empty string.")
        if isinstance(self.sweep_attempts, bool) or not isinstance(self.sweep_attempts, int) or self.sweep_attempts < 0:
            raise ValidationError(
                f"TagOutboxRecord 'sweep_attempts' must be a non-negative integer, got {self.sweep_attempts!r}."
            )
        if self.destination_version_id.strip() == "null":
            raise ValidationError("TagOutboxRecord 'destination_version_id' cannot be 'null'.")
        self.committed_at = parse_utc_iso_datetime(self.committed_at, "committed_at")

    @staticmethod
    def outbox_pk() -> str:
        """DynamoDB PK for all tag-promotion outbox records."""
        return "OUTBOX#TAG_PROMOTION"

    @staticmethod
    def outbox_sk(intent_id: str) -> str:
        """DynamoDB SK for a specific outbox record."""
        return f"INTENT#{intent_id}"


@dataclass(frozen=True)
class AtomicAttachmentCommitResult:
    """Return value of ``commit_verified_attachment_versioned`` — committed state copies.

    All returned objects are deep-copied snapshots of the state *after* the
    atomic commit.  ``outbox`` may be ``None`` on an idempotent replay after
    the tag-promotion sweeper has already processed and deleted the outbox item.
    """
    complaint: Complaint
    intent: UploadIntent
    attempt: UploadAttempt
    outbox: TagOutboxRecord | None = None
