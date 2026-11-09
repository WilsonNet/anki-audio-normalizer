import array
import io
import json
import math
import os
import re
import subprocess
import tempfile
import wave

from anki import hooks as anki_hooks
from aqt import gui_hooks, mw
from aqt.qt import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    Qt,
    QVBoxLayout,
)
from aqt.utils import qconnect, showInfo, showWarning, tooltip

ADDON_DIR = os.path.dirname(__file__)
USER_FILES_DIR = os.path.join(ADDON_DIR, "user_files")
CACHE_PATH = os.path.join(USER_FILES_DIR, "normalized_cache.json")

MIN_TARGET_DBFS = -30.0
MAX_TARGET_DBFS = -3.0
DEFAULT_TARGET_DBFS = -14.0

SHORT_MAX = 32767.0
GATE_DBFS = -50.0
PEAK_CEILING = 0.89  # -1.0 dBFS hard ceiling
LIMIT_HEADROOM = 2.0  # 6 dB transient headroom into soft limiter
LIMIT_KNEE = 0.7
MAX_GAIN = 20.0
FADE_MS = 12

AUDIO_EXTS = {".mp3", ".ogg", ".opus", ".m4a", ".wav"}

DEFAULT_CONFIG = {
    "target_dbfs": DEFAULT_TARGET_DBFS,
    "auto_normalize": True,
    "audio_fields": "Audio, Sentence Audio",
    "highpass_enabled": True,
    "highpass_hz": 80,
    "voice_eq_enabled": True,
    "compressor_enabled": True,
    "soft_limiter_enabled": True,
    "fade_edges_enabled": True,
    "shortcut": "Ctrl+Shift+N",
}

_trace_log = []


def _trace(msg):
    from datetime import datetime
    _trace_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(_trace_log) > 200:
        _trace_log[:] = _trace_log[-100:]


def clamp_target_dbfs(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TARGET_DBFS
    if math.isnan(v) or math.isinf(v):
        return DEFAULT_TARGET_DBFS
    return max(MIN_TARGET_DBFS, min(MAX_TARGET_DBFS, v))


def _get_config():
    try:
        config = mw.addonManager.getConfig(__name__)
    except Exception:
        config = None
    if not isinstance(config, dict):
        config = {}
    merged = {}
    for key, value in DEFAULT_CONFIG.items():
        merged[key] = config.get(key, value)
    for key, value in config.items():
        if key not in merged:
            merged[key] = value
    merged["target_dbfs"] = clamp_target_dbfs(merged.get("target_dbfs", DEFAULT_TARGET_DBFS))
    return merged


def _save_config(config):
    safe = dict(config)
    safe["target_dbfs"] = clamp_target_dbfs(safe.get("target_dbfs", DEFAULT_TARGET_DBFS))
    mw.addonManager.writeConfig(__name__, safe)


def parse_audio_fields(fields_str):
    if not fields_str or not str(fields_str).strip():
        return []
    raw = str(fields_str).strip()
    if raw == "*":
        return []
    return [f.strip() for f in raw.split(",") if f.strip()]


def extract_audio_filenames(field_html):
    if not field_html:
        return []
    return [m.strip() for m in re.findall(r"\[sound:(.*?)\]", field_html) if m.strip()]


def extract_first_audio(field_html):
    files = extract_audio_filenames(field_html)
    return files[0] if files else None


def _fx_signature(config):
    target = clamp_target_dbfs(config.get("target_dbfs", DEFAULT_TARGET_DBFS))
    hp = int(config.get("highpass_hz", 80)) if config.get("highpass_enabled", True) else 0
    eq = 1 if config.get("voice_eq_enabled", True) else 0
    comp = 1 if config.get("compressor_enabled", True) else 0
    lim = 1 if config.get("soft_limiter_enabled", True) else 0
    fade = 1 if config.get("fade_edges_enabled", True) else 0
    return f"t={target:.1f}|hp={hp}|eq={eq}|comp={comp}|lim={lim}|fade={fade}"


def _load_cache():
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        _trace(f"cache load error: {e}")
    return {}


def _save_cache(cache):
    try:
        os.makedirs(USER_FILES_DIR, exist_ok=True)
        if len(cache) > 25000:
            keys = list(cache.keys())[-20000:]
            cache = {k: cache[k] for k in keys}
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        _trace(f"cache save error: {e}")


def _is_cached(filepath, config, cache):
    try:
        st = os.stat(filepath)
    except OSError:
        return False
    fname = os.path.basename(filepath)
    entry = cache.get(fname)
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("mtime") == int(st.st_mtime)
        and entry.get("size") == int(st.st_size)
        and entry.get("sig") == _fx_signature(config)
    )


def _mark_cached(filepath, config, cache):
    try:
        st = os.stat(filepath)
    except OSError:
        return
    fname = os.path.basename(filepath)
    cache[fname] = {
        "mtime": int(st.st_mtime),
        "size": int(st.st_size),
        "sig": _fx_signature(config),
    }


# ---------------------------------------------------------------------------
# Ear-friendly FX chain + DSM Voiced-RMS Normalizer
# ---------------------------------------------------------------------------

def build_ffmpeg_af(config):
    filters = []
    if config.get("highpass_enabled", True):
        hz = max(30, min(250, int(config.get("highpass_hz", 80))))
        filters.append(f"highpass=f={hz}:poles=2")

    if config.get("voice_eq_enabled", True):
        # 1) Cut boxy low-mid mud at 250 Hz (-1.5 dB)
        # 2) Gentle vocal presence clarity boost at 2.8 kHz (+1.5 dB)
        # 3) Tame piercing sibilance/harshness at 6.3 kHz (-2.5 dB)
        filters.append("equalizer=f=250:t=q:w=1.2:g=-1.5")
        filters.append("equalizer=f=2800:t=q:w=1.0:g=1.5")
        filters.append("equalizer=f=6300:t=q:w=2.0:g=-2.5")

    if config.get("compressor_enabled", True):
        # Smooth soft-knee voice compressor (-24 dBFS, 3:1, 8ms attack, 150ms release)
        filters.append("acompressor=threshold=-24dB:ratio=3:attack=8:release=150:knee=6:makeup=1")

    return ",".join(filters)


def soft_limit_sample(x):
    knee = PEAK_CEILING * LIMIT_KNEE
    a = abs(x)
    if a <= knee:
        return x
    room = PEAK_CEILING - knee
    return math.copysign(knee + room * math.tanh((a - knee) / room), x)


def fade_edges_pcm(pcm, sample_rate, channels, fade_ms=FADE_MS):
    if channels <= 0 or sample_rate <= 0:
        return pcm
    total_frames = len(pcm) // channels
    fade_frames = max(1, (fade_ms * sample_rate) // 1000)
    if total_frames <= fade_frames * 2:
        return pcm
    out = array.array("h", pcm)
    for frame in range(fade_frames):
        gain = frame / fade_frames
        base_in = frame * channels
        base_out = (total_frames - 1 - frame) * channels
        for ch in range(channels):
            out[base_in + ch] = int(math.floor(out[base_in + ch] * gain + 0.5))
            out[base_out + ch] = int(math.floor(out[base_out + ch] * gain + 0.5))
    return out


def normalize_pcm(pcm, sample_rate, channels, config=None):
    """
    Port of Droid Sentence Miner's AudioPcm.normalize with speaker-protection cap (-3.0 dBFS max).
    Returns (out_pcm_or_None, gain, pre_rms_dbfs, post_rms_dbfs).
    """
    if config is None:
        config = DEFAULT_CONFIG
    if not pcm or sample_rate <= 0 or channels <= 0:
        return None, 1.0, None, None

    target_dbfs = clamp_target_dbfs(config.get("target_dbfs", DEFAULT_TARGET_DBFS))
    soft_limiter = bool(config.get("soft_limiter_enabled", True))
    fade_edges = bool(config.get("fade_edges_enabled", True))

    frame = max(1, (sample_rate * channels) // 50)
    gate = SHORT_MAX * (10.0 ** (GATE_DBFS / 20.0))
    gate_sq = gate * gate

    total_sum = 0.0
    total_count = 0
    n = len(pcm)

    min_val = min(pcm)
    max_val = max(pcm)
    peak = max(abs(min_val), abs(max_val))
    if peak == 0:
        return None, 1.0, None, None

    for i in range(0, n, frame):
        chunk = pcm[i : min(i + frame, n)]
        clen = len(chunk)
        fsum = float(sum(v * v for v in chunk))
        if fsum >= gate_sq * clen:
            total_sum += fsum
            total_count += clen

    if total_count == 0:
        return None, 1.0, None, None

    rms = math.sqrt(total_sum / total_count)
    pre_rms_dbfs = 20.0 * math.log10(rms / SHORT_MAX)

    headroom = LIMIT_HEADROOM if soft_limiter else 1.0
    gain = min(
        SHORT_MAX * (10.0 ** (target_dbfs / 20.0)) / rms,
        SHORT_MAX * PEAK_CEILING * headroom / peak,
        MAX_GAIN,
    )

    if abs(gain - 1.0) < 0.05:
        return None, gain, pre_rms_dbfs, pre_rms_dbfs

    ceiling_int = int(SHORT_MAX * PEAK_CEILING)

    def _map_int16(v):
        if soft_limiter:
            x = soft_limit_sample(v * gain / SHORT_MAX)
            scaled = math.floor(x * SHORT_MAX + 0.5)
        else:
            scaled = math.floor(v * gain + 0.5)
            scaled = max(-ceiling_int, min(ceiling_int, scaled))
        return max(-32768, min(32767, int(scaled)))

    lut = [_map_int16((i - 65536) if i >= 32768 else i) for i in range(65536)]
    u16 = array.array("H")
    u16.frombytes(pcm.tobytes())
    out = array.array("h", (lut[x] for x in u16))

    if fade_edges:
        out = fade_edges_pcm(out, sample_rate, channels, FADE_MS)

    post_sum = 0.0
    post_count = 0
    for i in range(0, len(out), frame):
        chunk = out[i : min(i + frame, len(out))]
        clen = len(chunk)
        fsum = float(sum(v * v for v in chunk))
        if fsum >= gate_sq * clen:
            post_sum += fsum
            post_count += clen
    post_rms_dbfs = (
        20.0 * math.log10(math.sqrt(post_sum / post_count) / SHORT_MAX)
        if post_count > 0
        else pre_rms_dbfs
    )

    return out, gain, pre_rms_dbfs, post_rms_dbfs


def _encode_args_for_ext(ext):
    ext = ext.lower()
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if ext == ".ogg":
        return ["-c:a", "libvorbis", "-q:a", "5"]
    if ext == ".opus":
        return ["-c:a", "libopus", "-b:a", "96k"]
    if ext == ".m4a":
        return ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    raise ValueError(f"Unsupported audio extension: {ext}")


def normalize_audio_file(filepath, config=None, force=False, cache=None):
    """
    Decode -> FX chain (HPF + Voice EQ + Compressor) -> Voiced RMS Normalize (-3 dBFS cap) + Soft Limiter -> Encode in-place.
    Returns dict with status ('normalized', 'skipped', 'error') and metrics.
    """
    if config is None:
        config = _get_config()
    config = dict(config)
    config["target_dbfs"] = clamp_target_dbfs(config.get("target_dbfs", DEFAULT_TARGET_DBFS))

    if not os.path.exists(filepath):
        return {"status": "error", "reason": "file_not_found"}

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in AUDIO_EXTS:
        return {"status": "skipped", "reason": "unsupported_ext"}

    own_cache = cache is None
    if own_cache:
        cache = _load_cache()

    if not force and _is_cached(filepath, config, cache):
        return {"status": "skipped", "reason": "cached"}

    af = build_ffmpeg_af(config)
    dec_cmd = ["ffmpeg", "-v", "error", "-i", filepath]
    if af:
        dec_cmd.extend(["-af", af])
    dec_cmd.extend(["-c:a", "pcm_s16le", "-f", "wav", "pipe:1"])

    try:
        dec = subprocess.run(dec_cmd, capture_output=True, timeout=30)
        if dec.returncode != 0 or not dec.stdout:
            _trace(f"ffmpeg decode failed for {filepath}: {dec.stderr.decode('utf-8', 'ignore')[:200]}")
            return {"status": "error", "reason": "decode_failed"}

        with wave.open(io.BytesIO(dec.stdout), "rb") as w:
            channels = w.getnchannels()
            sample_rate = w.getframerate()
            raw = w.readframes(w.getnframes())
            pcm = array.array("h")
            pcm.frombytes(raw)
    except Exception as e:
        _trace(f"decode exception for {filepath}: {e}")
        return {"status": "error", "reason": str(e)}

    out_pcm, gain, pre_db, post_db = normalize_pcm(pcm, sample_rate, channels, config)
    if out_pcm is None:
        _mark_cached(filepath, config, cache)
        if own_cache:
            _save_cache(cache)
        return {
            "status": "skipped",
            "reason": "already_at_target",
            "gain": gain,
            "pre_dbfs": pre_db,
            "post_dbfs": post_db,
        }

    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(out_pcm.tobytes())
    wav_bytes = wav_buf.getvalue()

    fd, tmp_path = tempfile.mkstemp(
        suffix=ext,
        dir=tempfile.gettempdir(),
        prefix=".anki_norm_",
    )
    os.close(fd)

    try:
        enc_cmd = (
            ["ffmpeg", "-v", "error", "-y", "-f", "wav", "-i", "pipe:0"]
            + _encode_args_for_ext(ext)
            + [tmp_path]
        )
        enc = subprocess.run(enc_cmd, input=wav_bytes, capture_output=True, timeout=30)
        if enc.returncode != 0 and ext == ".ogg":
            enc_cmd = [
                "ffmpeg", "-v", "error", "-y", "-f", "wav", "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", "96k", tmp_path,
            ]
            enc = subprocess.run(enc_cmd, input=wav_bytes, capture_output=True, timeout=30)

        if enc.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            _trace(f"ffmpeg encode failed for {filepath}: {enc.stderr.decode('utf-8', 'ignore')[:200]}")
            return {"status": "error", "reason": "encode_failed"}

        # Hard in-place modification of the original file in collection.media
        # (never creates a second file or duplicate in collection.media).
        with open(tmp_path, "rb") as src:
            new_bytes = src.read()
        os.unlink(tmp_path)
        with open(filepath, "wb") as dst:
            dst.write(new_bytes)

        _mark_cached(filepath, config, cache)
        if own_cache:
            _save_cache(cache)
        _trace(
            f"normalized {os.path.basename(filepath)}: "
            f"{pre_db:.1f} -> {post_db:.1f} dBFS (gain {gain:.2f}x)"
        )
        return {
            "status": "normalized",
            "gain": gain,
            "pre_dbfs": pre_db,
            "post_dbfs": post_db,
        }
    except Exception as e:
        _trace(f"encode exception for {filepath}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return {"status": "error", "reason": str(e)}


# ---------------------------------------------------------------------------
# Note audio extraction & hook handlers
# ---------------------------------------------------------------------------

def get_note_target_audio_files(note, config=None):
    if config is None:
        config = _get_config()
    allowed_fields = parse_audio_fields(config.get("audio_fields", ""))
    allowed_lower = {f.lower() for f in allowed_fields}
    files = []
    try:
        items = note.items()
    except Exception:
        return files

    for field_name, field_val in items:
        if allowed_lower and field_name.lower() not in allowed_lower:
            continue
        for fname in extract_audio_filenames(field_val):
            if fname not in files:
                files.append(fname)
    return files


def normalize_note_audio(note, config=None, force=False, media_dir=None):
    if config is None:
        config = _get_config()
    if media_dir is None:
        try:
            media_dir = mw.col.media.dir()
        except Exception:
            return []
    if not media_dir:
        return []

    filenames = get_note_target_audio_files(note, config)
    if not filenames:
        return []

    cache = _load_cache()
    results = []
    for fname in filenames:
        full_path = os.path.join(media_dir, fname)
        if os.path.exists(full_path):
            res = normalize_audio_file(full_path, config=config, force=force, cache=cache)
            res["filename"] = fname
            results.append(res)
    _save_cache(cache)
    return results


def _on_note_will_be_added(col, note, deck_id):
    config = _get_config()
    if not config.get("auto_normalize", True):
        return
    try:
        media_dir = col.media.dir()
        normalize_note_audio(note, config=config, force=False, media_dir=media_dir)
    except Exception as e:
        _trace(f"_on_note_will_be_added error: {e}")


def _on_note_will_flush(note):
    config = _get_config()
    if not config.get("auto_normalize", True):
        return
    try:
        col = getattr(note, "col", None)
        media_dir = col.media.dir() if col is not None else None
        normalize_note_audio(note, config=config, force=False, media_dir=media_dir)
    except Exception as e:
        _trace(f"_on_note_will_flush error: {e}")


def _on_add_cards_did_add_note(note):
    config = _get_config()
    if not config.get("auto_normalize", True):
        return
    try:
        normalize_note_audio(note, config=config, force=False)
    except Exception as e:
        _trace(f"_on_add_cards_did_add_note error: {e}")


def _on_editor_did_unfocus_field(changed, note, current_field_idx):
    config = _get_config()
    if not config.get("auto_normalize", True):
        return False
    try:
        normalize_note_audio(note, config=config, force=False)
    except Exception as e:
        _trace(f"_on_editor_did_unfocus_field error: {e}")
    return False


def normalize_editor_note(editor):
    note = getattr(editor, "note", None)
    if note is None:
        showWarning("No note loaded in editor.")
        return
    config = _get_config()
    results = normalize_note_audio(note, config=config, force=True)
    if not results:
        fields_desc = config.get("audio_fields", "Audio") or "all fields"
        tooltip(f"No sentence audio found in configured fields ({fields_desc}).")
        return
    normed = [r for r in results if r.get("status") == "normalized"]
    if normed:
        r0 = normed[0]
        tooltip(
            f"Normalized {len(normed)} audio clip(s): "
            f"{r0['pre_dbfs']:.1f} → {r0['post_dbfs']:.1f} dBFS"
        )
    else:
        tooltip(f"Audio already at target loudness ({config['target_dbfs']:.1f} dBFS).")


def _on_editor_buttons(buttons, editor):
    config = _get_config()
    btn = editor.addButton(
        icon=None,
        cmd="anki_audio_normalizer",
        func=normalize_editor_note,
        tip=f"Normalize audio in '{config.get('audio_fields', 'Audio')}' ({config.get('shortcut', 'Ctrl+Shift+N')})",
        label="Norm",
        keys=config.get("shortcut", "Ctrl+Shift+N"),
    )
    buttons.append(btn)


# ---------------------------------------------------------------------------
# UI Dialog: Target Loudness (capped at -3 dBFS), FX Chain, and Batch Runner
# ---------------------------------------------------------------------------

def describe_loudness(dbfs):
    dbfs = clamp_target_dbfs(dbfs)
    if dbfs >= -6.0:
        return "Very Loud (Near -3 dBFS speaker safety limit)"
    if dbfs >= -11.0:
        return "Loud & Forward (Great for quiet laptop/tablet speakers)"
    if dbfs >= -16.0:
        return "Recommended Studio Voice (-14 dBFS DSM standard)"
    if dbfs >= -21.0:
        return "Gentle / Broadcast (-18 dBFS relaxed listening)"
    return "Soft / Background level"


class NormalizerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._config = _get_config()
        self._set_up_ui()
        self._load_from_config()

    def _set_up_ui(self):
        self.setWindowTitle("Audio Normalizer & Ear-Friendly FX Chain")
        self.setMinimumWidth(540)

        root = QVBoxLayout(self)

        # 1. Target Loudness Group
        loud_group = QGroupBox("Target Voice Loudness (Speaker Protection Active)")
        loud_layout = QVBoxLayout(loud_group)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Target RMS:"))

        # Slider uses tenths of dB: -300 (-30.0 dB) to -30 (-3.0 dB)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(int(MIN_TARGET_DBFS * 10))
        self._slider.setMaximum(int(MAX_TARGET_DBFS * 10))
        self._slider.setSingleStep(5)
        self._slider.valueChanged.connect(self._on_slider_changed)
        slider_row.addWidget(self._slider, 1)

        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(1)
        self._spin.setMinimum(MIN_TARGET_DBFS)
        self._spin.setMaximum(MAX_TARGET_DBFS)
        self._spin.setSingleStep(0.5)
        self._spin.setSuffix(" dBFS")
        self._spin.valueChanged.connect(self._on_spin_changed)
        slider_row.addWidget(self._spin)

        loud_layout.addLayout(slider_row)

        self._desc_label = QLabel("")
        self._desc_label.setStyleSheet("color: #4a9eff; font-weight: bold;")
        loud_layout.addWidget(self._desc_label)

        safety_label = QLabel(
            "Speaker protection: Target loudness is strictly capped at -3.0 dBFS maximum "
            "(with a -1.0 dBFS tanh soft peak ceiling) so audio can never clip or blow out speakers."
        )
        safety_label.setWordWrap(True)
        safety_label.setStyleSheet("color: #888888; font-size: 11px;")
        loud_layout.addWidget(safety_label)

        root.addWidget(loud_group)

        # 2. Ear-Friendly FX Chain Group
        fx_group = QGroupBox("Ear-Friendly Voice FX Chain")
        fx_layout = QVBoxLayout(fx_group)

        self._hp_cb = QCheckBox(
            "Sub-bass Rumble High-Pass Filter (80 Hz Butterworth) — removes mic thuds & rumble"
        )
        fx_layout.addWidget(self._hp_cb)

        self._eq_cb = QCheckBox(
            "Ear-Friendly Voice EQ — cuts 250 Hz mud (-1.5 dB), boosts 2.8 kHz clarity (+1.5 dB), "
            "tames harsh 6.3 kHz sibilance (-2.5 dB)"
        )
        fx_layout.addWidget(self._eq_cb)

        self._comp_cb = QCheckBox(
            "Smooth Soft-Knee Voice Compressor (-24 dB, 3:1) — evens out whispers & anime shouts"
        )
        fx_layout.addWidget(self._comp_cb)

        self._lim_cb = QCheckBox(
            "Tanh Soft Peak Limiter (-1.0 dBFS ceiling) — rounds transient peaks without harsh clipping"
        )
        fx_layout.addWidget(self._lim_cb)

        self._fade_cb = QCheckBox(
            "Anti-Click Edge Micro-Fade (12 ms) — prevents pops at start/end of trimmed clips"
        )
        fx_layout.addWidget(self._fade_cb)

        root.addWidget(fx_group)

        # 3. Automation & Field Scope Group
        auto_group = QGroupBox("Automatic Normalization on Added Audio")
        auto_layout = QVBoxLayout(auto_group)

        self._auto_cb = QCheckBox(
            "Automatically normalize audio when new cards are added (AnkiConnect, Add dialog, Editor)"
        )
        auto_layout.addWidget(self._auto_cb)

        field_row = QHBoxLayout()
        field_row.addWidget(QLabel("Target Audio Fields (comma-separated):"))
        self._fields_edit = QLineEdit()
        self._fields_edit.setPlaceholderText("Audio, Sentence Audio (or * for all fields)")
        field_row.addWidget(self._fields_edit, 1)
        auto_layout.addLayout(field_row)

        root.addWidget(auto_group)

        # 4. Batch Normalize Existing Deck Group
        batch_group = QGroupBox("Batch Normalize Existing Cards")
        batch_layout = QVBoxLayout(batch_group)

        deck_row = QHBoxLayout()
        deck_row.addWidget(QLabel("Deck:"))
        self._deck_combo = QComboBox()
        self._deck_combo.addItem("All Decks", "")
        try:
            for dname in sorted(mw.col.decks.all_names()):
                self._deck_combo.addItem(dname, dname)
        except Exception:
            pass
        deck_row.addWidget(self._deck_combo, 1)

        self._force_cb = QCheckBox("Re-process already normalized files")
        deck_row.addWidget(self._force_cb)
        batch_layout.addLayout(deck_row)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        batch_layout.addWidget(self._progress)

        self._batch_status = QLabel("")
        batch_layout.addWidget(self._batch_status)

        self._batch_btn = QPushButton("Normalize Existing Cards in Selected Deck Now")
        self._batch_btn.clicked.connect(self._run_batch)
        batch_layout.addWidget(self._batch_btn)

        root.addWidget(batch_group)

        # 5. Dialog Action Buttons
        btn_row = QHBoxLayout()
        reset_btn = QPushButton("Reset Defaults (-14.0 dBFS)")
        reset_btn.clicked.connect(self._reset_defaults)
        btn_row.addWidget(reset_btn)

        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save Settings")
        save_btn.setStyleSheet(
            "QPushButton { background-color: #4a9eff; color: white; font-weight: bold; padding: 6px 14px; }"
        )
        save_btn.clicked.connect(self._save_and_close)
        btn_row.addWidget(save_btn)

        root.addLayout(btn_row)

    def _load_from_config(self):
        target = clamp_target_dbfs(self._config.get("target_dbfs", DEFAULT_TARGET_DBFS))
        self._spin.blockSignals(True)
        self._slider.blockSignals(True)
        self._spin.setValue(target)
        self._slider.setValue(int(round(target * 10)))
        self._spin.blockSignals(False)
        self._slider.blockSignals(False)
        self._update_desc(target)

        self._hp_cb.setChecked(bool(self._config.get("highpass_enabled", True)))
        self._eq_cb.setChecked(bool(self._config.get("voice_eq_enabled", True)))
        self._comp_cb.setChecked(bool(self._config.get("compressor_enabled", True)))
        self._lim_cb.setChecked(bool(self._config.get("soft_limiter_enabled", True)))
        self._fade_cb.setChecked(bool(self._config.get("fade_edges_enabled", True)))
        self._auto_cb.setChecked(bool(self._config.get("auto_normalize", True)))
        self._fields_edit.setText(str(self._config.get("audio_fields", "Audio, Sentence Audio")))

    def _update_desc(self, val):
        self._desc_label.setText(f"{val:.1f} dBFS — {describe_loudness(val)}")

    def _on_slider_changed(self, slider_val):
        val = clamp_target_dbfs(slider_val / 10.0)
        self._spin.blockSignals(True)
        self._spin.setValue(val)
        self._spin.blockSignals(False)
        self._update_desc(val)

    def _on_spin_changed(self, spin_val):
        val = clamp_target_dbfs(spin_val)
        if val != spin_val:
            self._spin.blockSignals(True)
            self._spin.setValue(val)
            self._spin.blockSignals(False)
        self._slider.blockSignals(True)
        self._slider.setValue(int(round(val * 10)))
        self._slider.blockSignals(False)
        self._update_desc(val)

    def _collect_ui_config(self):
        cfg = dict(self._config)
        cfg["target_dbfs"] = clamp_target_dbfs(self._spin.value())
        cfg["highpass_enabled"] = self._hp_cb.isChecked()
        cfg["voice_eq_enabled"] = self._eq_cb.isChecked()
        cfg["compressor_enabled"] = self._comp_cb.isChecked()
        cfg["soft_limiter_enabled"] = self._lim_cb.isChecked()
        cfg["fade_edges_enabled"] = self._fade_cb.isChecked()
        cfg["auto_normalize"] = self._auto_cb.isChecked()
        cfg["audio_fields"] = self._fields_edit.text().strip() or "Audio, Sentence Audio"
        return cfg

    def _reset_defaults(self):
        self._config = dict(DEFAULT_CONFIG)
        self._load_from_config()

    def _run_batch(self):
        cfg = self._collect_ui_config()
        _save_config(cfg)
        self._config = cfg

        deck = self._deck_combo.currentData()
        query = f'deck:"{deck}"' if deck else ""
        try:
            nids = mw.col.find_notes(query)
            media_dir = mw.col.media.dir()
        except Exception as e:
            showWarning(f"Could not query collection: {e}")
            return

        target_files = []
        seen = set()
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            for fname in get_note_target_audio_files(note, cfg):
                if fname not in seen:
                    seen.add(fname)
                    full = os.path.join(media_dir, fname)
                    if os.path.exists(full):
                        target_files.append(full)

        if not target_files:
            showInfo("No matching audio files found in the selected deck/fields.")
            return

        self._batch_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setMaximum(len(target_files))
        self._progress.setValue(0)

        cache = _load_cache()
        force = self._force_cb.isChecked()
        norm_count = 0
        skip_count = 0
        err_count = 0

        for idx, fpath in enumerate(target_files, 1):
            res = normalize_audio_file(fpath, config=cfg, force=force, cache=cache)
            st = res.get("status")
            if st == "normalized":
                norm_count += 1
            elif st == "skipped":
                skip_count += 1
            else:
                err_count += 1

            if idx % 10 == 0 or idx == len(target_files):
                self._progress.setValue(idx)
                self._batch_status.setText(
                    f"Processed {idx}/{len(target_files)} — "
                    f"normalized: {norm_count}, skipped: {skip_count}, errors: {err_count}"
                )
                QApplication.processEvents()

        _save_cache(cache)
        self._batch_btn.setEnabled(True)
        showInfo(
            f"Batch normalization complete!\n\n"
            f"Target loudness: {cfg['target_dbfs']:.1f} dBFS\n"
            f"Normalized: {norm_count}\n"
            f"Skipped (already normalized): {skip_count}\n"
            f"Errors: {err_count}"
        )

    def _save_and_close(self):
        cfg = self._collect_ui_config()
        _save_config(cfg)
        tooltip(f"Audio Normalizer saved (Target: {cfg['target_dbfs']:.1f} dBFS)")
        self.accept()


def open_settings_dialog():
    dlg = NormalizerDialog(parent=mw)
    dlg.exec()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def _run_diagnostics():
    config = _get_config()
    lines = [
        "=== Audio Normalizer - Diagnostics ===",
        "",
        "--- Config (with speaker safety cap) ---",
    ]
    for k, v in config.items():
        lines.append(f"  {k} = {v!r}")
    lines.append(f"  fx_signature = {_fx_signature(config)!r}")
    lines.append(f"  ffmpeg_af = {build_ffmpeg_af(config)!r}")
    lines.append("")

    for binary in ["ffmpeg"]:
        try:
            r = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=5)
            ver = r.stdout.split("\n")[0] if r.stdout else ""
            lines.append(f"[{'PASS' if r.returncode == 0 else 'FAIL'}] {binary}: {ver[:100]}")
        except Exception as e:
            lines.append(f"[FAIL] {binary}: {e}")

    lines.append("")
    lines.append("--- Recent trace log ---")
    if _trace_log:
        lines.extend(_trace_log[-50:])
    else:
        lines.append("  (no activity recorded yet)")

    showInfo("\n".join(lines))


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def _init():
    anki_hooks.note_will_be_added.append(_on_note_will_be_added)
    anki_hooks.note_will_flush.append(_on_note_will_flush)
    gui_hooks.add_cards_did_add_note.append(_on_add_cards_did_add_note)
    gui_hooks.editor_did_unfocus_field.append(_on_editor_did_unfocus_field)
    gui_hooks.editor_did_init_buttons.append(_on_editor_buttons)

    try:
        mw.addonManager.setConfigAction(__name__, open_settings_dialog)
    except Exception:
        pass

    settings_action = QAction("Audio Normalizer: Settings & Batch...", mw)
    qconnect(settings_action.triggered, open_settings_dialog)
    mw.form.menuTools.addAction(settings_action)

    diag_action = QAction("Audio Normalizer: Run Diagnostics", mw)
    qconnect(diag_action.triggered, _run_diagnostics)
    mw.form.menuTools.addAction(diag_action)


_init()
