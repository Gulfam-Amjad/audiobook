# Roadmap

Built and covered by tests:

- Foundation, image library, prompts, queue and mock provider
- Eleven API adapters plus manual assist, all off until enabled
- Providers screen, credits, spending guard, settings
- Manual worksheets, inbox matching, unmatched tray
- Video library, daily capacity, FFmpeg assembly, diagnostics
- Projects, scripts, scenes, voices and renders for long-form video
- Piper narration with per-line measured durations, and each voice's real
  speaking pace measured from the lines it has recorded
- Per-scene animation routing, FFmpeg camera moves, and honest accounting
  that separates generated motion from assembled duration
- Kaggle and Hugging Face Space adapters for borrowed GPU time, both
  unit-tested against mocked HTTP and neither yet run live
- Subtitles timed from measured narration, ducked mixing to -16 LUFS,
  final assembly and output validation

A real 60-second video has been produced and checked end to end. See
`docs/PROTOTYPE.md` for the measured numbers and the blockers.

Not a completion target: 20 minutes of free generated video a day. The
capacity screen measures the real shortfall instead.

Still only possible with a person: see `docs/OPEN_TASKS.md`.

After a key exists, the first real job is the verification. Until that file
lands, the adapter stays unverified on purpose.
