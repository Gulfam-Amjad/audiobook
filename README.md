# AI Video Generation Manager

A local Windows app that turns still images into video clips through cloud
providers, or through a manual worksheet when the only free credits live on
a website, then files the results and can join them with FFmpeg.

It runs on this PC. It does not need Docker, a paid host, or a separate
automation service. The database, thumbnails, downloads and logs live under
`AVM_DATA_ROOT` (default `D:\AIVideoManager`).

## What it will not do

- It will not drive provider websites. Seven of the ten free sites forbid that.
- It will not create accounts, rotate identities, or use extra accounts to
  beat a trial limit.
- It will not spend money unless paid generation is on, a monthly cap above
  $0 is set, and that specific account is authorised. All three default to off.
- It will not call an adapter "verified" until a real video has been downloaded
  through it. A passing connection test is not that.
- It will not invent a credit balance or assume a daily reset happened.
- It will not count a montage's length as generated AI video. Measured clip
  duration and assembled-file duration are separate numbers.

## Run

```powershell
.\start.ps1
```

Then open http://127.0.0.1:8765 . The queue starts paused.

## Free capacity, checked 2026-10-28

No image-to-video API offers a recurring free daily allowance. The only
one-off API trials found are Alibaba Model Studio (50 seconds per Wan
image-to-video model, 90 days) and BytePlus ModelArk (2,000,000 tokens on
each of two Seedance models).

The ten consumer sites together publish roughly 45 seconds to 2 minutes of
free image-to-video a day, most of it watermarked. That does not reach a
20-minute day. The Daily Capacity screen states the shortfall.

## Layout

The sidebar is split into four groups. The cloud group is folded shut by
default and opens by itself when you are on one of its pages.

| Group | Screen | What it is |
| --- | --- | --- |
| Books (local) | Book Video | Script, cover and voice in; a YouTube-ready video out, all on this laptop |
| Books (local) | Audiobook | Script or text file in; one MP3 out |
| Books (local) | Voices | Piper, Kokoro and edge voices, and the measured speaking pace |
| Books (local) | Files | Every finished audiobook and film in one list |
| Video Projects | Projects | One video, from brief through script, shots and render |
| Video Projects | Image Library | Scan folders, skip duplicates, thumbnails |
| Video Projects | Prompts | Templates with `{tokens}` |
| Video Projects | Assembly | Normalize and join with FFmpeg |
| Video Projects | Video Library | Measured clips, preview, zip export |
| Cloud & Remote GPU | Overview | Generated AI motion, spending, verified providers |
| Cloud & Remote GPU | Providers | Free and paid API adapters, prices, what is actually verified |
| Cloud & Remote GPU | Credits | Balances labelled by source, spending gates |
| Cloud & Remote GPU | Queue | State machine, pause, retry, reconcile |
| Cloud & Remote GPU | Manual Assist | Worksheets and an inbox watcher |
| Cloud & Remote GPU | Remote GPU | Borrowed GPU time on Kaggle, with a Hugging Face Space as fallback |
| Cloud & Remote GPU | Daily Capacity | Free-allowance maths against the target |
| System | Settings | Defaults and the spending switches |
| System | Diagnostics | Health, plus a secret-free download |

A finished project also has a Render screen: subtitles timed from the
measured narration, a loudness check, and upload assets (title,
description, tags, a disclosure, a thumbnail taken from the file). That
screen prepares files. It does not upload them.

Measured results from the real runs are in `docs/PROTOTYPE.md`. What still
needs you is in `docs/OPEN_TASKS.md`.

More detail is in `docs/`.
