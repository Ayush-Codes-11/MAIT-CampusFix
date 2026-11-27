"""Service integration tests for MAIT CampusFix."""

from datetime import datetime, timezone
import unittest

from campusfix.domain.models import (
    Actor,
    Complaint,
    ComplaintStatus,
    IssueCategory,
)
from campusfix.domain.rules import (
    ActiveFollowUpExistsError,
    FeedbackNotAllowedError,
    FollowUpEligibilityError,
    InvalidFeedbackRatingError,
    InvalidFollowUpReasonError,
    InvalidStatusTransitionError,
    MissingResolutionRemarksError,
    TerminalStatusError,
)
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.complaints import ComplaintNotFoundError, ComplaintService
from campusfix.services.id_generator import SequentialYearIdGenerator


class TestComplaintService(unittest.TestCase):
    """End-to-end integration tests using in-memory repository and service layer."""

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.id_gen = SequentialYearIdGenerator(starting_sequence=1)
        self.service = ComplaintService(repository=self.repo, id_generator=self.id_gen)

        # Standard Actors for testing
        self.student1 = Actor.student("stu-001")
        self.student2 = Actor.student("stu-002")
        self.staff_ramesh = Actor.maintenance("staff-ramesh")
        self.admin1 = Actor.admin("admin-1")
        self.tech = Actor.maintenance("tech")
        self.plumber = Actor.maintenance("plumber")

    # -------------------------------------------------------------------------
    # Creation Tests
    # -------------------------------------------------------------------------

    def test_create_standard_complaint(self):
        """Create standard complaint: status PENDING, overdue threshold 7 days."""
        complaint = self.service.create_complaint(
            title="Broken tap in Boys Washroom",
            description="The tap near the entrance is continuously leaking water on the floor.",
            building="Block A",
            floor="1st Floor",
            room_number="Washroom (Gents)",
            category=IssueCategory.PLUMBING,
            actor=self.student1,
        )

        self.assertEqual(complaint.id, f"CF-{datetime.now(timezone.utc).year}-0001")
        self.assertEqual(complaint.status, ComplaintStatus.PENDING)
        self.assertEqual(complaint.overdue_threshold_days, 7)
        self.assertEqual(len(complaint.upvotes), 0)
        self.assertEqual(len(complaint.status_history), 0)
        self.assertIsNone(complaint.resolved_at)
        self.assertFalse(complaint.is_follow_up)

        # Verify persisted in repository
        persisted = self.service.get_complaint(complaint.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.title, complaint.title)

    def test_create_fire_safety_complaint_has_3_day_overdue_threshold(self):
        """Fire Safety complaints must default to 3 days overdue threshold."""
        complaint = self.service.create_complaint(
            title="Empty fire extinguisher mount",
            description="The fire extinguisher near the workshop lathe machine is completely missing.",
            building="Workshop Block",
            floor="Ground Floor",
            room_number="Lathe Area",
            category=IssueCategory.FIRE_SAFETY,
            actor=self.student2,
        )
        self.assertEqual(complaint.overdue_threshold_days, 3)

    def test_create_complaint_validations(self):
        """Short descriptions and empty titles must raise ValueError."""
        with self.assertRaises(ValueError):
            self.service.create_complaint(
                title="",
                description="Valid description with enough characters.",
                building="Block A",
                floor="1st",
                room_number="101",
                category=IssueCategory.ELECTRICAL,
                actor=self.student1,
            )

        with self.assertRaises(ValueError):
            self.service.create_complaint(
                title="Short desc test",
                description="Too short",
                building="Block A",
                floor="1st",
                room_number="101",
                category=IssueCategory.ELECTRICAL,
                actor=self.student1,
            )

    # -------------------------------------------------------------------------
    # Status Transition Tests
    # -------------------------------------------------------------------------

    def test_full_lifecycle_forward_only(self):
        """Test forward lifecycle: PENDING -> IN_PROGRESS -> RESOLVED."""
        complaint = self.service.create_complaint(
            title="Flickering lights in Room 204",
            description="The overhead light fixture has been flickering intermittently all week.",
            building="Block C (Labs)",
            floor="2nd Floor",
            room_number="C-204",
            category=IssueCategory.ELECTRICAL,
            actor=self.student1,
        )

        # 1. PENDING -> IN_PROGRESS
        c_prog = self.service.transition_status(
            complaint_id=complaint.id,
            to_status=ComplaintStatus.IN_PROGRESS,
            actor=self.staff_ramesh,
            remarks="Electrician dispatched to check ballast.",
            assigned_staff="Ramesh Kumar",
        )
        self.assertEqual(c_prog.status, ComplaintStatus.IN_PROGRESS)
        self.assertEqual(c_prog.assigned_staff, "Ramesh Kumar")
        self.assertEqual(len(c_prog.status_history), 1)
        self.assertEqual(c_prog.status_history[0].from_status, ComplaintStatus.PENDING)
        self.assertEqual(c_prog.status_history[0].to_status, ComplaintStatus.IN_PROGRESS)
        self.assertEqual(c_prog.status_history[0].changed_by, "staff-ramesh")

        # 2. IN_PROGRESS -> RESOLVED
        c_res = self.service.transition_status(
            complaint_id=complaint.id,
            to_status=ComplaintStatus.RESOLVED,
            actor=self.staff_ramesh,
            remarks="Replaced faulty ballast and bulbs. Work complete.",
        )
        self.assertEqual(c_res.status, ComplaintStatus.RESOLVED)
        self.assertIsNotNone(c_res.resolved_at)
        self.assertEqual(c_res.resolution_remarks, "Replaced faulty ballast and bulbs. Work complete.")
        self.assertEqual(len(c_res.status_history), 2)
        self.assertEqual(c_res.status_history[1].from_status, ComplaintStatus.IN_PROGRESS)
        self.assertEqual(c_res.status_history[1].to_status, ComplaintStatus.RESOLVED)

    def test_transition_errors(self):
        """Verify transitions reject stage-skips, missing remarks, and terminal modifications."""
        complaint = self.service.create_complaint(
            title="Projector issue in MB-101",
            description="Projector HDMI signal is unstable during class presentations.",
            building="Main Block (MB)",
            floor="Ground Floor",
            room_number="MB-101",
            category=IssueCategory.ELECTRICAL,
            actor=self.student1,
        )

        # Stage skipping (PENDING -> RESOLVED) forbidden
        with self.assertRaises(InvalidStatusTransitionError):
            self.service.transition_status(
                complaint.id,
                to_status=ComplaintStatus.RESOLVED,
                actor=self.admin1,
                remarks="Trying to jump directly",
            )

        # Move to IN_PROGRESS
        self.service.transition_status(
            complaint.id,
            to_status=ComplaintStatus.IN_PROGRESS,
            actor=self.admin1,
        )

        # Missing remarks when moving to RESOLVED
        with self.assertRaises(MissingResolutionRemarksError):
            self.service.transition_status(
                complaint.id,
                to_status=ComplaintStatus.RESOLVED,
                actor=self.admin1,
                remarks="   ",
            )

        # Resolve successfully
        self.service.transition_status(
            complaint.id,
            to_status=ComplaintStatus.RESOLVED,
            actor=self.admin1,
            remarks="Cable replaced and tested successfully.",
        )

        # Reopening terminal resolved state is strictly forbidden
        with self.assertRaises(TerminalStatusError):
            self.service.transition_status(
                complaint.id,
                to_status=ComplaintStatus.IN_PROGRESS,
                actor=self.admin1,
            )

        with self.assertRaises(TerminalStatusError):
            self.service.transition_status(
                complaint.id,
                to_status=ComplaintStatus.PENDING,
                actor=self.admin1,
            )

    def test_nonexistent_complaint_raises_not_found(self):
        """Targeting non-existent complaint raises ComplaintNotFoundError."""
        with self.assertRaises(ComplaintNotFoundError):
            self.service.transition_status(
                "CF-9999-9999",
                to_status=ComplaintStatus.IN_PROGRESS,
                actor=self.admin1,
            )

    # -------------------------------------------------------------------------
    # Feedback Tests
    # -------------------------------------------------------------------------

    def test_feedback_lifecycle(self):
        """Feedback on resolved complaint succeeds and supports editing."""
        complaint = self.service.create_complaint(
            title="Water cooler not cooling",
            description="Canteen drinking water cooler is dispensing warm water continuously.",
            building="Canteen Block",
            floor="Ground Floor",
            room_number="Main Area",
            category=IssueCategory.ELECTRICAL,
            actor=self.student1,
        )

        # Feedback before resolution must fail
        with self.assertRaises(FeedbackNotAllowedError):
            self.service.submit_feedback(complaint.id, rating=5, actor=self.student1)

        # Resolve complaint
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.tech)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.tech, remarks="Compressor gas refilled."
        )

        # Initial feedback submission
        c1 = self.service.submit_feedback(
            complaint.id, rating=5, actor=self.student1, comment="Ice cold water now, thanks!"
        )
        self.assertIsNotNone(c1.feedback)
        self.assertEqual(c1.feedback.rating, 5)
        self.assertEqual(c1.feedback.comment, "Ice cold water now, thanks!")
        self.assertIsNone(c1.feedback.updated_at)
        initial_sub_at = c1.feedback.submitted_at

        # Edit feedback
        c2 = self.service.submit_feedback(
            complaint.id, rating=4, actor=self.student1, comment="Still good, slight noise though."
        )
        self.assertEqual(c2.feedback.rating, 4)
        self.assertEqual(c2.feedback.comment, "Still good, slight noise though.")
        self.assertEqual(c2.feedback.submitted_at, initial_sub_at)
        self.assertIsNotNone(c2.feedback.updated_at)

    def test_feedback_invalid_ratings(self):
        """Rating outside 1-5 raises InvalidFeedbackRatingError."""
        complaint = self.service.create_complaint(
            title="Broken desk in room 301",
            description="The wooden top of desk 12 in room 301 is cracked and unstable.",
            building="Block B",
            floor="3rd Floor",
            room_number="B-301",
            category=IssueCategory.FURNITURE,
            actor=self.student1,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.tech)
        self.service.transition_status(complaint.id, ComplaintStatus.RESOLVED, actor=self.tech, remarks="Replaced top.")

        for bad_rating in [0, 6, -1, 10]:
            with self.subTest(rating=bad_rating):
                with self.assertRaises(InvalidFeedbackRatingError):
                    self.service.submit_feedback(complaint.id, rating=bad_rating, actor=self.student1)

    # -------------------------------------------------------------------------
    # Follow-Up Tests
    # -------------------------------------------------------------------------

    def test_follow_up_creation_and_isolation(self):
        """Follow-up inherits location/category, starts PENDING, and keeps parent RESOLVED."""
        parent = self.service.create_complaint(
            title="Wi-Fi dead zone on 2nd Floor",
            description="The reference section in the library has no Wi-Fi coverage for laptops.",
            building="Library Block",
            floor="2nd Floor",
            room_number="Reference Section",
            category=IssueCategory.IT_NETWORK,
            actor=self.student1,
        )
        self.service.transition_status(parent.id, ComplaintStatus.IN_PROGRESS, actor=self.admin1)
        self.service.transition_status(
            parent.id, ComplaintStatus.RESOLVED, actor=self.admin1, remarks="Rebooted AP 204."
        )

        # Create follow-up
        follow_up = self.service.create_follow_up(
            original_complaint_id=parent.id,
            follow_up_reason="The AP stopped responding again today after 2 hours of use.",
            actor=self.student1,
        )

        # Follow-up attributes
        self.assertTrue(follow_up.is_follow_up)
        self.assertEqual(follow_up.original_complaint_id, parent.id)
        self.assertEqual(follow_up.status, ComplaintStatus.PENDING)
        self.assertEqual(follow_up.building, parent.building)
        self.assertEqual(follow_up.category, parent.category)
        self.assertEqual(len(follow_up.upvotes), 0)
        self.assertIsNone(follow_up.feedback)
        self.assertIn("The AP stopped responding again", follow_up.follow_up_reason)

        # Parent ticket remains permanently RESOLVED
        parent_fresh = self.service.get_complaint(parent.id)
        self.assertEqual(parent_fresh.status, ComplaintStatus.RESOLVED)

        # Parent shows linked follow-up
        linked = self.service.get_linked_follow_ups(parent.id)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0].id, follow_up.id)

    def test_duplicate_active_follow_up_safeguard(self):
        """Prevent raising multiple concurrent active follow-ups for the same parent."""
        parent = self.service.create_complaint(
            title="Water pipe leakage",
            description="Major leak in the basement plumbing line causing floor pooling.",
            building="Main Block (MB)",
            floor="Ground Floor",
            room_number="Basement",
            category=IssueCategory.PLUMBING,
            actor=self.student1,
        )
        self.service.transition_status(parent.id, ComplaintStatus.IN_PROGRESS, actor=self.plumber)
        self.service.transition_status(parent.id, ComplaintStatus.RESOLVED, actor=self.plumber, remarks="Fitted clamp.")

        # First follow-up
        f1 = self.service.create_follow_up(
            original_complaint_id=parent.id,
            follow_up_reason="The clamp leaked again when water supply resumed.",
            actor=self.student1,
        )

        # Attempt second follow-up while f1 is still PENDING -> must raise ActiveFollowUpExistsError
        with self.assertRaises(ActiveFollowUpExistsError):
            self.service.create_follow_up(
                original_complaint_id=parent.id,
                follow_up_reason="Attempting second follow up while first is open",
                actor=self.student1,
            )

        # Advance f1 to IN_PROGRESS -> still active, so second follow-up still blocked
        self.service.transition_status(f1.id, ComplaintStatus.IN_PROGRESS, actor=self.plumber)
        with self.assertRaises(ActiveFollowUpExistsError):
            self.service.create_follow_up(
                original_complaint_id=parent.id,
                follow_up_reason="Attempting second follow up while first is in-progress",
                actor=self.student1,
            )

        # Resolve f1
        self.service.transition_status(
            f1.id, ComplaintStatus.RESOLVED, actor=self.plumber, remarks="Replaced pipe segment."
        )

        # Now that f1 is RESOLVED, a subsequent follow-up is permitted
        f2 = self.service.create_follow_up(
            original_complaint_id=parent.id,
            follow_up_reason="New leak appeared downstream after pipe replacement.",
            actor=self.student1,
        )
        self.assertEqual(f2.status, ComplaintStatus.PENDING)
        self.assertEqual(len(self.service.get_linked_follow_ups(parent.id)), 2)

    # -------------------------------------------------------------------------
    # Upvotes Tests
    # -------------------------------------------------------------------------

    def test_upvote_toggle(self):
        """Upvote adds student on first call and removes on second call."""
        complaint = self.service.create_complaint(
            title="Ceiling fan wobbling",
            description="The ceiling fan in Room 102 is shaking violently and making noise.",
            building="Block B",
            floor="1st Floor",
            room_number="B-102",
            category=IssueCategory.ELECTRICAL,
            actor=self.student1,
        )

        # Add upvote
        c, added = self.service.toggle_upvote(complaint.id, actor=self.student1)
        self.assertTrue(added)
        self.assertIn("stu-001", c.upvotes)

        # Another student upvotes
        c, added = self.service.toggle_upvote(complaint.id, actor=self.student2)
        self.assertTrue(added)
        self.assertEqual(c.upvotes, ["stu-001", "stu-002"])

        # First student removes upvote
        c, added = self.service.toggle_upvote(complaint.id, actor=self.student1)
        self.assertFalse(added)
        self.assertEqual(c.upvotes, ["stu-002"])

    # -------------------------------------------------------------------------
    # Model Serialization Round-trip
    # -------------------------------------------------------------------------

    def test_model_dict_serialization_round_trip(self):
        """Complaint to_dict and from_dict preserve all properties accurately."""
        complaint = self.service.create_complaint(
            title="Air conditioning failure",
            description="Server room split AC has completely stopped cooling the servers.",
            building="Block A",
            floor="2nd Floor",
            room_number="A-205",
            category=IssueCategory.ELECTRICAL,
            actor=self.student1,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.tech)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.tech, remarks="Replaced PCB."
        )
        self.service.submit_feedback(complaint.id, rating=5, actor=self.student1, comment="Super cool now!")

        fresh = self.service.get_complaint(complaint.id)
        as_dict = fresh.to_dict()

        restored = Complaint.from_dict(as_dict)
        self.assertEqual(restored.id, fresh.id)
        self.assertEqual(restored.title, fresh.title)
        self.assertEqual(restored.status, fresh.status)
        self.assertEqual(restored.resolution_remarks, fresh.resolution_remarks)
        self.assertEqual(len(restored.status_history), 2)
        self.assertIsNotNone(restored.feedback)
        self.assertEqual(restored.feedback.rating, 5)


if __name__ == "__main__":
    unittest.main()
