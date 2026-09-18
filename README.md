# Broadlink IR Remote Learning

This is a Python script that can be used to learn the IR commands for a Broadlink RM2/RM3/RM Pro+ device.
The script will ask you to enter the IP address of your device, and then guide you through the process of 
learning each of the commands for your air conditioner or other IR-controlled device.

Its purpose is to automate the creation of a new JSON file for the 
[smartir Home Assistant integration](https://github.com/litinoveweedle/SmartIR), specifically for
[climate devices](https://github.com/litinoveweedle/SmartIR/blob/master/docs/CLIMATE.md).

## Requirements

- A Broadlink RM device
- Python 3
- The `broadlink` Python package

## Setup

First you will need to install the required packages. It is recommended to use a Python virtual environment.
Then just run `pip install -r requirements.txt`.

You will need the IP address of your Broadlink device. You can get that from your DHCP server or by
using the `broadlink_discovery` command from the 
[`python_broadlink` library](https://github.com/mjg59/python-broadlink/tree/master/cli).

## Configuration

Create a `smartir.json` file using the `template.json` as a template. Adjust the temperature range, 
manufacturer, model and operating, fan and swing modes.


## Web interface (recommended)

A browser UI that manages **several devices of different kinds in one project** —
air conditioners, TVs, fans and lights — and exports them for SmartIR.

```
pip install -r requirements.txt
python server.py
```

Then open <http://127.0.0.1:8777>.

Or use the run script for your platform, which installs dependencies, starts the
server, and opens the browser automatically:

```
run.bat      # Windows: double-click, or run from a terminal
./run.sh     # macOS / Linux
```

On first run the existing `smartir.json` is imported automatically as the first
device. `smartir.json` itself is never modified, so `learn.py` keeps working.
The project lives in `devices.json`.

### Supported platforms

| Platform | Code slots generated |
| --- | --- |
| `climate` | `off`, optional `on`, then operation → *preset* → fan → swing → temperature |
| `media_player` | `on`, `off`, `mute`, `volumeUp`, `volumeDown`, `nextChannel`, `previousChannel`, and one per source |
| `fan` | `on`, `off`, `oscillate`, and one per direction × speed |
| `light` | `on`, `off`, `night`, `brighten`, `dim`, `colder`, `warmer`, and one per brightness level / colour temperature |

Slot names and nesting follow the SmartIR component sources
(`climate.py`, `media_player.py`, `fan.py`, `light.py`), not the docs — the
docs list fan and media_player as TBD.

### Learning

Slots are grouped (for a climate device, one group per mode combination) and each
group can be:

- **learned in sequence** — walks the group, skipping codes already captured, so
  you can stop and resume at any time;
- **filled with one code** — for modes with no temperature selection, the
  equivalent of answering `s` in the CLI;
- **cleared**.

Individual slots have **Learn / Test / Delete**. *Test* replays a stored code
through the Broadlink so you can confirm a capture before moving on. Every code is
written to `devices.json` immediately, so an interrupted session loses nothing.

### Export

Two buttons, because they do different jobs:

- **One file for all devices** (`broadlink-devices.json`) — the whole project in a
  single document, for backup and for restoring into this tool.
- **ZIP ready for SmartIR** (`smartir-codes.zip`) — `codes/<platform>/<name>.json`,
  one file per device.

> Home Assistant's SmartIR loads **one file per device**, so the combined file
> cannot be dropped into HA directly. Use the ZIP for the actual install; use the
> combined file to back up or move your work.

You can also download a single device's SmartIR file, import an existing SmartIR
file as a new device, or restore a previously exported project.

## Usage (CLI)

The original script still works and is unaffected by the web UI. Run it from the
command line and pass the IP address of your Broadlink device as the first
argument:

```
python learn.py 192.168.0.100
```

It will guide you through learning each command for a single climate device,
reading and writing `smartir.json`.

## Output

As the script learns the commands for your device it will keep updating the `smartir.json` file. You
can then use this file for the `smartir` integration.

## References

* [`python_broadlink` library](https://github.com/mjg59/python-broadlink/)
* [Home Assistant Broadlink integration](https://www.home-assistant.io/integrations/broadlink/)
* [smartir Home Assistant integration](https://github.com/litinoveweedle/SmartIR)
