# Prototype results

Measured on this machine. Every figure below was read back off a file that
exists on disk; none is an estimate.

## The sixty-second cut

Project 2, "Sixty second prototype", render 3.

`D:\AIVideoManager\output\0002-Sixty second prototype\render-0003\Sixty second prototype.mp4`

| | |
|---|---|
| Planned length | 60.208s |
| Measured length | 60.116s |
| Frame | 1920x1080, 24fps, h264, yuv420p |
| Audio | aac, 48000Hz, present |
| A/V offset | 0.033s |
| Loudness | -16.6 LUFS against a -16 target, 4.9 LU range |
| Black runs | none |
| Check failures | none |
| Subtitle cues | 16, each timed from a measured narration file |
| Size | 9,604,985 bytes |

**None of it is animated.** All 60.2s is a camera move over a still
picture, which is editing, not animation. The report file says so in those
words, and the Shots screen says so too. A model has not yet produced a
single frame of motion for this project; `generated_motion_s` is 0.0.

The narration is real: sixteen Piper lines totalling 53.0s, each one a WAV
on disk whose length was read with ffprobe.

## What the prototype proved, and what it cost

The first attempt came out at 93.6s for a sixty-second brief, which is
render 2 and is still on disk. The script had been written to an assumed
150 words per minute while `en_US-joe-medium` actually speaks at 182.1 wpm
(17.02 characters per second), measured across 30 recorded lines. The app
now measures each voice's real pace from lines it has already recorded and
writes future scripts to that figure, which is what brought the second
attempt to 60.1s. Until a voice has eight measured lines it says "not
measured yet" on the Voices screen rather than guessing.

Running the pipeline twice over one project also exposed a counting bug:
the Shots screen reported 153.9s assembled for a sixty-second video,
because it was counting clips left behind by a superseded script version.
The render itself was always right, since it walks the approved script's
shots. There is now a regression test for it.

## Blockers

**There are no Gemini-generated images in the library.** The brief was to
build from existing Gemini stills. All 53 images in the library came from
`C:\Users\Gulfam\Pictures` and are screenshots, annotations and photos, so
the prototype is cut from those. Point the Image Library at the folder
holding the actual Gemini output before judging how the pipeline looks on
real material.

**No GPU work has run.** Both remote adapters are built and unit-tested
against mocked HTTP, and the Kaggle one is written against field names
read out of the published `kagglesdk` wheel, but neither has been given
credentials, so neither has completed a live call. The Hugging Face
adapter's capabilities report says `live_verified=False` for exactly this
reason. Nothing in this repo should be read as evidence that image-to-video
or lip-sync works here yet.

**The system drive is full.** C: has around 350 MB free out of 142 GB. The
test suite now writes its scratch files to `D:\AIVideoManager\tmp\pytest`,
because otherwise the free-space guards fail unrelated tests. Anything that
needs to install to C: will refuse until space is cleared.

## The longer cut

Project 3, "Five minute cut", render 4.

`D:\AIVideoManager\output\0003-Five minute cut\render-0004\Five minute cut.mp4`

| | |
|---|---|
| Planned length | 277.292s |
| Measured length | 277.284s |
| Frame | 1920x1080, 24fps, h264, yuv420p |
| Audio | aac, 48000Hz, stereo |
| A/V offset | 0.034s |
| Loudness | -16.9 LUFS against a -16 target |
| Check failures | none |
| Size | 43,430,628 bytes |
| Shots | 35, all camera moves |
| Generated motion | 0.0s |

The first check of this file failed with "FFprobe timed out after 30s". That happened while the encode was still loading the machine, and the file itself was already written. Measuring it again, with a longer limit, is what produced the figures above. The black-frame scan did not finish inside five minutes, so holes in the picture were not ruled out. That is recorded as a note, not as a pass.

It is 277 seconds, not 300. The narration that was actually recorded is 256.4 seconds, and the shots are cut to that plus a short pause on each one. The target of 300 seconds is what was asked for; 277 seconds is what the spoken lines add up to.

Upload assets for this file are in `publish.json` beside it. Nothing has been uploaded.

## Animated audiobook, five-minute summary

`D:\AIVideoManager\audiobooks\The Art of Seduction - 5 min summary\audiobook.mp4`

An original summary of *The Art of Seduction*, read by Kokoro `bm_george`, with the balloon ring and live word captions. Measured 2026-11-13.

| | |
|---|---|
| Measured length | 305.9s |
| Frame | 1920x1080, 24fps, h264 |
| Loudness | -16.5 LUFS against a -16 target, no silence over 4s |
| Size | 35.9 MB |
| Word timing | Whisper tiny.en, 30s for the whole book, 93.5% to 99.0% of the script words matched |
| Drawing | 39 frames a second, so the 306s video took 3 minutes 23s |

Kokoro spoke the 306 seconds in about 7.5 minutes. `base.en` was tried first and took about as long as the audio; `tiny.en` is the one used, because the script is what appears on screen and Whisper only supplies the timing.

A stopped run keeps finished chapters, word-timing files and 60-second video segments. The same command, or `--resume` with the folder, continues from there.

## Reproducing

The pipeline runs over HTTP against the local app: create a project, plan
and approve a script, synthesise speech, plan and render shots, then render
the project. Tests covering each of those stages are in `tests/`, and the
render test marked `real_ffprobe` builds and probes a genuine file rather
than a stub.
