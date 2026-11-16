# Protocol of Mira valves with the `267f` service

Everything mira-ble knows about how current Mira digital shower valves talk over Bluetooth,
and how it knows it. Some sources are stronger than others, so each fact says where it comes
from:

- **measured**: bytes recorded from a real valve, usually after sending it a known value.
  Sources: [mira-mode-control](https://github.com/ryan-shaw/mira-mode-control) (a Mode dual
  outlet valve with bath filler), [ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode)
  (a dual outlet valve), [Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA)
  (frames the vendor app sent to an Activate, from an Android HCI capture, and months of
  running through ESPHome proxies), and this project's own readings from two Mira Platinum
  valves: one before this library existed, and one with this library in November 2026.
- **app**: read from the Mira Android app (version 5.5). The app names the fields and builds
  every request, but that doesn't prove what a given valve's firmware actually does.
- **reported**: another project says so, without a recording to check.
- **unknown**: nobody knows yet.

## GATT

| UUID | Role |
| --- | --- |
| `267f0001-eb15-43f5-94c3-67d2221188f7` | Protocol service |
| `267f0002-…` | Commands: write without response |
| `267f0003-…` | Replies: notifications |
| `0000fe59-…` | Nordic Secure DFU (firmware update; not used) |

The service UUID is usually advertised, but **other devices use it too** (measured, by a user
of Mira-Activate-HA). The advertised name is what identifies a valve: `Mira <code>#<serial>` or `Mira <code> <room name>`, in either case,
where `<code>` is four hexadecimal digits that differ between products (`0051`, `003F` and
`004C` have been seen). One valve can use both forms.

The Bluetooth address is a "static random" address, and it has been seen to change after a
power cut (measured on an Activate: four addresses for one shower). ESP-IDF and BlueZ both
store a bond per address, so the bond doesn't follow the valve to its new address
(inferred).

### Bonding

The valve wants a bonded, encrypted link. Without one, a write, or even subscribing to
notifications, fails with GATT "insufficient authentication" (measured). It only accepts a new
bond while it's in pairing mode; otherwise it says no, which shows up as SMP reason 82 through
an ESPHome proxy (measured). Pairing is "Just Works"; one implementation asks for LE Secure
Connections with bonding and no MITM protection. Turning the valve's power off and on is said
to leave it pairable for a little while.

A Mira Platinum keeps at least two bonds at once: a Mac and an ESPHome proxy both stayed
paired (measured). The Mira app's text says ten, with a "Manage Paired Devices" list; one
project says one, for its valve. The real limit is unknown.

Through an ESPHome proxy, a stored bond isn't used automatically on a new connection: the
client has to ask the proxy to pair every time, which re-encrypts with the stored key
(measured). That pair call can return before encryption is actually up, so subscribing
straight away can still be refused; waiting about half a second fixes it (measured, and the
app waits 500 ms at the same point). A proxy can also keep accepting connections while its
bond store is stuck, giving the same refusal for days until it's restarted (measured).

Two proxy firmware problems can lose the bond. On every ESP-IDF version by default, a failed
encryption, including a connection that drops mid-encryption, deletes the bond from the
proxy's flash; from ESP-IDF 5.5.6 the option
`CONFIG_BT_BLE_SMP_REMOVE_BOND_ON_PAIR_FAIL_AS_CENTRAL` can turn that off (measured once on
5.5.5; ESP-IDF source). And ESP-IDF 5.5.6 itself forgets a bond after the first connection
until a restart, which ESPHome 2026.11 works around (esphome#21880, esphome#22559).

## Frame

```
AA 55 <channel> <opcode> <length> <payload ...> <checksum>
```

- `channel`: `00` on requests, `01` on replies.
- `length`: payload bytes only.
- `checksum`: two's complement of the sum of all preceding bytes; a valid frame sums to zero
  modulo 256.

A reply doesn't repeat the request's opcode: its opcode byte is always `01` (measured). So
the only way to tell which request a reply belongs to is order and shape. That means: send
one request at a time, don't accept a reply of the wrong shape (this library gives every read
a shape test), and don't keep using a connection after a request went unanswered.

A one-byte reply `01` means "done"; `80` means "no" (measured). The app treats bit 7 of the
first payload byte as the error flag. **A valve may also just not answer** a request it
doesn't support: on a Platinum most unsupported opcodes got silence, not `80` (measured). The
app waits 1.8 s for a reply and doesn't retry.

The state reply is 24 bytes, more than fits in one notification at the default MTU. The app
doesn't reassemble pieces and never asks for a bigger MTU, so it relies on one being
negotiated; whether replies ever arrive in pieces isn't known. This library puts pieces back
together anyway. Going the other way, only a preset write (67 bytes) is bigger than the
default MTU; the app and the other projects always write a frame in one go, and whether a
valve would put a split command back together is unknown.

## Opcodes this library uses

### `2B` read state (measured, app)

Request payload `02`. Reply, 18 bytes:

| Offset | Meaning | Source |
| --- | --- | --- |
| 0 | System status: bits 0–2 a status value, bits 3–6 flags. Bit 6 is reported to come on after the first poll of a session | app; reported |
| 1 | Low five bits: the preset running (the app reads it so, and treats 17 to 20 as a separate kind). On a Platinum it showed only during the shower proper of a preset started and continued at the wall (`24` = slot 4); zero in warm-up, at rest, and throughout any run started over Bluetooth | app; measured (Platinum) |
| 2–3 | Litres delivered since the valve left rest, 16 bits (the app subtracts it from a preset's volume). On a Platinum it rose by one about every 8 s at 7 L/min, held while the valve was held, cleared at rest. Zero in every recording from other models | app; measured (Platinum) |
| 5 | Equals byte 16 (the flow reached) on a Mode valve and on a Platinum | measured |
| 6 | Bit 4 (`10`): set while a Platinum warms a preset up; the app reads the same bit to pick its status label | app; measured (Platinum) |
| 8–9 | Bits 4–5 of byte 8: Wi-Fi module status (2 = provisioned); `80`/`90` seen at rest. On a Platinum: `A0 00` at rest, `20 00` in warm-up, `60 00` then `E0 00` while held, and during the shower proper `60` plus the seconds the preset has left (`61 DF` = 479 of 480). The app reads the low twelve bits of bytes 8–9 as the remaining time | app; measured |
| 10–11 | Target temperature, tenths of a degree, in the low nine bits. Higher bits are flags while running (`05 C2` and `0D C2` both reported for 45.0 °C; `09 86` measured on a Platinum). Zero means no target at rest on some models and "cold" while running; a Platinum at rest keeps the last value used | measured |
| 12 | Flow setting, quarter litres per minute. At rest: a default, or the last value used | measured |
| 13 | Outlets: `01`, `02`, `04` = outlets 1 to 3; `40` = held; `80` = error | measured (outlets, `40`), app (`80`) |
| 14–15 | Measured water temperature, tenths of a degree | measured |
| 16 | Flow reached, quarter litres per minute; ramps after a start. One project describes it as a slow counter at rest, which no recording shows | measured; reported |
| 17 | Error code; `01` = OK. The app holds a table of about 40 codes | app |

**Bit 6 of byte 13 is a hold.** The app labels it "paused" and sends it to close the last
outlet. On a Platinum (measured, two runs started at the wall) the valve sets it, with no
outlet bit, when a preset has warmed the water and waits for the outlet button, and again
after a stop; each time it dropped to rest by itself two minutes later. Flow reads zero
within a few seconds of the hold starting. An earlier reading on another Platinum had the
water temperature still rising with bit 6 set and no outlet bit.

So this library keeps two ideas apart. Water is *running* while an outlet bit is set or
byte 16 shows flow, whatever the other bits say, and a stop only counts once neither is true
on two reads. The valve is *active* while it's running or held, and at rest otherwise.

Mira-Activate-HA documents the measured temperature as the value divided by 8.5. The
recordings (25.6 °C idle, 24.6 → 26.0 °C warming), a Platinum's 20.9 °C at rest and the
app's own arithmetic all say tenths, which is what this library uses.

### `AB` set outlets (measured)

Payload: temperature (tenths, big endian), flow byte, outlet bitfield. The command states the
complete desired state; an outlet not named is switched off.

| Frame | Meaning | Source |
| --- | --- | --- |
| `AA 55 00 AB 04 01 7C 40 02 93` | 38.0 °C, 16 L/min, outlet 2 | vendor app, captured |
| `AA 55 00 AB 04 01 7C 40 03 92` | 38.0 °C, 16 L/min, both outlets | vendor app, captured |
| `AA 55 00 AB 04 01 86 64 01 66` | 39.0 °C, 25 L/min, outlet 1 | accepted by a Mode valve |
| `AA 55 00 AB 04 01 90 20 01 A0` | 40.0 °C, 8 L/min, outlet 1 | ran a Platinum, from rest and from its hold |
| `AA 55 00 AB 04 01 90 20 40 61` | pause: 40.0 °C, 8 L/min, hold bit only | stopped a Platinum, settings kept |
| `AA 55 00 AB 04 00 00 00 00 52` | stop everything | stopped a Mode valve and a Platinum |
| `AA 55 00 AB 04 01 86 00 00 CB` | stop, temperature left in place | stopped a Mode valve |

- A temperature of zero with outlets open is the app's "cold". This library sends it only to
  pass a "no target" state through unchanged.
- The app applies no limit of its own beyond its slider range. This library refuses anything
  above 48 °C or above the caller's limit. Values from 51.2 °C would spill into the flag bits.
- **Naming an outlet the valve does not have makes it ignore the whole command**, silently
  (measured: bit `04` on a two-outlet Activate). Never send a bit for an outlet the valve did
  not report.
- The app closes the last outlet by sending bitfield `40`, and its "all off" paths send a
  payload beginning `01`, which is simply the high bit of 25.6 °C with no flow and no
  outlets, not a flag.
- The outlet state has been reported to show 3 to 5 s after the write, most of it the valve
  acting. This library waits up to 8 s for it.
- **On a Platinum, `AB` works** (measured on one unit, November 2026), although the vendor
  app never sends it to one. An outlet command runs water at once, with no warm-up, and is
  also what starts the shower from the hold a preset leaves after its warm-up.
- **Two stops, with different after-effects on a Platinum** (measured). The app's pause (the
  valve's own temperature and flow with bitfield `40`) shuts the water off in about 3 s and
  leaves the valve held with its settings, exactly as a stop at the wall does: the
  controller shows the litres used and the valve rests two minutes later. The all-zero stop
  also shuts the water off and goes straight to rest, but overwrites the resting target and
  flow with zero and the minimum, which the controller then shows as "cold". This library
  sends the pause first and the all-zero stop only if water still runs.

### `B1` run preset (measured)

Payload: the slot, from 1. Measured on Mode valves and on one Platinum. One reading of the
app's code calls `B1` a read on the Activate; untested there.

The app sends nothing but `B1 <slot>` to run a preset: no duration, no second kind of
command. It then shows the preset number, the volume used and the time left from the state
(bytes 1, 2–3 and 8–9).

**`B1` with slot 0 stopped a running Platinum, reliably** (measured on an earlier unit). This
library uses it as the second stop when water still runs after the all-zero `AB`.

Started at the wall, a Platinum preset runs in two steps (measured): water runs until it is
up to temperature, then the valve shuts it off and holds (bit 6 of byte 13) until the
outlet button is pressed. `B1` over Bluetooth does the same (measured): about 15 s of water,
then the hold. What happens next depends on what ends the hold (all measured):

- **the same `B1` again** starts the preset proper: the state then carries the preset number
  (`26` for slot 6) and counts its duration down (`61 66` = 358 of 360 s), and the wall
  controller shows the timer;
- **an `AB` outlet command, or the outlet button at the wall**, starts a plain run: no preset
  number, no countdown, so only the outlet's maximum run time applies.

A preset started at the wall and continued with the outlet button counts down as the first
case does.

### `5D` read preset, `DD` write preset (measured)

Request payload: the slot. Reply and write payload, 61 bytes:

| Offset | Meaning |
| --- | --- |
| 0 | Slot, echoed. An invalid slot echoes `FF` |
| 1–32 | Name, NUL-padded ASCII; 31 characters are read back (app; the widest name measured is 16) |
| 33–34 | Duration in seconds, or zero for a volume preset |
| 35–36 | Volume in litres, or zero for a timed preset |
| 37 | Outlets in bits 2–4, and the top two bits of the temperature in bits 0–1 |
| 38 | Temperature low byte (tenths of a degree; the app rounds to whole degrees, the valve keeps tenths) |
| 39 | Flow byte |

Presets read from a valve re-encode to the same bytes. Writing a slot with zeroes after the
slot number clears it. A write replaces the whole slot. The app treats a preset with neither
duration nor volume as empty; this library refuses to write one.

The highest slot is unknown: this library addresses 1 to 9, another uses 0 to 15. The app
also recognises a shorter reply with a 16-byte name that no recorded valve has produced;
this library records such a reply as an error and moves on. One Platinum answered `5D`
only while running and another answered it at rest (both measured), so presets may not be
listable at rest on every valve.

### `1B` read outlet configuration (measured, app)

Request payload: outlet index from 0. Reply, 16 bytes:

| Offset | Meaning | Source |
| --- | --- | --- |
| 0 | Outlet index, echoed | measured |
| 1 | Outlet type: 1 hand shower, 21–23 bath filler, 31 overhead. Both outlets of one recorded dual valve report 1 | app; measured |
| 3–4 | Maximum temperature, tenths | measured |
| 5–6 | Minimum temperature, tenths | measured (one recording), app |
| 7–8 | Default temperature, tenths | measured (one recording), app |
| 9, 10 | Maximum and minimum flow bytes | measured (one recording), app |
| 11 | Default flow byte | measured |
| 12–13 | Maximum run time, seconds: the valve stops the outlet by itself after this | measured |

### `1A` read valve configuration (app)

No payload. Reply, 3 bytes; the third is the number of outlets.

### `5C` usage (measured, app)

Request `02 00`: the last finished run. Reply, 6 bytes: byte 0 echoed, duration in seconds
(1–2), volume in litres (3–4), blended temperature in half degrees (5; `FF` = none).
Measured against the app's screen. There is no energy field.

Request `00 <day>`: a daily record of 10 bytes (uses, total duration, water, average
temperature, hottest inlet, coldest inlet), named from the app's database columns. One
undecoded recording fits it. Scales unknown; not implemented.

### Identity (measured)

| Opcode | Request | Reply |
| --- | --- | --- |
| `44` | none | 16-byte NUL-padded name |
| `40` | `01` | 18-byte NUL-padded serial number. Eighteen `0` characters mean "none" (app). Characters 5–14 of a real serial are day, month, year, hour and minute of manufacture |
| `41` | none | 4 bytes: years since 2000, day, month nibble and 12 bits of minutes past midnight |

## Opcodes this library deliberately does not send

The Mira app builds these. They change lasting settings that are hard or impossible to put
back, or nobody knows what they do.

| Opcode | What the app uses it for | Why not |
| --- | --- | --- |
| `F4` | Reset (types 1 and 2); after type 2 the app forgets the product | Effect on configuration and registration unknown |
| `F8` | Commissioning state | State 2 presumably runs water |
| `C0` | Write the serial number when the valve reports none. One capture shows the app sending it in normal use, with a reply carrying the room name | May overwrite the valve's identity |
| `C2` | Wi-Fi SSID, password, cloud tenant and scope | Can take the module off the cloud |
| `9B` | Write outlet configuration: temperature and flow limits, run time, type | This is the safety envelope |
| `DF` | Controller configuration | Mostly unidentified bytes |
| `BC` | Warm-up setting | Persistent; values partly known |
| `C4` | Write the valve's name | Persistent |

**Don't sweep opcodes on a valve you care about.** Trying opcodes blindly with well-formed
one-byte payloads hits `F4` and `BC`, and both say "done". One valve probed that way was
later replaced.

Read opcodes the app builds that are not implemented here: `32` (firmware and type
identifiers), `3C` (warm-up setting), `42` (device ID; one recorded reply, `02` and fourteen
zero bytes, on a valve with none set), `5A`/`5B` (error log and a diagnostic trace of
temperatures, flows and motor positions, which another project reads as inlet temperatures
from a single idle recording), `5F` (controller configuration), `78` (commissioning state).

## Connection behaviour

- The vendor app holds one connection for the whole session and polls `2B` about once a
  second. It was captured running the link at about a 4 s interval when idle and 15 ms while
  talking.
- The valve has been reported to drop an idle link after about 36 s, to take around 6 s to
  answer a state read on a slow link through a proxy, and occasionally to stall beyond 10 s.
  A 30–75 ms interval with a 20 s supervision timeout is reported to work well through a
  proxy; 15 ms overloaded a shared proxy radio.
- This library leaves it to the caller how long to keep a connection. Through an ESPHome
  proxy, a connection kept open more than a few seconds answered in 7 to 10 s instead of
  0.1 to 0.3 s (measured), so the Home Assistant integration connects afresh every time; from
  a Mac, a connection kept open for minutes stayed quick (measured).
- A Mira Platinum accepts at least two connections at once (measured 2026-11-16: a Mac held one
  for five minutes, reading every 5 s, while Home Assistant connected eight times through an
  ESPHome proxy; nothing failed) and keeps advertising while connected. It also keeps at least
  two bonds (the Mac's and the proxy's). The Mira Mode is reported to take one connection
  (ha-mira-mode). The upper limit, and whether the wireless wall controller uses a slot, are
  unknown.

## Open questions

- Whether a Platinum stops the water by itself when a preset's countdown reaches zero.
- Whether other models take the app's pause the way the Platinum does.
- What bit 6 of the outlet byte and byte 3 mean on models other than the Platinum.
- How many bonds and connections a valve takes at most.
- The highest preset slot.
- Whether a valve joins a command split over several writes.
- The meaning of the status bits in state bytes 0 to 9.
