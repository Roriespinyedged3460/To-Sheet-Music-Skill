"""Inspect alternate stem routes and retain dense, unquantized note candidates.

Spectral power is a routing diagnostic, not instrument identity or note truth.
Use the same gain and sample interval for every source. Harmonics, ringing,
and leakage require contextual review before accepting a transcription.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np


def onset_pitch_candidates(times, energy, midi_pitches, onsets, *, lookback=.04, attack_window=.055, top_k=3):
    """Rank newly excited pitch bins instead of a louder sustained neighbour.

Input is a nonnegative aligned pitch-energy matrix, such as retained CQT.
Onsets come from independent transient observations. The output keeps top
alternatives and does not assert they are notes or map every attack to one
instrument. Release/reverb and missing fundamentals still need review.
    """
    times=np.asarray(times,dtype=float);energy=np.asarray(energy,dtype=float)
    pitches=np.asarray(midi_pitches)
    if (energy.shape!=(len(pitches),len(times)) or len(times)<2 or np.any(np.diff(times)<=0)
            or not np.isfinite(energy).all() or np.any(energy<0) or not np.isfinite(times).all()
            or lookback<=0 or attack_window<=0 or type(top_k) is not int or top_k<1):
        raise ValueError('Require increasing times, aligned nonnegative energy, and positive windows')
    if any(int(p)!=p or not 0<=p<=127 for p in pitches):raise ValueError('Invalid MIDI bins')
    rows=[]
    for at in onsets:
        if not np.isfinite(at):raise ValueError('Invalid onset')
        before=(times>=at-lookback)&(times<at)
        after=(times>=at)&(times<at+attack_window)
        if not before.any() or not after.any():
            rows.append({'onset_seconds':float(at),'status':'insufficient_window','candidates':[]});continue
        baseline=np.median(energy[:,before],axis=1)
        peak=np.max(energy[:,after],axis=1)
        novelty=np.maximum(peak-baseline,0)
        order=np.argsort(-novelty,kind='stable')[:top_k]
        rows.append({'onset_seconds':float(at),'status':'ranked_candidates' if novelty.max()>1e-12 else 'insufficient_attack_evidence',
            'candidates':[{'pitch':int(pitches[i]),'new_energy':float(novelty[i]),
                           'pre_attack_energy':float(baseline[i]),'peak_energy':float(peak[i])} for i in order]})
    return {'version':1,'attacks':rows,'musical_accuracy_verified':False,
            'scope':'onset-conditioned alternatives; not a finished transcription'}


def pitch_source_report(sources, sample_rate, pitches, *, cents=30):
    if not sources or sample_rate<=0 or not 0<cents<=100:
        raise ValueError('Require aligned sources, positive sample rate and 0 < cents <= 100')
    arrays={name:np.asarray(y,dtype=float) for name,y in sources.items()}
    if len({len(y) for y in arrays.values()})!=1:
        raise ValueError('Source lengths differ; do not trim each stem independently')
    n=len(next(iter(arrays.values())))
    if n<32 or any(y.ndim!=1 or not np.isfinite(y).all() for y in arrays.values()):
        raise ValueError('Require finite mono source windows of at least 32 samples')
    window=np.hanning(n); norm=max(float(np.sum(window**2))*n,1e-30)
    spectra={k:np.abs(np.fft.rfft(y*window))**2/norm for k,y in arrays.items()}
    frequencies=np.fft.rfftfreq(n,1/sample_rate); rows=[]
    for pitch in pitches:
        if type(pitch) is not int or not 0<=pitch<=127:raise ValueError('Invalid MIDI pitch')
        f=440*2**((pitch-69)/12)
        width=max(sample_rate/n,f*(2**(cents/1200)-1))
        if f+width>=sample_rate/2:raise ValueError('Requested pitch exceeds usable Nyquist range')
        mask=np.abs(frequencies-f)<=width
        values={k:float(s[mask].sum()) for k,s in spectra.items()}
        best=max(values,key=values.get)
        rows.append({'pitch':pitch,'frequency_hz':f,'band_half_width_hz':width,
                     'strongest_source':best if values[best]>1e-12 else None,
                     'status':'observed_energy' if values[best]>1e-12 else 'unobserved',
                     'sources':{k:{'power':v,'relative_to_best_db':
                                      float(10*np.log10(max(v,1e-30)/max(values[best],1e-30)))}
                                for k,v in values.items()}})
    return {'version':1,'pitches':rows,'silence_verified':False,
            'musical_accuracy_verified':False,
            'limitations':['spectral_energy_is_not_instrument_identity',
                          'harmonics_can_mimic_octaves_or_fifths',
                          'do_not_sum_alternative_model_outputs']}


def retain_attacks(raw, *, playable_range=None, duplicate_seconds=.012):
    """Keep fast attacks, weak notes, and out-of-range pitches for review.

Only overlapping same-pitch detections with nearly identical start AND end
are duplicates. No beat-relative minimum IOI or pitch-class collapse is used.
"""
    if not 0<=duplicate_seconds<=.02:raise ValueError('Duplicate window must be <= 20 ms')
    candidates=[]; duplicates=[]; review=[]
    for index,row in sorted(enumerate(raw),key=lambda item:item[1][0]):
        if len(row)!=4:raise ValueError('Expected [start_seconds,end_seconds,MIDI_pitch,confidence]')
        start,end,pitch,confidence=row
        if (not np.isfinite([start,end,confidence]).all() or end<=start
                or type(pitch) is not int or not 0<=pitch<=127):raise ValueError('Invalid raw note')
        dup=next((n for n in reversed(candidates) if n['note'][2]==pitch
                  and abs(n['note'][0]-start)<=duplicate_seconds
                  and abs(n['note'][1]-end)<=duplicate_seconds),None)
        if dup:
            duplicates.append({'raw_index':index,'kept_raw_index':dup['raw_index'],
                               'reason':'same_pitch_near_identical_interval'})
            continue
        candidates.append({'raw_index':index,'note':list(row)})
        if playable_range and not playable_range[0]<=pitch<=playable_range[1]:
            review.append({'raw_index':index,'reason':'outside_requested_range_retained'})
    return {'version':1,'raw_count':len(raw),'raw_sha256':hashlib.sha256(
        json.dumps(raw,separators=(',',':')).encode()).hexdigest(),
        'candidates':candidates,'duplicates':duplicates,'review':review,
        'quantized':False,'musical_accuracy_verified':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    route=sub.add_parser('route');route.add_argument('--manifest',type=Path,required=True)
    route.add_argument('--start',type=float,required=True);route.add_argument('--duration',type=float,required=True)
    route.add_argument('--pitches',type=int,nargs='+',required=True)
    route.add_argument('--roles',nargs='+',default=['keyboard_candidate','guitar_candidate','other','other_candidate'])
    retain=sub.add_parser('retain');retain.add_argument('--candidates',type=Path,required=True)
    retain.add_argument('--range',type=int,nargs=2)
    attacks=sub.add_parser('attacks');attacks.add_argument('--features',type=Path,required=True)
    attacks.add_argument('--onsets',type=Path,required=True)
    attacks.add_argument('--top-k',type=int,default=3)
    for p in (route,retain,attacks):p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    if args.command=='retain':
        result=retain_attacks(json.loads(args.candidates.read_text(encoding='utf-8')),playable_range=args.range)
    elif args.command=='attacks':
        with np.load(args.features,allow_pickle=False) as archive:
            features={key:archive[key] for key in ('cqt_times','cqt','cqt_midi')}
        result=onset_pitch_candidates(features['cqt_times'],features['cqt'],features['cqt_midi'],
            json.loads(args.onsets.read_text(encoding='utf-8')),top_k=args.top_k)
        result['features_sha256']=hashlib.sha256(args.features.read_bytes()).hexdigest()
    else:
        import soundfile as sf
        manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
        if manifest.get('status')!='complete' or args.duration<=0:raise ValueError('Incomplete cache or invalid interval')
        start=args.start-manifest.get('source_offset_seconds',0)
        if start<0:raise ValueError('Requested time precedes cached source')
        sr=manifest['sample_rate']; sources={}
        for role in args.roles:
            file=args.manifest.parent/manifest['roles'][role]
            y,actual_sr=sf.read(file,start=round(start*sr),stop=round((start+args.duration)*sr),always_2d=True)
            if actual_sr!=sr or len(y)!=round((start+args.duration)*sr)-round(start*sr):
                raise ValueError('Sample rate mismatch or truncated interval')
            sources[role]=y.mean(axis=1)
        result=pitch_source_report(sources,sr,args.pitches)
        result.update(input_sha256=manifest['input_sha256'],start_seconds=args.start,
                      duration_seconds=args.duration,roles={r:manifest['roles'][r] for r in args.roles})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(args.output),'musical_accuracy_verified':False}))


if __name__=='__main__':main()
