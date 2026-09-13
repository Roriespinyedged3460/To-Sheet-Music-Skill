import unittest
from fractions import Fraction
from score_events import build_score,validate_score

def score(events,part='lead'):
    return {'version':1,'title':'notation contract','tempo':171,'time':[4,4],
            'bars':[{}],'tracks':[{'bar':1,'part':part,'events':events}]}

class NotationFeatureTests(unittest.TestCase):
    def test_notation_only_retains_explicit_tab_without_claiming_playability(self):
        data=score([[0,64,.5,[[1,0]]],[.5,84,.5,[[1,20]]]])
        root,report=build_score(data,return_report=True,notation_only=True)
        self.assertEqual(report['status'],'incomplete')
        self.assertEqual([n.text for n in root.findall('part/measure/note/notations/technical/fret')],['0','20'])
        with self.assertRaises(ValueError):build_score(score([[0,64,1]]),notation_only=True)

    def test_keyboard_grace_group_does_not_consume_measured_beats(self):
        root=build_score(score([[0,83,1,None,{'grace':[[83,.25],[86,.25]]}]],'keys_rh'))
        notes=root.findall("part[@id='P4']/measure/note")
        self.assertEqual(sum(n.find('grace') is not None for n in notes),2)
        self.assertTrue(all(n.find('duration') is None for n in notes if n.find('grace') is not None))

    def test_dead_stroke_has_tab_cross_and_no_invented_candidate_pitch(self):
        data=score([[0,None,.25,[[6,0]],{'dead':True}]])
        lanes=validate_score(data)[3]
        self.assertEqual(lanes['lead'][0][2],[None])
        root,report=build_score(data,return_report=True)
        self.assertEqual(root.findtext('part/measure/note/notehead'),'x')
        self.assertNotEqual(report['status'],'passed')

    def test_mixed_pitched_and_dead_string_chord_keeps_both(self):
        data=score([[0,[48,60],.5,[[5,3],[3,5]],{'dead_tabs':[[4,0]]}]])
        root=build_score(data)
        notes=root.findall('part/measure/note[pitch]')
        self.assertEqual(len(notes),3)
        self.assertEqual(sum(n.findtext('notehead')=='x' for n in notes),1)
        self.assertEqual([n.findtext('notations/technical/string') for n in notes],['5','3','4'])

    def test_triplet_rest_keeps_exact_performed_duration(self):
        data=score([[0,64,'1/3',None,{'tuplet':[3,2]}],
                    ['1/3',None,'1/3',None,{'tuplet':[3,2]}],
                    ['2/3',67,'1/3',None,{'tuplet':[3,2]}]])
        root=build_score(data)
        div=int(root.findtext('part/measure/attributes/divisions'))
        notes=root.findall('part/measure/note')
        self.assertEqual([Fraction(n.findtext('duration'))/div for n in notes[:3]],[Fraction(1,3)]*3)
        self.assertEqual([n.findtext('time-modification/actual-notes') for n in notes[:3]],['3']*3)

    def test_missing_triplet_rest_rejected(self):
        with self.assertRaisesRegex(ValueError,'Tuplet|tuplet'):
            build_score(score([[0,64,'1/3',None,{'tuplet':[3,2]}]]))

    def test_pitched_and_dead_notes_cannot_use_same_string(self):
        with self.assertRaises(ValueError):
            build_score(score([[0,64,.5,[[1,0]],{'dead_tabs':[[1,0]]}]]))

if __name__=='__main__':unittest.main()
