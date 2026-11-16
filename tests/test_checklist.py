"""Local review persistence and a single optional completed-review snapshot."""
from copy import deepcopy
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from project_manager import checklist
from project_manager.yaml_fields import SetupError
from tests.fixtures import sample_project


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = sample_project(self.base / 'board', workflow=False)
        self.board = self.root / 'sample.kicad_pcb'
        self.project = self.root / 'sample.kicad_pro'
        self.schematic = self.root / 'sample.kicad_sch'
        self.schematic.write_text('(kicad_sch (sheet (property "Sheetfile" "history.kicad_sch")))')
        (self.root / 'history.kicad_sch').write_text('(kicad_sch (text "Revision history"))')
        self.storage = self.base / 'backups'

    def fingerprint(self):
        return checklist.design_fingerprint(self.root, self.board, self.project, self.schematic)

    def all_checked(self):
        state = checklist.empty_state()
        for identity, _, _ in checklist.ITEMS:
            state = checklist.update_item(state, identity, checked=True, reviewer='Alice', comment='Reviewed')
        return state

    def test_opening_does_not_write_and_answers_survive_reopen_and_copy_without_git(self):
        snapshot = checklist.load(self.root)
        self.assertIsNone(snapshot.original)
        self.assertFalse((self.root / checklist.STATE).exists())
        state = checklist.update_item(snapshot.state, 'pinouts', checked=True, reviewer='Alice', comment='Checked connector drawing')
        saved = checklist.save(snapshot, state, self.storage)
        self.assertEqual(checklist.load(self.root).state, saved.state)
        copied = self.base / 'copied-project'
        shutil.copytree(self.root, copied)
        self.assertEqual(checklist.load(copied).state, saved.state)
        self.assertFalse((copied / '.git').exists())

    def test_later_comments_and_other_reviewers_do_not_rewrite_an_existing_check(self):
        state = checklist.update_item(checklist.empty_state(), 'pinouts', checked=True, reviewer='Alice', comment='Pin 1 verified')
        old = deepcopy(state['items']['pinouts'])
        state = checklist.update_item(state, 'pinouts', checked=True, reviewer='Bob', comment='Datasheet attached')
        self.assertEqual(state['items']['pinouts']['reviewer'], 'Alice')
        self.assertEqual(state['items']['pinouts']['checked_at'], old['checked_at'])
        state = checklist.update_item(state, 'drc', checked=True, reviewer='Bob', comment='')
        self.assertEqual(state['items']['drc']['reviewer'], 'Bob')
        self.assertEqual(state['items']['pinouts']['comment'], 'Datasheet attached')

    def test_edits_between_checks_do_not_expire_or_clear_them(self):
        snapshot = checklist.load(self.root)
        state = checklist.update_item(snapshot.state, 'pad-connections', checked=True, reviewer='Alice', comment='Teardrops added')
        snapshot = checklist.save(snapshot, state, self.storage)
        self.board.write_text('(kicad_pcb (gr_text "Moved silkscreen"))')
        self.assertFalse(checklist.changed_since_review(snapshot.state, self.fingerprint()))
        self.assertTrue(checklist.load(self.root).state['items']['pad-connections']['checked'])

    def test_completed_review_detects_design_changes_but_not_answers_or_editor_preferences(self):
        state = checklist.complete(self.all_checked(), self.fingerprint(), 'Bob')
        snapshot = checklist.save(checklist.load(self.root), state, self.storage)
        self.assertFalse(checklist.changed_since_review(state, self.fingerprint()))
        self.board.write_bytes(self.board.read_bytes())
        (self.root / 'sample.kicad_prl').write_text('{"zoom": 3}')
        (self.root / 'Manufacturing').mkdir()
        (self.root / 'Manufacturing/output.csv').write_text('new render')
        state = checklist.update_item(state, 'pinouts', checked=True, reviewer='Bob', comment='Extra note')
        checklist.save(snapshot, state, self.storage)
        self.assertFalse(checklist.changed_since_review(state, self.fingerprint()))
        (self.root / 'history.kicad_sch').write_text('(kicad_sch (text "Changed history"))')
        self.assertTrue(checklist.changed_since_review(state, self.fingerprint()))
        self.assertTrue(all(checklist.item_value(state, identity)['checked'] for identity, _, _ in checklist.ITEMS))

    def test_file_additions_removals_and_export_configuration_changes_are_detected_after_completion(self):
        state = checklist.complete(self.all_checked(), self.fingerprint(), 'Alice')
        path = self.project.with_suffix('.kicad_dru')
        path.write_text('(version 1)')
        self.assertTrue(checklist.changed_since_review(state, self.fingerprint()))
        path.unlink()
        self.assertFalse(checklist.changed_since_review(state, self.fingerprint()))
        (self.root / 'kibot_yaml/origin.yaml').write_text('use_aux_axis_as_origin: true')
        self.assertTrue(checklist.changed_since_review(state, self.fingerprint()))

    def test_reviewer_and_completion_are_explicit(self):
        with self.assertRaisesRegex(SetupError, 'reviewer name'):
            checklist.update_item(checklist.empty_state(), 'pinouts', checked=True, reviewer='', comment='')
        with self.assertRaisesRegex(SetupError, 'Complete the checklist'):
            checklist.complete(checklist.empty_state(), self.fingerprint(), 'Alice')
        original = self.all_checked()
        changed = checklist.update_item(original, 'pinouts', checked=False, reviewer='Alice', comment='Revisit')
        self.assertFalse(changed['items']['pinouts']['checked'])
        self.assertEqual(changed['items']['pinouts']['checked_at'], '')
        self.assertTrue(original['items']['pinouts']['checked'])

    def test_concurrent_review_edits_are_not_overwritten(self):
        first = checklist.load(self.root)
        second = checklist.load(self.root)
        alice = checklist.update_item(first.state, 'pinouts', checked=True, reviewer='Alice', comment='')
        bob = checklist.update_item(second.state, 'drc', checked=True, reviewer='Bob', comment='')
        checklist.save(first, alice, self.storage)
        with self.assertRaisesRegex(SetupError, 'changed during apply'):
            checklist.save(second, bob, self.storage)
        self.assertEqual(checklist.load(self.root).state, alice)

    def test_invalid_records_are_preserved(self):
        path = self.root / checklist.STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        for data in (b'not json', b'{"schema_version": 99}',
                     b'{"schema_version":1,"reviewer":"A","items":{"drc":{}}}'):
            path.write_bytes(data)
            with self.assertRaisesRegex(SetupError, 'Could not read'):
                checklist.load(self.root)
            self.assertEqual(path.read_bytes(), data)

    def test_new_location_ignores_old_root_record(self):
        old = self.root / 'kpm-review.json'
        old.write_text('leave this file alone')
        snapshot = checklist.load(self.root)
        self.assertIsNone(snapshot.original)
        state = checklist.update_all(snapshot.state, checked=True, reviewer='Alice')
        checklist.save(snapshot, state, self.storage)
        self.assertTrue((self.root / 'kibot_resources/project_manager/review.json').is_file())
        self.assertEqual(old.read_text(), 'leave this file alone')

    def test_bulk_checks_preserve_existing_authorship_and_comments_and_reset_completion(self):
        state = checklist.update_item(checklist.empty_state(), 'pinouts', checked=True,
                                      reviewer='Alice', comment='Pin 1 verified')
        state = checklist.update_all(state, checked=True, reviewer='Bob')
        self.assertEqual(state['items']['pinouts']['reviewer'], 'Alice')
        self.assertEqual(state['items']['drc']['reviewer'], 'Bob')
        state = checklist.complete(state, self.fingerprint(), 'Bob')
        state = checklist.update_all(state, checked=False, reviewer='')
        self.assertFalse(state.get('completed_review'))
        self.assertFalse(any(item['checked'] for item in state['items'].values()))
        self.assertEqual(state['items']['pinouts']['comment'], 'Pin 1 verified')


if __name__ == '__main__':
    unittest.main()
