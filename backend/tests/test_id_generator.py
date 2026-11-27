"""Unit tests for complaint ID generation strategies."""

import concurrent.futures
from datetime import datetime, timezone
import re
import unittest

from campusfix.services.id_generator import (
    SequentialYearIdGenerator,
    UUIDYearIdGenerator,
)


class TestIdGenerators(unittest.TestCase):
    """Tests ID formatting, collision resistance, and multi-instance safety."""

    def test_uuid_generator_format(self):
        """Generated IDs must follow CF-{year}-{HEX32} format using full UUID4 entropy."""
        gen = UUIDYearIdGenerator()
        cid = gen.next_id()
        current_year = datetime.now(timezone.utc).year

        # Example format: CF-2026-550E8400E29B41D4A716446655440000
        pattern = rf"^CF-{current_year}-[A-F0-9]{{32}}$"
        self.assertRegex(cid, pattern)
        self.assertEqual(len(cid), 2 + 1 + 4 + 1 + 32)  # 40 characters

    def test_uuid_generator_token_length_validation(self):
        """Token lengths other than 32 must be rejected to enforce full UUID4 entropy."""
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(token_length=15)
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(token_length=16)  # 16 is rejected; exactly 32 required
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(token_length=31)
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(token_length=33)
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(token_length=True)  # type: ignore

        # Exactly 32 must succeed
        gen32 = UUIDYearIdGenerator(token_length=32)
        current_year = datetime.now(timezone.utc).year
        self.assertRegex(gen32.next_id(), rf"^CF-{current_year}-[A-F0-9]{{32}}$")

    def test_uuid_generator_prefix_validation(self):
        """Invalid prefixes must be rejected."""
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(prefix="")
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(prefix="   ")
        with self.assertRaises(ValueError):
            UUIDYearIdGenerator(prefix="CF-BAD")  # Hyphen not alphanumeric

    def test_uuid_generator_uniqueness_across_instances(self):
        """Generate 5,000 IDs across independent instances; all must be unique."""
        num_instances = 10
        ids_per_instance = 500
        total_expected = num_instances * ids_per_instance

        generators = [UUIDYearIdGenerator() for _ in range(num_instances)]
        generated_ids: list[str] = []

        def worker(gen: UUIDYearIdGenerator) -> list[str]:
            return [gen.next_id() for _ in range(ids_per_instance)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_instances) as executor:
            results = executor.map(worker, generators)
            for batch in results:
                generated_ids.extend(batch)

        self.assertEqual(len(generated_ids), total_expected)
        self.assertEqual(len(set(generated_ids)), total_expected, "Collision detected in UUID IDs!")

    def test_sequential_generator_thread_safety(self):
        """Sequential generator must increment monotonically without duplicate IDs in multi-threaded environment."""
        gen = SequentialYearIdGenerator(starting_sequence=1)
        total_calls = 1000
        results: list[str] = []

        def worker() -> str:
            return gen.next_id()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker) for _ in range(total_calls)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        self.assertEqual(len(results), total_calls)
        self.assertEqual(len(set(results)), total_calls, "Duplicate ID generated in sequential generator!")


if __name__ == "__main__":
    unittest.main()
