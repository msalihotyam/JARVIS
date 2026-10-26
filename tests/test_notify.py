import json
import unittest
from datetime import datetime, timezone

from jarvis.notify import NotificationCore
from jarvis.work_ledger import EventKind


AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
POINTS = {
    'questions_ready': 'normal', 'readback_ready': 'normal',
    'plan_ready': 'high', 'needs_input': 'high', 'blocked:budget': 'high',
    'slice_failed_after_cap': 'high', 'approval_required': 'high',
    'awaiting_acceptance': 'normal', 'done': 'normal', 'interrupted': 'normal',
    'slice_started': 'low', 'slice_accepted': 'low',
}


class FakeStore:
    def __init__(self):
        self.events = []
        self.outbox = []
        self.keys = set()

    def read(self, run_id):
        return [event for event in self.events if event.run_id == run_id]

    def write(self, event, *, idempotency_key, severity):
        if idempotency_key in self.keys:
            raise ValueError('duplicate identity')
        if any(e.run_id == event.run_id and e.seq == event.seq for e in self.events):
            raise ValueError('sequence collision')
        self.events.append(event)
        self.outbox.append((event.run_id, event.seq, event.kind.value, severity))
        self.keys.add(idempotency_key)
        return True


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.core = NotificationCore(self.store.write, self.store.read)

    def send(self, point='done', key='one', **kwargs):
        values = dict(seq=1, at=AT)
        values.update(kwargs)
        return self.core.notify('run-1', point, key, **values)

    def test_all_points_have_exactly_one_pair(self):
        for seq, (point, importance) in enumerate(POINTS.items(), 1):
            with self.subTest(point=point):
                self.assertTrue(self.send(point, seq=seq))
                event = self.store.events[-1]
                payload = json.loads(event.payload_json)
                self.assertEqual(payload['importance'], importance)
                self.assertEqual(payload['point'], point)
                self.assertEqual(payload['dedupe_key'], 'one')
                self.assertTrue(payload['title'])
                self.assertTrue(payload['text'])
                self.assertNotIn('?', payload['title'])
                expected = 'progress' if point.startswith('slice_') and point != 'slice_failed_after_cap' else point
                self.assertEqual(event.kind.value, expected)
                self.assertEqual(self.store.outbox[-1], ('run-1', seq, expected, importance))
        self.assertEqual(len(self.store.events), 12)
        self.assertEqual(len(self.store.outbox), 12)

    def test_replay_after_restart_ignores_new_sequence_time_and_importance(self):
        self.assertTrue(self.send())
        self.core = NotificationCore(self.store.write, self.store.read)
        self.assertFalse(self.send(seq=2, at=AT.replace(year=2027), importance='high'))
        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(len(self.store.outbox), 1)

    def test_identity_includes_run_point_and_key(self):
        self.send()
        self.send(key='two', seq=2)
        self.send(point='needs_input', seq=3)
        self.core.notify('run-2', 'done', 'one', seq=1, at=AT)
        self.assertEqual(len(self.store.keys), 4)
        self.assertEqual(len(self.store.outbox), 4)

    def test_invalid_points_fail_before_store(self):
        for point in ('unknown', 'progress', 'run_state', '', None, []):
            with self.subTest(point=point), self.assertRaises(ValueError):
                self.send(point=point)
        self.assertEqual(self.store.events, [])

    def test_bad_importance_fails_even_for_replay(self):
        self.send()
        for importance in ('urgent', '', 1, [], True):
            with self.subTest(importance=importance), self.assertRaises(ValueError):
                self.send(importance=importance)
        self.assertEqual(len(self.store.events), 1)

    def test_explicit_importance(self):
        for seq, importance in enumerate(('low', 'normal', 'high'), 1):
            self.send(key=importance, seq=seq, importance=importance)
            self.assertEqual(self.store.outbox[-1][-1], importance)

    def test_bad_event_fields_and_key_do_not_write(self):
        for values in ({'seq': 0}, {'seq': True}, {'at': AT.replace(tzinfo=None)}, {'step_key': ''}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.send(**values)
        for key in ('', ' ', None):
            with self.assertRaises(ValueError):
                self.send(key=key)
        self.assertEqual(self.store.events, [])

    def test_failure_is_retryable(self):
        def fail(*args, **kwargs):
            raise RuntimeError('write failed')
        self.core = NotificationCore(fail, self.store.read)
        with self.assertRaisesRegex(RuntimeError, 'write failed'):
            self.send()
        self.core = NotificationCore(self.store.write, self.store.read)
        self.assertTrue(self.send())
        self.assertEqual(len(self.store.outbox), 1)

    def test_sequence_collision_propagates(self):
        self.send()
        with self.assertRaisesRegex(ValueError, 'sequence collision'):
            self.send(key='two')
        self.assertEqual(len(self.store.outbox), 1)

    def test_progress_points_are_distinct(self):
        self.send(point='slice_started', step_key='slice-1')
        self.send(point='slice_accepted', seq=2, step_key='slice-1')
        self.assertEqual([e.kind for e in self.store.events], [EventKind.PROGRESS] * 2)
        self.assertEqual([e.step_key for e in self.store.events], ['slice-1'] * 2)


if __name__ == '__main__':
    unittest.main()
