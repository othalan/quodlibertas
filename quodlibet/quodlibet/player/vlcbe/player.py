# -*- coding: utf-8 -*-
# Copyright 2018 David Morris
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

###############################################################################
#
# VLC Backend
#
# This is a backend for the VLC media player using their python interface
# "python-vlc" available on PyPi and licensed under the GPL.
#
###############################################################################

from gi.repository import GLib

from quodlibet.player._base import BasePlayer
from quodlibet.util import print_d
from quodlibet.util import MainRunner
from quodlibet.util import MainRunnerError
from quodlibet.util import MainRunnerAbortedError
from quodlibet.util import MainRunnerTimeoutError
from quodlibet import util

import vlc


class VLCPlayer(BasePlayer):
    _paused = True     # Current Pause State
    _vlcmp = None      # The VLC MediaPlayer object
    _vlceq = None      # The VLC Equalizer pointer
    _eq_preamp = 0.0   # Equalizer Preamp Value
    _volume = 1.0      # Volume property storage
    _seekOnPlay = None # Location to seek to on next play
    _runner = None     # MainRunner instance to change async events to sync

    def __init__(self, librarian=None):
        super().__init__()
        self._librarian = librarian
        self._runner = MainRunner()

    @property
    def paused(self):
        return self._paused

    @paused.setter
    def paused(self, state):
        print_d(f"Pause Set to State [{state}]")

        # Detect if pause is not possible and alter the action accordingly
        if self._vlcmp is None or self._vlcmp.can_pause() == 0:
            state = True

        # Change the internal tracking
        prev_state = self._paused
        self._paused = state

        # Only emit a signal if the pause state changed
        if state != prev_state:
            # Emit a signal telling the application a pause/unpause has
            # occurred
            self.emit((self._paused and 'paused') or 'unpaused')

        # The signal handler might have changed the paused state
        # ... no matter what happens, set VLC to the current tracked pause
        #     state
        # ... but only if the vlc object exists!
        if self._vlcmp is not None:
            self._vlcmp.set_pause(self._paused)
        # Just to be certain eveything is consistent
        # ... If there is no player object, by definition we are paused
        else:
            self._paused = True

    def do_get_property(self, property):
        if property.name == 'volume':
            return self._volume
        elif property.name == 'seekable':
            if self.song is None:
                return False
            return True
        elif property.name == 'mute':
            if self._vlcmp is not None:
                isMuted = self._vlcmp.audio_get_mute()
                muteStates = {-1: False, 0: False, 1: True}
                return muteStates[isMuted]
            return False
        else:
            raise AttributeError

    def do_set_property(self, property, v):
        if property.name == 'volume':
            self._volume = v
            if self._vlcmp is not None:
                print_d("calc v")
                v = self.calc_replaygain_volume(v)
                v = min(100, int(v * 100))
                print_d(f"set v: [{self._volume}] [{v}]")
                self._vlcmp.audio_set_volume(v)
                print_d("set v complete")
        elif property.name == 'mute':
            if self._vlcmp is not None:
                self._vlcmp.audio_set_mute(v)
        else:
            raise AttributeError

    def _destroy(self):
        """Clean up"""
        if self._vlcmp is not None:
            self._vlcmp.release()
            self._vlcmp = None
            self._events = None
        self._runner.abort()

    def _end(self, stopped, next_song=None):
        """Start playing the current song from the source or
        next_song if it isn't None.
        """
        print_d("End song")

        # We need to set self.song to None before calling our signal
        # handlers. Otherwise, if they try to end the song they're given
        # (e.g. by removing it), then we get in an infinite loop.
        song, info = self.song, self.info
        self.song = self.info = None
        if song is not info:
            self.emit('song-ended', info, stopped)
        self.emit('song-ended', song, stopped)

        current = self._source.current if next_song is None else next_song

        # Then, set up the next song.
        self.song = current
        self.info = current

        if self.song is not None:
            print_d("Next Song: %s" % self.song("~uri"))

            self._play()
        else:
            self._stop()

    def _play(self, seek=None):
        print_d("Playing current media with seek [%s]" % seek)

        if self._vlcmp is None:
            print_d("Creating New Media Player")
            # Create the new media player for the current song
            self._vlcmp = vlc.MediaPlayer(self.song("~uri"))

            # Connect to useful events
            # ... this is how we know what the media player is doing
            # ... also how we take action on media palyer state changes
            self._events = self._vlcmp.event_manager()
            self._events.event_attach(vlc.EventType.MediaPlayerPlaying,
                                      self._event_callback_async)
            self._events.event_attach(vlc.EventType.MediaPlayerEndReached,
                                      self._event_callback_async)

            # Setup the equalizer, if one exists
            if self._vlceq is not None:
                self._vlcmp.set_equalizer(self._vlceq)

        else:
            print_d("Reusing Existing Media Player")
            # Reuse the existing media player
            self._vlcmp.set_mrl(self.song("~uri"))

        # Save the seek location
        # ... seek happens on the VLC event MediaPlayerPlaying
        self._seekOnPlay = seek

        # Start the media playing
        self._vlcmp.play()

    def setup(self, playlist, song, seek_pos):
        # VLC cannot seek immediately at startup, perform seek by event instead
        self._seekOnPlay = seek_pos

        super().setup(playlist, song, seek_pos)

    def _stop(self):
        if self._vlcmp is not None:
            print_d("Destroying VLC Player Backend")

            # Release the player so that VLC performs internal cleanup
            # ... but only if actively playing, because it doesn't seem
            #     necessary otherwise
            # ... this is used instead of "stop" in order to ensure proper
            #     cleanup
            if self._vlcmp.get_state() in [vlc.State.Playing,
                                           vlc.State.Paused]:
                self._vlcmp.release()
                print_d("Release Complete")
            else:
                print_d("Not Releasing (not playing)")

            # Remove our references to the player
            self._vlcmp = None
            self._events = None

            # Note that the equalizer does not need to be released
            # ... equalizer objects are independent of the media player

    def stop(self):
        """Stop playback and reset the position."""
        super().stop()
        self._stop()

    def _event_callback_async(self, event):
        try:
            if event.type == vlc.EventType.MediaPlayerPlaying:
                callback = self._event_playing_sync
            elif event.type == vlc.EventType.MediaPlayerEndReached:
                callback = self._event_ended_sync

            self._runner.call(callback,
                                event,
                                priority=GLib.PRIORITY_HIGH,
                                timeout=0.5)
        except MainRunnerTimeoutError as e:
            # Due to some locks being held during this signal we can get
            # into a deadlock when a seek or state change event happens
            # in the mainloop before our function gets scheduled.
            #
            # XXX In this case abort and do nothing, which results in ???
            print_d("EVENT Play (async): %s" % e)
            return
        except MainRunnerAbortedError as e:
            print_d("EVENT Play (async): %s" % e)
            return
        except MainRunnerError:
            util.print_exc()
            return

    def _event_playing_sync(self, event):
        print_d("Playing Event (sync) [%s] seek [%s]" % (
            self._paused, self._seekOnPlay))

        # Set replay gain volume
        # XXX This causes VLC to hang ... need to wait a bit before use?
        #self.volume = self.volume

        print_d("Volume Set Complete")

        # Set the current pause state in the player to align with
        # the current requested pause state
        if self._paused:
            self.paused = self._paused

        # Notify the rest of the application of the state change
        self.emit('song-started', self.song)
        self.notify("seekable")

        # This should really be handled by the seekable event
        # ... However, it seems that when the seekable event is triggered, the
        #     length is not available
        if self._seekOnPlay is not None and self._vlcmp.is_seekable():
            self._vlcmp.set_position(self._seekOnPlay
                                   / self._vlcmp.get_length())
            print_d("VLC Position Set")
            # Notify the rest of the applicaiton that the seek happened
            self.emit('seek', self.song, self._seekOnPlay)

            # Reset the seek indicator so it doesn't accidentally get resused
            self._seekOnPlay = None

        print_d("Song play startup complete!")

    def _event_ended_sync(self, event):
        print_d("Playback Ended")

        # NOTE: Ideally, we would not destroy the media player here because it
        # could be reused during continuous playback. The media player object
        # will be destroyed by the _end() method if necessary.
        #
        # However, experimentation indicates that media player object reuse
        # does not work so well.  For now, simply destroy the old media player
        # object and a new one will be created.
        self._stop()

        # Tell the source that the song ended
        self._source.next_ended()

        # Start the next song
        self._end(False)

    def seek(self, position):
        """Seek to absolute position in milliseconds.
        If position is larger than the duration start the next song
        """
        print_d("Seeking to position [%s]" % position)

        if self._vlcmp is not None:
            # XXX Detect if we should skip to the next song !?!?
            if self._vlcmp.get_state() == vlc.State.Paused:
                self._vlcmp.set_position(position / self._vlcmp.get_length())
                self.emit('seek', self.song, position)

            elif self._vlcmp.get_state() == vlc.State.Playing:
                self._vlcmp.set_position(position / self._vlcmp.get_length())
                self.emit('seek', self.song, position)

            else:
                self._vlcmp.stop()

                # VLC can only seek while playing so store the seek value for
                # later
                self._seekOnPlay = position

                # Tell VLC to start playing
                # ... but the custom event handler will pause playback as
                #     necessary
                self._vlcmp.play()

        elif self.song is not None:
            self._play(position)

    def get_position(self):
        """Return the current playback position in milliseconds,
        or 0 if no song is playing."""
        if self._vlcmp is not None and self._vlcmp.get_state() in [
                vlc.State.Playing, vlc.State.Paused]:
            return int(self._vlcmp.get_position() * self._vlcmp.get_length())
        else:
            return 0

    def can_play_uri(self, uri):
        """Whether the player supports playing the given URI scheme"""

        # XXX Implement Me!
        return True

    @property
    def eq_bands(self):
        """read-only list of equalizer bands (in Hz) supported."""

        eqBands = []
        for band in range(vlc.libvlc_audio_equalizer_get_band_count()):
            eqBands.append(vlc.libvlc_audio_equalizer_get_band_frequency(band))

        return eqBands

    def update_eq_values(self):
        """Set equalizer values in the backend"""

        # Always release any previous equalizer, if it exists
        vlc.libvlc_audio_equalizer_release(self._vlceq)

        # Always start from a flat equalizer
        # ... because QuodLibet has no master preamp setting
        # ... so this gives a consistent preamp starting point
        self._vlceq = vlc.libvlc_audio_equalizer_new_from_preset(0)

        # Only configure the equalizer if there are non-zero values
        # ... otherwise the VLC defaults will be used
        if any(self._eq_values):
            for band, val in enumerate(self._eq_values):
                # NOTE: VLC equalizers have a range [-20,20], different from
                #       QuodLibet! This will be handled automatically by the
                #       VLC backend.
                vlc.libvlc_audio_equalizer_set_amp_at_index(self._vlceq,
                                                            val, band)

        # Set the equalizer if the media player exists
        if self._vlcmp is not None:
            self._vlcmp.set_equalizer(self._vlceq)

    @property
    def eq_has_preamp(self):
        """If the equalizer has a preamp."""

        return True

    @property
    def eq_preamp(self):
        """The equalizer preamp value.

        Only vaid if eq_has_preamp() == True"""

        return self._eq_preamp

    @eq_preamp.setter
    def eq_preamp(self, value):
        """Set the equalizer preamp value.

        Only vaid if eq_has_preamp() == True"""

        self._eq_preamp = value

        if self._vlceq is not None:
            vlc.libvlc_audio_equalizer_set_preamp(self._vlceq, value)

    @property
    def eq_preset_list(self):
        """List of presets provided by the equalizer."""
        presetList = []

        for preset in range(vlc.libvlc_audio_equalizer_get_preset_count()):
            bname = vlc.libvlc_audio_equalizer_get_preset_name(preset)
            name = ('VLC: ' + bname.decode('utf-8'))
            presetEq = vlc.libvlc_audio_equalizer_new_from_preset(preset)
            preamp = vlc.libvlc_audio_equalizer_get_preamp(presetEq)
            bands = []

            for band in range(vlc.libvlc_audio_equalizer_get_band_count()):
                bands.append(vlc.libvlc_audio_equalizer_get_amp_at_index(
                    presetEq, band))

            vlc.libvlc_audio_equalizer_release(presetEq)

            presetList.append(self.EqualizerPreset(name, bands, preamp))

        return presetList

    @property
    def eq_range(self):
        """Return the (min,max) equalizer range."""

        return (-20, 20)


def init(librarian):
    return VLCPlayer(librarian)
