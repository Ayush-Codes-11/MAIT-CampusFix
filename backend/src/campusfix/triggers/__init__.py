"""Amazon Cognito Lambda triggers package for MAIT CampusFix."""

from .post_confirmation import lambda_handler as post_confirmation_handler
from .pre_signup import PreSignUpFailedException, lambda_handler as pre_signup_handler

__all__ = [
    "PreSignUpFailedException",
    "pre_signup_handler",
    "post_confirmation_handler",
]
