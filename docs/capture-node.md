# Throat + Air Capture Node — build spec

**Status:** proposal / roadmap. Hardware companion to dual-mic fusion.
The host side ships: `/api/audio-stream`
receives the PCM (see `scripts/stream_pcm.py` for a client). It refuses any peer
that is not on the same machine, even if the server is bound to a network address;
a node on Wi-Fi needs an authenticated LAN listener, which is not built yet.

A stealth, minimal wearable that digitizes a **throat** element and an **air** mic on
**one shared clock** and streams the 2-channel PCM to a Voxwire host. The host fuses
(the DSP already exists in `voxwire/fusion.py`) and transcribes. No Bluetooth HFP, so
no 8 kHz narrowband ceiling; sample-aligned by construction, so no drift.

## Principle: dumb node, smart host

```
  ┌──────────── capture node (worn) ────────────┐
  │ throat element ─▶[preamp]─▶ L ┐              │
  │                               ├─ stereo ADC ─┼─ I2S ─▶ ESP32-S3 ─▶ Wi-Fi ─▶ host
  │ air mic ────────▶[preamp]─▶ R ┘  (ONE clock) │      (ring buf +          (fuse → STT
  │ LiPo + charger                               │       throat-VAD gate)      → agent)
  └──────────────────────────────────────────────┘
      ch0 = throat, ch1 = air, converted together → sample-aligned, no drift
```

The node only **digitizes and streams** (plus an optional throat-energy gate so it
transmits only while you voice — saves battery and is inherently stealthy). **Fusion
and STT stay on the host**: fusion is cheap but we want the raw throat+air pair
(needed to train enhancement, and to retune the crossover); STT is far too
heavy for a wearable-class board.

## Bill of materials (v1)

| Part | Suggested | Why / notes | ~USD |
|---|---|---|---|
| MCU board | **Arduino Nano ESP32 (ABX00092)** | ESP32-S3, 8 MB PSRAM (audio/jitter buffers), 16 MB flash, 45×18 mm, USB-C, MicroPython/Arduino. Flexible I2S via GPIO matrix. | 20–27 |
| Stereo audio ADC/codec | **TLV320AIC3204** (or WM8960 / ES8388) | Two analog inputs → one I2S stream = the shared clock. Built-in mic PGA + bias cuts external analog parts. I²C config, I²S slave. | 5–10 |
| Air mic | Electret capsule **or** analog MEMS | Line/lapel placement. Into codec R input (uses codec mic bias/PGA). | 1–3 |
| Throat element | Wired contact/throat capsule (piezo or dynamic; e.g. a wired IASUS NT element) | **Not the Bluetooth Stealth** — this rig needs a wired element. Into codec L. | 15–40 |
| Throat buffer | JFET / high-Z op-amp buffer (only if the element is **piezo**) | Piezo is high-impedance; needs a buffer before the codec PGA. Dynamic/electret may go straight to the PGA. | 1–3 |
| Battery + charge | 400–500 mAh LiPo + TP4056/MCP73831 | Nano ESP32 has no onboard LiPo charger. VAD-gating stretches runtime to hours. | 5 |
| Enclosure | small collar clip / lapel pod | throat element on the neck, node + air mic at the collar | — |

**Alternatives:** cheapest brain → a generic ESP32-S3 devkit **with PSRAM** (more I2S
audio examples, ~$8–12). If you decide to move compute onto the wearable later (on-board
fusion, small STT), a **Pi Zero 2 W** instead — bigger, ~10× the power, boots an OS.

**Do not** use the ESP32's built-in ADC for audio — it's noisy. The audio path is the
external I2S codec.

## Clocking (the whole point)

Both mics are analog inputs to **one** stereo codec, so one BCLK + one word-select
(LRCK) convert L and R on the same edges → the two channels are **sample-aligned with
zero drift**. That's why a single 2-channel converter beats two separate mics: the
software time-alignment in `fusion.align()` becomes unnecessary (`fuse_stereo(...,
do_align=False)` is already how the host handles this case).

## Wiring (illustrative — ESP32-S3 GPIO is matrix-flexible)

| Codec pin | Nano ESP32 pin | Signal |
|---|---|---|
| MCLK | D2 | master clock out (codec SCKI; 256×fs) |
| BCLK | D3 | bit clock |
| LRCK/WS | D4 | word select (L/R) |
| DOUT | D5 | I2S data in (ADC → ESP32) |
| SDA | A4 | I²C config |
| SCL | A5 | I²C config |
| VDD/GND | 3V3 / GND | 3.3 V only — **not 5 V tolerant** |

Analog: throat → (buffer) → codec IN_L; air → codec IN_R; enable codec mic bias/PGA
in firmware.

## Firmware (ESP32-S3 — Arduino/C or MicroPython)

Format: **16 kHz, stereo, 16-bit** (matches the host STT sample rate; ~512 kbps raw —
trivial for Wi-Fi). 48 kHz is fine too if you'd rather downsample on the host.

```
setup:
  i2c_configure(codec)          # ADC on, mic bias, PGA gain for throat(L)+air(R), I2S slave
  i2s_master(mclk=D2, bclk=D3, ws=D4, din=D5, rate=16000, bits=16, ch=2)
  wifi_connect(); stream = connect(host, transport)   # see host side below
  seq = 0

loop (per ~20 ms DMA block = 320 frames/ch):
  buf = i2s_read()                       # int16 interleaved L,R  (throat, air)
  voiced = short_time_energy(buf.L) > THRESH    # optional throat-VAD gate
  if voiced or in_tail_window:
      send(stream, header(seq, rate=16000, n=320, flags=voiced) + buf)
      seq += 1
```

- **Ring buffer in PSRAM** absorbs Wi-Fi jitter (the 8 MB PSRAM earns its keep here).
- **Throat-VAD gate** uses the throat channel only (noise-immune), so background sound
  never triggers transmission — battery + stealth. Keep a short post-voicing tail so
  trailing consonants aren't clipped.

## Host side (Voxwire) — the "network PCM input"

Smallest integration that reuses everything already built: a **WebSocket endpoint on
the existing FastAPI server** — the node behaves like a remote warm mic.

```
@ws  /api/audio-stream            # node connects, sends binary stereo PCM frames
  - assemble frames into a stereo buffer (mirror of the _warm ring buffer)
  - utterance boundary from the node's `voiced` flag (or a host-side VAD)
  - on end: gather stereo window
           → fusion.fuse_stereo(win, mode)      # ALREADY IMPLEMENTED
           → _finalize_clip / stt.transcribe     # ALREADY IMPLEMENTED
           → gateway.dispatch or dictation paste  # ALREADY IMPLEMENTED
```

So the only new host code is the **receiver + framing**; fusion, STT, and routing are
done. The clean generalization (later) is a `voxwire/inputs/` plugin layer mirroring
`stt/` and `integrations/` — “bring your own input” — with this network node as the
first plugin and the local sounddevice capture as another.

**Transport:** v1 = WebSocket/TCP (reliable, reuses the server, LAN is fine). v2 =
UDP/RTP if you want lower latency and can tolerate loss. Put the host on a tailnet if
the node roams off the home network.

## Power budget (rough)

ESP32-S3 Wi-Fi TX peaks ~120–240 mA; with VAD-gated transmit the average is far lower
(mostly idle listening on the throat channel). A 400–500 mAh LiPo → several hours of
practical use; light sleep between utterances extends it further.

## Bring-up milestones

1. Codec + I2S loopback: read stereo, dump to serial, confirm two aligned channels.
2. Wire throat (L) + air (R); check levels/PGA; save a few seconds to SD/serial and
   eyeball the two channels.
3. Wi-Fi stream to a laptop script that writes a stereo WAV; verify with the host
   `fusion.fuse_stereo` offline.
4. Add the `/api/audio-stream` endpoint; end-to-end murmur → transcription.
5. Add the throat-VAD gate; measure battery.
6. Enclosure + element placement for all-day stealth wear.

## Open decisions

- **Codec choice** — TLV320AIC3204 (flexible, mic bias/PGA) vs a bare I2S ADC like
  PCM1808 (simpler, needs external mic preamps + an MCLK). Leaning codec for “minimal.”
- **Sample rate** — 16 kHz (matches STT, least bandwidth) vs 48 kHz (headroom, resample
  on host).
- **VAD on node vs host** — node-side saves battery/bandwidth; host-side is simpler to
  iterate. Probably both: crude gate on node, real boundary on host.
- **Transport** — WebSocket/TCP first; UDP/RTP if latency matters.
