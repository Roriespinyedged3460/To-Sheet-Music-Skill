import unittest
import numpy as np
from transcription_evidence import pitch_source_report, retain_attacks, onset_pitch_candidates


class TranscriptionEvidenceTests(unittest.TestCase):
    def test_new_attack_survives_a_louder_sustained_neighbour(self):
        times=np.arange(100)/1000
        energy=np.vstack([np.full(100,10.),np.where(times>=.05,2.,0.)])
        result=onset_pitch_candidates(times,energy,[60,62],[.05])
        self.assertEqual(result['attacks'][0]['candidates'][0]['pitch'],62)
        self.assertEqual(len(result['attacks'][0]['candidates']),2)

    def test_no_new_energy_is_unknown_not_a_new_note(self):
        times=np.arange(100)/1000;energy=np.ones((2,100))
        result=onset_pitch_candidates(times,energy,[60,62],[.05])
        self.assertEqual(result['attacks'][0]['status'],'insufficient_attack_evidence')

    def test_role_label_does_not_override_audible_high_piano(self):
        sr=8000; t=np.arange(sr)/sr
        report=pitch_source_report({'piano':.001*np.sin(2*np.pi*100*t),
                                    'other':.1*np.sin(2*np.pi*1318.51*t)},sr,[88])
        self.assertEqual(report['pitches'][0]['strongest_source'],'other')
        self.assertFalse(report['musical_accuracy_verified'])

    def test_octaves_are_measured_separately(self):
        sr=8000; t=np.arange(sr)/sr
        r=pitch_source_report({'bass':np.sin(2*np.pi*220*t)},sr,[45,57])
        self.assertGreater(r['pitches'][1]['sources']['bass']['power'],
                           r['pitches'][0]['sources']['bass']['power']*100)

    def test_silent_candidates_are_unobserved_not_verified_rests(self):
        r=pitch_source_report({'piano':np.zeros(8000)},8000,[88])
        self.assertEqual(r['pitches'][0]['status'],'unobserved')
        self.assertFalse(r['silence_verified'])

    def test_unequal_source_lengths_rejected(self):
        with self.assertRaises(ValueError):
            pitch_source_report({'a':np.zeros(100),'b':np.zeros(200)},8000,[60])

    def test_sixteenths_same_note_and_high_register_survive(self):
        raw=[[1,1.08,90,.2],[1.0877,1.17,90,.15],[1.1754,1.26,88,.7]]
        out=retain_attacks(raw,playable_range=[21,88])
        self.assertEqual(len(out['candidates']),3)
        self.assertEqual(raw[0][2],90)
        self.assertEqual(len(out['review']),2)

    def test_only_near_identical_detector_duplicates_merge(self):
        raw=[[1,1.2,64,.9],[1.004,1.21,64,.8],[1.005,1.2,67,.8]]
        r=retain_attacks(raw)
        self.assertEqual(len(r['candidates']),2)
        self.assertEqual(len(r['duplicates']),1)
        self.assertEqual(r['raw_count'],3)


if __name__=='__main__':unittest.main()
