"""Only disposable state; diagnostics must not copy arbitrary task text."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay.manager import Manager
from tests.test_manager import FakeClient


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / 'state', self.client)
        self.addCleanup(self.manager.close)
        self.task = self.manager.create_task('Example', self.project, 'Inspect only', max_minutes=5, direct=False)

    def test_diagnostics_are_readonly_allowlisted_and_budget_is_explained(self):
        task = self.manager._task(self.task['id'])
        secret = 'PRIVATE_SENTINEL_DO_NOT_EXPORT'
        task.update(title=secret, goal=secret, error=secret, mode=secret, state='paused',
                    thread_id=secret, elapsed_seconds=300.425, messages=[{'id': secret, 'text': secret}],
                    source_snapshot={'thread_id': secret}, requirements=[secret])
        self.manager._save(task, 'test_private_event', {'text': secret})
        before = copy.deepcopy(self.manager.get_task(task['id']))
        with patch('relay.transport.CodexClient', side_effect=AssertionError('No native calls')):
            report = self.manager.diagnostics()
            destination = self.root / 'diagnostics.json'
            self.manager.export_diagnostics(destination)
        serialized = json.dumps(report)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(task['id'], serialized)
        self.assertNotIn(str(self.project), serialized)
        self.assertNotIn(secret, destination.read_text(encoding='utf-8'))
        self.assertEqual(report['tasks'][0]['mode'], 'unknown')
        self.assertTrue(report['tasks'][0]['budget']['minutes_reached'])
        self.assertEqual(report['tasks'][0]['next_action'], 'adjust_budget')
        self.assertEqual(before, self.manager.get_task(task['id']))
        self.assertEqual([], self.client.calls)

    def test_diagnostics_never_overwrite_or_write_inside_state_or_projects(self):
        destination = self.root / 'existing.json'
        destination.write_text('Keep', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.manager.export_diagnostics(destination)
        self.assertEqual(destination.read_text(encoding='utf-8'), 'Keep')
        for folder in (self.manager.root, self.project):
            with self.assertRaises(ValueError):
                self.manager.export_diagnostics(folder / 'diagnostics.json')
            self.assertFalse((folder / 'diagnostics.json').exists())

    def test_unknown_ownership_takes_precedence_over_budget_in_next_action(self):
        task = self.manager._task(self.task['id'])
        task.update(state='needs_reconcile', elapsed_seconds=400, intent={'kind': 'turn'})
        self.manager._save(task)
        self.assertEqual('reconcile_before_work', self.manager.diagnostics()['tasks'][0]['next_action'])

    def test_markdown_export_preserves_reply_but_is_not_a_new_inference(self):
        task = self.manager._task(self.task['id'])
        task.update(last_message='中文结果\n```python\nprint(1)\n```', last_message_kind='work')
        self.manager._save(task)
        path = self.root / 'report.md'
        self.manager.export_task(task['id'], path)
        result = path.read_text(encoding='utf-8')
        self.assertFalse(result.startswith('{'))
        self.assertIn('print(1)', result)
        self.assertIn('中文结果', result)
        with self.assertRaises(ValueError):
            self.manager.export_task(task['id'], path)
        self.assertEqual(result, path.read_text(encoding='utf-8'))
        self.assertEqual([], self.client.calls)


if __name__ == '__main__':
    unittest.main()
