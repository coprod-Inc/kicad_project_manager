"""Prepare board release notes and KiBot history entries without editing CAD files."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .setup import Change, read_file
from .sexpr import ExpressionError, atoms, forms
from .yaml_fields import ScalarBlock, SetupError, scalar

CHANGELOG = 'CHANGELOG.md'
VARIABLES = 'kibot_yaml/kibot_pre_set_text_variables.yaml'
STATE = 'kibot_resources/project_manager/release.json'
DEFAULT_NOTES = '### Fixed\n\n-   Fixes\n\n### Added\n\n-   Additions\n\n### Changed\n\n-   Changes\n\n### Removed\n\n-   Deletions'


def version_number(value: str) -> str:
    value = value.strip().removeprefix('v')
    if not re.fullmatch(r'(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})', value):
        raise SetupError('Enter a release version such as 1.0.0 (an optional leading v is accepted).')
    return value


def notes_range(text: str) -> tuple[int, int]:
    matches = list(re.finditer(r'^##\s+\[Unreleased\][^\r\n]*(?:\r?\n|$)', text, re.M | re.I))
    if len(matches) != 1:
        raise SetupError('CHANGELOG.md needs exactly one ## [Unreleased] heading.')
    start = matches[0].end()
    following = re.search(r'^(?:##\s+|\[[^\]\r\n]+\]:)', text[start:], re.M)
    return start, start + following.start() if following else len(text)


def published_versions(data: bytes | None) -> set[str]:
    return set(re.findall(r'^##\s+\[(\d+\.\d+\.\d+)\]', (data or b'').decode('utf-8-sig'), re.M))


def selected_version(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        value = json.loads(data.decode('utf-8-sig'))
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not isinstance(value.get('version'), str)):
            raise ValueError('Unsupported release record.')
        version = value['version']
        if version and version_number(version) != version:
            raise ValueError('Noncanonical release version.')
        return version
    except (ValueError, UnicodeError) as exc:
        raise SetupError(f'Could not read {STATE}; restore or correct this release-selection record.') from exc


@dataclass(frozen=True)
class ReleaseSnapshot:
    root: Path
    changelog: bytes | None
    variables: bytes | None
    state: bytes | None = None

    @property
    def notes(self) -> str:
        if self.changelog is None:
            return DEFAULT_NOTES
        text = self.changelog.decode('utf-8-sig')
        start, end = notes_range(text)
        return text[start:end].strip('\r\n').replace('\r\n', '\n')

    @property
    def pending_version(self) -> str:
        selected = selected_version(self.state)
        # Enabled history entries may have been copied from another project.
        # Only an explicit Project Manager choice identifies the next release.
        return selected if selected and selected not in published_versions(self.changelog) else ''


def read_release(root: Path) -> ReleaseSnapshot:
    snapshot = ReleaseSnapshot(root, read_file(root, CHANGELOG), read_file(root, VARIABLES), read_file(root, STATE))
    snapshot.notes  # Validate the editable range before presenting it.
    selected_version(snapshot.state)
    return snapshot


def update_notes(snapshot: ReleaseSnapshot, notes: str) -> bytes:
    notes = notes.replace('\r\n', '\n').strip('\n')
    if re.search(r'^(?:##\s+|\[[^\]\r\n]+\]:)', notes, re.M):
        raise SetupError('Edit only the content below [Unreleased]; keep release headings and link definitions outside this editor.')
    if snapshot.changelog is not None and notes == snapshot.notes:
        return snapshot.changelog
    data = snapshot.changelog or b'# Changelog\n\n## [Unreleased]\n\n'
    text = data.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    start, end = notes_range(text)
    body = notes.replace('\n', newline)
    return (text[:start] + newline + body + newline + newline + text[end:]).encode('utf-8')


def enable_history(data: bytes, version: str) -> bytes:
    """Activate the standard title/body commands, preserving all other entries."""
    version = version_number(version)
    ScalarBlock(data, 'definitions', ('GET_TITLE_CMD', 'GET_BODY_CMD'))
    text = data.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    additions = []
    for kind in ('TITLE', 'BODY'):
        pattern = re.compile(r"^(?P<indent> *)(?P<comment>#[ \t]*)?- variable:[ \t]*(?P<q>['\"]?)"
                             + re.escape(f'@RELEASE_{kind}_VAR@{version}') + r"(?P=q)[ \t]*$")
        candidates = [(i, pattern.fullmatch(line.rstrip('\r\n'))) for i, line in enumerate(lines)]
        candidates = [(i, match) for i, match in candidates if match]
        active = [(i, match) for i, match in candidates if not match['comment']]
        selected = active or candidates
        if len(selected) > 1:
            raise SetupError(f'Duplicate {kind.lower()} entries for {version}; resolve them in {VARIABLES}.')
        pair = [f"    - variable: '@RELEASE_{kind}_VAR@{version}'{newline}",
                f"      command: '@GET_{kind}_CMD@ {version}'{newline}"]
        if not selected:
            # Refuse to add a second definition hidden behind an unfamiliar form.
            if any(f'@RELEASE_{kind}_VAR@{version}' in line and not line.lstrip().startswith('#') for line in lines):
                raise SetupError(f'Unsupported {kind.lower()} entry for {version}; edit it in {VARIABLES}.')
            additions.extend(pair)
            continue
        index, match = selected[0]
        if len(match['indent']) != 4 or index + 1 >= len(lines):
            raise SetupError('Release history entries must align with the other set_text_variables list entries.')
        command = lines[index + 1].strip()
        if not match['comment'] and not lines[index + 1].startswith('      command:'):
            raise SetupError('Release history commands must be indented beneath their variable entries.')
        if match['comment']:
            if not command.startswith('#'):
                raise SetupError(f'The commented {kind.lower()} entry is missing its matching command.')
            command = command[1:].lstrip()
        if not command.startswith('command:'):
            raise SetupError(f'The {kind.lower()} entry is missing its matching command.')
        value, _ = scalar(command.removeprefix('command:').lstrip())
        if value != f'@GET_{kind}_CMD@ {version}':
            raise SetupError(f'The {kind.lower()} command for {version} is customized; review it in {VARIABLES}.')
        if match['comment']:
            lines[index:index + 2] = pair
    if additions:
        marker = next((i for i, line in enumerate(lines)
                       if re.match(r"^    - variable: ['\"]?@RELEASE_TITLE_VAR@UNRELEASED", line)), None)
        if marker is None:
            raise SetupError('This text-variable file has no supported UNRELEASED history entries. Use a compatible KiBot template.')
        lines[marker:marker] = additions + [newline]
    return ''.join(lines).encode('utf-8')


def prepare_release(snapshot: ReleaseSnapshot, version: str, notes: str) -> tuple[Change, ...]:
    version = version_number(version) if version.strip() else ''
    changes = []
    updated = update_notes(snapshot, notes)
    # Missing changelogs are created only when explicitly preparing/editing a release.
    if updated != snapshot.changelog:
        changes.append(Change(CHANGELOG, snapshot.changelog, updated))
    if version:
        if version in published_versions(snapshot.changelog):
            raise SetupError(f'Version {version} already has a released changelog section. Choose a new version.')
        if snapshot.variables is None:
            raise SetupError(f'Missing {VARIABLES}. Add it through template review before preparing release variables.')
        updated = enable_history(snapshot.variables, version)
        if updated != snapshot.variables:
            changes.append(Change(VARIABLES, snapshot.variables, updated))
    # Numbered YAML entries belong to schematic history, not to the current
    # release selection. Explicitly persist blank as well as older choices.
    selected = selected_version(snapshot.state)
    if ((selected is not None and selected != version)
            or (selected is None and (changes or version != snapshot.pending_version))):
        state = (json.dumps({'schema_version': 1, 'version': version}, indent=2) + '\n').encode('utf-8')
        changes.append(Change(STATE, snapshot.state, state))
    return tuple(changes)


def schematic_sources(root: Path, main: Path) -> dict[str, bytes]:
    """Read the selected saved hierarchy, including sheets in subdirectories."""
    root = root.resolve(strict=True)
    pending = [main]
    sources = {}
    while pending:
        path = pending.pop()
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise SetupError('A schematic sheet is outside the project; inspect its revision history in KiCad.') from exc
        if relative in sources:
            continue
        if len(sources) >= 1000:
            raise SetupError('This schematic hierarchy is too large to inspect here.')
        data = read_file(root, relative, limit=32 * 1024 * 1024)
        if data is None:
            raise SetupError(f'Save the schematic sheet {relative} before checking revision history.')
        sources[relative] = data
        try:
            for name, node in forms(data.decode('utf-8-sig'), children=True):
                if name == 'sheet':
                    for child_name, child in forms(node, children=True):
                        values = atoms(child)
                        if child_name == 'property' and len(values) >= 3 and values[1] in ('Sheetfile', 'Sheet file'):
                            pending.append((root / relative).parent / values[2])
        except (ExpressionError, UnicodeError) as exc:
            raise SetupError(f'Could not inspect saved schematic {relative}; check it in KiCad.') from exc
    return sources


def history_status(snapshot: ReleaseSnapshot, version: str, main: Path) -> str:
    version = version_number(version)
    sources = schematic_sources(snapshot.root, main)
    fields = ScalarBlock(snapshot.variables or b'', 'definitions', (),
                         optional=('RELEASE_TITLE_VAR', 'RELEASE_BODY_VAR')).values
    messages = []
    for kind in ('TITLE', 'BODY'):
        variable = (fields.get(f'RELEASE_{kind}_VAR') or f'RELEASE_{kind}_') + version
        target = '${' + variable + '}'
        found = []
        for relative, data in sources.items():
            for name, node in forms(data.decode('utf-8-sig'), children=True):
                if name in ('text', 'text_box') and len(values := atoms(node)) >= 2 and target in values[1]:
                    found.append(relative)
                    break
        messages.append(f'{target}: ' + (', '.join(found) if found else 'missing from saved schematic text'))
    return '\n'.join(messages) + '\nInspect the revision-history layout and the final generated schematic PDF. Versioned values are filled when release CI creates the dated changelog section.'
