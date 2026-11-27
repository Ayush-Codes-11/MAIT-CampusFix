"""Unit tests for Amazon Cognito Lambda Triggers (PreSignUp and PostConfirmation).

Verifies:
1. PreSignUp:
   - Public self-registration (PreSignUp_SignUp) allows only verified student domain (cst.mait.ac.in).
   - Faculty/staff domain (mait.ac.in) and public domains are strictly rejected for self-registration.
   - Strict email validation: exactly one '@', non-empty local part and domain, whitespace rejection,
     no silent trimming of local part, lowercase domain normalization, exact domain matching.
   - DNS label validation: labels must be non-empty, ASCII alphanumeric and hyphens, no leading/trailing hyphens.
   - Subdomain spoofing prevention.
   - Administrative creation (PreSignUp_AdminCreateUser) permits staff and student domains,
     rejects external domains, and does not claim email auto-verification.
   - Dedicated PreSignUpFailedException raised on all validation failures.
   - Unsupported trigger sources rejected.
2. PostConfirmation:
   - Fires strictly for PostConfirmation_ConfirmSignUp.
   - Enrolls user into 'students' group using event.userName (never sub).
   - Group name is strictly 'students' (never maintenance or admins).
   - Other trigger sources (e.g., PostConfirmation_ConfirmForgotPassword) return event without calling Cognito.
   - Missing or whitespace userPoolId/userName raises ValueError.
   - Sanitized logging: never logs complete event, userAttributes, raw usernames, or full AWS error messages.
   - Native idempotency: successful group addition returns event normally.
   - Client errors are re-raised unchanged.
"""

from __future__ import annotations

import os
import sys
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from campusfix.domain.models import CampusFixDomainError
from campusfix.triggers.post_confirmation import (
    CONFIRM_SIGN_UP_TRIGGER,
    STUDENT_GROUP_NAME,
    lambda_handler as post_confirmation_handler,
)
from campusfix.triggers.pre_signup import (
    DEFAULT_STAFF_DOMAINS,
    DEFAULT_STUDENT_DOMAINS,
    PreSignUpFailedException,
    lambda_handler as pre_signup_handler,
)


class TestPreSignUpTrigger(unittest.TestCase):
    """Test suite for Cognito PreSignUp Lambda trigger."""

    def _make_signup_event(
        self,
        email: Any,
        trigger_source: str = "PreSignUp_SignUp",
        auto_confirm: bool | None = None,
        auto_verify: bool | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "version": "1",
            "region": "ap-south-1",
            "userPoolId": "ap-south-1_TestPool123",
            "userName": "test-uuid-student-001",
            "triggerSource": trigger_source,
            "request": {
                "userAttributes": {
                    "email": email,
                },
            },
            "response": {},
        }
        if auto_confirm is not None:
            event["response"]["autoConfirmUser"] = auto_confirm
        if auto_verify is not None:
            event["response"]["autoVerifyEmail"] = auto_verify
        return event

    def _make_admin_create_event(self, email: Any) -> dict[str, Any]:
        return self._make_signup_event(email, trigger_source="PreSignUp_AdminCreateUser")

    # -------------------------------------------------------------------------
    # Exception Hierarchy
    # -------------------------------------------------------------------------

    def test_exception_inheritance(self) -> None:
        """PreSignUpFailedException must inherit from CampusFixDomainError."""
        self.assertTrue(issubclass(PreSignUpFailedException, CampusFixDomainError))
        self.assertTrue(issubclass(PreSignUpFailedException, Exception))

    # -------------------------------------------------------------------------
    # Public Self-Registration (PreSignUp_SignUp) - Allowed
    # -------------------------------------------------------------------------

    def test_presignup_signup_valid_student_domain(self) -> None:
        """Valid student domain cst.mait.ac.in must be accepted with verification required."""
        event = self._make_signup_event("student001@cst.mait.ac.in")
        result = pre_signup_handler(event)

        self.assertIs(result, event)
        self.assertFalse(result["response"]["autoConfirmUser"])
        self.assertFalse(result["response"]["autoVerifyEmail"])

    def test_presignup_signup_uppercase_student_domain(self) -> None:
        """Uppercase student domain must be normalized and accepted."""
        event = self._make_signup_event("student002@CST.MAIT.AC.IN")
        result = pre_signup_handler(event)

        self.assertIs(result, event)
        self.assertFalse(result["response"]["autoConfirmUser"])
        self.assertFalse(result["response"]["autoVerifyEmail"])

    def test_presignup_signup_mixed_case_student_domain(self) -> None:
        """Mixed-case student domain must be normalized and accepted."""
        event = self._make_signup_event("student003@Cst.Mait.Ac.In")
        result = pre_signup_handler(event)

        self.assertIs(result, event)
        self.assertFalse(result["response"]["autoConfirmUser"])
        self.assertFalse(result["response"]["autoVerifyEmail"])

    def test_presignup_signup_preserves_local_part_case(self) -> None:
        """Local part case must be preserved (not altered or stripped)."""
        event = self._make_signup_event("John.Doe+Test@cst.mait.ac.in")
        result = pre_signup_handler(event)
        self.assertIs(result, event)
        self.assertEqual(
            result["request"]["userAttributes"]["email"],
            "John.Doe+Test@cst.mait.ac.in",
        )

    # -------------------------------------------------------------------------
    # Public Self-Registration (PreSignUp_SignUp) - Staff Rejected
    # -------------------------------------------------------------------------

    def test_presignup_signup_staff_domain_rejected(self) -> None:
        """Faculty/staff @mait.ac.in domain must be rejected for public self-registration."""
        event = self._make_signup_event("faculty@mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        msg = str(ctx.exception)
        self.assertIn("Faculty and staff accounts cannot self-register", msg)
        self.assertIn("MAIT Estate Office", msg)

    def test_presignup_signup_uppercase_staff_domain_rejected(self) -> None:
        """Uppercase staff domain @MAIT.AC.IN must be normalized and rejected."""
        event = self._make_signup_event("director@MAIT.AC.IN")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        self.assertIn("Faculty and staff accounts cannot self-register", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Public Self-Registration (PreSignUp_SignUp) - External & Spoofed Domains Rejected
    # -------------------------------------------------------------------------

    def test_presignup_signup_public_gmail_rejected(self) -> None:
        """Public @gmail.com must be rejected."""
        event = self._make_signup_event("student@gmail.com")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        self.assertIn("Domain '@gmail.com' is not authorized for student self-registration", str(ctx.exception))
        self.assertIn("cst.mait.ac.in", str(ctx.exception))

    def test_presignup_signup_public_yahoo_rejected(self) -> None:
        """Public @yahoo.com must be rejected."""
        event = self._make_signup_event("user@yahoo.com")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        self.assertIn("Domain '@yahoo.com' is not authorized", str(ctx.exception))

    def test_presignup_signup_subdomain_spoofing_suffix_rejected(self) -> None:
        """Domain suffix attack (@cst.mait.ac.in.evil.com) must be rejected."""
        event = self._make_signup_event("attacker@cst.mait.ac.in.evil.com")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        self.assertIn("Domain '@cst.mait.ac.in.evil.com' is not authorized", str(ctx.exception))

    def test_presignup_signup_subdomain_spoofing_prefix_rejected(self) -> None:
        """Prefix attack (@fakecst.mait.ac.in) must be rejected."""
        event = self._make_signup_event("attacker@fakecst.mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        self.assertIn("Domain '@fakecst.mait.ac.in' is not authorized", str(ctx.exception))

    def test_presignup_signup_parent_domain_rejected(self) -> None:
        """Parent domain mait.ac.in must not match cst.mait.ac.in."""
        event = self._make_signup_event("someone@mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        # Classified as staff domain
        self.assertIn("Faculty and staff accounts cannot self-register", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Email Validation Rules: Whitespace, Format, Malformed
    # -------------------------------------------------------------------------

    def test_presignup_missing_email_attribute(self) -> None:
        """Missing email attribute must raise PreSignUpFailedException."""
        event: dict[str, Any] = {
            "triggerSource": "PreSignUp_SignUp",
            "request": {"userAttributes": {}},
        }
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertEqual(str(ctx.exception), "Email attribute is required.")

    def test_presignup_empty_email_string(self) -> None:
        """Empty email string must raise PreSignUpFailedException."""
        event = self._make_signup_event("")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertEqual(str(ctx.exception), "Email attribute is required.")

    def test_presignup_none_email(self) -> None:
        """None email must raise PreSignUpFailedException."""
        event = self._make_signup_event(None)
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertEqual(str(ctx.exception), "Email attribute is required.")

    def test_presignup_non_string_email(self) -> None:
        """Integer email must raise PreSignUpFailedException."""
        event = self._make_signup_event(12345)
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertEqual(str(ctx.exception), "Email attribute is required.")

    def test_presignup_leading_whitespace_rejected(self) -> None:
        """Leading whitespace must be rejected (no silent strip)."""
        event = self._make_signup_event(" student@cst.mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertIn("whitespace is not permitted", str(ctx.exception))

    def test_presignup_trailing_whitespace_rejected(self) -> None:
        """Trailing whitespace must be rejected (no silent strip)."""
        event = self._make_signup_event("student@cst.mait.ac.in ")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertIn("whitespace is not permitted", str(ctx.exception))

    def test_presignup_embedded_whitespace_rejected(self) -> None:
        """Whitespace within email must be rejected."""
        event = self._make_signup_event("stu dent@cst.mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertIn("whitespace is not permitted", str(ctx.exception))

    def test_presignup_tab_or_newline_rejected(self) -> None:
        """Tabs and newlines must be rejected."""
        for bad_email in ["student\t@cst.mait.ac.in", "student@cst.mait.ac.in\n"]:
            with self.subTest(bad_email=bad_email):
                event = self._make_signup_event(bad_email)
                with self.assertRaises(PreSignUpFailedException) as ctx:
                    pre_signup_handler(event)
                self.assertIn("whitespace is not permitted", str(ctx.exception))

    def test_presignup_missing_at_sign(self) -> None:
        """Email missing '@' must be rejected."""
        event = self._make_signup_event("studentcst.mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertIn("exactly one '@' character required", str(ctx.exception))

    def test_presignup_multiple_at_signs(self) -> None:
        """Email with multiple '@' must be rejected."""
        event = self._make_signup_event("student@@cst.mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertIn("exactly one '@' character required", str(ctx.exception))

    def test_presignup_empty_local_part(self) -> None:
        """Email with empty local part (@cst.mait.ac.in) must be rejected."""
        event = self._make_signup_event("@cst.mait.ac.in")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertIn("local part cannot be empty", str(ctx.exception))

    def test_presignup_empty_domain(self) -> None:
        """Email with empty domain (student@) must be rejected."""
        event = self._make_signup_event("student@")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)
        self.assertIn("domain cannot be empty", str(ctx.exception))

    def test_presignup_malformed_domain_dots(self) -> None:
        """Domain with leading, trailing, or consecutive dots must be rejected."""
        bad_domains = [
            "student@.cst.mait.ac.in",
            "student@cst.mait.ac.in.",
            "student@cst..mait.ac.in",
        ]
        for bad_email in bad_domains:
            with self.subTest(bad_email=bad_email):
                event = self._make_signup_event(bad_email)
                with self.assertRaises(PreSignUpFailedException) as ctx:
                    pre_signup_handler(event)
                self.assertIn("malformed domain name", str(ctx.exception))

    def test_presignup_malformed_domain_labels_rejected(self) -> None:
        """Domain labels with leading/trailing hyphens, underscores, or invalid chars must be rejected."""
        malformed_domains = [
            ("student@-cst.mait.ac.in", "start or end with a hyphen"),
            ("student@cst-.mait.ac.in", "start or end with a hyphen"),
            ("student@cst.-mait.ac.in", "start or end with a hyphen"),
            ("student@cst.mait-.ac.in", "start or end with a hyphen"),
            ("student@cst.mait.-ac.in", "start or end with a hyphen"),
            ("student@cst.mait.ac-.in", "start or end with a hyphen"),
            ("student@cst_mait.ac.in", "only ASCII letters, digits, and hyphens"),
            ("student@cst.mait_staff.ac.in", "only ASCII letters, digits, and hyphens"),
            ("student@cst.mait.ac.in/", "only ASCII letters, digits, and hyphens"),
            ("student@cst.mait.ac.in:8080", "only ASCII letters, digits, and hyphens"),
            ("student@cst!mait.ac.in", "only ASCII letters, digits, and hyphens"),
            ("student@cst#mait.ac.in", "only ASCII letters, digits, and hyphens"),
            ("student@cst", "at least two labels"),
        ]
        for bad_email, expected_substr in malformed_domains:
            with self.subTest(bad_email=bad_email):
                event = self._make_signup_event(bad_email)
                with self.assertRaises(PreSignUpFailedException) as ctx:
                    pre_signup_handler(event)
                self.assertIn(expected_substr, str(ctx.exception))

    # -------------------------------------------------------------------------
    # Administrative Creation (PreSignUp_AdminCreateUser)
    # -------------------------------------------------------------------------

    def test_presignup_admin_create_staff_domain_accepted(self) -> None:
        """Admin creating staff account with @mait.ac.in must be accepted."""
        event = self._make_admin_create_event("technician01@mait.ac.in")
        result = pre_signup_handler(event)

        self.assertIs(result, event)
        # Verify trigger does NOT claim to verify email for AdminCreateUser
        response = result.get("response", {})
        self.assertNotIn("autoVerifyEmail", response)
        self.assertNotIn("autoConfirmUser", response)

    def test_presignup_admin_create_student_domain_accepted(self) -> None:
        """Admin creating student account with @cst.mait.ac.in must be accepted."""
        event = self._make_admin_create_event("teststudent@cst.mait.ac.in")
        result = pre_signup_handler(event)

        self.assertIs(result, event)
        response = result.get("response", {})
        self.assertNotIn("autoVerifyEmail", response)
        self.assertNotIn("autoConfirmUser", response)

    def test_presignup_admin_create_uppercase_domain_accepted(self) -> None:
        """Admin creating account with uppercase @MAIT.AC.IN must be accepted."""
        event = self._make_admin_create_event("estate_officer@MAIT.AC.IN")
        result = pre_signup_handler(event)
        self.assertIs(result, event)

    def test_presignup_admin_create_external_domain_rejected(self) -> None:
        """Admin creating account with unauthorized external domain (@gmail.com) must be rejected."""
        event = self._make_admin_create_event("tech@gmail.com")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        msg = str(ctx.exception)
        self.assertIn("Admin provisioning rejected for domain '@gmail.com'", msg)
        self.assertIn("cst.mait.ac.in", msg)
        self.assertIn("mait.ac.in", msg)

    # -------------------------------------------------------------------------
    # General Event Validation & Custom Environments
    # -------------------------------------------------------------------------

    def test_presignup_invalid_event_payload(self) -> None:
        """Non-dict event payload must raise PreSignUpFailedException."""
        for invalid_event in [None, "not a dict", [1, 2, 3]]:
            with self.subTest(invalid_event=invalid_event):
                with self.assertRaises(PreSignUpFailedException) as ctx:
                    pre_signup_handler(invalid_event)  # type: ignore[arg-type]
                self.assertIn("Event payload must be a dictionary", str(ctx.exception))

    def test_presignup_unsupported_trigger_source(self) -> None:
        """Unsupported trigger source must raise PreSignUpFailedException."""
        event = self._make_signup_event(
            "student@cst.mait.ac.in",
            trigger_source="PreSignUp_ExternalProvider",
        )
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event)

        self.assertIn("Unsupported trigger source 'PreSignUp_ExternalProvider'", str(ctx.exception))

    @patch.dict(os.environ, {
        "ALLOWED_STUDENT_EMAIL_DOMAINS": "students.campus.edu, it.campus.edu",
        "ALLOWED_STAFF_EMAIL_DOMAINS": "staff.campus.edu",
    })
    def test_presignup_custom_domains_via_env(self) -> None:
        """Custom domain allowlists configured via environment variables must be respected."""
        # Allowed custom student domain
        event1 = self._make_signup_event("s1@students.campus.edu")
        self.assertIs(pre_signup_handler(event1), event1)

        # Allowed second custom student domain
        event2 = self._make_signup_event("s2@it.campus.edu")
        self.assertIs(pre_signup_handler(event2), event2)

        # Custom staff domain rejected for public signup
        event3 = self._make_signup_event("staff1@staff.campus.edu")
        with self.assertRaises(PreSignUpFailedException) as ctx:
            pre_signup_handler(event3)
        self.assertIn("Faculty and staff accounts cannot self-register", str(ctx.exception))

        # Default domain no longer in custom allowlist
        event4 = self._make_signup_event("student@cst.mait.ac.in")
        with self.assertRaises(PreSignUpFailedException):
            pre_signup_handler(event4)


class TestPostConfirmationTrigger(unittest.TestCase):
    """Test suite for Cognito PostConfirmation Lambda trigger."""

    def _make_confirm_event(
        self,
        trigger_source: str = CONFIRM_SIGN_UP_TRIGGER,
        user_pool_id: str = "ap-south-1_CampusFixPool",
        user_name: str = "cognito-username-uuid-1234",
        sub: str = "sub-uuid-different-value",
        email: str = "student@cst.mait.ac.in",
    ) -> dict[str, Any]:
        return {
            "version": "1",
            "region": "ap-south-1",
            "userPoolId": user_pool_id,
            "userName": user_name,
            "triggerSource": trigger_source,
            "request": {
                "userAttributes": {
                    "sub": sub,
                    "email": email,
                    "email_verified": "true",
                },
            },
            "response": {},
        }

    # -------------------------------------------------------------------------
    # Successful Group Assignment (PostConfirmation_ConfirmSignUp)
    # -------------------------------------------------------------------------

    def test_post_confirmation_adds_student_to_students_group(self) -> None:
        """Self-confirmed student must be added to 'students' group using event.userName."""
        event = self._make_confirm_event(
            user_pool_id="ap-south-1_CampusFixPool",
            user_name="student-username-456",
            sub="student-sub-789",
        )
        mock_client = MagicMock()

        result = post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIs(result, event)
        # CRITICAL: Username MUST be event["userName"] ("student-username-456"), NOT sub!
        mock_client.admin_add_user_to_group.assert_called_once_with(
            UserPoolId="ap-south-1_CampusFixPool",
            Username="student-username-456",
            GroupName=STUDENT_GROUP_NAME,
        )
        self.assertEqual(STUDENT_GROUP_NAME, "students")

    def test_post_confirmation_native_idempotency(self) -> None:
        """Cognito AdminAddUserToGroup returns HTTP 200 if user is already in group."""
        event = self._make_confirm_event()
        mock_client = MagicMock()
        mock_client.admin_add_user_to_group.return_value = {
            "ResponseMetadata": {"HTTPStatusCode": 200}
        }

        result = post_confirmation_handler(event, cognito_client=mock_client)
        self.assertIs(result, event)
        mock_client.admin_add_user_to_group.assert_called_once()

    # -------------------------------------------------------------------------
    # Trigger Source Filtering (Other Triggers Ignored)
    # -------------------------------------------------------------------------

    def test_post_confirmation_ignores_forgot_password(self) -> None:
        """PostConfirmation_ConfirmForgotPassword must return event with zero Cognito API calls."""
        event = self._make_confirm_event(trigger_source="PostConfirmation_ConfirmForgotPassword")
        mock_client = MagicMock()

        result = post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIs(result, event)
        mock_client.admin_add_user_to_group.assert_not_called()

    def test_post_confirmation_ignores_admin_create_user(self) -> None:
        """Admin-created users do not get auto-assigned to 'students' group."""
        event = self._make_confirm_event(trigger_source="PostConfirmation_AdminCreateUser")
        mock_client = MagicMock()

        result = post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIs(result, event)
        mock_client.admin_add_user_to_group.assert_not_called()

    def test_post_confirmation_ignores_arbitrary_trigger_source(self) -> None:
        """Unknown or arbitrary trigger sources must be ignored safely."""
        event = self._make_confirm_event(trigger_source="PostConfirmation_CustomSource")
        mock_client = MagicMock()

        result = post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIs(result, event)
        mock_client.admin_add_user_to_group.assert_not_called()

    # -------------------------------------------------------------------------
    # Identifier Validation (userPoolId and userName)
    # -------------------------------------------------------------------------

    def test_post_confirmation_missing_user_pool_id(self) -> None:
        """Missing userPoolId must raise ValueError."""
        event = self._make_confirm_event()
        del event["userPoolId"]
        mock_client = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIn("missing or invalid userPoolId", str(ctx.exception))
        mock_client.admin_add_user_to_group.assert_not_called()

    def test_post_confirmation_whitespace_user_pool_id(self) -> None:
        """Whitespace userPoolId must raise ValueError."""
        event = self._make_confirm_event(user_pool_id="   ")
        mock_client = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIn("missing or invalid userPoolId", str(ctx.exception))
        mock_client.admin_add_user_to_group.assert_not_called()

    def test_post_confirmation_missing_user_name(self) -> None:
        """Missing userName must raise ValueError."""
        event = self._make_confirm_event()
        del event["userName"]
        mock_client = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIn("missing or invalid userName", str(ctx.exception))
        mock_client.admin_add_user_to_group.assert_not_called()

    def test_post_confirmation_whitespace_user_name(self) -> None:
        """Whitespace userName must raise ValueError."""
        event = self._make_confirm_event(user_name="   ")
        mock_client = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            post_confirmation_handler(event, cognito_client=mock_client)

        self.assertIn("missing or invalid userName", str(ctx.exception))
        mock_client.admin_add_user_to_group.assert_not_called()

    def test_post_confirmation_invalid_event_type(self) -> None:
        """Non-dict event payload must raise ValueError."""
        mock_client = MagicMock()
        with self.assertRaises(ValueError) as ctx:
            post_confirmation_handler("invalid string", cognito_client=mock_client)  # type: ignore[arg-type]

        self.assertIn("expected a dictionary", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Sanitized Logging & PII Protection (Regression Tests)
    # -------------------------------------------------------------------------

    def test_post_confirmation_invalid_identifiers_log_no_pii_or_event_data(self) -> None:
        """Invalid userPoolId or userName must never log raw event data or user attributes."""
        sensitive_email = "student_secret_privacy@cst.mait.ac.in"
        sensitive_phone = "+919876543210"
        sensitive_sub = "secret-sub-identifier-999"

        # Case A: Missing userPoolId
        event_missing_pool = {
            "triggerSource": CONFIRM_SIGN_UP_TRIGGER,
            "userName": "student-user-123",
            "request": {
                "userAttributes": {
                    "email": sensitive_email,
                    "phone_number": sensitive_phone,
                    "sub": sensitive_sub,
                }
            },
        }

        with self.assertLogs("campusfix.triggers.post_confirmation", level="ERROR") as cm:
            with self.assertRaises(ValueError):
                post_confirmation_handler(event_missing_pool, cognito_client=MagicMock())

        combined_logs_pool = "\n".join(cm.output)
        self.assertNotIn(sensitive_email, combined_logs_pool)
        self.assertNotIn(sensitive_phone, combined_logs_pool)
        self.assertNotIn(sensitive_sub, combined_logs_pool)
        self.assertNotIn("userAttributes", combined_logs_pool)
        self.assertIn("missing or invalid userPoolId", combined_logs_pool)

        # Case B: Missing userName
        event_missing_user = {
            "triggerSource": CONFIRM_SIGN_UP_TRIGGER,
            "userPoolId": "ap-south-1_CampusFixPool",
            "userName": "   ",
            "request": {
                "userAttributes": {
                    "email": sensitive_email,
                    "phone_number": sensitive_phone,
                    "sub": sensitive_sub,
                }
            },
        }

        with self.assertLogs("campusfix.triggers.post_confirmation", level="ERROR") as cm:
            with self.assertRaises(ValueError):
                post_confirmation_handler(event_missing_user, cognito_client=MagicMock())

        combined_logs_user = "\n".join(cm.output)
        self.assertNotIn(sensitive_email, combined_logs_user)
        self.assertNotIn(sensitive_phone, combined_logs_user)
        self.assertNotIn(sensitive_sub, combined_logs_user)
        self.assertNotIn("userAttributes", combined_logs_user)
        self.assertIn("missing or invalid userName", combined_logs_user)

    def test_post_confirmation_aws_failure_sanitizes_logs_and_reraises(self) -> None:
        """AWS failures must not log raw username or sensitive exception details, and re-raise."""
        raw_username_as_email = "student_login@cst.mait.ac.in"
        sensitive_token_leak = "SecretAuthTokenBearer-XYZ12345"
        sensitive_error_detail = f"Failed to authenticate with token: {sensitive_token_leak}"

        event = self._make_confirm_event(
            user_name=raw_username_as_email,
            user_pool_id="ap-south-1_CampusFixPool",
            email=raw_username_as_email,
        )

        class MockClientError(Exception):
            def __init__(self, message: str, code: str):
                super().__init__(message)
                self.response = {"Error": {"Code": code, "Message": message}}

        original_err = MockClientError(sensitive_error_detail, "InternalServiceException")
        mock_client = MagicMock()
        mock_client.admin_add_user_to_group.side_effect = original_err

        with self.assertLogs("campusfix.triggers.post_confirmation", level="ERROR") as cm:
            with self.assertRaises(MockClientError) as ctx:
                post_confirmation_handler(event, cognito_client=mock_client)

            # Assert original exception is re-raised unchanged
            self.assertIs(ctx.exception, original_err)

        combined_logs = "\n".join(cm.output)
        # Assert raw username/email is NOT logged
        self.assertNotIn(raw_username_as_email, combined_logs)
        # Assert full exception message and token leak are NOT logged
        self.assertNotIn(sensitive_error_detail, combined_logs)
        self.assertNotIn(sensitive_token_leak, combined_logs)
        # Assert sanitized error identifier and fixed operation description ARE logged
        self.assertIn("InternalServiceException", combined_logs)
        self.assertIn("Failed group assignment operation", combined_logs)
        self.assertIn("ap-south-1_CampusFixPool", combined_logs)

    # -------------------------------------------------------------------------
    # Error Handling & Client Error Propagation
    # -------------------------------------------------------------------------

    def test_post_confirmation_reraises_cognito_errors(self) -> None:
        """Unexpected Cognito API exceptions must be re-raised for CloudWatch error recording."""
        event = self._make_confirm_event()
        mock_client = MagicMock()
        mock_client.admin_add_user_to_group.side_effect = RuntimeError("Cognito service error")

        with self.assertRaises(RuntimeError) as ctx:
            post_confirmation_handler(event, cognito_client=mock_client)

        self.assertEqual(str(ctx.exception), "Cognito service error")

    def test_post_confirmation_requires_boto3_when_no_client_injected(self) -> None:
        """Calling handler without an injected client raises RuntimeError if boto3 is missing."""
        event = self._make_confirm_event()
        # Reset cached client and simulate missing boto3 regardless of installed packages.
        with (
            patch("campusfix.triggers.post_confirmation._cached_client", None),
            patch.dict(sys.modules, {"boto3": None}),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                post_confirmation_handler(event)
            self.assertIn("boto3 is required", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
