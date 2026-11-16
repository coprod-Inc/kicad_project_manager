"""Synthetic board release preparation, history inspection and guarded writes."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import shutil

from project_manager import release, setup
from project_manager.templates import canonical
from project_manager.yaml_fields import SetupError
from tests.fixtures import sample_project
from tests.test_templates import VARIABLES, VAR_PATH, HISTORY

CHANGELOG = '# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Pending change\n\n## [0.5.0] - 2026-01-01\n\n- Old change\n\n[Unreleased]: https://example.org/compare\n'


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = sample_project(Path(self.temp.name) / 'project')
        (self.root / release.CHANGELOG).write_text(CHANGELOG, encoding='utf-8')
        (self.root / release.VARIABLES).write_text(VARIABLES, encoding='utf-8')

    def test_unreleased_edit_preserves_history_links_bom_and_line_endings(self):
        path = self.root / release.CHANGELOG
        original = ('\ufeff' + CHANGELOG).replace('\n', '\r\n').encode()
        path.write_bytes(original)
        snapshot = release.read_release(self.root)
        self.assertEqual(release.update_notes(snapshot, snapshot.notes), original)
        updated = release.update_notes(snapshot, '### Added\n\n- Café display')
        self.assertTrue(updated.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(updated[updated.index(b'## [0.5.0]'):], original[original.index(b'## [0.5.0]'):])
        self.assertIn('Café display\r\n'.encode(), updated)
        self.assertNotIn(b'Pending change', updated)

    def test_activate_four_example_lines_and_preserve_other_history(self):
        data = VARIABLES.replace("    - variable: '@RELEASE_TITLE_VAR@UNRELEASED'", HISTORY + "    - variable: '@RELEASE_TITLE_VAR@UNRELEASED'").encode()
        for original in (data, b'\xef\xbb\xbf' + data.replace(b'\n', b'\r\n')):
            with self.subTest(crlf=b'\r\n' in original):
                updated = release.enable_history(original, 'v1.0.0')
                self.assertIn(b"    - variable: '@RELEASE_TITLE_VAR@1.0.0'", updated)
                self.assertIn(b"      command: '@GET_BODY_CMD@ 1.0.0'", updated)
                self.assertIn(HISTORY.strip().encode().replace(b'\n', b'\r\n') if b'\r\n' in original else HISTORY.strip().encode(), updated)
                self.assertEqual(release.enable_history(updated, '1.0.0'), updated)
                self.assertEqual(canonical(VAR_PATH, original), canonical(VAR_PATH, updated))

    def test_new_version_is_inserted_without_removing_previous_entries(self):
        original = release.enable_history(VARIABLES.encode(), '1.0.0')
        updated = release.enable_history(original, '2.3.4')
        self.assertIn(b'@GET_TITLE_CMD@ 1.0.0', updated)
        self.assertEqual(updated.count(b"- variable: '@RELEASE_TITLE_VAR@2.3.4'"), 1)
        self.assertEqual(updated.count(b"- variable: '@RELEASE_BODY_VAR@2.3.4'"), 1)
        self.assertIn(b'@GET_BODY_CMD@ Unreleased', updated)

    def test_custom_commands_duplicate_entries_and_invalid_versions_are_rejected(self):
        for value in ('1.0.0; touch /tmp/no', '1.0', '01.0.0', '1.2.3\n4.5.6', ''):
            with self.subTest(value=value), self.assertRaises(SetupError):
                release.enable_history(VARIABLES.encode(), value)
        custom = VARIABLES.replace('@GET_BODY_CMD@ 1.0.0', '@GET_BODY_CMD@ other')
        with self.assertRaisesRegex(SetupError, 'customized'):
            release.enable_history(custom.encode(), '1.0.0')
        active = release.enable_history(VARIABLES.encode(), '1.0.0')
        duplicate = active.replace(b"    - variable: '@RELEASE_TITLE_VAR@UNRELEASED'",
                                   b"    - variable: '@RELEASE_TITLE_VAR@1.0.0'\n      command: '@GET_TITLE_CMD@ 1.0.0'\n    - variable: '@RELEASE_TITLE_VAR@UNRELEASED'")
        with self.assertRaisesRegex(SetupError, 'Duplicate'):
            release.enable_history(duplicate, '1.0.0')

    def test_notes_cannot_insert_release_sections_and_released_version_cannot_be_reused(self):
        snapshot = release.read_release(self.root)
        with self.assertRaisesRegex(SetupError, 'only the content'):
            release.prepare_release(snapshot, '', '## [2.0.0]\n- Change')
        with self.assertRaisesRegex(SetupError, 'already has a released'):
            release.prepare_release(snapshot, '0.5.0', snapshot.notes)
        self.assertEqual(release.prepare_release(snapshot, '', snapshot.notes), ())

    def test_release_and_setup_share_a_preview_backup_and_external_edit_guard(self):
        snapshot = release.read_release(self.root)
        setup_snapshot = setup.read_setup(self.root)
        changes = release.prepare_release(snapshot, '1.0.0', '### Fixed\n\n- Corrected layout')
        plan = setup.prepare(setup_snapshot, setup_snapshot.metadata, 'CHECKED')
        checks = ((release.CHANGELOG, snapshot.changelog), (release.VARIABLES, snapshot.variables),
                  (release.STATE, snapshot.state))
        plan = replace(plan, changes=plan.changes + changes, checks=checks)
        self.assertEqual((self.root / release.CHANGELOG).read_bytes(), snapshot.changelog)
        self.assertIn('Corrected layout', plan.diff())
        path = self.root / release.VARIABLES
        path.write_bytes(snapshot.variables + b'\n# external edit\n')
        with self.assertRaisesRegex(SetupError, 'changed since'):
            setup.apply(plan, Path(self.temp.name) / 'backups')
        self.assertEqual(setup.read_setup(self.root), setup_snapshot)
        path.write_bytes(snapshot.variables)
        backup = setup.apply(plan, Path(self.temp.name) / 'backups')
        self.assertEqual((backup / release.CHANGELOG).read_bytes(), snapshot.changelog)
        self.assertIn('## [Unreleased]', (self.root / release.CHANGELOG).read_text())
        self.assertNotIn('## [1.0.0]', (self.root / release.CHANGELOG).read_text())
        self.assertEqual(release.read_release(self.root).pending_version, '1.0.0')

    def test_missing_changelog_can_be_created_and_missing_variables_do_not_block_notes(self):
        (self.root / release.CHANGELOG).unlink()
        (self.root / release.VARIABLES).unlink()
        snapshot = release.read_release(self.root)
        changes = release.prepare_release(snapshot, '', '### Added\n\n- Initial board')
        self.assertEqual({change.relative for change in changes}, {release.CHANGELOG, release.STATE})
        self.assertTrue(all(change.before is None for change in changes))
        with self.assertRaisesRegex(SetupError, 'Missing'):
            release.prepare_release(snapshot, '1.0.0', snapshot.notes)

    def test_selected_older_version_and_unreleased_survive_reopen_without_changing_history(self):
        variables = release.enable_history(release.enable_history(VARIABLES.encode(), '1.0.0'), '1.0.2')
        (self.root / release.VARIABLES).write_bytes(variables)
        self.assertEqual(release.read_release(self.root).pending_version, '')
        self.assertFalse((self.root / release.STATE).exists())
        for version in ('1.0.0', '', '1.0.2', ''):
            snapshot = release.read_release(self.root)
            changes = release.prepare_release(snapshot, version, snapshot.notes)
            self.assertEqual({change.relative for change in changes}, {release.STATE})
            setup.commit_changes(self.root, changes, Path(self.temp.name) / 'backups')
            reopened = release.read_release(self.root)
            self.assertEqual(reopened.pending_version, version)
            self.assertEqual(release.prepare_release(reopened, version, reopened.notes), ())
            self.assertEqual((self.root / release.VARIABLES).read_bytes(), variables)
        copied = Path(self.temp.name) / 'copied-project'
        shutil.copytree(self.root, copied)
        self.assertEqual(release.read_release(copied).pending_version, '')

    def test_completed_release_returns_to_unreleased_instead_of_an_older_enabled_entry(self):
        variables = release.enable_history(VARIABLES.encode(), '1.0.0')
        (self.root / release.VARIABLES).write_bytes(variables)
        snapshot = release.read_release(self.root)
        setup.commit_changes(self.root, release.prepare_release(snapshot, '1.0.2', snapshot.notes),
                             Path(self.temp.name) / 'backups')
        self.assertEqual(release.read_release(self.root).pending_version, '1.0.2')
        (self.root / release.CHANGELOG).write_text(CHANGELOG + '\n## [1.0.2] - 2026-10-24\n\n- Released\n')
        self.assertEqual(release.read_release(self.root).pending_version, '')

    def test_adopting_an_already_released_board_keeps_history_and_starts_without_a_pending_release(self):
        history = ('# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Work in progress\n\n'
                   '## [1.0.2] - 2026-09-01\n\n- Existing release\n\n'
                   '## [1.0.0] - 2026-07-01\n\n- First release\n')
        variables = release.enable_history(release.enable_history(VARIABLES.encode(), '1.0.0'), '1.0.2')
        (self.root / release.CHANGELOG).write_text(history)
        (self.root / release.VARIABLES).write_bytes(variables)
        snapshot = release.read_release(self.root)
        self.assertEqual(snapshot.pending_version, '')
        self.assertFalse((self.root / release.STATE).exists())
        with self.assertRaisesRegex(SetupError, 'already has a released'):
            release.prepare_release(snapshot, '1.0.2', snapshot.notes)
        changes = release.prepare_release(snapshot, '1.1.0', snapshot.notes)
        setup.commit_changes(self.root, changes, Path(self.temp.name) / 'backups')
        reopened = release.read_release(self.root)
        self.assertEqual(reopened.pending_version, '1.1.0')
        self.assertEqual((self.root / release.CHANGELOG).read_text(), history)
        for version in ('1.0.0', '1.0.2', '1.1.0'):
            self.assertIn(f'@GET_TITLE_CMD@ {version}'.encode(), reopened.variables)
            self.assertIn(f'@GET_BODY_CMD@ {version}'.encode(), reopened.variables)

    def test_invalid_selection_records_are_not_overwritten(self):
        path = self.root / release.STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        for data in (b'not json', b'{"schema_version":2,"version":""}',
                     b'{"schema_version":1,"version":"1.2"}', b'{"schema_version":1,"version":null}'):
            path.write_bytes(data)
            with self.assertRaisesRegex(SetupError, 'Could not read'):
                release.read_release(self.root)
            self.assertEqual(path.read_bytes(), data)

    def test_release_selection_uses_the_same_external_edit_guard_as_notes(self):
        snapshot = release.read_release(self.root)
        setup_snapshot = setup.read_setup(self.root)
        plan = replace(setup.prepare(setup_snapshot, setup_snapshot.metadata, 'CHECKED'),
                       changes=release.prepare_release(snapshot, '1.0.0', snapshot.notes),
                       checks=((release.STATE, snapshot.state),))
        path = self.root / release.STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        external = b'{"schema_version":1,"version":"1.0.2"}\n'
        path.write_bytes(external)
        with self.assertRaisesRegex(SetupError, 'changed since'):
            setup.apply(plan, Path(self.temp.name) / 'backups')
        self.assertEqual(path.read_bytes(), external)
        self.assertEqual((self.root / release.VARIABLES).read_bytes(), snapshot.variables)

    def test_schematic_history_follows_hierarchy_and_only_checks_visible_text(self):
        main = self.root / 'sample.kicad_sch'
        main.write_text('(kicad_sch (sheet (property "Sheetfile" "sheets/history.kicad_sch")))')
        child = self.root / 'sheets/history.kicad_sch'
        child.parent.mkdir()
        child.write_text('(kicad_sch (text_box "${RELEASE_TITLE_1.0.0}") '
                         '(text "${RELEASE_BODY_1.0.0}"))')
        snapshot = release.read_release(self.root)
        message = release.history_status(snapshot, '1.0.0', main)
        self.assertNotIn('missing', message)
        self.assertIn('sheets/history.kicad_sch', message)
        child.write_text('(kicad_sch (property "Unused" "${RELEASE_BODY_1.0.0}") '
                         '(text "${RELEASE_TITLE_1.0.0}"))')
        self.assertIn('${RELEASE_BODY_1.0.0}: missing', release.history_status(snapshot, '1.0.0', main))


if __name__ == '__main__':
    unittest.main()
