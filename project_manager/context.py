"""Read project identity from the launching editor, never from cwd or a glob."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
import os
from .compat import SUPPORTED_MAJORS


class ContextError(RuntimeError):
    """An actionable connection or project-selection problem."""


def initialize_project_variables(context: ProjectContext, defaults: Mapping[str, str],
                                 socket_path: str | None = None, *,
                                 environ: Mapping[str, str] | None = None,
                                 client_factory: Callable[..., Any] | None = None) -> tuple[str, ...]:
    """Seed missing values through the live editor so later saves retain them."""
    if not context.project_saved or not context.project_file:
        raise ContextError('Save the KiCad project before initializing its text variables.')
    if any(not isinstance(key, str) or not key or not isinstance(value, str)
           for key, value in defaults.items()):
        raise ContextError('Project text-variable defaults must be names and text values.')
    env = os.environ if environ is None else environ
    endpoint = socket_path or env.get('KICAD_API_SOCKET')
    if not endpoint:
        raise ContextError('Launch Project Manager from PCB Editor to initialize project text variables.')
    if not endpoint.startswith('ipc://'):
        endpoint = 'ipc://' + endpoint
    try:
        if client_factory is None:
            from kipy import KiCad
            client_factory = KiCad
        from kipy.proto.common.types import DocumentType, MapMergeMode
        from kipy.proto.common.types import project_settings_pb2
        from kipy.project_types import TextVariables
        client = client_factory(socket_path=endpoint, kicad_token=env.get('KICAD_API_TOKEN', ''),
                                client_name='kicad-project-manager', timeout_ms=2500)

        def matching_document():
            documents = client.get_open_documents(DocumentType.DOCTYPE_PCB)
            if len(documents) != 1:
                raise ContextError('The active KiCad project changed. Refresh before initializing text variables.')
            current = context_from_document(documents[0], context.kicad_version)
            if (current.project_file, current.board_file) != (context.project_file, context.board_file):
                raise ContextError('The active KiCad project changed. Refresh before initializing text variables.')
            return documents[0]

        project = client.get_project(matching_document())
        existing = project.get_text_variables()
        missing = {key: value for key, value in defaults.items() if key not in existing}
        if missing:
            matching_document()
            variables = TextVariables(project_settings_pb2.TextVariables())
            variables.variables = missing
            project.set_text_variables(variables, merge_mode=MapMergeMode.MMM_MERGE)
        return tuple(missing)
    except ContextError:
        raise
    except Exception as exc:
        raise ContextError('Could not initialize project text variables through KiCad. '
                           'Add the missing REVIEWER variable under Project Properties → Text Variables, '
                           'then save the project. Existing values were not replaced.') from exc


@dataclass(frozen=True)
class ProjectContext:
    kicad_version: str
    project_name: str
    project_directory: str
    project_file: str
    board_file: str
    project_saved: bool
    board_saved: bool
    kibot_config_present: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def context_from_document(document: Any, version: str) -> ProjectContext:
    """Resolve only paths explicitly identified by KiCad's DocumentSpecifier."""
    name = document.project.name
    directory = document.project.path
    filename = document.board_filename
    if not filename:
        raise ContextError("Save the board, then click Refresh.")
    board = Path(filename)
    if not board.is_absolute():
        if not directory or not Path(directory).is_absolute():
            raise ContextError("KiCad has not supplied a saved board location. Save the project first.")
        board = Path(directory) / board
    # Standalone boards may have no project. Do not invent a .kicad_pro path.
    project_file = ""
    if name and directory:
        project_name = name if name.endswith(".kicad_pro") else name + ".kicad_pro"
        project_file = str(Path(directory) / project_name)
    setup_directory = Path(project_file).parent if project_file else board.parent
    return ProjectContext(
        kicad_version=version,
        project_name=name.removesuffix(".kicad_pro") if name else "Standalone board",
        project_directory=directory or str(board.parent),
        project_file=project_file,
        board_file=str(board),
        project_saved=bool(project_file and Path(project_file).is_file()),
        board_saved=board.is_file(),
        kibot_config_present=(setup_directory / "kibot_yaml" / "kibot_main.yaml").is_file(),
    )


def detect_context(
    socket_path: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> ProjectContext:
    env = os.environ if environ is None else environ
    endpoint = socket_path or env.get("KICAD_API_SOCKET")
    if not endpoint:
        raise ContextError(
            "Open this plugin from PCB Editor → Tools → External Plugins. "
            "For terminal development, specify --socket for the intended editor."
        )
    if not endpoint.startswith("ipc://"):
        endpoint = "ipc://" + endpoint
    try:
        if client_factory is None:
            from kipy import KiCad
            client_factory = KiCad
        from kipy.proto.common.types import DocumentType
        client = client_factory(
            socket_path=endpoint,
            kicad_token=env.get("KICAD_API_TOKEN", ""),
            client_name="kicad-project-manager",
            timeout_ms=2500,
        )
        version = client.get_version()
        if version.major not in SUPPORTED_MAJORS:
            raise ContextError(
                f"This release supports KiCad 9 and 10; the connected editor is {version.full_version}."
            )
        documents = client.get_open_documents(DocumentType.DOCTYPE_PCB)
        if not documents:
            raise ContextError("No board is open in this editor. Open a board, then click Refresh.")
        if len(documents) != 1:
            raise ContextError("More than one board was reported. Launch from the intended PCB Editor.")
        return context_from_document(documents[0], version.full_version)
    except ContextError:
        raise
    except ImportError as exc:
        raise ContextError(
            "The KiCad Python dependency is missing. Recreate this plugin's environment "
            "in Preferences → Plugins."
        ) from exc
    except Exception as exc:
        # Do not include raw transport errors, which may contain endpoint/credential data.
        raise ContextError(
            "Could not read the PCB Editor. Enable the KiCad API in Preferences → Plugins, "
            "close any open modal dialog, and try Refresh."
        ) from exc
