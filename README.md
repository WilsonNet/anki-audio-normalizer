# Anki Audio Normalizer & Voice FX Chain

An Anki 2.1+ add-on that automatically normalizes newly added card audio (via AnkiConnect, Yomitan, ASBPlayer, GameSentenceMiner, Droid Sentence Miner, or the Anki Editor) and batch-normalizes existing decks using [Droid Sentence Miner](https://github.com/WilsonNet/droid-sentence-miner)'s voiced-frame RMS normalizer and an ear-friendly voice FX chain.

## Why Voiced-Frame RMS + Soft Tanh Limiter?

Standard broadcast normalizers (`loudnorm` / EBU R128) use a 3-second integration window that pumps background noise and distorts short 1–4 second sentence clips. Simple peak normalization leaves any sentence containing a single loud syllable or sound effect quiet.

This add-on uses a 5-stage voice chain designed specifically for sentence mining:

1. **Sub-bass Rumble High-Pass (`80 Hz` Butterworth)**: Removes mic thuds, room rumble, and explosion sub-bass that eat headroom without adding speech clarity.
2. **Ear-Friendly 3-Band Voice EQ**:
   - **`250 Hz` (`-1.5 dB`, `Q=1.2`)**: Cuts boxy low-mid mud from compressed game/anime audio.
   - **`2.8 kHz` (`+1.5 dB`, `Q=1.0`)**: Gently lifts consonant intelligibility.
   - **`6.3 kHz` (`-2.5 dB`, `Q=2.0`)**: Tames piercing sibilance and harsh treble resonances so loud cards never fatigue the ears.
3. **Smooth Soft-Knee Voice Compressor (`-24 dB`, `3:1`, `8/150 ms`)**: Gently evens out whispers and loud anime shouts before loudness scaling.
4. **Voiced-Frame Gated Loudness Normalizer (`-14.0 dBFS` default) + Tanh Soft Peak Limiter (`-1.0 dBFS` ceiling)**:
   - Measures RMS only across voiced 20 ms frames (`>= -50 dBFS`), so pauses and leading/trailing silence never inflate the gain.
   - Applies a single constant gain across the clip (zero dynamic pumping) and rounds transient peaks up to `+6 dB` over the knee onto a smooth `tanh` curve approaching `-1.0 dBFS`.
   - **Hard Speaker Protection Cap**: Target loudness is strictly clamped to **`[-30.0, -3.0] dBFS`** in both the UI and DSP engine so no setting can ever blow out speakers or headphones.
5. **Anti-Click Edge Micro-Fade (`12 ms`)**: Smooths clip start and end boundaries to eliminate pops on trimmed lines.

## Features

- **Automatic on Add / Edit**: Hooks into `note_will_be_added`, `add_cards_did_add_note`, and `editor_did_unfocus_field` to normalize `[sound:...]` files in your configured sentence audio fields (`Audio, Sentence Audio` by default) as soon as cards are created.
- **Settings & Batch Normalizer UI**: Open **Tools → Audio Normalizer: Settings & Batch...** (or click **Config** in Anki's Add-ons window) to:
  - Set target loudness via slider + spinbox (`-30.0` to `-3.0 dBFS` max).
  - Toggle each stage of the ear-friendly FX chain.
  - Batch-normalize existing cards in any deck (or all decks) with a live progress bar.
- **Editor Toolbar Button**: Click **Norm** (`Ctrl+Shift+N`) in the card editor to normalize the current card's audio on demand.
- **Idempotent Cache**: Remembers normalized file timestamps, sizes, and FX signatures so already-normalized files aren't re-encoded twice.

## Requirements

- Anki 2.1.50+ (PyQt6)
- `ffmpeg` on `PATH`

## Running Tests

```bash
python3 -m pytest tests/ -v
```
