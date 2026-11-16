"""Optional remote checks and explicit fast-forward updates of local sources."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlsplit

from .preferences import load_settings, save_settings
from .yaml_fields import SetupError

_LOCK = threading.RLock()


def _git(root: Path, *arguments: str, timeout=15) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(key, None)
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=8')
    try:
        result = subprocess.run(['git', '-C', str(root), *arguments], capture_output=True, text=True,
                                errors='replace', timeout=timeout, env=env,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError('Git is unavailable or the repository check timed out. Local sources remain usable.') from exc
    if result.returncode:
        raise SetupError((result.stderr or result.stdout or 'Git operation failed.').strip())
    return result.stdout.strip('\r\n')


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value or value.startswith('-') or any(ord(char) < 32 for char in value):
        raise SetupError('Enter an HTTPS or SSH Git repository URL.')
    if re.fullmatch(r'[\w.-]+/[\w.-]+', value):
        return f'https://github.com/{value.removesuffix(".git")}.git'
    if Path(value).is_absolute():
        return str(Path(value).resolve())
    if re.fullmatch(r'[\w.-]+@[\w.-]+:[^\s]+', value):
        return value
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise SetupError('Enter a valid repository clone URL.') from exc
    if parsed.scheme not in ('https', 'http', 'ssh', 'file') or not parsed.path:
        raise SetupError('Use a repository URL, such as https://host/team/template.git or git@host:team/template.git.')
    if parsed.password or (parsed.username and parsed.scheme in ('http', 'https')):
        raise SetupError('Use Git’s configured credentials instead of embedding credentials in the repository URL.')
    if parsed.query or parsed.fragment or (parsed.scheme != 'file' and not parsed.hostname):
        raise SetupError('Use the repository’s clone URL without query parameters or fragments.')
    return value.rstrip('/')


def repository_identity(value: str) -> tuple[str, str]:
    value = normalize_url(value)
    match = re.fullmatch(r'[\w.-]+@([\w.-]+):(.+)', value)
    if match:
        return match[1].lower(), match[2].strip('/').removesuffix('.git')
    if Path(value).is_absolute():
        return 'file', str(Path(value).resolve())
    parsed = urlsplit(value)
    if parsed.scheme == 'file':
        return 'file', str(Path(parsed.path).resolve())
    host = (parsed.hostname or '').lower()
    if parsed.port and parsed.port != {'https': 443, 'http': 80, 'ssh': 22}.get(parsed.scheme):
        host += f':{parsed.port}'
    return host, parsed.path.strip('/').removesuffix('.git')


def load_repository(root: Path, storage: Path | None = None) -> dict:
    values = load_settings(storage).get('source_repositories', {})
    value = values.get(str(root.resolve()), {}) if isinstance(values, dict) else {}
    return value if isinstance(value, dict) else {}


def save_repository(root: Path, url: str, branch: str, automatic: bool, storage: Path | None = None):
    branch = branch.strip()
    if not branch or branch.startswith('-'):
        raise SetupError('Enter the source branch, for example main.')
    if url.strip():
        _git(root, 'check-ref-format', '--branch', branch)
    values = load_settings(storage).get('source_repositories', {})
    values = dict(values) if isinstance(values, dict) else {}
    values[str(root.resolve())] = {'url': normalize_url(url) if url.strip() else '',
                                 'branch': branch, 'automatic': bool(automatic)}
    save_settings({'source_repositories': values}, storage)


def local_origin(root: Path) -> str:
    try:
        return _git(root, 'remote', 'get-url', 'origin')
    except SetupError:
        return ''


def current_branch(root: Path) -> str:
    try:
        return _git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    except SetupError:
        return ''


def _dirty_paths(root: Path) -> tuple[str, ...]:
    entries = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
    paths = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if 'R' in entry[:2] or 'C' in entry[:2]:
            if index < len(entries):
                paths.append(entries[index])
                index += 1
    return tuple(paths)


@dataclass(frozen=True)
class RepositoryStatus:
    root: Path
    branch: str
    checked_out: str
    ahead: int
    behind: int
    dirty: tuple[str, ...]
    conflicts: tuple[str, ...]

    @property
    def can_pull(self):
        return self.checked_out == self.branch and self.ahead == 0 and self.behind > 0 and not self.conflicts

    @property
    def message(self):
        if self.checked_out != self.branch:
            return f'On {self.checked_out or "detached HEAD"}; switch to {self.branch} to pull its updates.'
        if self.ahead and self.behind:
            return 'Local and remote history have diverged; update the source manually.'
        if self.conflicts:
            return 'Remote updates overlap local edits; resolve or save those edits before pulling.'
        if self.behind:
            return f'{self.behind} new commit(s) available on {self.branch}.' + (' Local edits do not overlap.' if self.dirty else '')
        if self.ahead:
            return f'{self.ahead} local commit(s) ahead of the remote.'
        return f'{self.branch} is up to date.' + (' Local edits are present.' if self.dirty else '')


def check_repository(root: Path, url: str, branch: str) -> RepositoryStatus:
    with _LOCK:
        origin = local_origin(root)
        if not origin:
            raise SetupError('Choose an existing Git checkout with an origin remote. Ordinary local folders remain supported without a Git URL.')
        if repository_identity(origin) != repository_identity(url):
            raise SetupError('The configured Git URL does not match this checkout’s origin. Choose the matching source folder or URL.')
        if not branch or branch.startswith('-'):
            raise SetupError('Enter a valid source branch.')
        _git(root, 'check-ref-format', '--branch', branch)
        tracking = f'refs/remotes/origin/{branch}'
        remote = _git(root, 'ls-remote', '--heads', 'origin', f'refs/heads/{branch}')
        match = re.search(r'^([0-9a-f]{40,64})\s+' + re.escape(f'refs/heads/{branch}') + r'$', remote, re.M)
        if not match:
            raise SetupError(f'The remote has no branch named {branch}.')
        try:
            saved = _git(root, 'rev-parse', '--verify', tracking)
        except SetupError:
            saved = ''
        if saved != match[1]:
            _git(root, 'fetch', '--no-tags', 'origin', f'+refs/heads/{branch}:{tracking}', timeout=30)
        ahead, behind = map(int, _git(root, 'rev-list', '--left-right', '--count', f'HEAD...{tracking}').split())
        dirty = _dirty_paths(root)
        incoming = _git(root, 'diff', '--name-only', '-z', f'HEAD..{tracking}').split('\0') if behind else []
        conflicts = tuple(path for path in dirty if any(other and (path == other or path.startswith(other + '/')
                                                                 or other.startswith(path + '/')) for other in incoming))
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'rebase-merge', 'rebase-apply'):
            path = Path(_git(root, 'rev-parse', '--git-path', marker))
            if (path if path.is_absolute() else root / path).exists():
                conflicts += ('An unfinished Git operation is in progress.',)
                break
        return RepositoryStatus(root, branch, current_branch(root), ahead, behind, dirty, conflicts)


def pull_repository(root: Path, url: str, branch: str) -> RepositoryStatus:
    with _LOCK:
        status = check_repository(root, url, branch)
        if not status.can_pull:
            raise SetupError(status.message)
        _git(root, 'merge', '--ff-only', f'refs/remotes/origin/{branch}', timeout=30)
        return check_repository(root, url, branch)
