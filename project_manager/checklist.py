"""A bundled, local review checklist; no Git, standards catalog or online services."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .release import schematic_sources
from .setup import Change, commit_changes, read_file
from .yaml_fields import SetupError

STATE = 'kibot_resources/project_manager/review.json'
ITEMS = (
    ('design-intent', 'Designer', 'Review the block diagram and electrical intent where applicable.'),
    ('rules', 'Designer', 'Check design rules, net classifications and isolation requirements.'),
    ('bom', 'Designer', 'Consolidate the BOM; check part numbers, fitted status and availability.'),
    ('pinouts', 'Designer', 'Verify critical pinouts, polarity, footprints and component ratings.'),
    ('schematic', 'Designer', 'Check schematic organization, sheet numbering and revision information.'),
    ('erc', 'Designer', 'Run ERC and resolve errors or document justified exceptions.'),
    ('stackup', 'Designer', 'Verify stackup, copper layers, thickness and surface requirements.'),
    ('pad-connections', 'Designer', 'Review thermal reliefs, pad connections and teardrops where appropriate.'),
    ('test-access', 'Designer', 'Check test-point spacing, programming access and rework access.'),
    ('silkscreen', 'Designer', 'Inspect both silkscreens and fabrication layers for readability and overlaps.'),
    ('dimensions', 'Designer', 'Complete dimensions and check mounting holes, connectors and mechanical fit.'),
    ('markings', 'Designer', 'Check layer names, layer identifiers and board markings where required.'),
    ('drc', 'Designer', 'Run DRC and resolve errors or document justified exceptions.'),
    ('generation', 'Outputs', 'Generate outputs from the intended saved revision and review the reports.'),
    ('manufacturing', 'Outputs', 'Inspect final copper, mask, silkscreen, outline, drill and slot outputs.'),
    ('assembly', 'Outputs', 'Check BOM, placement file, assembly variant and orientation conventions.'),
    ('origins', 'Outputs', 'Confirm Gerbers, drills and component placement share the same origin.'),
    ('independent-electrical', 'Independent review', 'Have another reviewer check electrical intent, pinouts and critical layout.'),
    ('independent-mechanical', 'Independent review', 'Have another reviewer inspect mechanical fit, 3D views and assembly access.'),
    ('findings', 'Independent review', 'Resolve review findings and repeat the affected checks after changes.'),
    ('supplier-review', 'Before ordering', 'Review supplier DFM and placement previews where available.'),
    ('order-options', 'Before ordering', 'Match order settings and manufacturing files to the reviewed board.'),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def empty_state() -> dict:
    return {'schema_version': 1, 'reviewer': '', 'items': {}, 'completed_review': None}


@dataclass(frozen=True)
class ChecklistSnapshot:
    root: Path
    original: bytes | None
    state: dict


def load(root: Path) -> ChecklistSnapshot:
    root = root.resolve(strict=True)
    data = read_file(root, STATE)
    try:
        state = json.loads(data.decode('utf-8-sig')) if data is not None else empty_state()
        if not isinstance(state, dict) or state.get('schema_version') != 1:
            raise ValueError('Unsupported review record version')
        if not isinstance(state.get('reviewer'), str) or not isinstance(state.get('items'), dict):
            raise ValueError('Invalid review record')
        for value in state['items'].values():
            if (not isinstance(value, dict) or not isinstance(value.get('checked'), bool)
                    or any(not isinstance(value.get(key), str) for key in ('reviewer', 'comment', 'checked_at'))):
                raise ValueError('Invalid checklist item')
        completed = state.get('completed_review')
        if completed is not None and (not isinstance(completed, dict)
                                     or not isinstance(completed.get('reviewer'), str)
                                     or not isinstance(completed.get('date'), str)
                                     or not isinstance(completed.get('files'), dict)
                                     or any(not isinstance(key, str) or not isinstance(value, str)
                                            for key, value in completed['files'].items())):
            raise ValueError('Invalid completed review')
    except (ValueError, UnicodeError, TypeError) as exc:
        raise SetupError(f'Could not read {STATE}: {exc}. Keep this file and restore a valid copy before editing the checklist.') from exc
    return ChecklistSnapshot(root, data, state)


def item_value(state: dict, identity: str) -> dict:
    return state['items'].get(identity, {'checked': False, 'reviewer': '', 'comment': '', 'checked_at': ''})


def update_item(state: dict, identity: str, *, checked: bool, reviewer: str, comment: str) -> dict:
    if identity not in {item[0] for item in ITEMS}:
        raise SetupError('Unknown checklist item.')
    if checked and not reviewer.strip():
        raise SetupError('Enter your reviewer name before checking an item.')
    updated = deepcopy(state)
    previous = item_value(state, identity)
    value = dict(previous, checked=checked, comment=comment)
    if checked and not previous['checked']:
        value.update(reviewer=reviewer.strip(), checked_at=now())
    elif not checked:
        value.update(reviewer='', checked_at='')
    updated['items'][identity] = value
    updated['reviewer'] = reviewer.strip()
    return updated


def update_all(state: dict, *, checked: bool, reviewer: str) -> dict:
    updated = deepcopy(state)
    for identity, _, _ in ITEMS:
        updated = update_item(updated, identity, checked=checked, reviewer=reviewer,
                              comment=item_value(updated, identity)['comment'])
    if not checked:
        updated['completed_review'] = None
    return updated


def design_fingerprint(root: Path, board: Path, project: Path | None, schematic: Path | None) -> dict[str, str]:
    root = root.resolve(strict=True)
    files = [board]
    if project is not None:
        files.extend((project, project.with_suffix('.kicad_dru')))
    sources = schematic_sources(root, schematic) if schematic is not None and schematic.is_file() else {}
    # Saved project/design sources and export configuration matter; editor
    # preferences, checklist answers and generated outputs do not expire review.
    for path in sorted((root / 'kibot_yaml').glob('*.yaml')):
        files.append(path)
    for path in files:
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise SetupError('The review files must be inside the active project.') from exc
        data = read_file(root, relative, limit=128 * 1024 * 1024)
        if data is not None:
            sources[relative] = data
        elif path == board:
            raise SetupError('Save the board before recording a completed review.')
    return {relative: hashlib.sha256(data).hexdigest() for relative, data in sorted(sources.items())}


def complete(state: dict, fingerprint: dict[str, str], reviewer: str) -> dict:
    if not reviewer.strip():
        raise SetupError('Enter the name of the reviewer recording this completed review.')
    if not all(item_value(state, identity)['checked'] for identity, _, _ in ITEMS):
        raise SetupError('Complete the checklist before recording the finished review. Use comments for items that are not applicable.')
    updated = deepcopy(state)
    updated['reviewer'] = reviewer.strip()
    updated['completed_review'] = {'reviewer': reviewer.strip(), 'date': now(), 'files': fingerprint}
    return updated


def changed_since_review(state: dict, fingerprint: dict[str, str]) -> bool:
    completed = state.get('completed_review')
    return completed is not None and completed['files'] != fingerprint


def save(snapshot: ChecklistSnapshot, state: dict, storage: Path | None = None) -> ChecklistSnapshot:
    data = (json.dumps(state, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    if data != snapshot.original:
        commit_changes(snapshot.root, (Change(STATE, snapshot.original, data),), storage)
    return ChecklistSnapshot(snapshot.root, data, deepcopy(state))
