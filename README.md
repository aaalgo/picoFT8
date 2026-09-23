# picoFT8

picoFT8 is an FT8 platform that combines reception from remote web SDRs with
local transmission through picoFT8 hardware. It receives and decodes FT8 traffic,
coordinates contacts through a central API server, and controls a serial-connected
Arduino/Si5351 transmitter to send FT8 signals.

The receive side currently connects directly to **KiwiSDR** servers. Other WebSDR
services need a compatible audio source integration; the client does not accept
arbitrary WebSDR browser URLs. Recorded audio can also be replayed for development
and analysis.

```text
KiwiSDR → audio receiver → FT8 decoder → API server / QSO state
                                             ↓
                                      transmit scheduler
                                             ↓
                                  picoFT8 Arduino / Si5351 → RF
```

The receiver, server, and transmitter run as separate processes and communicate
over HTTP. They can run on the same computer or on separate hosts.

## Components

| Component | Purpose |
| --- | --- |
| `sdr_ft8_client.py` | Receive KiwiSDR audio or replay WAV files, calibrate slot timing, decode with `jt9`, and submit received messages. |
| `server.py` | Store received messages in SQLite, track QSO progress, and offer messages for upcoming TX slots. |
| `transmit.py` | Encode server-selected or fixed messages and control picoFT8 hardware over USB serial. |
| `monitor.py` | Display message history and poll for new messages. |
| `export_adi.py` | Export stored correspondence with a callsign as an ADIF contact. |
| `simulator.py` | Exercise the API and QSO flow without a receiver or transmitter. |

## Setup

Use Python 3.10 or newer. From the project directory, create an environment and
install the Python dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install numpy scipy flask sqlalchemy pyserial
```

The receive and transmit paths also require external components:

- **Decoder:** the `jt9` executable from WSJT-X, available on `PATH`.
- **KiwiSDR client:** a `kiwiclient` checkout at `./kiwiclient`, or a path supplied
  with `--kiwiclient-path`.
- **FT8 encoder:** an `ft8_lib` checkout that provides the `msg2freq` build target.
  Place it at `./ft8_lib` and run `make -C ft8_lib`; the transmitter expects
  `./ft8_lib/msg2freq`.
- **Transmitter:** picoFT8 Arduino/Si5351 hardware with firmware that accepts
  `PING`, `F <frequency>`, `ON`, and `OFF` over 115200-baud serial and replies
  `OK`. Frequency commands use hundredths of a hertz.

The external checkouts and hardware firmware are not included in the tracked
files of this repository. Keep the host clock synchronized to UTC for FT8 slot
scheduling.

Station identity is currently configured in the source. Before transmitting,
replace the `AC8SS` callsign and `EN82` grid defaults in `server.py` (including
its fallback CQ) and `transmit.py`. Keep the defaults in `qso.py` and the station
filters in `monitor.py` and `export_adi.py` consistent with your callsign.

## Run the platform

Run the following commands in separate terminals with the environment activated.

### 1. Start the API server

```sh
python server.py --port 7777
```

The server creates `db.sqlite3` automatically. Set `DATABASE_URL` to choose a
different database. By default it rebuilds QSO state from the last 1,800 seconds
of received messages; `--replay 0` starts with empty runtime QSO state.

### 2. Receive from a KiwiSDR

Replace `kiwi.example.org:8073` with your receiver's host and port:

```sh
python sdr_ft8_client.py \
  --server kiwi.example.org:8073 \
  --frequency-khz 14074 \
  --site_id 1 \
  --master 127.0.0.1:7777
```

The receiver uses USB audio at 12 kHz, calibrates the incoming stream to FT8's
15-second slots, prints detections as JSON lines, and posts batches to the server.
Supply `--site_id` because the API requires a receiving-site ID. Use distinct IDs
for different receivers.

`--frequency-khz` is the receiver's dial frequency in **kHz**. The example tunes
to 14.074 MHz; the current code default is 14.075 MHz, so set it explicitly.
Optional settings include `--password`, `--low-cut-hz`, and `--high-cut-hz`.

Add `--record received.wav` to save the audio and a JSON sidecar containing its
start timestamp. If posting to the master fails, the client logs the failure and
disables further posting for that run; restart it after restoring connectivity.

### 3. Transmit through picoFT8 hardware

```sh
python transmit.py \
  --master 127.0.0.1:7777 \
  --port /dev/ttyUSB0 \
  --freq 1407500000
```

`--freq` is the lowest transmitted RF tone in **hundredths of a hertz**:
`1407500000` means 14.075 MHz, or a 1,000 Hz audio offset from the receiver dial
frequency in the example above. Adjust the serial device and frequency for your
hardware and operating setup.

The transmitter requests a message for each upcoming 15-second slot. The server
selects a QSO response or returns its fallback CQ. Encoding and HTTP requests run
outside the serial timing thread. RF begins at slot +0.5 seconds and sends 79
symbols of 160 ms each. Late offers are skipped, and completed QSO transmissions
are acknowledged to the server. Actual timing depends on USB serial latency and
the firmware.

For a fixed message without the API server:

```sh
python transmit.py --msg 'CQ YOURCALL GRID' --port /dev/ttyUSB0 --freq 1407500000
```

Replace `YOURCALL` and `GRID` with your station identity. Fixed-message mode
repeats every 15 seconds. `--master` and `--msg` are mutually exclusive. Stop the
transmitter with Ctrl-C.

### 4. Monitor and export

```sh
python monitor.py --master 127.0.0.1:7777 --all
python export_adi.py W1ABC > W1ABC.adi
```

The monitor polls every five seconds. Without `--all`, it filters for the local
callsign currently coded in the script. The ADIF exporter currently queries
`127.0.0.1:7777` and labels contacts as 20 m FT8.

## Replay and testing

Replay a recorded mono, 16-bit, 12 kHz WAV through the receive pipeline:

```sh
python sdr_ft8_client.py --file received.wav --site_id 1 --master 127.0.0.1:7777
```

The client reads the recording's JSON sidecar for its start time. For recordings
without a sidecar, supply `--start-utc` as Unix seconds or a timezone-qualified
ISO timestamp. Add `--realtime` to pace playback at its original rate. Replay
submits detections to the configured server, just like live reception.

To exercise QSO scheduling without radio hardware, start the server and run:

```sh
python simulator.py --master 127.0.0.1:7777 --callsign W1ABC --grid FN31
```

Choose a simulated callsign different from the server's local callsign. The
simulator uses real UTC slots and simulates a remote station, background traffic,
and message loss. Use `--drop-rate 0` for loss-free exchanges, `--background 0`
to remove unrelated traffic, or `--seed 42` for reproducible random choices.

Run the hardware-free regression tests with:

```sh
python -m unittest test_simulator test_server test_sdr_ft8_client test_transmit
```

See [API.md](API.md) for endpoint contracts, nanosecond timestamps, restart
behavior, and migration of older seconds-based databases.
