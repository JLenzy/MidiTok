"""MIDI-Like tokenizer."""

from __future__ import annotations

from symusic import Note, Pedal, PitchBend, Score, Tempo, TimeSignature, Track

from miditok.classes import Event, TokSequence
from miditok.constants import MIDI_INSTRUMENTS, TIME_SIGNATURE
from miditok.midi_tokenizer import MIDITokenizer
from miditok.utils import compute_ticks_per_beat


class MIDILike(MIDITokenizer):
    r"""
    MIDI-Like tokenizer.

    Introduced in `This time with feeling (Oore et al.) <https://arxiv.org/abs/1808.03715>`_
    and later used with `Music Transformer (Huang et al.) <https://openreview.net/forum?id=rJe4ShAcF7>`_
    and `MT3 (Gardner et al.) <https://openreview.net/forum?id=iMSjopcOn0p>`_,
    this tokenization simply converts MIDI messages (*NoteOn*, *NoteOff*,
    *TimeShift*...) to tokens, hence the name "MIDI-Like".
    ``MIDILike`` decode tokens following a FIFO (First In First Out) logic. When
    decoding tokens, you can limit the duration of the created notes by setting a
    ``max_duration`` entry in the tokenizer's config
    (``config.additional_params["max_duration"]``) to be given as a tuple of three
    integers following ``(num_beats, num_frames, res_frames)``, the resolutions being
    in the frames per beat.
    If you specify `use_programs` as `True` in the config file, the tokenizer will add
    ``Program`` tokens before each `Pitch` tokens to specify its instrument, and will
    treat all tracks as a single stream of tokens.

    **Note:** as `MIDILike` uses *TimeShifts* events to move the time from note to
    note, it could be unsuited for tracks with long pauses. In such case, the
    maximum *TimeShift* value will be used. Also, the `MIDILike` tokenizer might alter
    the durations of overlapping notes. If two notes of the same instrument with the
    same pitch are overlapping, i.e. a first one is still being played when a second
    one is also played, the offset time of the first will be set to the onset time of
    the second. This is done to prevent unwanted duration alterations that could happen
    in such case, as the `NoteOff` token associated to the first note will also end the
    second one.
    **Note:** When decoding multiple token sequences (of multiple tracks), i.e. when
    ``config.use_programs`` is False, only the tempos and time signatures of the first
    sequence will be decoded for the whole MIDI.
    """

    def _tweak_config_before_creating_voc(self) -> None:
        self._note_on_off = True

    def _add_time_events(self, events: list[Event], time_division: int) -> list[Event]:
        r"""
        Create the time events from a list of global and track events.

        Internal method intended to be implemented by child classes.
        The returned sequence is the final token sequence ready to be converted to ids
        to be fed to a model.

        :param events: sequence of global and track events to create tokens time from.
        :param time_division: time division in ticks per quarter of the MIDI being
            tokenized.
        :return: the same events, with time events inserted.
        """
        # Add time events
        all_events = []
        previous_tick = 0
        previous_note_end = 0
        ticks_per_beat = compute_ticks_per_beat(TIME_SIGNATURE[1], time_division)
        for event in events:
            # No time shift
            if event.time != previous_tick:
                # (Rest)
                if (
                    self.config.use_rests
                    and event.time - previous_note_end >= self._min_rest(ticks_per_beat)
                ):
                    previous_tick = previous_note_end
                    rest_values = self._time_ticks_to_tokens(
                        event.time - previous_tick, ticks_per_beat, rest=True
                    )
                    for dur_value, dur_ticks in zip(*rest_values):
                        all_events.append(
                            Event(
                                type_="Rest",
                                value=".".join(map(str, dur_value)),
                                time=previous_tick,
                                desc=f"{event.time - previous_tick} ticks",
                            )
                        )
                        previous_tick += dur_ticks

                # Time shift
                # no else here as previous might have changed with rests
                if event.time != previous_tick:
                    time_shift = event.time - previous_tick
                    for dur_value, dur_ticks in zip(
                        *self._time_ticks_to_tokens(time_shift, ticks_per_beat)
                    ):
                        all_events.append(
                            Event(
                                type_="TimeShift",
                                value=".".join(map(str, dur_value)),
                                time=previous_tick,
                                desc=f"{time_shift} ticks",
                            )
                        )
                        previous_tick += dur_ticks
                previous_tick = event.time

            # Time Signature: Update ticks per beat
            if event.type_ == "TimeSig":
                ticks_per_beat = compute_ticks_per_beat(
                    int(event.value.split("/")[1]), time_division
                )

            all_events.append(event)

            # Update max offset time of the notes encountered
            if event.type_ in ["NoteOn", "PitchIntervalTime", "PitchIntervalChord"]:
                previous_note_end = max(previous_note_end, event.desc)
            elif event.type_ in [
                "Program",
                "Tempo",
                "Pedal",
                "PedalOff",
                "PitchBend",
                "Chord",
            ]:
                previous_note_end = max(previous_note_end, event.time)

        return all_events

    def _sort_events(self, events: list[Event]) -> None:
        # This could be removed if we find a way to insert NoteOff tokens before Chords
        if self.config.use_chords:
            events.sort(key=lambda e: (e.time, self._order(e)))
        else:
            super()._sort_events(events)

    @staticmethod
    def _order(event: Event) -> int:
        """
        Return the order number of an ``Event``.

        Internal method used to sort events (tokens) depending on their type or
        context of appearance. This is required, especially for multitrack
        one-token-stream situations where there can be several tokens appearing at
        the same moment (tick) from different tracks, that need to be sorted.

        :param event: event to determine priority.
        :return: priority as an int
        """
        # Global MIDI tokens first
        if event.type_ in ["Tempo", "TimeSig"]:
            return 0
        # Then NoteOff
        if event.type_ == "NoteOff" or (
            event.type_ == "Program" and event.desc == "ProgramNoteOff"
        ):
            return 1
        # Then track effects
        if event.type_ in ["Pedal", "PedalOff"] or (
            event.type_ == "Duration" and event.desc == "PedalDuration"
        ):
            return 2
        if event.type_ == "PitchBend" or (
            event.type_ == "Program" and event.desc == "ProgramPitchBend"
        ):
            return 3
        if event.type_ == "ControlChange":
            return 4
        # Track notes then
        return 10

    def _tokens_to_midi(
        self,
        tokens: TokSequence | list[TokSequence],
        programs: list[tuple[int, bool]] | None = None,
    ) -> Score:
        r"""
        Convert tokens (:class:`miditok.TokSequence`) into a MIDI.

        This is an internal method called by ``self.tokens_to_midi``, intended to be
        implemented by classes inheriting :class:`miditok.MidiTokenizer`.

        :param tokens: tokens to convert. Can be either a list of
            :class:`miditok.TokSequence` or a list of :class:`miditok.TokSequence`s.
        :param programs: programs of the tracks. If none is given, will default to
            piano, program 0. (default: ``None``)
        :return: the midi object (:class:`symusic.Score`).
        """
        # Unsqueeze tokens in case of one_token_stream
        if self.one_token_stream:  # ie single token seq
            tokens = [tokens]
        for i in range(len(tokens)):
            tokens[i] = tokens[i].tokens
        midi = Score(self.time_division)
        max_duration_str = self.config.additional_params.get("max_duration", None)
        max_duration = None

        # RESULTS
        tracks: dict[int, Track] = {}
        tempo_changes, time_signature_changes = [], []
        active_notes: dict[int, dict[int, list[tuple[int, int]]]] = {
            prog: {
                pi: []
                for pi in range(
                    self.config.pitch_range[0], self.config.pitch_range[1] + 1
                )
            }
            for prog in self.config.programs
        }

        def check_inst(prog: int) -> None:
            if prog not in tracks:
                tracks[prog] = Track(
                    program=0 if prog == -1 else prog,
                    is_drum=prog == -1,
                    name="Drums" if prog == -1 else MIDI_INSTRUMENTS[prog]["name"],
                )

        def clear_active_notes() -> None:
            if max_duration is not None:
                if self.one_token_stream:
                    for program, active_notes_ in active_notes.items():
                        for pitch_, note_ons in active_notes_.items():
                            for onset_tick, vel_ in note_ons:
                                check_inst(program)
                                tracks[program].notes.append(
                                    Note(
                                        onset_tick,
                                        max_duration,
                                        pitch_,
                                        vel_,
                                    )
                                )
                else:
                    for pitch_, note_ons in active_notes[
                        current_instrument.program
                    ].items():
                        for onset_tick, vel_ in note_ons:
                            current_instrument.notes.append(
                                Note(onset_tick, max_duration, pitch_, vel_)
                            )

        current_instrument = None
        for si, seq in enumerate(tokens):
            # Set tracking variables
            current_tick = 0
            current_program = 0
            previous_pitch_onset = {prog: -128 for prog in self.config.programs}
            previous_pitch_chord = {prog: -128 for prog in self.config.programs}
            active_pedals = {}
            ticks_per_beat = midi.ticks_per_quarter
            if max_duration_str is not None:
                max_duration = self._time_token_to_ticks(
                    max_duration_str, ticks_per_beat
                )
            # Set track / sequence program if needed
            if not self.one_token_stream:
                is_drum = False
                if programs is not None:
                    current_program, is_drum = programs[si]
                current_instrument = Track(
                    program=current_program,
                    is_drum=is_drum,
                    name="Drums"
                    if current_program == -1
                    else MIDI_INSTRUMENTS[current_program]["name"],
                )

            # Decode tokens
            for ti, token in enumerate(seq):
                tok_type, tok_val = token.split("_")
                if tok_type == "TimeShift":
                    current_tick += self._tpb_tokens_to_ticks[ticks_per_beat][tok_val]
                elif tok_type == "Rest":
                    current_tick += self._tpb_rests_to_ticks[ticks_per_beat][tok_val]
                elif tok_type in [
                    "NoteOn",
                    "NoteOff",
                    "PitchIntervalTime",
                    "PitchIntervalChord",
                ]:
                    # We update previous_pitch_onset and previous_pitch_chord even if
                    # the try fails.
                    if tok_type == "PitchIntervalTime":
                        pitch = previous_pitch_onset[current_program] + int(tok_val)
                        previous_pitch_onset[current_program] = pitch
                        previous_pitch_chord[current_program] = pitch
                    elif tok_type == "PitchIntervalChord":
                        pitch = previous_pitch_chord[current_program] + int(tok_val)
                        previous_pitch_chord[current_program] = pitch
                    else:
                        pitch = int(tok_val)
                        if tok_type == "NoteOn":
                            previous_pitch_onset[current_program] = pitch
                            previous_pitch_chord[current_program] = pitch

                    # if NoteOn adds it to the queue with FIFO
                    if tok_type != "NoteOff":
                        if ti + 1 < len(seq):
                            vel = int(seq[ti + 1].split("_")[1])
                            active_notes[current_program][pitch].append(
                                (current_tick, vel)
                            )
                    # NoteOff, creates the note
                    elif len(active_notes[current_program][pitch]) > 0:
                        note_onset_tick, vel = active_notes[current_program][pitch].pop(
                            0
                        )
                        duration = current_tick - note_onset_tick
                        if max_duration is not None and duration > max_duration:
                            duration = max_duration
                        new_note = Note(note_onset_tick, duration, pitch, vel)
                        if self.one_token_stream:
                            check_inst(current_program)
                            tracks[current_program].notes.append(new_note)
                        else:
                            current_instrument.notes.append(new_note)
                elif tok_type == "Program":
                    current_program = int(tok_val)
                elif tok_type == "Tempo" and si == 0:
                    tempo_changes.append(Tempo(current_tick, float(tok_val)))
                elif tok_type == "TimeSig":
                    num, den = self._parse_token_time_signature(tok_val)
                    ticks_per_beat = self._tpb_per_ts[den]
                    if max_duration is not None:
                        max_duration = self._time_token_to_ticks(
                            max_duration_str, ticks_per_beat
                        )
                    if si == 0:
                        time_signature_changes.append(
                            TimeSignature(current_tick, num, den)
                        )
                elif tok_type == "Pedal":
                    pedal_prog = (
                        int(tok_val) if self.config.use_programs else current_program
                    )
                    if self.config.sustain_pedal_duration and ti + 1 < len(seq):
                        if seq[ti + 1].split("_")[0] == "Duration":
                            duration = self._tpb_tokens_to_ticks[ticks_per_beat][
                                seq[ti + 1].split("_")[1]
                            ]
                            # Add instrument if it doesn't exist, can happen for the
                            # first tokens
                            new_pedal = Pedal(current_tick, duration)
                            if self.one_token_stream:
                                check_inst(pedal_prog)
                                tracks[pedal_prog].pedals.append(new_pedal)
                            else:
                                current_instrument.pedals.append(new_pedal)
                    elif pedal_prog not in active_pedals:
                        active_pedals[pedal_prog] = current_tick
                elif tok_type == "PedalOff":
                    pedal_prog = (
                        int(tok_val) if self.config.use_programs else current_program
                    )
                    if pedal_prog in active_pedals:
                        new_pedal = Pedal(
                            active_pedals[pedal_prog],
                            current_tick - active_pedals[pedal_prog],
                        )
                        if self.one_token_stream:
                            check_inst(pedal_prog)
                            tracks[pedal_prog].pedals.append(
                                Pedal(
                                    active_pedals[pedal_prog],
                                    current_tick - active_pedals[pedal_prog],
                                )
                            )
                        else:
                            current_instrument.pedals.append(new_pedal)
                        del active_pedals[pedal_prog]
                elif tok_type == "PitchBend":
                    new_pitch_bend = PitchBend(current_tick, int(tok_val))
                    if self.one_token_stream:
                        check_inst(current_program)
                        tracks[current_program].pitch_bends.append(new_pitch_bend)
                    else:
                        current_instrument.pitch_bends.append(new_pitch_bend)

            # Add current_inst to midi and handle notes still active
            if not self.one_token_stream:
                midi.tracks.append(current_instrument)
                clear_active_notes()
                active_notes[current_instrument.program] = {
                    pi: []
                    for pi in range(
                        self.config.pitch_range[0], self.config.pitch_range[1] + 1
                    )
                }

        # Handle notes still active
        if self.one_token_stream:
            clear_active_notes()

        # create MidiFile
        if self.one_token_stream:
            midi.tracks = list(tracks.values())
        midi.tempos = tempo_changes
        midi.time_signatures = time_signature_changes

        return midi

    def _create_base_vocabulary(self) -> list[str]:
        r"""
        Create the vocabulary, as a list of string tokens.

        Each token is given as the form ``"Type_Value"``, with its type and value
        separated with an underscore. Example: ``Pitch_58``.
        The :class:`miditok.MIDITokenizer` main class will then create the "real"
        vocabulary as a dictionary. Special tokens have to be given when creating the
        tokenizer, and will be added to the vocabulary by
        :class:`miditok.MIDITokenizer`.

        :return: the vocabulary as a list of string.
        """
        vocab = []

        # NOTE ON
        vocab += [f"NoteOn_{i}" for i in range(*self.config.pitch_range)]

        # NOTE OFF
        vocab += [f"NoteOff_{i}" for i in range(*self.config.pitch_range)]

        # VELOCITY
        vocab += [f"Velocity_{i}" for i in self.velocities]

        # TIME SHIFTS
        vocab += [
            f'TimeShift_{".".join(map(str, duration))}' for duration in self.durations
        ]

        # Add additional tokens
        self._add_additional_tokens_to_vocab_list(vocab)

        # Add durations if needed
        if self.config.use_sustain_pedals and self.config.sustain_pedal_duration:
            vocab += [
                f'Duration_{".".join(map(str, duration))}'
                for duration in self.durations
            ]

        return vocab

    def _create_token_types_graph(self) -> dict[str, list[str]]:
        r"""
        Return a graph/dictionary of the possible token types successions.

        :return: the token types transitions dictionary.
        """
        dic = {"NoteOn": ["Velocity"]}

        if self.config.use_programs:
            first_note_token_type = (
                "NoteOn" if self.config.program_changes else "Program"
            )
            dic["Program"] = ["NoteOn", "NoteOff"]
        else:
            first_note_token_type = "NoteOn"
        dic["Velocity"] = [first_note_token_type, "TimeShift"]
        dic["NoteOff"] = ["NoteOff", first_note_token_type, "TimeShift"]
        dic["TimeShift"] = ["NoteOff", first_note_token_type, "TimeShift"]
        if self.config.use_pitch_intervals:
            for token_type in ("PitchIntervalTime", "PitchIntervalChord"):
                dic[token_type] = ["Velocity"]
                if self.config.use_programs:
                    dic["Program"].append(token_type)
                else:
                    dic["Velocity"].append(token_type)
                    dic["NoteOff"].append(token_type)
                    dic["TimeShift"].append(token_type)
        if self.config.program_changes:
            for token_type in ["Velocity", "NoteOff"]:
                dic[token_type].append("Program")

        if self.config.use_chords:
            dic["Chord"] = [first_note_token_type]
            dic["TimeShift"] += ["Chord"]
            dic["NoteOff"] += ["Chord"]
            if self.config.use_programs:
                dic["Program"].append("Chord")
            if self.config.use_pitch_intervals:
                dic["Chord"] += ["PitchIntervalTime", "PitchIntervalChord"]

        if self.config.use_tempos:
            dic["TimeShift"] += ["Tempo"]
            dic["Tempo"] = [first_note_token_type, "TimeShift"]
            if not self.config.use_programs or self.config.program_changes:
                dic["Tempo"].append("NoteOff")
            if self.config.use_chords:
                dic["Tempo"] += ["Chord"]
            if self.config.use_rests:
                dic["Tempo"].append("Rest")  # only for first token
            if self.config.use_pitch_intervals:
                dic["Tempo"] += ["PitchIntervalTime", "PitchIntervalChord"]

        if self.config.use_time_signatures:
            dic["TimeShift"] += ["TimeSig"]
            dic["TimeSig"] = [first_note_token_type, "TimeShift"]
            if not self.config.use_programs or self.config.program_changes:
                dic["TimeSig"].append("NoteOff")
            if self.config.use_chords:
                dic["TimeSig"] += ["Chord"]
            if self.config.use_rests:
                dic["TimeSig"].append("Rest")  # only for first token
            if self.config.use_tempos:
                dic["TimeSig"].append("Tempo")
            if self.config.use_pitch_intervals:
                dic["TimeSig"] += ["PitchIntervalTime", "PitchIntervalChord"]

        if self.config.use_sustain_pedals:
            dic["TimeShift"].append("Pedal")
            dic["NoteOff"].append("Pedal")
            if not self.config.sustain_pedal_duration:
                dic["NoteOff"].append("PedalOff")
            if self.config.sustain_pedal_duration:
                dic["Pedal"] = ["Duration"]
                dic["Duration"] = [
                    first_note_token_type,
                    "NoteOff",
                    "TimeShift",
                    "Pedal",
                ]
                if self.config.use_pitch_intervals:
                    dic["Duration"] += ["PitchIntervalTime", "PitchIntervalChord"]
            else:
                dic["PedalOff"] = [
                    "Pedal",
                    "PedalOff",
                    first_note_token_type,
                    "NoteOff",
                    "TimeShift",
                ]
                dic["Pedal"] = ["Pedal", first_note_token_type, "NoteOff", "TimeShift"]
                dic["TimeShift"].append("PedalOff")
                if self.config.use_pitch_intervals:
                    dic["Pedal"] += ["PitchIntervalTime", "PitchIntervalChord"]
                    dic["PedalOff"] += ["PitchIntervalTime", "PitchIntervalChord"]
            if self.config.use_chords:
                dic["Pedal"].append("Chord")
                if not self.config.sustain_pedal_duration:
                    dic["PedalOff"].append("Chord")
                    dic["Chord"].append("PedalOff")
            if self.config.use_rests:
                dic["Pedal"].append("Rest")
                if not self.config.sustain_pedal_duration:
                    dic["PedalOff"].append("Rest")
            if self.config.use_tempos:
                dic["Tempo"].append("Pedal")
                if not self.config.sustain_pedal_duration:
                    dic["Tempo"].append("PedalOff")
            if self.config.use_time_signatures:
                dic["TimeSig"].append("Pedal")
                if not self.config.sustain_pedal_duration:
                    dic["TimeSig"].append("PedalOff")

        if self.config.use_pitch_bends:
            # As a Program token will precede PitchBend otherwise
            # Else no need to add Program as its already in
            dic["PitchBend"] = [first_note_token_type, "NoteOff", "TimeShift"]
            if self.config.use_programs and not self.config.program_changes:
                dic["Program"].append("PitchBend")
            else:
                dic["TimeShift"].append("PitchBend")
                dic["NoteOff"].append("PitchBend")
                if self.config.use_tempos:
                    dic["Tempo"].append("PitchBend")
                if self.config.use_time_signatures:
                    dic["TimeSig"].append("PitchBend")
                if self.config.use_sustain_pedals:
                    dic["Pedal"].append("PitchBend")
                    if self.config.sustain_pedal_duration:
                        dic["Duration"].append("PitchBend")
                    else:
                        dic["PedalOff"].append("PitchBend")
            if self.config.use_chords:
                dic["PitchBend"].append("Chord")
            if self.config.use_rests:
                dic["PitchBend"].append("Rest")

        if self.config.use_rests:
            dic["Rest"] = ["Rest", first_note_token_type, "TimeShift"]
            dic["NoteOff"] += ["Rest"]
            if self.config.use_chords:
                dic["Rest"] += ["Chord"]
            if self.config.use_tempos:
                dic["Rest"].append("Tempo")
            if self.config.use_time_signatures:
                dic["Rest"].append("TimeSig")
            if self.config.use_sustain_pedals:
                dic["Rest"].append("Pedal")
                if self.config.sustain_pedal_duration:
                    dic["Duration"].append("Rest")
                else:
                    dic["Rest"].append("PedalOff")
                    dic["PedalOff"].append("Rest")
            if self.config.use_pitch_bends:
                dic["Rest"].append("PitchBend")
            if self.config.use_pitch_intervals:
                dic["Rest"] += ["PitchIntervalTime", "PitchIntervalChord"]
        else:
            dic["TimeShift"].append("TimeShift")

        if self.config.program_changes:
            for token_type in [
                "TimeShift",
                "Rest",
                "PitchBend",
                "Pedal",
                "PedalOff",
                "Tempo",
                "TimeSig",
                "Chord",
            ]:
                if token_type in dic:
                    dic["Program"].append(token_type)
                    dic[token_type].append("Program")

        return dic

    def _tokens_errors(self, tokens: list[str]) -> int:
        r"""
        Return the number of errors in a sequence of tokens.

        The method checks if a sequence of tokens is made of good token types
        successions and values. The number of errors should not be higher than the
        number of tokens.

        In ``MIDILike``, we also check the presence of *NoteOff* tokens with a
        corresponding *NoteOn* token, and vice-versa.

        :param tokens: sequence of tokens string to check.
        :return: the number of errors predicted (no more than one per token).
        """
        err = 0
        current_program = 0
        active_pitches: dict[int, dict[int, list[int]]] = {
            prog: {
                pi: []
                for pi in range(
                    self.config.pitch_range[0], self.config.pitch_range[1] + 1
                )
            }
            for prog in self.config.programs
        }
        current_pitches_tick = {p: [] for p in self.config.programs}
        ticks_per_beat = self.time_division
        max_duration_str = self.config.additional_params.get("max_duration", None)
        if max_duration_str is not None:
            max_duration = self._time_token_to_ticks(max_duration_str, ticks_per_beat)
        else:
            max_duration = None
        previous_pitch_onset = {program: -128 for program in self.config.programs}
        previous_pitch_chord = {program: -128 for program in self.config.programs}

        events = [Event(*tok.split("_")) for tok in tokens]
        current_tick = 0

        for i in range(len(events)):
            # err_tokens = events[i - 4 : i + 4]  # uncomment for debug
            # Bad token type
            if (
                i > 0
                and events[i].type_ not in self.tokens_types_graph[events[i - 1].type_]
            ):
                err += 1
            # Good token type
            elif events[i].type_ in [
                "NoteOn",
                "PitchIntervalTime",
                "PitchIntervalChord",
            ]:
                if events[i].type_ == "NoteOn":
                    pitch_val = int(events[i].value)
                    previous_pitch_onset[current_program] = pitch_val
                    previous_pitch_chord[current_program] = pitch_val
                elif events[i].type_ == "PitchIntervalTime":
                    pitch_val = previous_pitch_onset[current_program] + int(
                        events[i].value
                    )
                    previous_pitch_onset[current_program] = pitch_val
                    previous_pitch_chord[current_program] = pitch_val
                else:  # PitchIntervalChord
                    pitch_val = previous_pitch_chord[current_program] + int(
                        events[i].value
                    )
                    previous_pitch_chord[current_program] = pitch_val

                active_pitches[current_program][pitch_val].append(current_tick)
                if (
                    self.config.remove_duplicated_notes
                    and pitch_val in current_pitches_tick[current_program]
                ):
                    err += 1  # note already being played at current tick
                    continue

                current_pitches_tick[current_program].append(pitch_val)
            elif events[i].type_ == "NoteOff":
                if len(active_pitches[current_program][int(events[i].value)]) == 0:
                    err += 1  # this pitch wasn't being played
                    continue
                # Check if duration is not exceeding limit
                note_onset_tick = active_pitches[current_program][
                    int(events[i].value)
                ].pop(0)
                duration = current_tick - note_onset_tick
                if max_duration is not None and duration > max_duration:
                    err += 1
            elif events[i].type_ == "Program" and i + 1 < len(events):
                current_program = int(events[i].value)
            elif events[i].type_ in ["TimeShift", "Rest"]:
                current_pitches_tick = {p: [] for p in self.config.programs}
                if events[i].type_ == "TimeShift":
                    current_tick += self._tpb_tokens_to_ticks[ticks_per_beat][
                        events[i].value
                    ]
                else:
                    current_tick += self._tpb_rests_to_ticks[ticks_per_beat][
                        events[i].value
                    ]
            elif events[i].type_ == "TimeSig":
                num, den = self._parse_token_time_signature(events[i].value)
                ticks_per_beat = self._tpb_per_ts[den]
                if max_duration is not None:
                    max_duration = self._time_token_to_ticks(
                        max_duration_str, ticks_per_beat
                    )

        # Check for un-ended notes
        for pitches in active_pitches.values():
            for actives in pitches.values():
                err += len(actives)

        return err
