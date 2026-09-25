#!/usr/bin/env python3
"""Write song.mid (the notes/song.png melody, bars 1-8) and chord versions.

song.mid         melody
song_thirds.mid  melody + major third above (2 voices)
song_triads.mid  melody + major third + perfect fifth above (3 voices)

and two part-per-channel arrangements (repeats not taken):
song-harmony.mid  notes/two-voice-song.png: melody, bass
song3.mid         notes/song3.png: melody, inner harmony, bass (bass played
                  an octave above where it is written)
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

# G major, 4/4, 16 bars; slurs are phrasing, not ties.
_CHORUS = [
    ('D5', 4),                                                                # 9
    ('D5', 1), ('C5', 1), ('D5', 1), ('C5', 1),                               # 10
    ('B4', 1), ('D4', 1), ('D4', 1), ('D4', 1),                               # 11
    ('D4', 4),                                                                # 12
]
_BASS_CHORUS = [
    ('G2', 1), ('B2', 1), ('D3', 1), ('G3', 1),                               # 9
    ('C2', 1), ('E2', 1), ('G2', 1), ('C3', 1),                               # 10
    ('G2', 1), ('B2', 1), ('D3', 1), ('G3', 1),                               # 11
    ('D2', 1), ('F#2', 1), ('A2', 1), ('D3', 1),                              # 12
]
HARMONY = {
    'treble': [
        ('G4', 1), ('A4', 1), ('B4', 1), ('A4', .5), ('B4', .5),              # 1
        ('C5', 1), ('B4', 1), ('A4', 1), ('G4', .5), ('A4', .5),              # 2
        ('B4', 1), ('A4', 1), ('G4', 1), ('B4', 1),                           # 3
        ('A4', 4),                                                            # 4
        ('D5', 1), ('B4', 1), ('A4', 1), ('B4', .5), ('D5', .5),              # 5
        ('C5', 1), ('B4', 1), ('A4', 1), ('G4', .5), ('A4', .5),              # 6
        ('B4', 1), ('A4', 1), ('G4', 1), ('B4', 1),                           # 7
        ('A4', 4),                                                            # 8
    ] + _CHORUS * 2,                                                          # 9-16
    'bass': [
        ('G2', 4), ('C3', 4), ('G2', 4),                                      # 1-3
        ('D3', 1), ('D2', 1), ('E2', 1), ('F#2', 1),                          # 4
        ('G2', 4), ('C3', 4), ('G2', 4),                                      # 5-7
        ('D3', 1), ('C3', 1), ('B2', 1), ('A2', 1),                           # 8
    ] + _BASS_CHORUS * 2,                                                     # 9-16
}

# F major, 4/4, 4 bars.  The treble's lower notes are their own part.
SONG3 = {
    'melody': [
        ('F4', 1), ('G4', .5), ('A4', 1), ('G4', 1), ('A4', .5),              # 1
        ('Bb4', 1), ('A4', .5), ('G4', 1), ('F4', 1), ('G4', .5),             # 2
        ('A4', 1), ('C4', .5), ('C4', 1), ('C4', .5), ('C4', 4),              # 3-4
        ('R', 1),
    ],
    'harmony': [
        ('C4', 1), ('R', 1), ('C4', 1), ('R', 1),                             # 1
        ('D4', 1), ('R', 1), ('D4', 1), ('R', 1),                             # 2
        ('C4', 1), ('R', 7),                                                  # 3-4
    ],
    'bass': [
        ('F2', 3), ('F2', 1),                                                 # 1
        ('Bb2', 3), ('Bb2', 1),                                               # 2
        ('F2', 3), ('F2', 1),                                                 # 3
        ('C3', 1), ('C3', .5), ('C2', 1), ('C2', .5), ('C3', 1),              # 4
    ],
}

PC = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
TPB = 480


def midi_number(name):
    acc = name.count('#') - name.count('b')
    return 12 * (int(name[1:].strip('#b')) + 1) + PC[name[0]] + acc


OUT = os.path.dirname(os.path.abspath(__file__))
VERSIONS = {'song.mid': (), 'song_thirds.mid': (4,), 'song_triads.mid': (4, 7)}


def write_parts(path, parts, bpm, key, shift=None):
    """One track per part, each on its own channel.

    *shift* maps a part name to semitones added to every note of it.
    """
    shift = shift or {}
    mid = mido.MidiFile(ticks_per_beat=TPB)
    for ch, (name, notes) in enumerate(parts.items()):
        tr = mido.MidiTrack()
        mid.tracks.append(tr)
        tr.append(mido.MetaMessage('track_name', name=name, time=0))
        if ch == 0:
            tr.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(bpm),
                                       time=0))
            tr.append(mido.MetaMessage('time_signature', numerator=4,
                                       denominator=4, time=0))
            tr.append(mido.MetaMessage('key_signature', key=key, time=0))
        wait = 0
        for note, beats in notes:
            ticks = round(beats * TPB)
            if note == 'R':
                wait += ticks
                continue
            n = midi_number(note) + shift.get(name, 0)
            tr.append(mido.Message('note_on', channel=ch, note=n, velocity=80,
                                   time=wait))
            tr.append(mido.Message('note_off', channel=ch, note=n, velocity=0,
                                   time=ticks))
            wait = 0
        tr.append(mido.MetaMessage('end_of_track', time=wait))
    mid.save(path)
    print('wrote {} ({:.1f} s)'.format(path, mid.length))


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
    write_parts(os.path.join(OUT, 'song-harmony.mid'), HARMONY, bpm=120,
                key='G')
    write_parts(os.path.join(OUT, 'song3.mid'), SONG3, bpm=100, key='F',
                shift={'bass': 12})
