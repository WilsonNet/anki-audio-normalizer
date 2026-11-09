## Audio Normalizer & Ear-Friendly Voice FX Chain

Automatically normalizes newly added card audio (via AnkiConnect, Add dialog, or Editor) and existing decks using Droid Sentence Miner's voiced-frame RMS normalizer + a warm, non-fatiguing voice FX chain.

### Speaker Safety Cap
- **`target_dbfs`** is strictly clamped to **`[-30.0, -3.0] dBFS`** (default `-14.0 dBFS`). Values above `-3.0 dBFS` are blocked to protect speakers and headphones, and a `-1.0 dBFS` tanh soft limiter prevents clipping.

### Configuration Keys
- **`target_dbfs`** (`-14.0`) — Target voiced-frame RMS loudness in dBFS (clamped to `-30.0 .. -3.0`).
- **`auto_normalize`** (`true`) — Automatically normalize audio when notes are added or edited.
- **`audio_fields`** (`"Audio, Sentence Audio"`) — Comma-separated field names to normalize (`"*"` for all fields).
- **`highpass_enabled`** (`true`) — 80 Hz Butterworth high-pass filter to remove sub-bass rumble & mic thuds.
- **`voice_eq_enabled`** (`true`) — Ear-friendly 3-band voice EQ (`250 Hz` mud cut `-1.5 dB`, `2.8 kHz` presence `+1.5 dB`, `6.3 kHz` de-harsh `-2.5 dB`).
- **`compressor_enabled`** (`true`) — Smooth soft-knee voice compressor (`-24 dB`, `3:1`) so shouts don't keep lines quiet.
- **`soft_limiter_enabled`** (`true`) — Tanh soft peak limiter (`-1.0 dBFS` ceiling).
- **`fade_edges_enabled`** (`true`) — 12 ms anti-click micro-fade at clip boundaries.
- **`shortcut`** (`"Ctrl+Shift+N"`) — Editor toolbar button keyboard shortcut.
