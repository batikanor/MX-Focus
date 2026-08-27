# EEG pipelines

Muse headband to focus score, running entirely on the local machine.

## Why this exists

The pipeline the root README describes is gone. The scikit-learn calmness
model, the Lambda inference, and the band-power DSP were never committed to
this repo — they lived in the AWS console — and the API Gateway endpoint
(`7me4t4owwi.execute-api.us-west-2.amazonaws.com`) no longer resolves in DNS.
None of it is recoverable from source.

This is a rebuild of that half, with three differences: it is local, it is in
git, and it has tests. The repository now contains two complementary local
interfaces over the same MuseLSL input:

- `focus_metrics.py` provides the recommended REST metrics service used by
  the existing `MxInkHandler` integration, plus session logging and reports.
- `bandpower.py` -> `focus.py` -> `serve_focus.py` provides a smaller,
  independently tested WebSocket focus-score pipeline and an alternative
  Unity client.

## Architecture

```
Muse 2  ->  MuseLSL  ->  bandpower.py  ->  focus.py  ->  serve_focus.py  ->  Unity
                         4x4 band          one 0-1       ws://127.0.0.1
                         powers, 4/s       score         :8766
```

Replacing the original: MQTT to AWS IoT Core, a Lambda running scikit-learn,
API Gateway, and Unity polling that REST endpoint 100 times a second for a
value that changed 4 times a second. Round trip drops from several hundred
milliseconds to roughly 5, and there is no cloud account to lose.

## Modules

| File | Does |
|---|---|
| `bandpower.py` | Windowed per-channel band power (delta/theta/alpha/beta) |
| `focus.py` | Band powers to one 0-1 score, calibrated per person |
| `serve_focus.py` | WebSocket server; also the entry point (`python serve_focus.py`) |
| `stream_eeg.py` | Raw EEG over WebSocket — recording path, **see Known issues** |
| `receive_eeg.py` | Client for the above, writes CSV |
| `upload_egg_aws.py` | Original MQTT uploader, unused by this pipeline |

Every design decision is documented in each module's docstring, with the
reasoning that produced it. Read those before changing behaviour — several
choices that look arbitrary are load-bearing.

## Running it

```
pip install -r requirements.txt
muselsl stream                 # in one terminal
python serve_focus.py          # in another
```

Then connect to `ws://127.0.0.1:8766`. Each message is flat JSON:

```json
{"t": 123.45, "value": 0.72, "z": -0.41,
 "calibrating": false, "artifact": false, "held": false}
```

`value` is the wobble multiplier: 1.0 is fully focused, 0.0 is maximum
wobble. It is **never null** — during calibration it is neutral (0.5) and
`calibrating` is true. That is deliberate: Unity's `JsonUtility` turns a null
float into `0.0`, which this pipeline reads as *maximum* wobble, so a parse
failure would otherwise shake the pen violently during the one period it
should be still.

The first 30 seconds are calibration, and they should happen **during real
work**, not at rest. That is what fixes `z = 0` as "this student's normal
working state", so a drop below it is the event worth catching.

## Tests

```
python -m pytest muse/ -q          # 49 tests
```

All synthetic — no headband, no Quest, no AWS. That is the point: the
original pipeline could not be tested without wearing the hardware, which
made every bug hunt an expedition.

## Known issues and what is not done

**`focus.SQUASH_SIGMA` has never seen real EEG.** It was tuned against
synthetic signals and saturated on them (z ≈ −20). That is probably an
artifact of an unrealistically extreme synthetic contrast rather than a
design flaw, but it is unproven. Validate against a recorded session before
trusting the scale.

**`receive_eeg.py` writes a new CSV per message received.** Thousands of tiny
files, and its `save_data` does blocking pandas I/O inside an async function.

**The alternative WebSocket Unity components are uncompiled.**
`Assets/MXFocus/Scripts/` contains `FocusClient.cs` and `PenWobble.cs`, written
without access to a Unity install. They still need an Editor compile and scene
wiring before that optional path can be used. The existing `MxInkHandler`
instead uses the REST metrics service documented in the root README.

**Not addressed in the Unity code:** a GameObject is still spawned per frame
while drawing, the cube buttons still fire every frame with no debounce, and
`MxInkHandler.cs` is still a vendor SDK file carrying project logic.

**No machine learning.** `focus.py` is a formula (Pope engagement index) with
per-subject calibration, not a trained model. Supervised learning here would
need EEG labelled with ground-truth attention, which requires running a study
rather than writing code. The formula is also interpretable, needs no
training data, and cannot silently overfit. If a model is ever wanted, it is
downstream of recordings this pipeline does not yet produce.
