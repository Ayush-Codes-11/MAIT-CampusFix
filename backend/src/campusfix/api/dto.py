"""Data Transfer Objects and privacy serialization for MAIT CampusFix API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from campusfix.domain.models import Actor, Complaint, ComplaintStatus


def complaint_to_dto(complaint: Complaint, actor: Actor) -> dict[str, Any]:
    """Serialize a Complaint domain aggregate to a camelCase frontend DTO with strict privacy rules.
    
    Privacy Rules:
    - submittedBy UUID is exposed ONLY to staff (Maintenance/Admin) or the ticket owner.
      For other students viewing the shared board, submittedBy is None (masked).
    - upvotedBy collection of student IDs is strictly stripped to prevent voter identity tracking.
      Instead, an aggregate upvotes count and a caller-specific hasUpvoted boolean are returned.
    - isOwner boolean is computed relative to the authenticated actor.
    - isOverdue boolean is derived dynamically against the category SLA threshold.
    """
    now = datetime.now(timezone.utc)
    is_owner = complaint.submitted_by == actor.user_id
    is_staff = actor.is_staff

    # PII Protection: Mask submitter identifier for non-owner students
    visible_submitted_by = complaint.submitted_by if (is_owner or is_staff) else None

    # Calculate SLA overdue status
    is_overdue = False
    if complaint.status != ComplaintStatus.RESOLVED:
        age_seconds = (now - complaint.submitted_at).total_seconds()
        is_overdue = age_seconds > (complaint.overdue_threshold_days * 86400)

    has_upvoted = actor.user_id in complaint.upvotes

    return {
        "id": complaint.id,
        "title": complaint.title,
        "description": complaint.description,
        "building": complaint.building,
        "floor": complaint.floor,
        "roomNumber": complaint.room_number,
        "category": complaint.category.value,
        "status": complaint.status.value,
        "submittedAt": complaint.submitted_at.isoformat(),
        "resolvedAt": complaint.resolved_at.isoformat() if complaint.resolved_at else None,
        "submittedBy": visible_submitted_by,
        "isOwner": is_owner,
        "overdueThresholdDays": complaint.overdue_threshold_days,
        "isOverdue": is_overdue,
        "upvotes": len(complaint.upvotes),
        "hasUpvoted": has_upvoted,
        "assignedStaff": complaint.assigned_staff,
        "resolutionRemarks": complaint.resolution_remarks,
        "attachmentKey": complaint.attachment_key,
        "attachmentStatus": complaint.attachment_status,
        "statusHistory": [h.to_dict() for h in complaint.status_history],
        "feedback": complaint.feedback.to_dict() if complaint.feedback else None,
        "isFollowUp": complaint.is_follow_up,
        "originalComplaintId": complaint.original_complaint_id,
        "followUpReason": complaint.follow_up_reason,
        "version": complaint.version,
    }
