"""controllers.sync.SyncOwner: one SYNC producer, whatever started the last
one; canopen's start() replaces its handle without stopping the old task."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from puckutility.controllers.sync import ADC_SYNC_HZ, SyncOwner  # noqa: E402


class FakeSync:
    """canopen.sync.SyncProducer's start/stop/_task, with every task kept."""

    def __init__(self):
        self._task = None
        self.period = None
        self.tasks = []

    def start(self, period=None):
        if period is not None:
            self.period = period
        self._task = mock.Mock(period=self.period)
        self.tasks.append(self._task)

    def stop(self):
        if self._task is not None:
            self._task.stop()


class TestSyncOwner(unittest.TestCase):

    def setUp(self):
        self.net = mock.Mock(sync=FakeSync())
        self.owner = SyncOwner(lambda: self.net)

    def running(self):
        return [t for t in self.net.sync.tasks if not t.stop.called]

    def test_start_then_off(self):
        self.owner.set_sync_rate(ADC_SYNC_HZ)
        self.assertEqual(self.owner.sync_rate, ADC_SYNC_HZ)
        self.assertEqual([t.period for t in self.running()], [1.0 / ADC_SYNC_HZ])
        self.owner.set_sync_rate(0)
        self.assertEqual(self.owner.sync_rate, 0)
        self.assertEqual(self.running(), [])
        self.assertIsNone(self.net.sync._task)     # stopped is not running

    def test_a_rate_change_leaves_one_producer(self):
        self.owner.set_sync_rate(1000)
        self.owner.set_sync_rate(ADC_SYNC_HZ)
        self.assertEqual([t.period for t in self.running()], [1.0 / ADC_SYNC_HZ])

    def test_stops_a_producer_someone_else_started(self):
        self.net.sync.start(0.001)
        self.owner.set_sync_rate(0)
        self.assertEqual(self.running(), [])

    def test_no_network(self):
        owner = SyncOwner(lambda: None)
        owner.set_sync_rate(ADC_SYNC_HZ)
        self.assertEqual(owner.sync_rate, 0)

    def test_a_stop_that_raises_is_harmless(self):
        self.owner.set_sync_rate(ADC_SYNC_HZ)
        self.net.sync._task.stop.side_effect = OSError('bus gone')
        self.owner.set_sync_rate(ADC_SYNC_HZ)
        self.assertEqual(self.owner.sync_rate, ADC_SYNC_HZ)


if __name__ == '__main__':
    unittest.main()
