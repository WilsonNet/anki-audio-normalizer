# AGENTS.md

## Project

An Anki 2.1+ add-on (`anki_audio_normalizer`) that normalizes newly added and existing card audio using Droid Sentence Miner's voiced-frame gated RMS normalizer (`AudioPcm.normalize`), a `-1.0 dBFS` tanh soft peak limiter, a `-3.0 dBFS` hard speaker-protection cap, and an ear-friendly voice FX chain (`80 Hz` high-pass, 3-band voice EQ, soft-knee compressor, and `12 ms` edge micro-fades).

## Commands

```bash
# Run unit tests (standalone, no Anki required)
python3 -m pytest tests/ -v

# Create publishable .ankiaddon
rm -rf __pycache__ tests/__pycache__ user_files
zip -r anki_audio_normalizer.ankiaddon * -x ".git/*" ".gitignore" "AGENTS.md" "tests/*"
```

## Architecture

```
__init__.py          <-- single-file add-on, zero external Python dependencies
├── config           _get_config(), _save_config(), clamp_target_dbfs() (-30.0 .. -3.0 dBFS cap)
├── fx + dsp         build_ffmpeg_af(), normalize_pcm(), soft_limit_sample(), fade_edges_pcm()
├── file processor   normalize_audio_file(), idempotent cache in user_files/normalized_cache.json
├── hooks            _on_note_will_be_added(), _on_add_cards_did_add_note(), _on_editor_did_unfocus_field()
├── NormalizerDialog Qt6 UI: slider + spinbox (-3.0 dBFS max), FX toggles, field filter, deck batch runner
└── _run_diagnostics Tools menu -> trace log + ffmpeg/config check
```
