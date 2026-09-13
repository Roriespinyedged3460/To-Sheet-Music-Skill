import unittest
from test_score_tools import fixture
from score_tools import apply_native_dead_notes

class NativeDeadTests(unittest.TestCase):
    def test_dead_patch_mutes_only_declared_cross_head(self):
        root=fixture();note=root.find('Score/Staff/Measure/voice/Chord/Note')
        import xml.etree.ElementTree as ET
        ET.SubElement(note,'head').text='cross'
        data={'tracks':[{'bar':1,'part':'lead','events':[[0,None,.5,[[1,0]],{'dead':True}]]}]}
        result=apply_native_dead_notes(root,data)
        self.assertEqual(result['muted_notes'],1)
        self.assertEqual(note.findtext('dead'),'1');self.assertEqual(note.findtext('play'),'0')

    def test_missing_or_wrong_tab_cross_refuses_to_patch(self):
        data={'tracks':[{'bar':1,'part':'lead','events':[[0,None,.5,[[2,0]],{'dead':True}]]}]}
        with self.assertRaises(ValueError):apply_native_dead_notes(fixture(),data)

    def test_normal_native_notes_unchanged_without_dead_events(self):
        root=fixture()
        import xml.etree.ElementTree as ET
        before=ET.tostring(root);apply_native_dead_notes(root,{'tracks':[]})
        self.assertEqual(ET.tostring(root),before)

    def test_musescore4_staff_definitions_without_ids_are_supported(self):
        root=fixture();root.find('Score/Part/Staff').attrib.pop('id')
        import xml.etree.ElementTree as ET
        ET.SubElement(root.find('Score/Staff/Measure/voice/Chord/Note'),'head').text='cross'
        data={'tracks':[{'bar':1,'part':'lead','events':[[0,None,.5,[[1,0]],{'dead':True}]]}]}
        self.assertEqual(apply_native_dead_notes(root,data)['muted_notes'],1)

if __name__=='__main__':unittest.main()
