#!/usr/bin/env python3
"""Write song.mid (the notes/song.png melody, bars 1-8) and chord versions.

song.mid         melody
song_thirds.mid  melody + major third above (2 voices)
song_triads.mid  melody + major third + perfect fifth above (3 voices)
"""
import os

import mido

# D major, 4/4.  (note, beats); ties summed.  'R' is a rest.
SONG = [
    ('D4', 1), ('E4', .5), ('F#4', 1), ('E4', 1), ('F#4', .5),                # 1
    ('G4', 1), ('F#4', .5), ('E4', 1), ('D4', 1), ('E4', .5),                 # 2
    ('F#4', 1), ('E4', .5), ('D4', 1.5), ('F#4', .5), ('E4', 4.5),            # 3-4
    ('A4', 1), ('F#4', .5), ('E4', 1), ('F#4', 1), ('A4', .5),                # 5
    ('G4', 1), ('F#4', .5), ('E4', 1), ('D4', 1), ('E4', .5),                 # 6
    ('F#4', 1), ('E4', .5), ('D4', 1.5), ('F#4', .5), ('E4', 3.5),            # 7-8
]

PC = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
TPB = 480


def midi_number(name):
    acc = name.count('#') - name.count('b')
    return 12 * (int(name[1:].strip('#b')) + 1) + PC[name[0]] + acc


OUT = os.path.dirname(os.path.abspath(__file__))
VERSIONS = {'song.mid': (), 'song_thirds.mid': (4,), 'song_triads.mid': (4, 7)}


def write(path, above=(), bpm=100):
    mid = mido.MidiFile(ticks_per_beat=TPB)
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.MetaMessage('track_name', name=path.rsplit('/', 1)[-1],
                               time=0))
    tr.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(bpm), time=0))
    tr.append(mido.MetaMessage('time_signature', numerator=4, denominator=4,
                               time=0))
    tr.append(mido.MetaMessage('key_signature', key='D', time=0))
    wait = 0
    for name, beats in SONG:
        ticks = round(beats * TPB)
        if name == 'R':
            wait += ticks
            continue
        chord = [midi_number(name) + i for i in (0,) + tuple(above)]
        for k, n in enumerate(chord):
            tr.append(mido.Message('note_on', note=n, velocity=80,
                                   time=wait if k == 0 else 0))
        for k, n in enumerate(chord):
            tr.append(mido.Message('note_off', note=n, velocity=0,
                                   time=ticks if k == 0 else 0))
        wait = 0
    tr.append(mido.MetaMessage('end_of_track', time=wait))
    mid.save(path)
    print('wrote {} ({:.1f} s)'.format(path, mid.length))


if __name__ == '__main__':
    for name, above in VERSIONS.items():
        write(os.path.join(OUT, name), above)
