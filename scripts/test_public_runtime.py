"""Offline public-projection checks: no host, account or network operations."""
import ast
import base64
import copy
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / path) for path in (
    'services/praxis45', 'services/praxis-switchboard', 'skills/orca-dispatch',
    'skills/chief-of-staff', 'hooks', 'scripts')]


class PublicRuntime(unittest.TestCase):
    def test_every_python_source_parses(self):
        for path in ROOT.rglob('*.py'):
            with self.subTest(path=str(path.relative_to(ROOT))):
                compile(path.read_text(), str(path), 'exec')

    def test_core_import_closure(self):
        for name in ('ordinary', 'acquisition', 'immutable_code', 'source_access',
                     'operation_evidence', 'operation_recount', 'adaptive_general',
                     'room_adaptive', 'room_attractor', 'switchboard', 'dispatcher',
                     'release_support', 'router', 'agent_home_guard'):
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_query_plan_is_bounded_and_input_tampering_refuses(self):
        import acquisition
        bound = acquisition.bind_user('synthetic', 'What does the supplied record say?',
                                      ['Claim ' + str(i) for i in range(6)])
        self.assertLessEqual(len(bound['plan']['queries']), 3)
        self.assertEqual(acquisition.reconstruct(bound), bound['plan'])
        bound['user']['claims'][0] = 'Changed claim'
        with self.assertRaisesRegex(ValueError, 'drifted'):
            acquisition.reconstruct(bound)

    def test_overflow_claims_remain_explicitly_unchecked(self):
        import ordinary
        accepted, overflow = ordinary.split_claims(['Synthetic claim ' + str(i) for i in range(8)])
        self.assertEqual(len(accepted['claims']), 6)
        self.assertEqual(len(overflow), 2)
        self.assertTrue(all(row['processing_status'] == 'not_checked' and
                            row['verdict'] == 'unresolved' for row in overflow))

    def test_claim_input_rejects_duplicate_ids(self):
        from native_capabilities import validate_claim_input
        value = {'claims': [{'claim_id': 'same', 'claim': 'a'}, {'claim_id': 'same', 'claim': 'b'}],
                 'sources': [], 'evidence_policy': 'bounded-public-web'}
        with self.assertRaises(ValueError):
            validate_claim_input(value, discovery=True)

    def test_private_and_credential_urls_refuse_without_request(self):
        from source_access import public_url
        for url in ('file:///tmp/input', 'http://example.org', 'https://127.0.0.1/a',
                    'https://user:secret@example.org/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)

    def test_immutable_code_hash_mismatch_refuses(self):
        from immutable_code import authenticate
        raw = b'print("synthetic")\n'
        path = 'services/praxis45/example.py'
        archive = {'commit': 'synthetic', 'files': {path: {
            'content': base64.b64encode(raw).decode(),
            'git_blob': hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()}}}
        binding = {'path': '/synthetic/' + path, 'sha256': hashlib.sha256(raw).hexdigest()}
        self.assertEqual(authenticate(binding, 'synthetic', archive), raw)
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            authenticate(dict(binding, sha256='0'*64), 'synthetic', archive)

    def test_effect_images_require_declared_target_and_postimage(self):
        from adaptive_general import verify_effect_images
        effect = {'operation': 'orca_terminal_rename', 'target': 'terminal_fixture',
                  'expected': {'title': 'before'}, 'value': 'after'}
        row = {'preimage': {'target': 'terminal_fixture', 'title': 'before'},
               'postimage': {'target': 'terminal_fixture', 'title': 'after'}}
        verify_effect_images(effect, row)
        broken = copy.deepcopy(row); broken['postimage']['target'] = 'other'
        with self.assertRaises(ValueError):
            verify_effect_images(effect, broken)

    def test_unsupported_reader_profile_fails_closed(self):
        from read_evidence import validate_read
        with self.assertRaisesRegex(ValueError, 'outside this public package'):
            validate_read({}, {'reading_kind': 'cloud'})

    def test_personal_entrypoints_are_absent_and_refused(self):
        import local, registry
        self.assertEqual(local.PROBES, ())
        with self.assertRaisesRegex(registry.RoutineError, 'excluded'):
            registry.routine_spec('any-routine')
        for name in ('adaptive_routines.py','adaptive_shadow.py','adaptive_qualification.py',
                     'adaptive_admission.py','adaptive_effects.py'):
            self.assertFalse((ROOT/'services/praxis45'/name).exists())

    def test_context_discovery_is_research_and_development_only(self):
        import project_rooms
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ('projects','papers','Personal','Administration','Executive Education','COURSE101'):
                (root/name).mkdir()
            self.assertEqual({kind for kind,path in project_rooms.context_roots(root)}, {'project','paper'})
            with self.assertRaises(Exception):
                project_rooms.loader_name('course')

    def test_hash_ledger_detects_mutation_and_reordering(self):
        import research_trace as trace
        rows=[]
        for index in (1,2):
            row={'schema_version':trace.SCHEMA_VERSION,'sequence':index,
                'previous_hash':rows[-1]['record_hash'] if rows else trace.ZERO_HASH,
                'evidence':{'artifact_sha256':str(index)*64}}
            row['record_hash']=trace._record_hash(row)
            rows.append(row)
        self.assertTrue(trace._validate_chain(rows).ok)
        changed=copy.deepcopy(rows); changed[0]['evidence']['artifact_sha256']='0'*64
        self.assertFalse(trace._validate_chain(changed).ok)
        self.assertFalse(trace._validate_chain(list(reversed(rows))).ok)

    @mock.patch('named_launch.seat_for_unit', return_value=None)
    def test_schedule_uses_explicit_timezone_and_rejects_invalid_configuration(self, synthetic_seat):
        import dispatcher as d
        plan = {'schema_version': d.SCHEMA_VERSION, 'id': 'synthetic-schedule',
                'objective': 'Synthetic validation only',
                'units': [{'id': 'one', 'task_class': 'research', 'spec': 'No effects',
                           'data_domains': []}],
                'schedule': {'timezone': 'Asia/Tokyo', 'local_times': ['09:00']}}
        d.validate_plan(plan)
        when = datetime(2026, 1, 2, 1, tzinfo=timezone.utc)
        self.assertEqual(d.routine_run_key(plan, when=when),
                         'synthetic-schedule/2026-01-02T0900+0900')
        plan['schedule']['timezone'] = 'UTC'
        d.validate_plan(plan)
        self.assertEqual(d.routine_run_key(plan, when=when),
                         'synthetic-schedule/2026-01-01T0900+0000')
        for invalid in (None, '', 123, '../UTC', 'invalid/timezone'):
            plan['schedule']['timezone'] = invalid
            with self.subTest(timezone=invalid):
                with self.assertRaisesRegex(d.DispatchError, 'timezone'):
                    d.validate_plan(plan)
                with self.assertRaisesRegex(d.DispatchError, 'timezone'):
                    d.routine_run_key(plan, when=when)
        plan['schedule'] = {'timezone': 'UTC', 'local_times': ['25:00']}
        with self.assertRaisesRegex(d.DispatchError, 'schedule time'):
            d.validate_plan(plan)

    def test_planning_artifact_root_requires_configuration_and_contains_outputs(self):
        import room_planning as planning
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            note = root/'context.md'; note.write_text('---\ntitle: Synthetic\n---\n')
            project = SimpleNamespace(note=note)
            with mock.patch.dict('os.environ', {}, clear=True):
                with self.assertRaisesRegex(planning.PlanningError, 'PRAXIS_ARTIFACT_ROOT'):
                    planning._reader_folder(project)
            with mock.patch.dict('os.environ', {'PRAXIS_ARTIFACT_ROOT': str(root)}, clear=True):
                self.assertEqual(planning._reader_folder(project), (root/'planning').resolve())
                note.write_text('---\nfolder: /outside-configured-root\n---\n')
                with self.assertRaisesRegex(planning.PlanningError, 'outside'):
                    planning._reader_folder(project)

    def test_configured_content_roots_preserve_home_guard(self):
        import agent_home_guard as guard
        env = {'PRAXIS_ARTIFACT_ROOT': '/synthetic/artifacts'}
        for assignments in ({'CODEX_HOME': '/synthetic/artifacts/runtime'},
                            {'HOME': ''}, {'HOME': '/synthetic/other'}):
            self.assertTrue(guard.check(assignments, environ=env, home='/synthetic/home'))
        self.assertFalse(guard.check({'HOME': '/synthetic/home'}, environ=env,
                                    home='/synthetic/home'))

    def test_discovered_development_context_satisfies_planning_identity(self):
        import project_rooms, room_planning, rooms
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            note = root/'system/praxis/Praxis Dev.md'
            note.parent.mkdir(parents=True)
            note.write_text('# Praxis Dev\nSynthetic context only.\n')
            (root/'system/memory/institution').mkdir(parents=True)
            room = rooms.Room('praxis-dev-room', 'Development', 'Chair', [], root/'record.md',
                              planning_template=room_planning.TEMPLATE)
            with mock.patch.dict('os.environ', {'VAULT_ROOT': str(root)}, clear=True):
                project = project_rooms.resolve_room_context('Praxis Dev', room, root)
                room_planning._eligible(room, project)
                wrong = project_rooms.ProjectContext('Other', note, 'project')
                with self.assertRaises(room_planning.PlanningError):
                    room_planning._eligible(room, wrong)

    def test_excluded_routine_acknowledgement_does_not_claim_execution(self):
        import registry, routing
        req = routing.Request('synthetic', routing.Sender('synthetic'), 'excluded',
                              [routing.ROUTINE_NAME], routine=registry.RoutineSpec())
        self.assertEqual(routing.acknowledgement(req),
                         'Not dispatched. Routine entrypoints are excluded from this public runtime.')

    def test_projected_hashes_match_provenance(self):
        manifest = json.loads((ROOT/'runtime-provenance.json').read_text())
        for row in manifest['files']:
            with self.subTest(path=row['path']):
                self.assertEqual(hashlib.sha256((ROOT/row['path']).read_bytes()).hexdigest(),
                                 row['projected_sha256'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
