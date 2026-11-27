"""Unit tests for API Gateway Cognito authentication and authorization middleware."""

import json
import unittest
from typing import Any

from campusfix.domain.models import Actor, Role
from campusfix.domain.rules import UnauthorizedError
from campusfix.middleware import ForbiddenError, extract_verified_actor


def make_api_gateway_event(
    claims: dict[str, Any] | None = None,
    body: str | dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    query_params: dict[str, str] | None = None,
    malform_level: str | None = None,
) -> dict[str, Any]:
    """Helper to construct API Gateway HTTP API v2 events with JWT authorizer context."""
    if malform_level == "empty_event":
        return {}
    if malform_level == "no_request_context":
        return {"body": body}
    if malform_level == "no_authorizer":
        return {"requestContext": {}}
    if malform_level == "no_jwt":
        return {"requestContext": {"authorizer": {}}}
    if malform_level == "no_claims":
        return {"requestContext": {"authorizer": {"jwt": {}}}}

    effective_claims = claims if claims is not None else {
        "sub": "test-student-sub-001",
        "token_use": "access",
        "cognito:groups": ["students"],
    }

    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": "POST /v1/complaints",
        "rawPath": "/v1/complaints",
        "rawQueryString": "",
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "campusfix-api",
            "authorizer": {
                "jwt": {
                    "claims": effective_claims,
                    "scopes": ["aws.cognito.signin.user.admin"],
                }
            },
            "http": {
                "method": "POST",
                "path": "/v1/complaints",
                "protocol": "HTTP/1.1",
            },
        },
    }

    if body is not None:
        event["body"] = json.dumps(body) if isinstance(body, dict) else body
    if headers is not None:
        event["headers"] = headers
    if query_params is not None:
        event["queryStringParameters"] = query_params

    return event


class TestAuthMiddlewareValidTokens(unittest.TestCase):
    """Test valid access token claim extraction and exact role mappings."""

    def test_valid_access_token_with_student_group_maps_student_actor(self):
        """Access token with groups=['students'] returns Actor(role=Role.STUDENT)."""
        event = make_api_gateway_event(
            claims={
                "sub": "stu-sub-101",
                "token_use": "access",
                "cognito:groups": ["students"],
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.user_id, "stu-sub-101")
        self.assertEqual(actor.role, Role.STUDENT)
        self.assertTrue(actor.is_student)
        self.assertFalse(actor.is_staff)

    def test_valid_access_token_with_maintenance_group_maps_maintenance_actor(self):
        """Access token with groups=['maintenance'] returns Actor(role=Role.MAINTENANCE)."""
        event = make_api_gateway_event(
            claims={
                "sub": "maint-sub-202",
                "token_use": "access",
                "cognito:groups": ["maintenance"],
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.user_id, "maint-sub-202")
        self.assertEqual(actor.role, Role.MAINTENANCE)
        self.assertTrue(actor.is_maintenance)
        self.assertTrue(actor.is_staff)

    def test_valid_access_token_with_admins_group_maps_admin_actor(self):
        """Access token with groups=['admins'] returns Actor(role=Role.ADMIN)."""
        event = make_api_gateway_event(
            claims={
                "sub": "admin-sub-303",
                "token_use": "access",
                "cognito:groups": ["admins"],
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.user_id, "admin-sub-303")
        self.assertEqual(actor.role, Role.ADMIN)
        self.assertTrue(actor.is_admin)
        self.assertTrue(actor.is_staff)

    def test_cognito_groups_as_string_single_student_returns_student_actor(self):
        """Bare recognized group string 'students' correctly maps to Role.STUDENT."""
        event = make_api_gateway_event(
            claims={
                "sub": "stu-sub-102",
                "token_use": "access",
                "cognito:groups": "students",
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.role, Role.STUDENT)

    def test_cognito_groups_as_string_single_maintenance_returns_maintenance_actor(self):
        """Bare recognized group string 'maintenance' correctly maps to Role.MAINTENANCE."""
        event = make_api_gateway_event(
            claims={
                "sub": "maint-sub-203",
                "token_use": "access",
                "cognito:groups": "maintenance",
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.role, Role.MAINTENANCE)

    def test_cognito_groups_as_string_single_admins_returns_admin_actor(self):
        """Bare recognized group string 'admins' correctly maps to Role.ADMIN."""
        event = make_api_gateway_event(
            claims={
                "sub": "admin-sub-304",
                "token_use": "access",
                "cognito:groups": "admins",
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.role, Role.ADMIN)

    def test_valid_json_encoded_single_group_array(self):
        """Valid JSON-encoded single-group arrays like '[\"students\"]' map correctly."""
        for role_name, expected_role in [
            ('["students"]', Role.STUDENT),
            ('["maintenance"]', Role.MAINTENANCE),
            ('["admins"]', Role.ADMIN),
        ]:
            with self.subTest(role_name=role_name):
                event = make_api_gateway_event(
                    claims={
                        "sub": "stu-json-sub",
                        "token_use": "access",
                        "cognito:groups": role_name,
                    }
                )
                actor = extract_verified_actor(event)
                self.assertEqual(actor.role, expected_role)

    def test_cognito_groups_as_tuple_and_set(self):
        """Groups passed as tuple or set correctly map to corresponding role."""
        event_tuple = make_api_gateway_event(
            claims={
                "sub": "stu-sub-104",
                "token_use": "access",
                "cognito:groups": ("students",),
            }
        )
        self.assertEqual(extract_verified_actor(event_tuple).role, Role.STUDENT)

        event_set = make_api_gateway_event(
            claims={
                "sub": "maint-sub-204",
                "token_use": "access",
                "cognito:groups": {"maintenance"},
            }
        )
        self.assertEqual(extract_verified_actor(event_set).role, Role.MAINTENANCE)


class TestAuthMiddlewareTokenTypeValidation(unittest.TestCase):
    """Test strict enforcement of token_use == 'access'."""

    def test_id_token_rejected_with_unauthorized_error(self):
        """Token with token_use='id' is strictly rejected with UnauthorizedError (401)."""
        event = make_api_gateway_event(
            claims={
                "sub": "stu-id-token",
                "token_use": "id",
                "cognito:groups": ["students"],
            }
        )
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("ID tokens are not permitted", str(ctx.exception))

    def test_refresh_token_rejected_with_unauthorized_error(self):
        """Token with token_use='refresh' is rejected with UnauthorizedError (401)."""
        event = make_api_gateway_event(
            claims={
                "sub": "stu-refresh-token",
                "token_use": "refresh",
                "cognito:groups": ["students"],
            }
        )
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("expected 'access'", str(ctx.exception))

    def test_missing_token_use_claim_rejected(self):
        """Token missing token_use claim is rejected with UnauthorizedError (401)."""
        event = make_api_gateway_event(
            claims={
                "sub": "stu-no-token-use",
                "cognito:groups": ["students"],
            }
        )
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("Missing 'token_use' claim", str(ctx.exception))

    def test_non_string_token_use_rejected(self):
        """Token with non-string token_use claim is rejected with UnauthorizedError (401)."""
        for bad_use in [123, True, [], {}]:
            with self.subTest(bad_use=bad_use):
                event = make_api_gateway_event(
                    claims={
                        "sub": "stu-bad-token-use",
                        "token_use": bad_use,
                        "cognito:groups": ["students"],
                    }
                )
                with self.assertRaises(UnauthorizedError):
                    extract_verified_actor(event)


class TestAuthMiddlewareSubValidation(unittest.TestCase):
    """Test validation of user identifier (sub)."""

    def test_missing_sub_claim_rejected(self):
        """Token missing sub claim raises UnauthorizedError (401)."""
        event = make_api_gateway_event(
            claims={
                "token_use": "access",
                "cognito:groups": ["students"],
            }
        )
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("Missing 'sub' claim", str(ctx.exception))

    def test_none_sub_claim_rejected(self):
        """Token with sub=None raises UnauthorizedError (401)."""
        event = make_api_gateway_event(
            claims={
                "sub": None,
                "token_use": "access",
                "cognito:groups": ["students"],
            }
        )
        with self.assertRaises(UnauthorizedError):
            extract_verified_actor(event)

    def test_empty_sub_claim_rejected(self):
        """Token with sub='' raises UnauthorizedError (401)."""
        event = make_api_gateway_event(
            claims={
                "sub": "",
                "token_use": "access",
                "cognito:groups": ["students"],
            }
        )
        with self.assertRaises(UnauthorizedError):
            extract_verified_actor(event)

    def test_whitespace_only_sub_claim_rejected(self):
        """Token with sub='   ' raises UnauthorizedError (401)."""
        event = make_api_gateway_event(
            claims={
                "sub": "   ",
                "token_use": "access",
                "cognito:groups": ["students"],
            }
        )
        with self.assertRaises(UnauthorizedError):
            extract_verified_actor(event)

    def test_non_string_sub_claim_rejected(self):
        """Token with sub as integer, bool, list, or dict raises UnauthorizedError (401)."""
        for bad_sub in [12345, True, False, ["sub"], {"sub": "id"}]:
            with self.subTest(bad_sub=bad_sub):
                event = make_api_gateway_event(
                    claims={
                        "sub": bad_sub,
                        "token_use": "access",
                        "cognito:groups": ["students"],
                    }
                )
                with self.assertRaises(UnauthorizedError):
                    extract_verified_actor(event)

    def test_sub_with_leading_or_trailing_whitespace_is_strictly_rejected(self):
        """Token sub with leading or trailing whitespace must be rejected without silent normalization."""
        for padded_sub in [
            "  student-uuid-leading",
            "student-uuid-trailing  ",
            "  student-uuid-both  ",
            "\tstudent-uuid-tab\n",
        ]:
            with self.subTest(padded_sub=padded_sub):
                event = make_api_gateway_event(
                    claims={
                        "sub": padded_sub,
                        "token_use": "access",
                        "cognito:groups": ["students"],
                    }
                )
                with self.assertRaises(UnauthorizedError) as ctx:
                    extract_verified_actor(event)
                self.assertIn("whitespace", str(ctx.exception).lower())

    def test_normal_valid_cognito_sub_remains_unchanged(self):
        """Standard valid UUID subs are preserved exactly as passed from Cognito."""
        valid_uuids = [
            "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "c73bcdcc-2669-4bf6-81d3-e4ae73fb11fd",
            "us-east-1:a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
        ]
        for valid_sub in valid_uuids:
            with self.subTest(valid_sub=valid_sub):
                event = make_api_gateway_event(
                    claims={
                        "sub": valid_sub,
                        "token_use": "access",
                        "cognito:groups": ["students"],
                    }
                )
                actor = extract_verified_actor(event)
                self.assertEqual(actor.user_id, valid_sub)


class TestAuthMiddlewareGroupValidation(unittest.TestCase):
    """Test group parsing, role validation, unknown groups, and mutual exclusivity."""

    def test_missing_cognito_groups_claim_rejected_forbidden(self):
        """Token missing cognito:groups claim raises ForbiddenError (403)."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-no-groups",
                "token_use": "access",
            }
        )
        with self.assertRaises(ForbiddenError) as ctx:
            extract_verified_actor(event)
        self.assertIn("Missing 'cognito:groups' claim", str(ctx.exception))

    def test_none_cognito_groups_claim_rejected_forbidden(self):
        """Token with cognito:groups=None raises ForbiddenError (403)."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-none-groups",
                "token_use": "access",
                "cognito:groups": None,
            }
        )
        with self.assertRaises(ForbiddenError):
            extract_verified_actor(event)

    def test_empty_groups_list_rejected_forbidden(self):
        """Token with empty groups list [] raises ForbiddenError (403)."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-empty-groups",
                "token_use": "access",
                "cognito:groups": [],
            }
        )
        with self.assertRaises(ForbiddenError):
            extract_verified_actor(event)

    def test_empty_groups_string_rejected_forbidden(self):
        """Token with empty or whitespace groups string raises ForbiddenError (403)."""
        for empty_str in ["", "   "]:
            with self.subTest(empty_str=empty_str):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-empty-str-groups",
                        "token_use": "access",
                        "cognito:groups": empty_str,
                    }
                )
                with self.assertRaises(ForbiddenError):
                    extract_verified_actor(event)

    def test_unrecognized_single_group_rejected_forbidden(self):
        """Token with unrecognized group ['alumni'] raises ForbiddenError (403)."""
        for unknown in [["alumni"], ["guest"], ["faculty"], ["dean"]]:
            with self.subTest(unknown=unknown):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-unknown-group",
                        "token_use": "access",
                        "cognito:groups": unknown,
                    }
                )
                with self.assertRaises(ForbiddenError) as ctx:
                    extract_verified_actor(event)
                self.assertIn("Unrecognized group(s)", str(ctx.exception))

    def test_conflicting_student_and_admin_groups_rejected_forbidden(self):
        """Token containing both 'students' and 'admins' raises ForbiddenError (403)."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-dual-role",
                "token_use": "access",
                "cognito:groups": ["students", "admins"],
            }
        )
        with self.assertRaises(ForbiddenError) as ctx:
            extract_verified_actor(event)
        self.assertIn("Conflicting multiple role groups", str(ctx.exception))

    def test_conflicting_student_and_maintenance_groups_rejected_forbidden(self):
        """Token containing both 'students' and 'maintenance' raises ForbiddenError (403)."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-student-maint",
                "token_use": "access",
                "cognito:groups": ["students", "maintenance"],
            }
        )
        with self.assertRaises(ForbiddenError):
            extract_verified_actor(event)

    def test_conflicting_maintenance_and_admin_groups_rejected_forbidden(self):
        """Token containing both 'maintenance' and 'admins' raises ForbiddenError (403)."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-maint-admin",
                "token_use": "access",
                "cognito:groups": ["maintenance", "admins"],
            }
        )
        with self.assertRaises(ForbiddenError):
            extract_verified_actor(event)

    def test_conflicting_all_three_groups_rejected_forbidden(self):
        """Token containing all three recognized groups raises ForbiddenError (403)."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-all-roles",
                "token_use": "access",
                "cognito:groups": ["students", "maintenance", "admins"],
            }
        )
        with self.assertRaises(ForbiddenError):
            extract_verified_actor(event)

    def test_valid_json_encoded_multi_group_array_rejects_conflicting_roles(self):
        """Valid JSON-encoded array with multiple groups must reject conflicting roles."""
        for multi_json in [
            '["students", "admins"]',
            '["students", "maintenance"]',
            '["maintenance", "admins"]',
            '["students", "maintenance", "admins"]',
        ]:
            with self.subTest(multi_json=multi_json):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-json-multi",
                        "token_use": "access",
                        "cognito:groups": multi_json,
                    }
                )
                with self.assertRaises(ForbiddenError) as ctx:
                    extract_verified_actor(event)
                self.assertIn("Conflicting multiple role groups", str(ctx.exception))

    def test_duplicate_group_entries_rejected_forbidden(self):
        """Duplicate entries in groups list or JSON array must be rejected."""
        duplicates = [
            ["students", "students"],
            ["maintenance", "maintenance"],
            ["admins", "admins"],
            ["students", "admins", "students"],
            '["students", "students"]',
            '["maintenance", "maintenance"]',
        ]
        for dup in duplicates:
            with self.subTest(dup=dup):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-duplicate-groups",
                        "token_use": "access",
                        "cognito:groups": dup,
                    }
                )
                with self.assertRaises(ForbiddenError) as ctx:
                    extract_verified_actor(event)
                self.assertIn("Duplicate group entries", str(ctx.exception))

    def test_recognized_plus_unknown_group_rejected_forbidden(self):
        """Token containing a recognized group plus an unknown group raises ForbiddenError (403)."""
        for mixed in [
            ["students", "alumni"],
            ["maintenance", "technicians"],
            ["admins", "superuser"],
            '["students", "alumni"]',
        ]:
            with self.subTest(mixed=mixed):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-mixed-groups",
                        "token_use": "access",
                        "cognito:groups": mixed,
                    }
                )
                with self.assertRaises(ForbiddenError) as ctx:
                    extract_verified_actor(event)
                self.assertIn("Unrecognized group(s)", str(ctx.exception))


class TestAuthMiddlewareMalformedGroupStrings(unittest.TestCase):
    """Regression tests for strict parsing of malformed group strings and delimiters."""

    def test_unmatched_open_bracket_rejected(self):
        """Group string with unmatched opening bracket '[students' must be rejected."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-unmatched-open",
                "token_use": "access",
                "cognito:groups": "[students",
            }
        )
        with self.assertRaises(ForbiddenError):
            extract_verified_actor(event)

    def test_unmatched_close_bracket_rejected(self):
        """Group string with unmatched closing bracket 'students]' must be rejected."""
        event = make_api_gateway_event(
            claims={
                "sub": "user-unmatched-close",
                "token_use": "access",
                "cognito:groups": "students]",
            }
        )
        with self.assertRaises(ForbiddenError):
            extract_verified_actor(event)

    def test_unexpected_delimiters_rejected(self):
        """Group string with unexpected commas 'students,,,' must be rejected."""
        for bad_delimiter in [
            "students,,,",
            ",students",
            "students,",
            "students, admins",
            "students admins",
        ]:
            with self.subTest(bad_delimiter=bad_delimiter):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-bad-delimiters",
                        "token_use": "access",
                        "cognito:groups": bad_delimiter,
                    }
                )
                with self.assertRaises(ForbiddenError):
                    extract_verified_actor(event)

    def test_malformed_quoted_strings_rejected(self):
        """Malformed quotes, single-quotes, or unmatched quotes must be rejected."""
        for bad_quotes in [
            "'students\"",
            "\"students'",
            "'students'",
            "\"students\"",
            "`students`",
        ]:
            with self.subTest(bad_quotes=bad_quotes):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-bad-quotes",
                        "token_use": "access",
                        "cognito:groups": bad_quotes,
                    }
                )
                with self.assertRaises(ForbiddenError):
                    extract_verified_actor(event)

    def test_malformed_json_arrays_rejected(self):
        """Malformed JSON array structures must be rejected."""
        for malformed_json in [
            "[students]",          # Missing quotes around identifier
            "['students']",        # Single quotes (invalid JSON)
            '["students",]',       # Trailing comma (invalid JSON)
            '["students"',         # Unclosed bracket
            '{"group": "students"}', # Object instead of array
            '["students", ["nested"]]', # Nested structure
            '["students", ""]',    # Empty string element
            '["students", "   "]', # Whitespace element
            '["students", 123]',   # Non-string element
            '[]',                  # Empty array
            '[ ]',                 # Whitespace-only array
        ]:
            with self.subTest(malformed_json=malformed_json):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-bad-json",
                        "token_use": "access",
                        "cognito:groups": malformed_json,
                    }
                )
                with self.assertRaises(ForbiddenError):
                    extract_verified_actor(event)

    def test_case_sensitive_exact_matching(self):
        """Group matching must be case-sensitive; non-exact cases must be rejected."""
        for bad_case in ["Students", "STUDENTS", "Maintenance", "MAINTENANCE", "Admins", "ADMINS"]:
            with self.subTest(bad_case=bad_case):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-bad-case",
                        "token_use": "access",
                        "cognito:groups": bad_case,
                    }
                )
                with self.assertRaises(ForbiddenError):
                    extract_verified_actor(event)

    def test_malformed_group_entry_in_list_rejected_forbidden(self):
        """Non-string, empty, or whitespace-padded item inside groups list raises ForbiddenError."""
        for bad_item in [
            ["students", 123],
            ["students", True],
            ["students", ""],
            ["students", "   "],
            [" students "],
            ["students "],
            [" students"],
            [None],
        ]:
            with self.subTest(bad_item=bad_item):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-bad-item",
                        "token_use": "access",
                        "cognito:groups": bad_item,
                    }
                )
                with self.assertRaises(ForbiddenError):
                    extract_verified_actor(event)

    def test_malformed_groups_type_rejected_forbidden(self):
        """Groups claim with unexpected non-container, non-string type raises ForbiddenError."""
        for bad_type in [123, 45.6, True, False, {"group": "students"}]:
            with self.subTest(bad_type=bad_type):
                event = make_api_gateway_event(
                    claims={
                        "sub": "user-bad-groups-type",
                        "token_use": "access",
                        "cognito:groups": bad_type,
                    }
                )
                with self.assertRaises(ForbiddenError):
                    extract_verified_actor(event)


class TestAuthMiddlewareEventStructure(unittest.TestCase):
    """Test strict fail-closed behavior on missing or malformed event wrappers."""

    def test_non_dict_event_rejected_unauthorized(self):
        """Non-dict event payload raises UnauthorizedError (401)."""
        for non_dict in [None, "event_string", [1, 2, 3], 42, True]:
            with self.subTest(non_dict=non_dict):
                with self.assertRaises(UnauthorizedError):
                    extract_verified_actor(non_dict)  # type: ignore

    def test_missing_request_context_rejected_unauthorized(self):
        """Event missing requestContext raises UnauthorizedError (401)."""
        event = make_api_gateway_event(malform_level="no_request_context")
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("requestContext", str(ctx.exception))

    def test_non_dict_request_context_rejected_unauthorized(self):
        """Event with non-dict requestContext raises UnauthorizedError (401)."""
        event = {"requestContext": "invalid_string"}
        with self.assertRaises(UnauthorizedError):
            extract_verified_actor(event)

    def test_missing_authorizer_rejected_unauthorized(self):
        """Event missing authorizer raises UnauthorizedError (401)."""
        event = make_api_gateway_event(malform_level="no_authorizer")
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("authorizer", str(ctx.exception))

    def test_non_dict_authorizer_rejected_unauthorized(self):
        """Event with non-dict authorizer raises UnauthorizedError (401)."""
        event = {"requestContext": {"authorizer": 123}}
        with self.assertRaises(UnauthorizedError):
            extract_verified_actor(event)

    def test_missing_jwt_rejected_unauthorized(self):
        """Event missing jwt authorizer context raises UnauthorizedError (401)."""
        event = make_api_gateway_event(malform_level="no_jwt")
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("jwt", str(ctx.exception))

    def test_non_dict_jwt_rejected_unauthorized(self):
        """Event with non-dict jwt context raises UnauthorizedError (401)."""
        event = {"requestContext": {"authorizer": {"jwt": []}}}
        with self.assertRaises(UnauthorizedError):
            extract_verified_actor(event)

    def test_missing_claims_rejected_unauthorized(self):
        """Event missing claims dictionary raises UnauthorizedError (401)."""
        event = make_api_gateway_event(malform_level="no_claims")
        with self.assertRaises(UnauthorizedError) as ctx:
            extract_verified_actor(event)
        self.assertIn("claims", str(ctx.exception))

    def test_non_dict_claims_rejected_unauthorized(self):
        """Event with non-dict claims raises UnauthorizedError (401)."""
        event = {"requestContext": {"authorizer": {"jwt": {"claims": "string_claims"}}}}
        with self.assertRaises(UnauthorizedError):
            extract_verified_actor(event)


class TestAuthMiddlewareSecurityDecoupling(unittest.TestCase):
    """Test anti-tampering, request body isolation, and email claim non-inference."""

    def test_request_body_identity_cannot_override_authorizer_claims(self):
        """Request body attempting to spoof identity or role is completely ignored."""
        event = make_api_gateway_event(
            claims={
                "sub": "verified-student-uuid",
                "token_use": "access",
                "cognito:groups": ["students"],
            },
            body={
                "user_id": "malicious-admin-uuid",
                "role": "ADMIN",
                "submitted_by": "another-student",
                "actor": "admin-principal",
            },
            headers={
                "x-user-id": "hacker-123",
                "x-actor-role": "ADMIN",
            },
            query_params={
                "user_id": "spoofed-id",
                "role": "ADMIN",
            },
        )
        actor = extract_verified_actor(event)
        # Actor MUST be constructed exclusively from verified JWT claims
        self.assertEqual(actor.user_id, "verified-student-uuid")
        self.assertEqual(actor.role, Role.STUDENT)
        self.assertFalse(actor.is_admin)

    def test_unverified_email_claim_does_not_affect_actor_creation(self):
        """Unverified email in claims does not alter Actor construction or fail valid auth."""
        event = make_api_gateway_event(
            claims={
                "sub": "student-unverified-email",
                "token_use": "access",
                "cognito:groups": ["students"],
                "email": "student@cst.mait.ac.in",
                "email_verified": False,
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.user_id, "student-unverified-email")
        self.assertEqual(actor.role, Role.STUDENT)

    def test_verified_email_claim_does_not_escalate_role(self):
        """Verified email claim (even director/admin domain) cannot escalate role beyond cognito:groups."""
        event = make_api_gateway_event(
            claims={
                "sub": "director-sub-with-student-group",
                "token_use": "access",
                "cognito:groups": ["students"],
                "email": "director@mait.ac.in",
                "email_verified": True,
            }
        )
        actor = extract_verified_actor(event)
        # Role is strictly governed by cognito:groups, never email domain or email_verified
        self.assertEqual(actor.role, Role.STUDENT)
        self.assertFalse(actor.is_admin)
        self.assertFalse(actor.is_staff)

    def test_staff_unverified_email_does_not_block_role_mapping(self):
        """Staff account with unverified email still maps to MAINTENANCE role from valid token."""
        event = make_api_gateway_event(
            claims={
                "sub": "tech-sub-unverified",
                "token_use": "access",
                "cognito:groups": ["maintenance"],
                "email": "plumber@mait.ac.in",
                "email_verified": False,
            }
        )
        actor = extract_verified_actor(event)
        self.assertEqual(actor.user_id, "tech-sub-unverified")
        self.assertEqual(actor.role, Role.MAINTENANCE)
        self.assertTrue(actor.is_maintenance)


if __name__ == "__main__":
    unittest.main()
