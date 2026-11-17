from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from kipy.project_types import TextVariables
from kipy.proto.common.types import MapMergeMode, project_settings_pb2

from project_manager.context import ContextError, context_from_document, initialize_project_variables


class ProjectVariableTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'sample.kicad_pro').write_text('{"text_variables":{"CUSTOM":"kept"}}\n')
        (self.root / 'sample.kicad_pcb').write_text('(kicad_pcb)\n')
        self.document = NS(project=NS(path=str(self.root), name='sample'), board_filename='sample.kicad_pcb')
        self.context = context_from_document(self.document, '10.0.7')
        self.existing = TextVariables(project_settings_pb2.TextVariables())
        self.existing.variables = {'CUSTOM': 'kept', 'BOARD_NAME': 'User title'}
        self.project = Mock()
        self.project.get_text_variables.return_value = self.existing
        self.client = Mock()
        self.client.get_open_documents.return_value = [self.document]
        self.client.get_project.return_value = self.project
        self.factory = Mock(return_value=self.client)

    def initialize(self, defaults=None):
        return initialize_project_variables(self.context, {'REVIEWER': ''} if defaults is None else defaults,
                                            '/tmp/intended-editor.sock', environ={'KICAD_API_TOKEN': 'fake-token'},
                                            client_factory=self.factory)

    def test_missing_reviewer_is_merged_through_the_intended_editor(self):
        before = (self.root / 'sample.kicad_pro').read_bytes()
        self.assertEqual(self.initialize(), ('REVIEWER',))
        args, kwargs = self.project.set_text_variables.call_args
        self.assertEqual(args[0].variables, {'REVIEWER': ''})
        self.assertEqual(kwargs['merge_mode'], MapMergeMode.MMM_MERGE)
        self.assertEqual(self.existing.variables, {'CUSTOM': 'kept', 'BOARD_NAME': 'User title'})
        self.assertEqual((self.root / 'sample.kicad_pro').read_bytes(), before)
        self.assertEqual(self.factory.call_args.kwargs['socket_path'], 'ipc:///tmp/intended-editor.sock')
        self.assertEqual(self.factory.call_args.kwargs['kicad_token'], 'fake-token')

    def test_existing_blank_or_named_reviewer_is_never_overwritten(self):
        for value in ('', 'Designer review'):
            with self.subTest(value=value):
                self.existing['REVIEWER'] = value
                self.assertEqual(self.initialize({'REVIEWER': 'YAML default'}), ())
                self.project.set_text_variables.assert_not_called()
                self.assertEqual(self.existing['REVIEWER'], value)

    def test_metadata_default_is_used_when_the_variable_is_missing(self):
        self.assertEqual(self.initialize({'REVIEWER': 'Reviewer name'}), ('REVIEWER',))
        self.assertEqual(self.project.set_text_variables.call_args.args[0].variables,
                         {'REVIEWER': 'Reviewer name'})

    def test_project_switch_before_or_during_initialization_is_rejected(self):
        other = NS(project=NS(path=str(self.root), name='other'), board_filename='other.kicad_pcb')
        for replies in (([other],), ([self.document], [other]), ([],), ([self.document, other],)):
            with self.subTest(replies=replies):
                self.client.get_open_documents.side_effect = replies
                with self.assertRaisesRegex(ContextError, 'active KiCad project changed'):
                    self.initialize()
                self.project.set_text_variables.assert_not_called()

    def test_transport_failure_gives_manual_recovery_without_leaking_credentials(self):
        self.project.get_text_variables.side_effect = RuntimeError('fake-token')
        with self.assertRaises(ContextError) as result:
            self.initialize()
        self.assertIn('REVIEWER', str(result.exception))
        self.assertNotIn('fake-token', str(result.exception))
        self.project.set_text_variables.assert_not_called()

    def test_missing_endpoint_and_standalone_board_are_not_guessed(self):
        with self.assertRaisesRegex(ContextError, 'Launch Project Manager'):
            initialize_project_variables(self.context, {'REVIEWER': ''}, environ={}, client_factory=self.factory)
        standalone = context_from_document(NS(project=NS(path='', name=''),
                                              board_filename=str(self.root / 'sample.kicad_pcb')), '10.0.7')
        with self.assertRaisesRegex(ContextError, 'Save the KiCad project'):
            initialize_project_variables(standalone, {'REVIEWER': ''}, environ={}, client_factory=self.factory)
        self.factory.assert_not_called()
