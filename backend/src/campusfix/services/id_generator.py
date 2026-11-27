"""ID generation strategies for MAIT CampusFix complaints.

Architecture Note on Production ID Generation:
----------------------------------------------
In a distributed cloud environment (e.g. AWS Lambda + DynamoDB or ECS + PostgreSQL),
sequential counter IDs based on in-memory state or scanning the database lead to race
conditions and duplicate key violations across concurrent workers.

Instead, MAIT CampusFix supports:
1. `UUIDYearIdGenerator`: Collision-resistant identifiers formatted as
   `CF-{year}-{UUID4_HEX_32}` (e.g., `CF-2026-550E8400E29B41D4A716446655440000`).
   This works safely across any number of independent, stateless worker instances without
   coordination, while remaining student-friendly and legible.
2. `SequentialYearIdGenerator`: Thread-safe sequential generator for local single-process
   prototyping or deterministic testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import re
import threading
import uuid


class IdGenerator(ABC):
    """Abstract strategy for generating unique complaint identifiers."""

    @abstractmethod
    def next_id(self) -> str:
        """Generate and return the next unique complaint ID."""


class UUIDYearIdGenerator(IdGenerator):
    """Generates collision-resistant, multi-instance-safe complaint IDs using full UUID4.
    
    Format: CF-{year}-{UUID4_HEX_32}
    Example: CF-2026-550E8400E29B41D4A716446655440000
    
    Advantages:
    - Retains the full 128-bit entropy (32 hex characters) of RFC 4122 UUID4.
    - Zero coordination required between distributed serverless instances.
    - Preserves the student-friendly `CF-{year}-` prefix for display, search queries,
      and acknowledgement slips.
    """

    TOKEN_LENGTH = 32

    def __init__(self, prefix: str = "CF", token_length: int = 32) -> None:
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("ID generator prefix cannot be empty.")
        clean_prefix = prefix.strip().upper()
        if not re.match(r"^[A-Z0-9]+$", clean_prefix):
            raise ValueError(f"ID generator prefix must contain only alphanumeric characters, got: '{prefix}'.")

        if isinstance(token_length, bool) or not isinstance(token_length, int) or token_length != 32:
            raise ValueError(f"UUIDYearIdGenerator requires token_length to be exactly 32 hexadecimal characters, got {token_length!r}.")

        self._prefix = clean_prefix
        self._token_length = 32

    def next_id(self) -> str:
        """Generate a collision-resistant unique ID."""
        year = datetime.now(timezone.utc).year
        # Generate full 32-hex character random UUID4
        token = uuid.uuid4().hex.upper()
        return f"{self._prefix}-{year}-{token}"


class SequentialYearIdGenerator(IdGenerator):
    """Generates sequential human-readable complaint IDs per calendar year.
    
    Format: CF-{year}-{sequence:04d}
    Example: CF-2026-0001
    
    Thread-safe in-memory generator suitable for local development and deterministic testing.
    """

    def __init__(self, starting_sequence: int = 1, prefix: str = "CF") -> None:
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("ID generator prefix cannot be empty.")
        clean_prefix = prefix.strip().upper()
        if not re.match(r"^[A-Z0-9]+$", clean_prefix):
            raise ValueError(f"ID generator prefix must contain only alphanumeric characters, got: '{prefix}'.")

        if isinstance(starting_sequence, bool) or not isinstance(starting_sequence, int) or starting_sequence < 1:
            raise ValueError("Starting sequence must be a positive integer >= 1.")

        self._lock = threading.Lock()
        self._sequence = starting_sequence
        self._prefix = clean_prefix

    def next_id(self) -> str:
        with self._lock:
            year = datetime.now(timezone.utc).year
            cid = f"{self._prefix}-{year}-{self._sequence:04d}"
            self._sequence += 1
            return cid
