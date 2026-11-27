"""Unit tests for actor permissions and server-side authorization enforcement."""

import unittest

from campusfix.domain.models import (
    Actor,
    ComplaintStatus,
    IssueCategory,
    Role,
)
from campusfix.domain.rules import UnauthorizedError, ValidationError
from campusfix.repositories.memory import InMemoryComplaintRepository
from campusfix.services.complaints import ComplaintService


class TestServerSideAuthorization(unittest.TestCase):
    """Tests role-based, ownership-based, and type-safe server authorization."""

    def setUp(self) -> None:
        self.repo = InMemoryComplaintRepository()
        self.service = ComplaintService(repository=self.repo)

        # Standard Actors
        self.student_alice = Actor.student("alice-stu-1")
        self.student_bob = Actor.student("bob-stu-2")
        self.staff_ramesh = Actor.maintenance("ramesh-tech-1")
        self.admin_director = Actor.admin("director-admin-1")

    # -------------------------------------------------------------------------
    # Creation Authorization
    # -------------------------------------------------------------------------

    def test_student_can_create_complaint(self):
        """Students are authorized to file complaints."""
        complaint = self.service.create_complaint(
            title="Leaking pipe in Block A",
            description="Continuous water leakage from ceiling joint.",
            building="Block A",
            floor="1st",
            room_number="101",
            category=IssueCategory.PLUMBING,
            actor=self.student_alice,
        )
        self.assertEqual(complaint.submitted_by, self.student_alice.user_id)

    def test_student_cannot_impersonate_another_student_at_creation(self):
        """Student Alice cannot set submitted_by to Bob."""
        with self.assertRaises(UnauthorizedError) as ctx:
            self.service.create_complaint(
                title="Spoofed ticket creation",
                description="Attempting to file a complaint in another student's name.",
                building="Block A",
                floor="1st",
                room_number="101",
                category=IssueCategory.PLUMBING,
                actor=self.student_alice,
                submitted_by=self.student_bob.user_id,
            )
        self.assertIn("does not match authenticated student", str(ctx.exception))

    def test_non_students_cannot_create_complaint(self):
        """Maintenance and admin staff cannot directly file student complaints."""
        for unauthorized_actor in [self.staff_ramesh, self.admin_director]:
            with self.subTest(role=unauthorized_actor.role):
                with self.assertRaises(UnauthorizedError):
                    self.service.create_complaint(
                        title="Unauthorized creation attempt",
                        description="This should fail authorization immediately.",
                        building="Block B",
                        floor="2nd",
                        room_number="202",
                        category=IssueCategory.ELECTRICAL,
                        actor=unauthorized_actor,
                    )

    # -------------------------------------------------------------------------
    # String Actor & Invalid Type Rejection (Anti-Coercion Tests)
    # -------------------------------------------------------------------------

    def test_string_maintenance_id_cannot_authorize_status_transition(self):
        """Passing a raw string maintenance ID must raise UnauthorizedError, not coerce."""
        complaint = self.service.create_complaint(
            title="Broken door lock in Block C",
            description="Lock mechanism is jammed and key will not turn in door.",
            building="Block C",
            floor="1st",
            room_number="104",
            category=IssueCategory.CIVIL_STRUCTURAL,
            actor=self.student_alice,
        )

        # Raw string instead of Actor instance must be rejected
        with self.assertRaises(UnauthorizedError) as ctx:
            self.service.transition_status(
                complaint.id,
                to_status=ComplaintStatus.IN_PROGRESS,
                actor="ramesh-tech-1",  # type: ignore
            )
        self.assertIn("requires an authenticated Actor instance", str(ctx.exception))

    def test_string_admin_id_cannot_authorize_staff_assignment(self):
        """Passing a raw string admin ID must raise UnauthorizedError, not coerce."""
        complaint = self.service.create_complaint(
            title="Broken laboratory bench",
            description="Chemical bench top has broken support leg.",
            building="Block C",
            floor="2nd",
            room_number="205",
            category=IssueCategory.FURNITURE,
            actor=self.student_alice,
        )

        with self.assertRaises(UnauthorizedError) as ctx:
            self.service.assign_staff(
                complaint.id,
                staff_name="Sunil Sharma",
                actor="director-admin-1",  # type: ignore
            )
        self.assertIn("requires an authenticated Actor instance", str(ctx.exception))

    def test_invalid_actor_types_rejected_across_all_methods(self):
        """Non-Actor objects (ints, dicts, booleans, None) must be rejected."""
        complaint = self.service.create_complaint(
            title="Flickering corridor light",
            description="Light in Block B corridor keeps turning off abruptly.",
            building="Block B",
            floor="1st",
            room_number="Corridor",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )

        bad_actors = [None, 12345, True, {"role": "ADMIN", "user_id": "hacker"}]
        for bad_actor in bad_actors:
            with self.subTest(actor=bad_actor):
                with self.assertRaises(UnauthorizedError):
                    self.service.toggle_upvote(complaint.id, actor=bad_actor)  # type: ignore

                with self.assertRaises(UnauthorizedError):
                    self.service.transition_status(
                        complaint.id, to_status=ComplaintStatus.IN_PROGRESS, actor=bad_actor  # type: ignore
                    )

    # -------------------------------------------------------------------------
    # Upvoting Authorization
    # -------------------------------------------------------------------------

    def test_student_can_upvote_complaint(self):
        """Students can upvote any existing complaint."""
        complaint = self.service.create_complaint(
            title="Broken switchboard",
            description="Sparking switchboard in the corridor.",
            building="Block C",
            floor="Ground",
            room_number="Corridor",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )

        c, added = self.service.toggle_upvote(complaint.id, actor=self.student_bob)
        self.assertTrue(added)
        self.assertIn(self.student_bob.user_id, c.upvotes)

    def test_raw_string_upvote_rejected(self):
        """Passing a raw string student ID to upvote must be rejected."""
        complaint = self.service.create_complaint(
            title="Leaking AC vent in Hall",
            description="Cold condensation dripping onto student seating area.",
            building="Block A",
            floor="2nd",
            room_number="Hall 1",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )

        with self.assertRaises(UnauthorizedError):
            self.service.toggle_upvote(complaint.id, actor="bob-stu-2")  # type: ignore

    def test_staff_and_admin_cannot_upvote(self):
        """Staff and Admin actors cannot upvote complaints."""
        complaint = self.service.create_complaint(
            title="Broken window latch",
            description="Window rattling in windy weather.",
            building="Block C",
            floor="Ground",
            room_number="102",
            category=IssueCategory.CIVIL_STRUCTURAL,
            actor=self.student_alice,
        )

        for staff_actor in [self.staff_ramesh, self.admin_director]:
            with self.subTest(role=staff_actor.role):
                with self.assertRaises(UnauthorizedError):
                    self.service.toggle_upvote(complaint.id, actor=staff_actor)

    # -------------------------------------------------------------------------
    # Status Transition & Assignment Authorization
    # -------------------------------------------------------------------------

    def test_staff_and_admin_can_transition_status(self):
        """Maintenance staff and Admins are authorized to advance complaint status."""
        complaint = self.service.create_complaint(
            title="Loose electrical wire",
            description="Dangerous live wire hanging near laboratory door.",
            building="Block C",
            floor="1st",
            room_number="Lab 1",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )

        # Maintenance advances to IN_PROGRESS
        c1 = self.service.transition_status(
            complaint.id,
            to_status=ComplaintStatus.IN_PROGRESS,
            actor=self.staff_ramesh,
        )
        self.assertEqual(c1.status, ComplaintStatus.IN_PROGRESS)

        # Admin resolves complaint
        c2 = self.service.transition_status(
            complaint.id,
            to_status=ComplaintStatus.RESOLVED,
            actor=self.admin_director,
            remarks="Insulated and secured in conduit box.",
        )
        self.assertEqual(c2.status, ComplaintStatus.RESOLVED)

    def test_students_cannot_transition_status(self):
        """Students cannot update complaint statuses."""
        complaint = self.service.create_complaint(
            title="Broken desk leg",
            description="Desk 14 in room 302 wobbles and cannot hold books.",
            building="Block B",
            floor="3rd",
            room_number="302",
            category=IssueCategory.FURNITURE,
            actor=self.student_alice,
        )

        with self.assertRaises(UnauthorizedError):
            self.service.transition_status(
                complaint.id,
                to_status=ComplaintStatus.IN_PROGRESS,
                actor=self.student_alice,
            )

    def test_students_cannot_assign_staff(self):
        """Students cannot assign staff to tickets."""
        complaint = self.service.create_complaint(
            title="AC water drip",
            description="Water leaking from indoor split AC unit.",
            building="Block A",
            floor="2nd",
            room_number="204",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )

        with self.assertRaises(UnauthorizedError):
            self.service.assign_staff(
                complaint.id,
                staff_name="Sunil Sharma",
                actor=self.student_alice,
            )

    # -------------------------------------------------------------------------
    # Feedback Authorization & Ownership
    # -------------------------------------------------------------------------

    def test_student_can_submit_feedback_on_own_resolved_complaint(self):
        """Students can submit and edit feedback on their own resolved complaint."""
        complaint = self.service.create_complaint(
            title="Burnt out tubelight",
            description="All four tubelights are dead in room 105.",
            building="Block A",
            floor="1st",
            room_number="105",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="Replaced with LEDs."
        )

        c = self.service.submit_feedback(
            complaint.id, rating=5, actor=self.student_alice, comment="Bright and clean!"
        )
        self.assertIsNotNone(c.feedback)
        self.assertEqual(c.feedback.rating, 5)

    def test_student_cannot_submit_feedback_on_another_students_complaint(self):
        """Students cannot submit feedback on complaints filed by someone else."""
        complaint = self.service.create_complaint(
            title="Water dispenser broken",
            description="Dispenser lever snapped off on ground floor.",
            building="Main Block",
            floor="Ground",
            room_number="Lobby",
            category=IssueCategory.PLUMBING,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="New lever attached."
        )

        # Bob attempts to submit feedback on Alice's ticket -> must be denied!
        with self.assertRaises(UnauthorizedError) as ctx:
            self.service.submit_feedback(
                complaint.id, rating=1, actor=self.student_bob, comment="Malicious review"
            )
        self.assertIn("cannot submit feedback", str(ctx.exception))

    def test_raw_string_feedback_submission_rejected(self):
        """Passing raw student string to feedback submission must be rejected."""
        complaint = self.service.create_complaint(
            title="Loose stair tread",
            description="Metal strip on edge of step 4 is loose and raised.",
            building="Block B",
            floor="Staircase",
            room_number="Stairs B",
            category=IssueCategory.CIVIL_STRUCTURAL,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="Fastened screws."
        )

        with self.assertRaises(UnauthorizedError):
            self.service.submit_feedback(complaint.id, rating=5, actor="alice-stu-1")  # type: ignore

    def test_non_students_cannot_submit_feedback(self):
        """Staff and Admin roles cannot submit student satisfaction feedback."""
        complaint = self.service.create_complaint(
            title="Broken chair",
            description="Backrest detached from library chair.",
            building="Library",
            floor="1st",
            room_number="Reading Hall",
            category=IssueCategory.FURNITURE,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="Screwed back on."
        )

        for staff_actor in [self.staff_ramesh, self.admin_director]:
            with self.subTest(role=staff_actor.role):
                with self.assertRaises(UnauthorizedError):
                    self.service.submit_feedback(
                        complaint.id, rating=5, actor=staff_actor
                    )

    # -------------------------------------------------------------------------
    # Follow-Up Authorization & Ownership
    # -------------------------------------------------------------------------

    def test_student_can_create_follow_up_on_own_resolved_complaint(self):
        """Student can raise a follow-up ticket for their own resolved complaint."""
        complaint = self.service.create_complaint(
            title="Broken projector screen",
            description="Screen roller spring is stuck and will not pull down.",
            building="Block B",
            floor="2nd",
            room_number="Seminar Hall",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="Lubricated roller."
        )

        follow_up = self.service.create_follow_up(
            original_complaint_id=complaint.id,
            follow_up_reason="The screen locked up again during morning lecture.",
            actor=self.student_alice,
        )
        self.assertTrue(follow_up.is_follow_up)
        self.assertEqual(follow_up.submitted_by, self.student_alice.user_id)

    def test_student_cannot_create_follow_up_on_another_students_complaint(self):
        """Students cannot create follow-up complaints on someone else's ticket."""
        complaint = self.service.create_complaint(
            title="Air cooler pump failure",
            description="Pump humming but no water circulating on pads.",
            building="Block A",
            floor="3rd",
            room_number="305",
            category=IssueCategory.ELECTRICAL,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="Pump freed."
        )

        # Bob attempts to create a follow-up on Alice's ticket -> must be rejected!
        with self.assertRaises(UnauthorizedError) as ctx:
            self.service.create_follow_up(
                original_complaint_id=complaint.id,
                follow_up_reason="I noticed it still doesn't work.",
                actor=self.student_bob,
            )
        self.assertIn("cannot create a follow-up", str(ctx.exception))

    def test_raw_string_follow_up_rejected(self):
        """Passing raw student string to follow-up creation must be rejected."""
        complaint = self.service.create_complaint(
            title="Broken tap valve",
            description="Valve handle broke off in washroom.",
            building="Block A",
            floor="1st",
            room_number="Washroom",
            category=IssueCategory.PLUMBING,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="Fitted new handle."
        )

        with self.assertRaises(UnauthorizedError):
            self.service.create_follow_up(
                original_complaint_id=complaint.id,
                follow_up_reason="Water still dripping slowly from spindle.",
                actor="alice-stu-1",  # type: ignore
            )

    def test_non_students_cannot_create_follow_ups(self):
        """Staff and Admin actors cannot create follow-up tickets."""
        complaint = self.service.create_complaint(
            title="Loose floor tile",
            description="Floor tile rocking and tripping students in doorway.",
            building="Block C",
            floor="1st",
            room_number="103",
            category=IssueCategory.CIVIL_STRUCTURAL,
            actor=self.student_alice,
        )
        self.service.transition_status(complaint.id, ComplaintStatus.IN_PROGRESS, actor=self.staff_ramesh)
        self.service.transition_status(
            complaint.id, ComplaintStatus.RESOLVED, actor=self.staff_ramesh, remarks="Adhesive applied."
        )

        for staff_actor in [self.staff_ramesh, self.admin_director]:
            with self.subTest(role=staff_actor.role):
                with self.assertRaises(UnauthorizedError):
                    self.service.create_follow_up(
                        original_complaint_id=complaint.id,
                        follow_up_reason="Staff follow up attempt",
                        actor=staff_actor,
                    )


if __name__ == "__main__":
    unittest.main()
