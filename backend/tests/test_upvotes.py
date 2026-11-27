"""Unit tests for idempotent upvote operations (PUT/DELETE) in MAIT CampusFix."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest

from campusfix.domain.models import (
    Actor,
    Complaint,
    ComplaintStatus,
    IssueCategory,
    Role,
)
from campusfix.domain.rules import ComplaintNotFoundError, UnauthorizedError
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.complaints import ComplaintService


class TestIdempotentUpvotes(unittest.TestCase):
    """Test suite for Option B idempotent upvote semantics."""

    def setUp(self) -> None:
        self.repository = InMemoryComplaintRepository()
        self.service = ComplaintService(self.repository)
        self.owner = Actor(user_id="student-owner-001", role=Role.STUDENT)
        self.voter_1 = Actor(user_id="student-voter-001", role=Role.STUDENT)
        self.voter_2 = Actor(user_id="student-voter-002", role=Role.STUDENT)
        self.staff = Actor(user_id="staff-001", role=Role.MAINTENANCE)
        self.admin = Actor(user_id="admin-001", role=Role.ADMIN)

        # Seed initial complaint
        self.complaint = Complaint(
            id="CF-2026-TEST01",
            title="Leaking pipe in lab",
            description="Continuous water leakage under sink in Room 204.",
            building="Block A",
            floor="2nd Floor",
            room_number="204",
            category=IssueCategory.PLUMBING,
            status=ComplaintStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
            submitted_by=self.owner.user_id,
            upvotes=[],
            version=1,
        )
        self.repository.seed(self.complaint)

    def test_add_upvote_success(self) -> None:
        """Adding an upvote for the first time registers the vote and increments version."""
        complaint, changed = self.service.add_upvote(self.complaint.id, self.voter_1)
        self.assertTrue(changed)
        self.assertIn(self.voter_1.user_id, complaint.upvotes)
        self.assertEqual(len(complaint.upvotes), 1)
        self.assertEqual(complaint.version, 2)

    def test_add_upvote_idempotency_repeated(self) -> None:
        """Repeated PUT upvotes return success without incrementing version or duplicating votes."""
        complaint1, changed1 = self.service.add_upvote(self.complaint.id, self.voter_1)
        self.assertTrue(changed1)
        self.assertEqual(complaint1.version, 2)

        # Second PUT call from same student
        complaint2, changed2 = self.service.add_upvote(self.complaint.id, self.voter_1)
        self.assertFalse(changed2)
        self.assertEqual(complaint2.version, 2)  # Version must NOT inflate
        self.assertEqual(len(complaint2.upvotes), 1)
        self.assertEqual(complaint2.upvotes, [self.voter_1.user_id])

        # Third PUT call
        complaint3, changed3 = self.service.add_upvote(self.complaint.id, self.voter_1)
        self.assertFalse(changed3)
        self.assertEqual(complaint3.version, 2)
        self.assertEqual(len(complaint3.upvotes), 1)

    def test_remove_upvote_success(self) -> None:
        """Removing an existing upvote withdraws the vote and increments version."""
        self.service.add_upvote(self.complaint.id, self.voter_1)
        complaint, changed = self.service.remove_upvote(self.complaint.id, self.voter_1)
        self.assertTrue(changed)
        self.assertNotIn(self.voter_1.user_id, complaint.upvotes)
        self.assertEqual(len(complaint.upvotes), 0)
        self.assertEqual(complaint.version, 3)

    def test_remove_upvote_idempotency_repeated(self) -> None:
        """Repeated DELETE calls on a non-upvoted complaint return without changing state or version."""
        # Voter 1 has not upvoted
        complaint1, changed1 = self.service.remove_upvote(self.complaint.id, self.voter_1)
        self.assertFalse(changed1)
        self.assertEqual(complaint1.version, 1)  # Version must NOT inflate
        self.assertEqual(len(complaint1.upvotes), 0)

        # Upvote then remove twice
        self.service.add_upvote(self.complaint.id, self.voter_1)
        complaint2, changed2 = self.service.remove_upvote(self.complaint.id, self.voter_1)
        self.assertTrue(changed2)
        self.assertEqual(complaint2.version, 3)

        complaint3, changed3 = self.service.remove_upvote(self.complaint.id, self.voter_1)
        self.assertFalse(changed3)
        self.assertEqual(complaint3.version, 3)

    def test_upvote_role_restriction_staff_and_admin_rejected(self) -> None:
        """Maintenance and Admin actors are strictly prohibited from upvoting complaints."""
        with self.assertRaises(UnauthorizedError):
            self.service.add_upvote(self.complaint.id, self.staff)

        with self.assertRaises(UnauthorizedError):
            self.service.add_upvote(self.complaint.id, self.admin)

        with self.assertRaises(UnauthorizedError):
            self.service.remove_upvote(self.complaint.id, self.staff)

        with self.assertRaises(UnauthorizedError):
            self.service.remove_upvote(self.complaint.id, self.admin)

    def test_upvote_nonexistent_complaint_raises_not_found(self) -> None:
        """Upvoting a non-existent complaint raises ComplaintNotFoundError."""
        with self.assertRaises(ComplaintNotFoundError):
            self.service.add_upvote("CF-2026-NONEXISTENT", self.voter_1)

        with self.assertRaises(ComplaintNotFoundError):
            self.service.remove_upvote("CF-2026-NONEXISTENT", self.voter_1)

    def test_concurrent_upvotes(self) -> None:
        """Concurrent upvotes from 20 distinct students are recorded thread-safely."""
        student_count = 20
        voters = [Actor(user_id=f"student-{i:03d}", role=Role.STUDENT) for i in range(student_count)]

        def do_upvote(v: Actor) -> None:
            self.service.add_upvote(self.complaint.id, v)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(do_upvote, voters))

        updated = self.repository.get(self.complaint.id)
        self.assertIsNotNone(updated)
        self.assertEqual(len(updated.upvotes), student_count)
        self.assertEqual(updated.version, 1 + student_count)


if __name__ == "__main__":
    unittest.main()
