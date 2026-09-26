# Fuji Mitsu maker Explore

FMakerExplore is a filesystem utility for the Fuji and Mitsu MakerCmd interface. It connects to the handset over USB, enters Maker Mode when necessary, and provides access to files and directories on the phone.

## Features

- Connect to and probe the MakerCmd interface.
- List available drive letters and directory contents.
- Display file size, attributes, and timestamp data.
- Download files with checksum validation and resume support.
- Upload files with checksum validation and automatic retry handling.
- Create directories, delete files, and change the read-only attribute.
- Display the available space on the `D:` drive.

## Requirements

- Python 3.10 or newer
- `pyusb`
- A working libusb backend

```bash
python3 -m pip install pyusb
```

## Usage

Connect to the phone and enter Maker Mode:

```bash
python3 FMakerExplore.py --connect
```

List the available drives:

```bash
python3 FMakerExplore.py --listpartition
```

List a remote directory:

```bash
python3 FMakerExplore.py --listdir 'D:\WcdmaMp\'
```

Display remote file information:

```bash
python3 FMakerExplore.py --listfile 'D:\WcdmaMp\file.bin'
```

Download a file:

```bash
python3 FMakerExplore.py --pull 'D:\WcdmaMp\file.bin' ./file.bin
```

Use `--overwrite` to replace an existing local file, or `--no-resume` to discard an existing partial download.

Upload a file:

```bash
python3 FMakerExplore.py --push ./file.bin 'D:\WcdmaMp\file.bin'
```

The upload command refuses to replace an existing remote file.

Create a remote directory:

```bash
python3 FMakerExplore.py --mkdir 'D:\WcdmaMp\newdir'
```

Delete a remote file:

```bash
python3 FMakerExplore.py --delete 'D:\WcdmaMp\file.bin'
```

Enable or disable the read-only attribute:

```bash
python3 FMakerExplore.py --setreadonly 'D:\WcdmaMp\file.bin' on
python3 FMakerExplore.py --setreadonly 'D:\WcdmaMp\file.bin' off
```

Display the available space on `D:`:

```bash
python3 FMakerExplore.py --checkdavailable
```

Use `--help` to display all available options. Remote paths must be absolute, use a drive letter, and contain only ASCII characters.

## Connection Workflow

1. Start the phone and connect it to the computer over USB.
2. Run the required FMakerExplore command.
3. The tool checks whether Maker Mode is already active.
4. If necessary, it switches the USB interface and waits for Maker Mode to become available.
5. The requested filesystem operation is then performed.

The default USB device ID is `06d3:21b0`. Use `--vid` and `--pid` to select another compatible device.
