"""Witness identity namespaces survive concurrency, nesting, and failure."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from elt_taskgen.generation import populations
from elt_taskgen.models import ColumnSpec, ColumnType


class WitnessMintingTests(unittest.TestCase):
    column = ColumnSpec(name="id", type=ColumnType.BIGINT)

    def mint(self):
        return populations._mint(self.column, "entities", 0)

    def test_nested_scope_restores_the_outer_offset_after_failure(self):
        baseline = self.mint()
        with populations._shape_mint_offset(100):
            self.assertEqual(self.mint(), baseline + 100)
            with self.assertRaisesRegex(RuntimeError, "witness failed"):
                with populations._shape_mint_offset(200):
                    self.assertEqual(self.mint(), baseline + 200)
                    raise RuntimeError("witness failed")
            self.assertEqual(self.mint(), baseline + 100)
        self.assertEqual(self.mint(), baseline)

    def test_overlapping_threads_do_not_share_offsets(self):
        baseline = self.mint()
        entered, observed = Barrier(2), Barrier(2)

        def generate(offset):
            with populations._shape_mint_offset(offset):
                entered.wait(timeout=5)
                value = self.mint()
                observed.wait(timeout=5)
            return value, self.mint()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(generate, 100)
            second = pool.submit(generate, 200)
            self.assertEqual(first.result(timeout=10), (baseline + 100, baseline))
            self.assertEqual(second.result(timeout=10), (baseline + 200, baseline))
        self.assertEqual(self.mint(), baseline)

    def test_overlapping_async_tasks_restore_their_inherited_scope(self):
        baseline = self.mint()

        async def run():
            first_entered, second_entered = asyncio.Event(), asyncio.Event()

            async def generate(offset, entered, other):
                with populations._shape_mint_offset(offset):
                    entered.set()
                    await asyncio.wait_for(other.wait(), timeout=5)
                    value = self.mint()
                return value, self.mint()

            with populations._shape_mint_offset(50):
                results = await asyncio.gather(
                    generate(100, first_entered, second_entered),
                    generate(200, second_entered, first_entered),
                )
                self.assertEqual(self.mint(), baseline + 50)
                return results

        self.assertEqual(asyncio.run(run()), [
            (baseline + 100, baseline + 50), (baseline + 200, baseline + 50),
        ])
        self.assertEqual(self.mint(), baseline)


if __name__ == "__main__":
    unittest.main()
