"""
Unit tests for Anki Audio Normalizer & Voice FX Chain add-on.

Run with: python3 -m pytest tests/ -v
"""

import array
import io
import math
import os
import sys
import wave
from unittest import mock

import pytest

_mocks = {}
for _mod in (
    "aqt",
    "aqt.gui_hooks",
    "aqt.editor",
    "aqt.utils",
    "aqt.qt",
    "anki",
    "anki.hooks",
):
    _mocks[_mod] = mock.MagicMock()
    sys.modules[_mod] = _mocks[_mod]

_mocks["aqt"].gui_hooks = _mocks["aqt.gui_hooks"]
_mocks["aqt"].utils = _mocks["aqt.utils"]
_mocks["aqt"].editor = _mocks["aqt.editor"]
_mocks["aqt"].qt = _mocks["aqt.qt"]
_mocks["aqt"].mw = mock.MagicMock()
_mocks["anki"].hooks = _mocks["anki.hooks"]

_here = os.path.dirname(__file__)
_addon_dir = os.path.dirname(_here)
sys.path.insert(0, _addon_dir)

import __init__ as addon  # noqa: E402


def _make_sine_pcm(sample_rate=24000, duration_s=1.0, amplitude=3000, freq=440.0):
    n = int(sample_rate * duration_s)
    return array.array(
        "h",
        [int(amplitude * math.sin(2.0 * math.pi * freq * i / sample_rate)) for i in range(n)],
    )


class TestSpeakerProtectionCap:
    def test_blocks_values_above_minus_3_dbfs(self):
        assert addon.clamp_target_dbfs(-2.9) == -3.0
        assert addon.clamp_target_dbfs(0.0) == -3.0
        assert addon.clamp_target_dbfs(5.0) == -3.0
        assert addon.clamp_target_dbfs(100.0) == -3.0

    def test_clamps_values_below_minus_30_dbfs(self):
        assert addon.clamp_target_dbfs(-35.0) == -30.0
        assert addon.clamp_target_dbfs(-100.0) == -30.0

    def test_allows_safe_range(self):
        assert addon.clamp_target_dbfs(-14.0) == -14.0
        assert addon.clamp_target_dbfs(-3.0) == -3.0
        assert addon.clamp_target_dbfs(-30.0) == -30.0

    def test_handles_invalid_inputs(self):
        assert addon.clamp_target_dbfs("invalid") == addon.DEFAULT_TARGET_DBFS
        assert addon.clamp_target_dbfs(None) == addon.DEFAULT_TARGET_DBFS
        assert addon.clamp_target_dbfs(float("nan")) == addon.DEFAULT_TARGET_DBFS
        assert addon.clamp_target_dbfs(float("inf")) == addon.DEFAULT_TARGET_DBFS

    def test_get_config_enforces_cap_even_if_config_json_has_dangerous_value(self):
        mw = mock.MagicMock()
        mw.addonManager.getConfig.return_value = {"target_dbfs": 6.0}
        with mock.patch.object(addon, "mw", mw):
            cfg = addon._get_config()
        assert cfg["target_dbfs"] == -3.0


class TestDsmNormalizationAndLimiter:
    def test_lifts_quiet_voice_to_target_dbfs(self):
        pcm = _make_sine_pcm(sample_rate=24000, duration_s=1.0, amplitude=2000)
        cfg = dict(addon.DEFAULT_CONFIG, target_dbfs=-14.0, fade_edges_enabled=False)
        out, gain, pre_db, post_db = addon.normalize_pcm(pcm, 24000, 1, cfg)
        assert out is not None
        assert gain > 3.0
        assert pre_db < -25.0
        assert abs(post_db - (-14.0)) < 0.4

    def test_never_exceeds_minus_3_dbfs_even_if_configured_higher(self):
        pcm = _make_sine_pcm(sample_rate=24000, duration_s=1.0, amplitude=2000)
        cfg = dict(addon.DEFAULT_CONFIG, target_dbfs=10.0, fade_edges_enabled=False)
        out, gain, pre_db, post_db = addon.normalize_pcm(pcm, 24000, 1, cfg)
        assert out is not None
        assert post_db <= -3.0 + 0.2
        peak_dbfs = 20.0 * math.log10(max(abs(x) for x in out) / addon.SHORT_MAX)
        assert peak_dbfs <= -0.95

    def test_ignores_silent_pauses_via_voice_gate(self):
        voiced = _make_sine_pcm(sample_rate=24000, duration_s=0.5, amplitude=2000)
        silence = array.array("h", [0] * 24000)
        combined = voiced + silence
        cfg = dict(addon.DEFAULT_CONFIG, target_dbfs=-14.0, fade_edges_enabled=False)
        _, gain_voiced, _, _ = addon.normalize_pcm(voiced, 24000, 1, cfg)
        _, gain_combined, _, _ = addon.normalize_pcm(combined, 24000, 1, cfg)
        assert abs(gain_voiced - gain_combined) < 0.01

    def test_skips_clip_already_at_target(self):
        # -14.0 dBFS RMS sine wave -> amplitude = sqrt(2) * 32767 * 10^(-14/20) ~= 9246
        pcm = _make_sine_pcm(sample_rate=24000, duration_s=1.0, amplitude=9246)
        cfg = dict(addon.DEFAULT_CONFIG, target_dbfs=-14.0)
        out, gain, _, _ = addon.normalize_pcm(pcm, 24000, 1, cfg)
        assert out is None
        assert abs(gain - 1.0) < 0.05

    def test_soft_limiter_rounds_transient_shout_without_clipping(self):
        pcm = _make_sine_pcm(sample_rate=24000, duration_s=1.0, amplitude=3000)
        # Add a single loud transient spike
        pcm[1000] = 32000
        cfg = dict(addon.DEFAULT_CONFIG, target_dbfs=-14.0, soft_limiter_enabled=True)
        out, gain, _, post_db = addon.normalize_pcm(pcm, 24000, 1, cfg)
        assert out is not None
        assert max(abs(x) for x in out) <= int(addon.SHORT_MAX * addon.PEAK_CEILING) + 1

    def test_fade_edges_smooths_boundaries(self):
        pcm = array.array("h", [10000] * 2400)
        faded = addon.fade_edges_pcm(pcm, 24000, 1, fade_ms=12)
        assert faded[0] == 0
        assert faded[-1] == 0
        assert faded[1200] == 10000


class TestFxChainAndExtraction:
    def test_build_ffmpeg_af_all_enabled(self):
        af = addon.build_ffmpeg_af(addon.DEFAULT_CONFIG)
        assert "highpass=f=80" in af
        assert "equalizer=f=250" in af
        assert "equalizer=f=2800" in af
        assert "equalizer=f=6300" in af
        assert "acompressor=" in af

    def test_build_ffmpeg_af_all_disabled(self):
        cfg = dict(
            addon.DEFAULT_CONFIG,
            highpass_enabled=False,
            voice_eq_enabled=False,
            compressor_enabled=False,
        )
        assert addon.build_ffmpeg_af(cfg) == ""

    def test_get_note_target_audio_files_filters_sentence_fields_only(self):
        note = mock.MagicMock()
        note.items.return_value = [
            ("Word Audio", "[sound:word.mp3]"),
            ("Audio", "[sound:sentence1.mp3] [sound:sentence2.ogg]"),
            ("Sentence", "テスト"),
        ]
        files = addon.get_note_target_audio_files(note, addon.DEFAULT_CONFIG)
        assert files == ["sentence1.mp3", "sentence2.ogg"]

    def test_normalize_audio_file_end_to_end_with_ffmpeg(self, tmp_path):
        wav_path = str(tmp_path / "quiet.mp3")
        pcm = _make_sine_pcm(sample_rate=24000, duration_s=1.0, amplitude=1500)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm.tobytes())
        import subprocess
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "wav", "-i", "pipe:0", "-c:a", "libmp3lame", "-q:a", "2", wav_path],
            input=buf.getvalue(),
            check=True,
        )
        cache = {}
        res1 = addon.normalize_audio_file(wav_path, config=addon.DEFAULT_CONFIG, force=False, cache=cache)
        assert res1["status"] == "normalized"
        assert abs(res1["post_dbfs"] - (-14.0)) < 1.0

        # Second call without force uses cache
        res2 = addon.normalize_audio_file(wav_path, config=addon.DEFAULT_CONFIG, force=False, cache=cache)
        assert res2["status"] == "skipped"
        assert res2["reason"] == "cached"

        # Verify no duplicate or temp files were created in the media directory
        assert os.listdir(str(tmp_path)) == ["quiet.mp3"]

    def test_in_place_overwrite_preserves_inode_and_creates_no_duplicates(self, tmp_path):
        mp3_path = str(tmp_path / "sentence.mp3")
        pcm = _make_sine_pcm(sample_rate=24000, duration_s=1.0, amplitude=1500)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm.tobytes())
        import subprocess
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "wav", "-i", "pipe:0", "-c:a", "libmp3lame", "-q:a", "2", mp3_path],
            input=buf.getvalue(),
            check=True,
        )
        inode_before = os.stat(mp3_path).st_ino
        res = addon.normalize_audio_file(mp3_path, config=addon.DEFAULT_CONFIG, force=True, cache={})
        assert res["status"] == "normalized"
        assert os.stat(mp3_path).st_ino == inode_before
        assert os.listdir(str(tmp_path)) == ["sentence.mp3"]


