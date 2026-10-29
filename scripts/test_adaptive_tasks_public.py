"""Public task contracts with invented data; no native workers or network calls."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / p) for p in (
    'services/praxis45', 'services/praxis-switchboard', 'skills/orca-dispatch')]
import adaptive_general as general
import adaptive_task_example as example
import adaptive_tasks as tasks
from adaptive_contract import sha


class PublicTasks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'occurrence'
        self.workspace = self.base / 'outputs'
        self.prepared = example.prepare(self.root, self.workspace,
                                        'http://127.0.0.1:8123/fixture')
        self.document = json.loads((self.root / 'input-logical.json').read_text())
        self.compiled = {'logical_graph': self.document['graph_definitions'][0]}
        self.assignments = json.loads((self.root / 'assignments.json').read_text())
        store = self.root / 'fixture-switchboard/state.json'
        self.authority = {'path': str(store), 'sha256': sha(store)}

    def authorize(self):
        return tasks.authorize_scopes(self.compiled, self.root,
                                      self.assignments, self.authority)

    def node(self, name):
        return next(n for n in self.compiled['logical_graph']['nodes'] if n['id'] == name)

    def test_public_fixture_authorizes_and_compiles_without_native_launch(self):
        self.assertEqual(self.prepared['native_launches'], 0)
        self.assertEqual(len(self.authorize()['nodes']), 5)
        dispatcher = general.load_dispatcher()
        roster = {'Avery': SimpleNamespace(name='Avery', route='code-analysis')}
        boundary = SimpleNamespace(
            registry=SimpleNamespace(seats=lambda: roster),
            RESERVED={'named_contract', 'seat_contract', 'supplied_context'},
            NamedLaunchError=ValueError, seat_for_unit=lambda unit: roster[unit['seat']])
        with mock.patch.object(dispatcher, 'named_launch_module', return_value=boundary), \
                mock.patch.object(general, 'load_dispatcher', return_value=dispatcher):
            receipt = general.compile_entry(
                self.root / 'input-logical.json', self.root / 'input-policy.json',
                'general_task_example', self.root, fixture_authority=self.authority)
        self.assertEqual(receipt['native_launches'], 0)
        self.assertEqual(receipt['layers'][1], ['code', 'site', 'slides', 'writing'])
        self.assertEqual(receipt['layers'][2], ['publish_site'])

    def test_changed_request_and_changed_node_cannot_reuse_assignment(self):
        self.authorize()
        changed = copy.deepcopy(self.node('code'))
        changed['checks'][0]['argv'].append('uncommissioned')
        with self.assertRaisesRegex(ValueError, 'assignment differs'):
            tasks.assigned_scope(self.compiled, changed, self.root, self.authority)
        (self.root / 'fixture-switchboard/request.md').write_text('Prepare only.')
        with self.assertRaisesRegex(ValueError, 'authority_refused'):
            tasks.assigned_scope(self.compiled, self.node('code'), self.root, self.authority)

    def test_publication_needs_explicit_assignment_authority(self):
        self.assignments['publish_site']['authority']['publication_authorized'] = False
        with self.assertRaisesRegex(ValueError, 'request-bound authority'):
            self.authorize()

    def test_references_are_retained_but_operating_rules_remain_current(self):
        rules = self.base / 'GLOBAL-RULES.md'
        rules.write_text('current rules')
        self.document['source_bindings']['rules'] = {
            'path': str(rules), 'sha256': sha(rules), 'role': 'operating'}
        captured = tasks.snapshots(self.document, self.root)
        brief = captured['source_bindings']['brief']
        retained = Path(brief['path']).read_bytes()
        Path(brief['original_path']).write_text('changed reference')
        rules.write_text('new current rules')
        self.assertEqual(Path(brief['path']).read_bytes(), retained)
        self.assertTrue(tasks.source_drift(captured)[0]['changed'])
        self.assertEqual(tasks.operating_at_launch(captured)['rules']['sha256'], sha(rules))

    def test_outputs_checks_and_retained_bytes_are_independent_evidence(self):
        node = self.node('writing')
        path = Path(node['outputs'][0]['path'])
        path.write_text('Fictional project briefing.')
        outputs = tasks.validate_outputs(node)
        retained = tasks.retain_outputs(self.root, node, outputs)
        path.write_text('changed after retention')
        self.assertEqual(sha(retained[0]['path']), outputs[0]['sha256'])
        node['checks'] = [{'argv': [sys.executable, '-c', 'print("verified")'],
                          'cwd': str(self.workspace), 'timeout_seconds': 5}]
        checks = tasks.run_checks(self.root, node)
        result = json.loads(Path(checks[0]['path']).read_text())
        self.assertEqual((result['returncode'], result['stdout']), (0, 'verified\n'))
        with self.assertRaisesRegex(ValueError, 'original check evidence exists'):
            tasks.run_checks(self.root, node)

    def test_publication_readback_requires_exact_declared_bytes(self):
        node = self.node('publish_site')
        path = Path(node['outputs'][0]['path'])
        path.write_text('<title>Fixture</title><body>Fictional project.</body>')
        receipt = path.parent / 'receipt.txt'
        receipt.write_text('Synthetic publication receipt')
        declaration = node['scope']['publication']
        artifact = {'publication': {
            'argv': declaration['argv'], 'cwd': declaration['cwd'],
            'url': declaration['readback_url'], 'receipt': str(receipt),
            'readback_sha256': sha(path)}}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = path.read_bytes()
        with mock.patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            checked = tasks.validate_publication(node, artifact, self.root)
            self.assertEqual(checked['state'], 'published_readback_verified')
            response.read.return_value = b'different published version'
            with self.assertRaisesRegex(ValueError, 'does not match'):
                tasks.validate_publication(node, artifact, self.root)

    def test_effective_adaptive_and_task_prompts_preserve_requested_publication(self):
        import runner
        self.assertIn('A direct request to publish is authorization', runner.ADAPTIVE_WORKER_PREAMBLE)
        self.assertNotIn('This surface has no contract for external actions', runner.ADAPTIVE_WORKER_PREAMBLE)
        self.assertIn('Prepare-only requests grant no publication authority', runner.ADAPTIVE_WORKER_PREAMBLE)
        self.assertIn('This surface has no contract for external actions', runner.WORKER_PREAMBLE)
        dispatcher = general.load_dispatcher()
        unit = {'id': 'publication_fixture', 'seat': 'Avery', 'task_class': 'code-analysis',
                'spec': tasks.task_spec(self.node('publish_site'), 'Publish the fixture.',
                                        self.root / 'artifact.json', [], [], {}),
                'return_contract': {'max_chars': 2000}}
        boundary = SimpleNamespace(construct=lambda unit, agent: ('Synthetic seat charter.', None),
                                   NamedLaunchError=ValueError)
        with mock.patch.object(dispatcher, 'named_launch_module', return_value=boundary):
            prompt = dispatcher.unit_spec({'id': 'fixture'}, unit, {'agent': 'codex'})
        self.assertIn('A read-only or prepare-only unit remains so', prompt)
        self.assertIn('Publication is permitted only when the original request', prompt)
        self.assertNotIn('does not widen the read-only effects', prompt)


if __name__ == '__main__':
    unittest.main()
