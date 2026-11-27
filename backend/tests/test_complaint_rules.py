"""Unit tests for pure domain rules of MAIT CampusFix."""

from datetime import datetime, timezone
import unittest

from campusfix.domain.models import (
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
    can_transition,
    toggle_upvote_on_complaint,
    validate_feedback,
    validate_follow_up_eligibility,
    validate_status_transition,
)


def make_test_complaint(
    status: ComplaintStatus = ComplaintStatus.PENDING,
    category: IssueCategory = IssueCategory.ELECTRICAL,
    submitted_by: str = "stu-001",
) -> Complaint:
    """Helper factory for creating complaints in unit tests."""
    return Complaint(
        id="CF-2026-0001",
        title="Flickering lights in Lab C-204",
        description="The fluorescent lights have been flickering for a week.",
        building="Block C (Labs)",
        floor="2nd Floor",
        room_number="C-204",
        category=category,
        status=status,
        submitted_at=datetime.now(timezone.utc),
        submitted_by=submitted_by,
        overdue_threshold_days=7,
        upvotes=[],
        status_history=[],
    )


class TestWorkflowStatusTransitions(unittest.TestCase):
    """Tests strictly forward-only status progression rules."""

    def test_can_transition_matrix(self):
        """Verify the structural transition matrix."""
        # Allowed transitions
        self.assertTrue(can_transition(ComplaintStatus.PENDING, ComplaintStatus.IN_PROGRESS))
        self.assertTrue(can_transition(ComplaintStatus.IN_PROGRESS, ComplaintStatus.RESOLVED))

        # Forbidden transitions
        self.assertFalse(can_transition(ComplaintStatus.PENDING, ComplaintStatus.RESOLVED))
        self.assertFalse(can_transition(ComplaintStatus.IN_PROGRESS, ComplaintStatus.PENDING))
        self.assertFalse(can_transition(ComplaintStatus.RESOLVED, ComplaintStatus.PENDING))
        self.assertFalse(can_transition(ComplaintStatus.RESOLVED, ComplaintStatus.IN_PROGRESS))
        self.assertFalse(can_transition(ComplaintStatus.PENDING, ComplaintStatus.PENDING))

    def test_valid_forward_transitions(self):
        """Pending -> In Progress, and In Progress -> Resolved (with remarks) must succeed."""
        complaint = make_test_complaint(status=ComplaintStatus.PENDING)

        # Pending -> In Progress
        validate_status_transition(complaint, ComplaintStatus.IN_PROGRESS)

        # In Progress -> Resolved with remarks
        complaint.status = ComplaintStatus.IN_PROGRESS
        validate_status_transition(
            complaint, ComplaintStatus.RESOLVED, remarks="Wiring re-soldered and tested."
        )

    def test_stage_skipping_is_forbidden(self):
        """Pending directly to Resolved must raise InvalidStatusTransitionError."""
        complaint = make_test_complaint(status=ComplaintStatus.PENDING)

        with self.assertRaises(InvalidStatusTransitionError) as ctx:
            validate_status_transition(
                complaint, ComplaintStatus.RESOLVED, remarks="Trying to skip straight to resolved"
            )
        self.assertIn("Skipping stages is forbidden", str(ctx.exception))

    def test_backward_transition_is_forbidden(self):
        """In Progress back to Pending must raise InvalidStatusTransitionError."""
        complaint = make_test_complaint(status=ComplaintStatus.IN_PROGRESS)

        with self.assertRaises(InvalidStatusTransitionError) as ctx:
            validate_status_transition(complaint, ComplaintStatus.PENDING)
        self.assertIn("Reverting to Pending is forbidden", str(ctx.exception))

    def test_resolved_is_terminal_state(self):
        """Resolved complaints cannot transition to any other status."""
        complaint = make_test_complaint(status=ComplaintStatus.RESOLVED)

        with self.assertRaises(TerminalStatusError):
            validate_status_transition(complaint, ComplaintStatus.PENDING)

        with self.assertRaises(TerminalStatusError):
            validate_status_transition(complaint, ComplaintStatus.IN_PROGRESS)

        with self.assertRaises(TerminalStatusError):
            validate_status_transition(complaint, ComplaintStatus.RESOLVED)

    def test_missing_resolution_remarks_raises_error(self):
        """Transition to Resolved without non-empty remarks must be rejected."""
        complaint = make_test_complaint(status=ComplaintStatus.IN_PROGRESS)

        for empty_val in [None, "", "   ", "\n\t"]:
            with self.subTest(empty_val=empty_val):
                with self.assertRaises(MissingResolutionRemarksError):
                    validate_status_transition(
                        complaint, ComplaintStatus.RESOLVED, remarks=empty_val
                    )


class TestFeedbackRules(unittest.TestCase):
    """Tests student feedback validation and eligibility."""

    def test_feedback_not_allowed_on_unresolved_complaint(self):
        """Feedback on Pending or In-Progress complaints must raise FeedbackNotAllowedError."""
        for status in [ComplaintStatus.PENDING, ComplaintStatus.IN_PROGRESS]:
            with self.subTest(status=status):
                complaint = make_test_complaint(status=status)
                with self.assertRaises(FeedbackNotAllowedError):
                    validate_feedback(complaint, rating=5, comment="Great work!")

    def test_feedback_rating_bounds(self):
        """Feedback rating must be an integer between 1 and 5."""
        complaint = make_test_complaint(status=ComplaintStatus.RESOLVED)

        # Valid ratings: 1, 2, 3, 4, 5
        for valid_rating in [1, 2, 3, 4, 5]:
            with self.subTest(rating=valid_rating):
                validate_feedback(complaint, rating=valid_rating)

        # Invalid ratings: 0, 6, -1, non-integers
        for invalid_rating in [0, 6, -1, 100, 3.5]:  # type: ignore
            with self.subTest(rating=invalid_rating):
                with self.assertRaises(InvalidFeedbackRatingError):
                    validate_feedback(complaint, rating=invalid_rating)


class TestFollowUpRules(unittest.TestCase):
    """Tests follow-up eligibility, explanation length, and duplicate safeguards."""

    def test_follow_up_requires_resolved_parent(self):
        """Follow-up complaints can only be created against Resolved parent tickets."""
        for status in [ComplaintStatus.PENDING, ComplaintStatus.IN_PROGRESS]:
            with self.subTest(status=status):
                parent = make_test_complaint(status=status)
                with self.assertRaises(FollowUpEligibilityError):
                    validate_follow_up_eligibility(
                        parent_complaint=parent,
                        follow_up_reason="The issue recurred after 2 days",
                        has_active_follow_up=False,
                    )

    def test_follow_up_minimum_explanation_length(self):
        """Follow-up explanation must have at least 10 non-whitespace characters."""
        parent = make_test_complaint(status=ComplaintStatus.RESOLVED)

        # Too short
        for short_reason in ["", "   ", "still bad", "123456789"]:
            with self.subTest(reason=short_reason):
                with self.assertRaises(InvalidFollowUpReasonError):
                    validate_follow_up_eligibility(
                        parent_complaint=parent,
                        follow_up_reason=short_reason,
                        has_active_follow_up=False,
                    )

        # Valid (>= 10 chars)
        validate_follow_up_eligibility(
            parent_complaint=parent,
            follow_up_reason="The light started flickering again this morning.",
            has_active_follow_up=False,
        )

    def test_duplicate_active_follow_up_safeguard(self):
        """A new follow-up cannot be submitted if an active one is already open."""
        parent = make_test_complaint(status=ComplaintStatus.RESOLVED)

        with self.assertRaises(ActiveFollowUpExistsError):
            validate_follow_up_eligibility(
                parent_complaint=parent,
                follow_up_reason="The light is flickering again.",
                has_active_follow_up=True,
            )


class TestUpvoteRules(unittest.TestCase):
    """Tests upvote uniqueness and toggle behavior."""

    def test_add_and_remove_upvote(self):
        complaint = make_test_complaint()
        student = "stu-001"

        # Initially 0 upvotes
        self.assertEqual(len(complaint.upvotes), 0)

        # First toggle: added
        added = toggle_upvote_on_complaint(complaint, student)
        self.assertTrue(added)
        self.assertIn(student, complaint.upvotes)
        self.assertEqual(len(complaint.upvotes), 1)

        # Second toggle: removed
        added = toggle_upvote_on_complaint(complaint, student)
        self.assertFalse(added)
        self.assertNotIn(student, complaint.upvotes)
        self.assertEqual(len(complaint.upvotes), 0)

    def test_multiple_distinct_students_upvotes(self):
        complaint = make_test_complaint()

        toggle_upvote_on_complaint(complaint, "stu-001")
        toggle_upvote_on_complaint(complaint, "stu-002")
        toggle_upvote_on_complaint(complaint, "stu-003")

        self.assertEqual(complaint.upvotes, ["stu-001", "stu-002", "stu-003"])

        # Remove stu-002 only
        toggle_upvote_on_complaint(complaint, "stu-002")
        self.assertEqual(complaint.upvotes, ["stu-001", "stu-003"])

    def test_empty_student_id_raises_value_error(self):
        complaint = make_test_complaint()
        with self.assertRaises(ValueError):
            toggle_upvote_on_complaint(complaint, "")
        with self.assertRaises(ValueError):
            toggle_upvote_on_complaint(complaint, "   ")


if __name__ == "__main__":
    unittest.main()
