import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from pydovetail import Dovetail


class AsyncMapTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_input_creates_only_bounded_worker_tasks(self):
        original_create_task = asyncio.create_task
        created = []

        def track_task(coro):
            task = original_create_task(coro)
            created.append(task)
            return task

        with Dovetail(max_workers=4) as dvt:
            with patch('pydovetail._task.asyncio.create_task', side_effect=track_task):
                result = await dvt.task.map(lambda value: value + 1, range(1000), max_concurrency=3)
        self.assertEqual(result, list(range(1, 1001)))
        self.assertEqual(len(created), 3)

    async def test_multiple_failures_returned_in_input_order(self):
        def work(value):
            if value % 3 == 0:
                raise ValueError(str(value))
            return value * 2

        with Dovetail(max_workers=4) as dvt:
            results = await dvt.task.map(work, range(12), max_concurrency=3, return_exceptions=True)
        for index, result in enumerate(results):
            if index % 3 == 0:
                self.assertIsInstance(result, ValueError)
                self.assertEqual(str(result), str(index))
            else:
                self.assertEqual(result, index * 2)

    async def test_concurrent_maps_keep_separate_ordered_results(self):
        with Dovetail(max_workers=6) as dvt:
            first, second = await asyncio.gather(
                dvt.task.map(lambda value: value * 2, range(50), max_concurrency=2),
                dvt.task.map(lambda value: value + 100, range(50), max_concurrency=3),
            )
        self.assertEqual(first, [index * 2 for index in range(50)])
        self.assertEqual(second, [index + 100 for index in range(50)])

    async def test_cancelled_map_does_not_start_more_items(self):
        started = threading.Event()
        release = threading.Event()
        seen = []
        lock = threading.Lock()

        def work(value):
            with lock:
                seen.append(value)
            started.set()
            release.wait(timeout=2)
            return value

        with Dovetail(max_workers=2) as dvt:
            mapping = asyncio.create_task(dvt.task.map(work, range(100), max_concurrency=2))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(started.wait), 2))
                mapping.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await mapping
            finally:
                release.set()
        self.assertLessEqual(len(seen), 2)

    async def test_failure_cancels_remaining_map_workers(self):
        release = threading.Event()
        seen = []
        lock = threading.Lock()

        def work(value):
            with lock:
                seen.append(value)
            if value == 0:
                raise ValueError('first worker failed')
            release.wait(timeout=2)
            return value

        with Dovetail(max_workers=2) as dvt:
            try:
                with self.assertRaisesRegex(ValueError, 'first worker failed'):
                    await dvt.task.map(work, range(100), max_concurrency=2)
            finally:
                release.set()
        self.assertLessEqual(len(seen), 2)

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

    async def test_worker_events_visible_before_return(self):
        # A fast worker can finish before call_soon_threadsafe events run.
        with Dovetail(retries=2, retry_backoff=0) as dvt:
            events = []
            dvt.events.on(dvt.TaskEvent.QUEUED, lambda p: events.append("queued"))
            dvt.events.on(dvt.TaskEvent.STARTED, lambda p: events.append("started"))
            dvt.events.on(dvt.TaskEvent.DONE, lambda p: events.append("done"))
            dvt.events.on(dvt.TaskEvent.RETRY, lambda p: events.append("retry"))
            dvt.events.on(dvt.TaskEvent.ERROR, lambda p: events.append("error"))

            for _ in range(50):
                events.clear()
                self.assertEqual(await dvt.task.to_thread(lambda: 42), 42)
                self.assertEqual(events, ["queued", "started", "done"])

            attempts = 0

            def fail():
                nonlocal attempts
                attempts += 1
                raise ValueError("expected")

            events.clear()
            with self.assertRaises(ValueError):
                await dvt.task.to_thread(fail)
            self.assertEqual(attempts, 3)
            self.assertEqual(events, ["queued", "started", "retry", "started", "retry", "started", "error"])
