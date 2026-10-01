# Measuring fusion

Voxwire's claim is that fusing a throat mic with an air mic beats either one
alone: the throat mic keeps out room noise, and the air mic restores the
consonants a throat mic loses (see [DESIGN.md §1](DESIGN.md)). This page is how
to test that claim on real speech, and where the result goes.

`scripts/fusion_eval.py` records you reading a fixed list of phrases through a
2-channel device, keeps the raw stereo, and scores every take three ways:
throat only, air only and fusion. Each mode goes through the app's own path
(`fusion.render_capture`, then `stt.transcribe`), so the numbers describe what
the app does, not a lab version of it.

## What you need

- **A throat mic and an air mic on one 2-channel input**, throat on channel 0
  (left), air on channel 1 (right). A wired throat mic and an air mic on a
  2-channel USB audio interface is the setup fusion is built for: one clock, so
  the channels line up sample for sample.
  A macOS Aggregate Device also works, but a Bluetooth member adds its link
  latency, so its channel trails the other. `check` measures the offset, and the
  score report states it; fusion assumes there is none.
- **About 20 minutes**: 44 phrases, once in quiet and once in noise.
- **A steady noise source** for the noisy condition, such as a speaker playing
  café or street ambience about a metre away.

## Steps

Run these from the repository root.

```bash
# 1. Find the 2-channel input.
voxwire/.venv/bin/python scripts/fusion_eval.py devices

# 2. Speak for four seconds: levels, channel order, and the offset between channels.
voxwire/.venv/bin/python scripts/fusion_eval.py check --device "Throat + Air"

# 3. Quiet room. Enter, read the phrase, Enter. You can quit and resume.
voxwire/.venv/bin/python scripts/fusion_eval.py record --device "Throat + Air" --condition quiet

# 4. Same again with the noise playing.
voxwire/.venv/bin/python scripts/fusion_eval.py record --device "Throat + Air" --condition noisy

# 5. Score: every take, three ways, with this machine's default model (or --model KEY).
voxwire/.venv/bin/python scripts/fusion_eval.py score --markdown fusion-results.md
```

Fix what `check` reports before you record: swapped channels, clipping, a
silent channel, or a large offset.

For the noisy condition, set the noise once and leave it: about 65–70 dB(A) where
you sit (a sound-meter app will tell you), the same for every phrase. Note the
level; it belongs next to the numbers.

## Ground rules

- **Read each phrase the way you'd dictate it.** Don't over-articulate for the
  throat mic.
- **Redo a take only if you misread the phrase or were interrupted**, never
  because you think the mic misheard you. Redoing the bad takes would bias the
  test.
- **Keep, redo or stop after each take.** Enter keeps it, `r` throws it away, `q`
  keeps it and stops. Only kept takes are scored. A take with dropped samples (the
  computer got busy) is thrown away and asked for again.
- **The order is shuffled** per condition (fixed by `--seed`), so fatigue
  doesn't land on the same phrases every time.
- **One speaker on one setup is a result, not a benchmark.** Publish it with the
  devices, the model and the noise level.

## What stays on your machine

Your recordings and the per-phrase transcripts stay in `recordings/eval/`, which
git ignores. Only the summary from `score` is meant for the repository.

## How it's scored

Word error rate is (substitutions + deletions + insertions) ÷ reference words,
after lowercasing and dropping punctuation, totalled over all phrases in a
condition. The report also counts phrases transcribed exactly right, which is
what matters for a command.

The 95% intervals come from 2,000 bootstrap resamples of the phrases. Every
mode is scored on the same takes, so fusion's differences against throat only
and air only are paired: each resample compares the modes on identical
recordings.

The phrase list (`scripts/fusion_eval_phrases.txt`) is short commands and
sentences, heavy in the consonants a throat mic loses: s, sh, f, th, t, k, p
and ch. It has no numbers, because speech-to-text may write "6" or "six", and
no words whose spacing varies. The tests enforce those rules.

## Results

Not measured yet. When they are, the summary from `score` goes here and the
headline goes in the README's *Status*.
