# picoFT8

## World simulator

Start the API server, then run:

```sh
python simulator.py --master 127.0.0.1:7777 --callsign W1ABC --grid en82
```

The simulator needs only Python's standard library. It runs until Ctrl-C, using
real UTC 15-second slots and nanosecond API timestamps. Choose a callsign distinct
from the server's local callsign (`AC8SS`). Existing seconds-based databases must
first be migrated as described in [API.md](API.md#migrating-from-seconds).

The master keeps the TX parity of its first scheduled slot. Offers are requested
at least 10 seconds ahead, transmissions begin at slot +0.5 seconds, and TX is
acknowledged at slot +13 seconds. HTTP operations run in order on a worker so
network delays do not move the slot clock; late offers are discarded. Requests
for subsequent master slots follow the previous TX acknowledgment, allowing
roughly 17 seconds of preparation under normal conditions.

One remote station responds to actually transmitted CQs, sends its grid on the
opposite parity, then advances through R-report and 73. It repeats its grid or
R-report every 30 seconds until it hears the next relevant message. It sends 73
once and sends it again if it hears another RR73. Completed contacts do not restart
on later CQs; the server also keeps completed callsigns terminal.

Both directions independently drop messages with probability 0.5. Loss affects
reception, never whether an actual master transmission is acknowledged. Receive
batches are delivered at slot +13 seconds with the original slot's timestamp;
they include up to five random background CQs and any successfully decoded peer
response. The master listens only on its opposite parity.

Useful options:

- `--drop-rate 0`: loss-free exchange; `--drop-rate 1`: drop every peer/master decode.
- `--seed 42`: reproducible random choices for the same sequence of slot events.
- `--background 0`: disable unrelated traffic.
- `--allowance 10`: minimum request lead time, in seconds.
- `--timeout 10`: HTTP socket timeout, in seconds.
- `--site-id 1`: receiver identity used in RX records.

The server preserves prefetched offers across repeated RX and QSO phase changes.
Acknowledgments account for the stage that issued the offer, while preserving
newer scheduling decisions. Identical TX acknowledgments are idempotent for ten
minutes after the target slot. The simulator logs HTTP failures and does not
retry automatically; RX submissions remain non-idempotent.

Run the simulator and API tests with:

```sh
python -m unittest test_simulator test_server test_sdr_ft8_client
```

## Server-driven transmitter

Use the Arduino/Si5351 transmitter with the API server:

```sh
python transmit.py --master 127.0.0.1:7777 --port /dev/ttyUSB0
```

Install `pyserial` in the Python environment and build the message encoder with
`make -C ft8_lib` first. `--freq` retains the existing unit of hundredths of Hz.

A request worker obtains a slot-specific offer and converts it to 79 frequencies.
The main thread exclusively controls serial timing. A separate acknowledgment
worker records completed transmissions, retrying transient HTTP failures with
the same handle and slot within the server's ten-minute retention window.
Network calls and conversion never run on the serial thread.

Server mode uses a fixed TX parity: `--tx-parity auto` (the default) selects the
first slot with sufficient preparation time; `--tx-parity even` or `odd` fixes it
explicitly. Requests have at least `--allowance 10` seconds of lead time. The next
request starts at the previously requested slot boundary, allowing it to overlap
transmission. Offers that arrive or finish encoding after their target boundary
are discarded. Failed requests do not cause a fallback CQ or reuse of an old offer.

The serial thread preloads the first tone before the boundary and enables RF at
slot +0.5 seconds. A completed frame lasts 79 × 0.160 seconds, so acknowledgment
is queued after RF is turned off, around slot +13.14 seconds. Late preparation
or a start missed by more than 50 ms skips the frame. Serial failures or a full
symbol of dispatch lateness abort the frame with an attempt to turn RF off;
incomplete frames are not acknowledged. Hardware timing still depends on USB
serial latency and the Arduino firmware.

Queues are bounded. Queue overflow, missed slots, and permanently rejected or
expired acknowledgments are logged. Ctrl-C stops the transmitter; pending
acknowledgments are held only in memory and are not persisted across shutdown.
`--timeout` bounds HTTP socket waits and encoder subprocess execution.

Fixed-message mode remains available, transmitting on each 15-second slot:

```sh
python transmit.py --msg 'CQ AC8SS EN82' --port /dev/ttyUSB0
```

`--master` and `--msg` are mutually exclusive. Omitting both retains the default
fixed CQ. Run the hardware-free regression tests with:

```sh
python -m unittest test_transmit test_server test_simulator test_sdr_ft8_client
```
