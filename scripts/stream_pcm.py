#!/usr/bin/env python3
"""Stream a WAV to Voxwire's /api/audio-stream and print the transcript.

Proves the dual-mic network input (BYT-118 capture node) end to end without
hardware: feed a **stereo** WAV (ch0 = throat, ch1 = air) or a mono WAV, and the
host fuses (if stereo) + transcribes exactly as a real capture node would.

    voxwire/.venv/bin/python scripts/stream_pcm.py clip.wav
    voxwire/.venv/bin/python scripts/stream_pcm.py clip.wav --mode throat --paste
    voxwire/.venv/bin/python scripts/stream_pcm.py clip.wav --host 127.0.0.1:8123

Without --model the server uses its own default for this machine (MLX on Apple
Silicon, faster-whisper elsewhere). Only the first two channels are sent: the
host reads ch0 as throat and ch1 as air.
"""
from __future__ import annotations

import argparse
import asyncio
import json

import numpy as np
import soundfile as sf
import websockets


async def stream(args: argparse.Namespace) -> None:
    data, sr = sf.read(args.wav, dtype="int16", always_2d=True)   # (frames, channels)
    if data.shape[1] > 2:
        print(f"note: {data.shape[1]}-channel WAV, sending ch0 (throat) + ch1 (air) only")
        data = data[:, :2]
    channels = data.shape[1]
    pcm = np.ascontiguousarray(data).reshape(-1).tobytes()        # interleaved int16
    uri = f"ws://{args.host}/api/audio-stream"
    start = {"event": "start", "sr": int(sr), "channels": channels,
             "mode": args.mode, "paste": args.paste}
    if args.model:
        start["model"] = args.model
    async with websockets.connect(uri, max_size=None) as ws:
        await ws.send(json.dumps(start))
        step = int(sr * 0.05) * channels * 2                      # ~50 ms chunks
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i:i + step])
        await ws.send(json.dumps({"event": "stop"}))
        print(f"[{channels}ch @ {sr} Hz · mode={args.mode}] →", await ws.recv())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("wav", help="WAV file (stereo throat+air, or mono)")
    p.add_argument("--mode", default="fusion", choices=("fusion", "throat", "air"))
    p.add_argument("--model", default=None,
                   help="STT model key (default: the server's default for this machine)")
    p.add_argument("--host", default="127.0.0.1:8123")
    p.add_argument("--paste", action="store_true", help="paste the transcript at the cursor")
    asyncio.run(stream(p.parse_args()))


if __name__ == "__main__":
    main()
