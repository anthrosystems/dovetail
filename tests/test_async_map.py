import asyncio
import threading
import time
import unittest

from pydovetail import Dovetail


class AsyncMapTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_and_concurrency(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def work(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.005)
            with lock:
                active -= 1
            return value * 2

        with Dovetail(max_workers=8) as dvt:
            self.assertEqual(await dvt.task.map(work, range(20), max_concurrency=3), list(range(0, 40, 2)))
            self.assertLessEqual(peak, 3)
            with self.assertRaises(ValueError):
                await dvt.task.map(work, [1], max_concurrency=0)

    async def test_return_exceptions(self):
        def work(value):
            if value == 2:
                raise ValueError('expected')
            return value

        with Dovetail() as dvt:
            results = await dvt.task.map(work, [1, 2, 3], return_exceptions=True)
            self.assertEqual([results[0], results[2]], [1, 3])
            self.assertIsInstance(results[1], ValueError)
            with self.assertRaises(ValueError):
                await dvt.task.map(work, [2])

    async def test_events_on_loop(self):
        with Dovetail() as dvt:
            loop_thread = threading.get_ident()
            seen = []
            dvt.events.on(dvt.TaskEvent.DONE, lambda event: seen.append(threading.get_ident()))
            await dvt.task.to_thread(lambda: 1)
            await asyncio.sleep(0)
            self.assertIn(loop_thread, seen)
