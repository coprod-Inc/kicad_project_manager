"""Remote checks and explicit pulls against disposable local Git repositories."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from project_manager.source_repository import (check_repository, load_repository, normalize_url,
                                               pull_repository, repository_identity, save_repository)
from project_manager.yaml_fields import SetupError


class SourceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / 'remote.git'
        self.author = self.base / 'author'
        self.local = self.base / 'local'
        self.git(self.base, 'init', '--bare', '--initial-branch=main', str(self.remote))
        self.git(self.base, 'clone', str(self.remote), str(self.author))
        self.configure(self.author)
        (self.author / 'template.txt').write_text('initial')
        self.git(self.author, 'add', '.')
        self.git(self.author, 'commit', '-m', 'Fixture initial')
        self.git(self.author, 'push', 'origin', 'main')
        self.git(self.base, 'clone', str(self.remote), str(self.local))
        self.configure(self.local)

    @staticmethod
    def git(root, *arguments):
        return subprocess.run(['git', '-C', str(root), *arguments], check=True, capture_output=True,
                              text=True).stdout.strip()

    def configure(self, root):
        self.git(root, 'config', 'user.email', 'fixture@example.org')
        self.git(root, 'config', 'user.name', 'Fixture')
        self.git(root, 'config', 'commit.gpgsign', 'false')

    def advance(self):
        (self.author / 'template.txt').write_text('remote update')
        self.git(self.author, 'commit', '-am', 'Fixture update')
        self.git(self.author, 'push', 'origin', 'main')

    def check(self):
        return check_repository(self.local, str(self.remote), 'main')

    def test_check_fetches_but_does_not_change_checkout_then_explicit_pull_updates(self):
        head = self.git(self.local, 'rev-parse', 'HEAD')
        self.advance()
        status = self.check()
        self.assertEqual(status.behind, 1)
        self.assertTrue(status.can_pull)
        self.assertEqual(self.git(self.local, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.local / 'template.txt').read_text(), 'initial')
        status = pull_repository(self.local, str(self.remote), 'main')
        self.assertEqual(status.behind, 0)
        self.assertEqual((self.local / 'template.txt').read_text(), 'remote update')

    def test_overlapping_local_changes_refuse_pull_without_losing_edits(self):
        self.advance()
        (self.local / 'template.txt').write_text('my local changes')
        self.assertFalse(self.check().can_pull)
        with self.assertRaises(SetupError):
            pull_repository(self.local, str(self.remote), 'main')
        self.assertEqual((self.local / 'template.txt').read_text(), 'my local changes')

    def test_unrelated_untracked_file_survives_pull(self):
        (self.local / 'my-notes.txt').write_text('keep me')
        self.advance()
        self.assertTrue(self.check().can_pull)
        pull_repository(self.local, str(self.remote), 'main')
        self.assertEqual((self.local / 'my-notes.txt').read_text(), 'keep me')

    def test_different_branch_and_divergence_are_not_automatically_resolved(self):
        self.git(self.local, 'switch', '-c', 'my-work')
        self.advance()
        self.assertFalse(self.check().can_pull)
        self.git(self.local, 'switch', 'main')
        (self.local / 'own.txt').write_text('local work')
        self.git(self.local, 'add', '.')
        self.git(self.local, 'commit', '-m', 'Fixture local work')
        status = self.check()
        self.assertEqual((status.ahead, status.behind), (1, 1))
        self.assertFalse(status.can_pull)

    def test_origin_mismatch_and_missing_branch_are_reported(self):
        with self.assertRaisesRegex(SetupError, 'does not match'):
            check_repository(self.local, 'https://example.org/other.git', 'main')
        with self.assertRaisesRegex(SetupError, 'no branch'):
            check_repository(self.local, str(self.remote), 'missing')

    def test_optional_configuration_does_not_require_git_for_local_folders(self):
        storage = self.base / 'settings'
        save_repository(self.local, str(self.remote), 'main', True, storage)
        self.assertTrue(load_repository(self.local, storage)['automatic'])
        save_repository(self.base, '', 'main', False, storage)
        self.assertEqual(load_repository(self.base, storage)['url'], '')

    def test_url_validation_and_transport_identity(self):
        self.assertEqual(normalize_url('team/template'), 'https://github.com/team/template.git')
        self.assertEqual(repository_identity('git@github.com:team/template.git'),
                         repository_identity('https://github.com/team/template'))
        for value in ('--upload-pack=bad', 'https://user:secret@example.org/template',
                      'https://example.org/x?q=1', 'https://example.org:bad/template'):
            with self.assertRaises(SetupError):
                normalize_url(value)
