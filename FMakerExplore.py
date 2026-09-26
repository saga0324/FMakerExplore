#!/usr/bin/env python3

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import BinaryIO, Optional, Sequence

import usb.core
import usb.util


DEFAULT_VID = 0x06D3
DEFAULT_PID = 0x21B0
EP_OUT = 0x03
EP_IN = 0x82
EP_CONTROL_REPLY = 0x81
READ_SIZE = 4096
UPLOAD_CHUNK_SIZE = 1500
REQUEST_MAGIC = b"\x55\x56\x42"
RESPONSE_MAGIC = b"\x56\x55\x42"

STATUS_MORE = 0x90
STATUS_LAST = 0x91
STATUS_INFO = 0x92
STATUS_FILE_ERROR = 0xA1
STATUS_OK = 0x00
STATUS_UPLOAD_MORE = 0x20
STATUS_CHECKSUM_ERROR = 0x21
STATUS_NO_SPACE = 0x11
STATUS_FS_ERROR = 0x12


class FSAccessError(Exception):
    pass


class DeviceConnectionError(FSAccessError):
    pass


class ProtocolError(FSAccessError):
    pass


class TransportError(FSAccessError):
    pass


class TransferRestart(FSAccessError):
    pass


class ChecksumMismatch(ProtocolError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class RemoteError(FSAccessError):
    def __init__(self, message: str, status: Optional[int] = None,
                 os_error: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.os_error = os_error


def parse_int(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc


def mask_packet(packet: bytes) -> bytes:
    out = bytearray((0xFF,))
    for byte in packet:
        if byte in (0xFD, 0xFE, 0xFF):
            out.extend((0xFD, byte ^ 0x10))
        else:
            out.append(byte)
    out.append(0xFE)
    return bytes(out)


def unmask_packet(frame: bytes) -> bytes:
    if len(frame) < 2 or frame[0] != 0xFF or frame[-1] != 0xFE:
        raise ProtocolError("invalid MakerCmd frame delimiters")

    out = bytearray()
    pos = 1
    while pos < len(frame) - 1:
        byte = frame[pos]
        if byte == 0xFD:
            if pos + 1 >= len(frame) - 1:
                raise ProtocolError("truncated MakerCmd escape sequence")
            out.append(frame[pos + 1] ^ 0x10)
            pos += 2
        else:
            out.append(byte)
            pos += 1
    return bytes(out)


def encode_remote_path(path: str) -> bytes:
    normalized = path.replace("/", "\\")
    if not re.match(r"^[A-Za-z]:\\", normalized):
        raise FSAccessError(
            f"remote path must be absolute (for example D:\\Data\\file.bin): {path!r}"
        )
    try:
        return normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FSAccessError("MakerCmd paths must currently contain ASCII characters only") from exc


def normalize_remote_path(path: str) -> str:
    encoded = encode_remote_path(path)
    return encoded.decode("ascii")


def signed_be32(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True)


def format_size(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{value} B"


class FrameReader:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def clear(self) -> None:
        self.buffer.clear()

    def feed(self, data: bytes) -> Optional[bytes]:
        self.buffer.extend(data)
        while self.buffer and self.buffer[0] != 0xFF:
            del self.buffer[0]
        if not self.buffer:
            return None

        escaped = False
        for pos in range(1, len(self.buffer)):
            byte = self.buffer[pos]
            if escaped:
                escaped = False
                continue
            if byte == 0xFD:
                escaped = True
            elif byte == 0xFE:
                frame = bytes(self.buffer[:pos + 1])
                del self.buffer[:pos + 1]
                return frame
            elif byte == 0xFF:
                del self.buffer[:pos]
                return self.feed(b"")
        return None


class MakerCmdClient:
    def __init__(self, vid: int, pid: int, timeout: float, retries: int,
                 maker_wait: float, verbose: bool = False) -> None:
        self.vid = vid
        self.pid = pid
        self.timeout_ms = max(1, int(timeout * 1000))
        self.retries = max(0, retries)
        self.maker_wait = max(1.0, maker_wait)
        self.verbose = verbose
        self.dev = None
        self.usb_core = None
        self.usb_util = None
        self.reader = FrameReader()

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[debug] {message}", file=sys.stderr)

    def connect(self) -> None:
        self.usb_core = usb.core
        self.usb_util = usb.util
        try:
            self.dev = self.usb_core.find(idVendor=self.vid, idProduct=self.pid)
        except Exception as exc:
            raise DeviceConnectionError(f"USB discovery failed: {exc}") from exc
        if self.dev is None:
            raise DeviceConnectionError(
                f"D905i not found (VID=0x{self.vid:04x}, PID=0x{self.pid:04x}). "
                "Connect the phone and check USB permissions."
            )
        self._ensure_configuration()
        self.reader.clear()
        self.log("USB device found")

        try:
            response = self.request(b"\xC6\x13\x00z:\\905ifsaccess_probe", timeout_ms=1000)
            if response[:3] == b"\xC6\x13\xA1":
                self.log("phone is already in Maker Mode")
                return
        except FSAccessError:
            self.reader.clear()

        self._enter_maker_mode()

    def _ensure_configuration(self) -> None:
        assert self.dev is not None
        try:
            configuration = self.dev.get_active_configuration()
            self.log(f"USB configuration {configuration.bConfigurationValue} is active")
            return
        except Exception as exc:
            if self.usb_core is None or not isinstance(exc, self.usb_core.USBError):
                raise DeviceConnectionError(
                    f"cannot inspect USB configuration: {exc}"
                ) from exc
            self.log(f"USB configuration is not active: {exc}")

        try:
            self.dev.set_configuration()
            configuration = self.dev.get_active_configuration()
        except Exception as exc:
            raise DeviceConnectionError(
                f"cannot activate D905i USB configuration: {exc}"
            ) from exc
        self.log(f"activated USB configuration {configuration.bConfigurationValue}")

    def close(self) -> None:
        if self.dev is not None and self.usb_util is not None:
            try:
                self.usb_util.dispose_resources(self.dev)
            except Exception:
                pass
        self.dev = None
        self.reader.clear()

    def reconnect(self) -> None:
        self.close()
        time.sleep(0.25)
        self.connect()

    def _usb_call(self, description: str, callback):
        try:
            return callback()
        except Exception as exc:
            if self.usb_core is not None and isinstance(exc, self.usb_core.USBError):
                raise TransportError(f"USB {description} failed: {exc}") from exc
            raise

    def _drain_control_reply(self) -> None:
        try:
            self._usb_call(
                "control reply read",
                lambda: self.dev.read(EP_CONTROL_REPLY, 256, timeout=self.timeout_ms),
            )
        except TransportError as exc:
            cause = exc.__cause__
            if self.usb_core is not None and isinstance(
                cause, self.usb_core.USBTimeoutError
            ):
                self.log(str(exc))
            else:
                raise

    def _enter_maker_mode(self) -> None:
        assert self.dev is not None
        print("Entering Maker Mode...", file=sys.stderr)
        self._usb_call(
            "mode capability request",
            lambda: self.dev.ctrl_transfer(
                0x41, 0x62, 0x00, 0x00, b"\x02\xC0", timeout=self.timeout_ms
            ),
        )
        self._drain_control_reply()
        self._usb_call(
            "endpoint mode switch",
            lambda: self.dev.ctrl_transfer(
                0x41, 0x60, 0xC0, 0x00, timeout=self.timeout_ms
            ),
        )
        self._drain_control_reply()

        enter_packet = bytes.fromhex("FF 56 55 42 00 03 C1 01 00 FE")
        self._write_raw(enter_packet)
        try:
            self._usb_call(
                "mode-entry acknowledgement read",
                lambda: self.dev.read(EP_IN, 256, timeout=self.timeout_ms),
            )
        except TransportError as exc:
            cause = exc.__cause__
            if self.usb_core is not None and isinstance(
                cause, self.usb_core.USBTimeoutError
            ):
                self.log(f"mode-entry acknowledgement timed out: {exc}")
            else:
                raise

        deadline = time.monotonic() + self.maker_wait
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                response = self.request(
                    b"\xC6\x13\x00z:\\905ifsaccess_probe", timeout_ms=1000
                )
                if response[:3] == b"\xC6\x13\xA1":
                    print("Maker Mode connected.", file=sys.stderr)
                    return
                last_error = ProtocolError(
                    "Maker Mode probe returned unexpected payload: "
                    f"{response.hex(' ')}"
                )
            except FSAccessError as exc:
                last_error = exc
            time.sleep(1.0)
        detail = f": {last_error}" if last_error else ""
        raise DeviceConnectionError(
            f"phone did not enter Maker Mode within {self.maker_wait:.0f}s{detail}"
        )

    def _write_raw(self, data: bytes) -> None:
        if self.dev is None:
            raise DeviceConnectionError("USB device is not connected")
        written = self._usb_call(
            "write", lambda: self.dev.write(EP_OUT, data, timeout=self.timeout_ms)
        )
        if written != len(data):
            raise TransportError(f"short USB write: {written}/{len(data)} bytes")

    def _read_frame(self, timeout_ms: Optional[int] = None) -> bytes:
        if self.dev is None:
            raise DeviceConnectionError("USB device is not connected")
        deadline = time.monotonic() + ((timeout_ms or self.timeout_ms) / 1000.0)
        while True:
            frame = self.reader.feed(b"")
            if frame is not None:
                return unmask_packet(frame)
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TransportError("timed out waiting for MakerCmd response")
            chunk = self._usb_call(
                "read",
                lambda: bytes(
                    self.dev.read(EP_IN, READ_SIZE, timeout=max(1, remaining_ms))
                ),
            )
            frame = self.reader.feed(chunk)
            if frame is not None:
                return unmask_packet(frame)

    def request(self, payload: bytes, timeout_ms: Optional[int] = None) -> bytes:
        if len(payload) > 0xFFFF:
            raise ProtocolError("payload is too large for MakerCmd")
        packet = REQUEST_MAGIC + len(payload).to_bytes(2, "big") + payload
        self.log(f"> {payload.hex(' ')}")
        self._write_raw(mask_packet(packet))
        raw = self._read_frame(timeout_ms=timeout_ms)
        if len(raw) < 5 or raw[:3] not in (RESPONSE_MAGIC, REQUEST_MAGIC):
            raise ProtocolError(f"unexpected response header: {raw[:5].hex(' ')}")
        declared = int.from_bytes(raw[3:5], "big")
        if len(raw) < 5 + declared:
            raise ProtocolError(
                f"truncated response: declared {declared}, received {len(raw) - 5}"
            )
        response = raw[5:5 + declared]
        self.log(f"< {response.hex(' ')}")
        return response

    @staticmethod
    def _expect_group(response: bytes, group: bytes) -> bytes:
        if len(response) < 3 or response[:2] != group:
            raise ProtocolError(f"unexpected MakerCmd response: {response.hex(' ')}")
        return response

    @staticmethod
    def _remote_error(response: bytes, operation: str) -> RemoteError:
        status = response[2] if len(response) >= 3 else None
        os_error = None
        if len(response) >= 7 and status in (STATUS_FILE_ERROR, STATUS_FS_ERROR):
            os_error = signed_be32(response[3:7])
        suffix = f", Symbian error {os_error}" if os_error is not None else ""
        status_text = f"0x{status:02x}" if status is not None else "missing"
        return RemoteError(
            f"{operation} failed: remote status {status_text}{suffix}", status, os_error
        )

    def _query_with_reconnect(self, payload: bytes, operation: str) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self.request(payload)
            except TransportError as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                print(
                    f"{operation}: connection lost, reconnecting "
                    f"({attempt + 1}/{self.retries})...",
                    file=sys.stderr,
                )
                self.reconnect()
        assert last_error is not None
        raise last_error

    def check_d_available(self) -> int:
        response = self._expect_group(
            self._query_with_reconnect(b"\xC6\x13\x10", "space query"), b"\xC6\x13"
        )
        if response[2] != STATUS_INFO or len(response) < 8:
            raise self._remote_error(response, "space query")
        return int.from_bytes(response[4:8], "big")

    def list_partitions(self) -> list[str]:
        response = self._query_with_reconnect(b"\xC6\x13\x30", "partition list")
        if len(response) < 2 or response[:2] != b"\xC6\x13":
            raise ProtocolError(f"unexpected partition response: {response.hex(' ')}")
        partitions = []
        for value in response[2:]:
            char = chr(value)
            if char.isalpha():
                partitions.append(char.upper() + ":")
        return partitions

    def list_directory(self, remote_path: str) -> list[str]:
        path = normalize_remote_path(remote_path)
        if not path.endswith("\\"):
            path += "\\"
        initial = b"\xC6\x13\x31" + path.encode("ascii")
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                response = self.request(initial)
                output = bytearray()
                while True:
                    self._expect_group(response, b"\xC6\x13")
                    status = response[2]
                    if status in (STATUS_FILE_ERROR, STATUS_FS_ERROR):
                        raise self._remote_error(response, f"list directory {path}")
                    if status not in (STATUS_MORE, STATUS_LAST):
                        raise ProtocolError(f"unexpected directory status 0x{status:02x}")
                    output.extend(response[3:])
                    if status == STATUS_LAST:
                        text = output.decode("cp932", errors="replace")
                        return [entry for entry in text.split(",") if entry]
                    response = self.request(b"\xC6\x13\x20")
            except TransportError as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                print("Directory transfer interrupted; restarting it...", file=sys.stderr)
                self.reconnect()
        assert last_error is not None
        raise last_error

    def list_file(self, remote_path: str) -> dict:
        path = normalize_remote_path(remote_path)
        response = self._expect_group(
            self._query_with_reconnect(
                b"\xC6\x13\x32" + path.encode("ascii"), "file information"
            ),
            b"\xC6\x13",
        )
        if response[2] == STATUS_FS_ERROR:
            raise self._remote_error(response, f"stat {path}")
        if len(response) < 10:
            raise ProtocolError(f"short file information response: {response.hex(' ')}")

        attr = response[2]
        names = []
        for bit, name in ((0x01, "archive"), (0x02, "hidden"),
                          (0x04, "readonly"), (0x08, "system")):
            if attr & bit:
                names.append(name)
        result = {
            "path": path,
            "size": int.from_bytes(response[3:7], "big"),
            "attributes_raw": attr,
            "attributes": names,
            "year": int.from_bytes(response[7:9], "big"),
            "timestamp_raw": response[7:].hex(),
        }
        return result

    def set_readonly(self, remote_path: str, enabled: bool) -> None:
        path = normalize_remote_path(remote_path)
        flag = b"\x01" if enabled else b"\x00"
        response = self._expect_group(
            self._query_with_reconnect(
                b"\xC6\x13\x33" + flag + path.encode("ascii"), "set readonly"
            ),
            b"\xC6\x13",
        )
        if response[2] != STATUS_OK:
            raise self._remote_error(response, f"set readonly on {path}")

    def mkdir(self, remote_path: str) -> None:
        path = normalize_remote_path(remote_path)
        payload = b"\xC6\x14\x83" + b"\x00" * 5 + path.encode("ascii")
        response = self._expect_group(
            self._query_with_reconnect(payload, "mkdir"), b"\xC6\x14"
        )
        if response[2] != STATUS_OK:
            raise self._remote_error(response, f"mkdir {path}")

    def delete_file(self, remote_path: str) -> None:
        path = normalize_remote_path(remote_path)
        payload = b"\xC6\x14\x81" + b"\x00" * 5 + path.encode("ascii")
        response = self._expect_group(
            self._query_with_reconnect(payload, "delete file"), b"\xC6\x14"
        )
        if response[2] != STATUS_OK:
            raise self._remote_error(response, f"delete {path}")

    @staticmethod
    def _validate_download_chunk(response: bytes, operation: str) -> tuple[int, bytes]:
        MakerCmdClient._expect_group(response, b"\xC6\x13")
        status = response[2]
        if status == STATUS_FILE_ERROR:
            raise MakerCmdClient._remote_error(response, operation)
        if status not in (STATUS_MORE, STATUS_LAST):
            raise ProtocolError(f"unexpected download status 0x{status:02x}")
        if len(response) < 4:
            raise ProtocolError("download response has no checksum")
        data = response[4:]
        expected = response[3]
        actual = sum(data) & 0xFF
        if expected != actual:
            raise ChecksumMismatch(
                status,
                f"download checksum mismatch: expected 0x{expected:02x}, got 0x{actual:02x}"
            )
        return status, data

    @staticmethod
    def _merge_resumed_chunk(handle: BinaryIO, data: bytes, verified: int,
                             old_size: int) -> int:
        overlap = max(0, min(len(data), old_size - verified))
        if overlap:
            handle.seek(verified)
            old = handle.read(overlap)
            if old != data[:overlap]:
                handle.seek(verified)
                handle.truncate()
                old_size = verified
                overlap = 0
        if overlap < len(data):
            handle.seek(verified + overlap)
            handle.write(data[overlap:])
        return verified + len(data)

    def pull(self, remote_path: str, local_path: Path, resume: bool,
             overwrite: bool) -> int:
        remote = normalize_remote_path(remote_path)
        local_path = local_path.expanduser()
        part_path = local_path.with_name(local_path.name + ".part")
        meta_path = local_path.with_name(local_path.name + ".part.json")
        if local_path.exists() and not overwrite:
            raise FSAccessError(
                f"local file already exists: {local_path}; pass --overwrite to replace it"
            )
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if not resume:
            for stale in (part_path, meta_path):
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass
        elif meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            if meta.get("remote") != remote:
                raise FSAccessError(
                    f"partial file belongs to {meta.get('remote')!r}, not {remote!r}: {part_path}"
                )

        meta_path.write_text(json.dumps({"remote": remote}), encoding="utf-8")
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            old_size = part_path.stat().st_size if resume and part_path.exists() else 0
            mode = "r+b" if part_path.exists() else "w+b"
            verified = 0
            try:
                with part_path.open(mode) as handle:
                    response = self.request(
                        b"\xC6\x13\x00" + remote.encode("ascii")
                    )
                    while True:
                        checksum_retries = 0
                        while True:
                            try:
                                status, data = self._validate_download_chunk(
                                    response, f"pull {remote}"
                                )
                                break
                            except ChecksumMismatch as exc:
                                if exc.status == STATUS_LAST:
                                    raise TransferRestart(str(exc)) from exc
                                if checksum_retries >= self.retries:
                                    raise TransferRestart(str(exc)) from exc
                                checksum_retries += 1
                                response = self.request(b"\xC6\x13\x21")
                        verified = self._merge_resumed_chunk(
                            handle, data, verified, old_size
                        )
                        print(f"\rReceived {format_size(verified)}", end="", file=sys.stderr)
                        if status == STATUS_LAST:
                            handle.truncate(verified)
                            handle.flush()
                            os.fsync(handle.fileno())
                            break
                        response = self.request(b"\xC6\x13\x20")
                print(file=sys.stderr)
                os.replace(part_path, local_path)
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
                return verified
            except (TransportError, TransferRestart) as exc:
                print(file=sys.stderr)
                last_error = exc
                if attempt >= self.retries:
                    break
                if isinstance(exc, TransportError):
                    print(
                        f"Download interrupted; reconnecting and validating the partial file "
                        f"({attempt + 1}/{self.retries})...",
                        file=sys.stderr,
                    )
                    self.reconnect()
                else:
                    print(
                        f"Final block was corrupt; restarting and validating the partial file "
                        f"({attempt + 1}/{self.retries})...",
                        file=sys.stderr,
                    )
        assert last_error is not None
        raise last_error

    def _begin_push(self, remote: str, size: int) -> None:
        payload = (
            b"\xC6\x14\x80\x00"
            + size.to_bytes(4, "big")
            + remote.encode("ascii")
        )
        response = self._expect_group(self.request(payload), b"\xC6\x14")
        if response[2] == STATUS_NO_SPACE:
            raise RemoteError("push failed: insufficient free space on D:", STATUS_NO_SPACE)
        if response[2] != STATUS_UPLOAD_MORE:
            raise self._remote_error(response, f"open {remote} for upload")

    def _push_chunk(self, data: bytes, final: bool) -> None:
        opcode = 0x91 if final else 0x90
        payload = b"\xC6\x14" + bytes((opcode, sum(data) & 0xFF)) + data
        for attempt in range(self.retries + 1):
            response = self._expect_group(self.request(payload), b"\xC6\x14")
            expected = STATUS_OK if final else STATUS_UPLOAD_MORE
            if response[2] == expected:
                return
            if response[2] == STATUS_CHECKSUM_ERROR and attempt < self.retries:
                self.log(f"upload checksum rejected; retrying block {attempt + 1}")
                continue
            raise self._remote_error(response, "upload block")

    def push(self, local_path: Path, remote_path: str) -> int:
        local_path = local_path.expanduser()
        if not local_path.is_file():
            raise FSAccessError(f"local file does not exist: {local_path}")
        size = local_path.stat().st_size
        if size > 0xFFFFFFFF:
            raise FSAccessError("MakerCmd upload size is limited to 4 GiB - 1 byte")
        remote = normalize_remote_path(remote_path)

        try:
            existing = self.list_file(remote)
        except RemoteError as exc:
            if exc.os_error != -1:
                raise
        else:
            raise RemoteError(
                f"push refused: remote file already exists ({existing['size']} bytes): {remote}",
                STATUS_FS_ERROR,
                -11,
            )

        last_error: Optional[Exception] = None
        for restart in range(self.retries + 1):
            sent = 0
            try:
                self._begin_push(remote, size)
                with local_path.open("rb") as handle:
                    if size == 0:
                        self._push_chunk(b"", final=True)
                    else:
                        while True:
                            data = handle.read(UPLOAD_CHUNK_SIZE)
                            if not data:
                                break
                            final = sent + len(data) == size
                            self._push_chunk(data, final=final)
                            sent += len(data)
                            print(
                                f"\rSent {format_size(sent)} / {format_size(size)}",
                                end="",
                                file=sys.stderr,
                            )
                print(file=sys.stderr)
                return sent
            except TransportError as exc:
                print(file=sys.stderr)
                last_error = exc
                print(
                    "Upload response was lost; reconnecting to inspect the remote file...",
                    file=sys.stderr,
                )
                try:
                    self.reconnect()
                    info = self.list_file(remote)
                except RemoteError as inspect_error:
                    if inspect_error.os_error != -1:
                        raise FSAccessError(
                            f"upload failed ({exc}); cannot inspect partial file: "
                            f"{inspect_error}"
                        ) from exc
                    info = None
                except FSAccessError as inspect_error:
                    raise FSAccessError(
                        f"upload failed ({exc}); reconnect/inspection failed: {inspect_error}"
                    ) from exc

                if info is not None and info["size"] == size:
                    print(
                        "Remote size equals the local size; treating the lost final "
                        "acknowledgement as success.",
                        file=sys.stderr,
                    )
                    return size
                if info is not None:
                    if info["size"] > size:
                        raise FSAccessError(
                            f"upload failed and remote size {info['size']} exceeds local "
                            f"size {size}; refusing automatic deletion"
                        ) from exc
                    print(
                        f"Removing partial remote file ({info['size']} bytes)...",
                        file=sys.stderr,
                    )
                    self.delete_file(remote)

                if restart >= self.retries:
                    break
                print(
                    f"Restarting upload from byte 0 ({restart + 1}/{self.retries})...",
                    file=sys.stderr,
                )
        assert last_error is not None
        raise TransportError(
            f"upload failed after {self.retries + 1} attempt(s); any confirmed partial "
            f"file was removed: {last_error}"
        ) from last_error


def parse_readonly(values: Sequence[str]) -> tuple[str, bool]:
    if not 1 <= len(values) <= 2:
        raise FSAccessError("--setreadonly expects PATH and optional on/off")
    enabled = True
    if len(values) == 2:
        state = values[1].lower()
        if state in ("1", "on", "true", "yes", "set"):
            enabled = True
        elif state in ("0", "off", "false", "no", "clear"):
            enabled = False
        else:
            raise FSAccessError(f"invalid readonly state: {values[1]!r}")
    return values[0], enabled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MakerCmd file tool",
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--connect", action="store_true", help="connect")
    actions.add_argument("--push", nargs=2, metavar=("LOCAL", "REMOTE"), help="upload file")
    actions.add_argument("--pull", nargs=2, metavar=("REMOTE", "LOCAL"), help="download file")
    actions.add_argument("--mkdir", metavar="REMOTE_DIR", help="create directory")
    actions.add_argument("--delete", metavar="REMOTE_FILE", help="delete file")
    actions.add_argument("--checkdavailable", action="store_true", help="show D: free space")
    actions.add_argument("--listdir", metavar="REMOTE_DIR", help="list directory")
    actions.add_argument("--listfile", metavar="REMOTE_FILE", help="show file info")
    actions.add_argument("--listpartition", action="store_true", help="list drives")
    actions.add_argument(
        "--setreadonly", nargs="+", metavar=("REMOTE_FILE", "STATE"),
        help="set readonly",
    )
    parser.add_argument("--vid", type=parse_int, default=DEFAULT_VID, help="USB VID")
    parser.add_argument("--pid", type=parse_int, default=DEFAULT_PID, help="USB PID")
    parser.add_argument("--timeout", type=float, default=5.0, help="USB timeout")
    parser.add_argument("--maker-wait", type=float, default=60.0, help="connection timeout")
    parser.add_argument("--retries", type=int, default=3, help="retry count")
    parser.add_argument("--no-resume", action="store_true", help="disable resume")
    parser.add_argument("--overwrite", action="store_true", help="overwrite local file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output")
    return parser


def run(args: argparse.Namespace) -> int:
    client = MakerCmdClient(
        args.vid, args.pid, args.timeout, args.retries, args.maker_wait, args.verbose
    )
    try:
        client.connect()
        if args.connect:
            print(f"Connected to MakerCmd at {args.vid:04x}:{args.pid:04x}")
        elif args.push:
            count = client.push(Path(args.push[0]), args.push[1])
            print(f"Uploaded {count} bytes to {normalize_remote_path(args.push[1])}")
        elif args.pull:
            count = client.pull(
                args.pull[0], Path(args.pull[1]), not args.no_resume, args.overwrite
            )
            print(f"Downloaded {count} bytes to {Path(args.pull[1]).expanduser()}")
        elif args.mkdir:
            client.mkdir(args.mkdir)
            print(f"Created {normalize_remote_path(args.mkdir)}")
        elif args.delete:
            client.delete_file(args.delete)
            print(f"Deleted {normalize_remote_path(args.delete)}")
        elif args.checkdavailable:
            available = client.check_d_available()
            print(f"D: available: {available} bytes ({format_size(available)})")
        elif args.listdir:
            for entry in client.list_directory(args.listdir):
                print(entry)
        elif args.listfile:
            info = client.list_file(args.listfile)
            print(json.dumps(info, ensure_ascii=False, indent=2))
        elif args.listpartition:
            for partition in client.list_partitions():
                print(partition)
        elif args.setreadonly:
            path, enabled = parse_readonly(args.setreadonly)
            client.set_readonly(path, enabled)
            print(f"Readonly {'enabled' if enabled else 'disabled'}: {normalize_remote_path(path)}")
        return 0
    finally:
        client.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.retries < 0:
        parser.error("--retries must be non-negative")
    if args.timeout <= 0 or args.maker_wait <= 0:
        parser.error("--timeout and --maker-wait must be positive")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except FSAccessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        detail = exc.strerror or str(exc)
        if exc.errno == errno.ENOSPC:
            detail = "local disk is full"
        print(f"error: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
