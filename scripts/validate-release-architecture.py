#!/usr/bin/env python3
"""Validate native files in a release directory from their binary headers."""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BinaryInfo:
    format: str
    architectures: frozenset[str]


MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce": (">", False, False),
    b"\xce\xfa\xed\xfe": ("<", False, False),
    b"\xfe\xed\xfa\xcf": (">", False, False),
    b"\xcf\xfa\xed\xfe": ("<", False, False),
    b"\xca\xfe\xba\xbe": (">", True, False),
    b"\xbe\xba\xfe\xca": ("<", True, False),
    b"\xca\xfe\xba\xbf": (">", True, True),
    b"\xbf\xba\xfe\xca": ("<", True, True),
}
MACHO_ARCHITECTURES = {
    0x01000007: "amd64",
    0x0100000C: "arm64",
}
ELF_ARCHITECTURES = {
    0x003E: "amd64",
    0x00B7: "arm64",
}
PE_ARCHITECTURES = {
    0x8664: "amd64",
    0xAA64: "arm64",
}
EXPECTED_FORMAT = {
    "darwin": "Mach-O",
    "linux": "ELF",
    "windows": "PE",
}


def _architecture(mapping: dict[int, str], value: int) -> str:
    return mapping.get(value, f"unknown(0x{value:x})")


def inspect_binary(path: Path) -> BinaryInfo | None:
    with path.open("rb") as binary:
        header = binary.read(8)
        if len(header) < 4:
            return None

        magic = header[:4]
        if magic == b"\x7fELF":
            if len(header) < 6 or header[5] not in (1, 2):
                raise ValueError("invalid ELF byte order")
            binary.seek(18)
            machine = binary.read(2)
            if len(machine) != 2:
                raise ValueError("truncated ELF header")
            byte_order = "<" if header[5] == 1 else ">"
            architecture = _architecture(
                ELF_ARCHITECTURES, struct.unpack(f"{byte_order}H", machine)[0]
            )
            return BinaryInfo("ELF", frozenset({architecture}))

        if magic in MACHO_MAGICS:
            byte_order, is_fat, is_64_bit_fat = MACHO_MAGICS[magic]
            if not is_fat:
                architecture = _architecture(
                    MACHO_ARCHITECTURES, struct.unpack(f"{byte_order}I", header[4:8])[0]
                )
                return BinaryInfo("Mach-O", frozenset({architecture}))

            architecture_count = struct.unpack(f"{byte_order}I", header[4:8])[0]
            entry_size = 32 if is_64_bit_fat else 20
            architecture_data = binary.read(architecture_count * entry_size)
            if len(architecture_data) != architecture_count * entry_size:
                raise ValueError("truncated universal Mach-O header")
            architectures = {
                _architecture(
                    MACHO_ARCHITECTURES,
                    struct.unpack_from(f"{byte_order}I", architecture_data, offset)[0],
                )
                for offset in range(0, len(architecture_data), entry_size)
            }
            return BinaryInfo("Mach-O", frozenset(architectures))

        if header[:2] == b"MZ":
            binary.seek(0x3C)
            pe_offset_data = binary.read(4)
            if len(pe_offset_data) != 4:
                raise ValueError("truncated DOS header")
            pe_offset = struct.unpack("<I", pe_offset_data)[0]
            binary.seek(pe_offset)
            pe_header = binary.read(6)
            if len(pe_header) != 6 or pe_header[:4] != b"PE\0\0":
                raise ValueError("invalid PE header")
            architecture = _architecture(
                PE_ARCHITECTURES, struct.unpack("<H", pe_header[4:6])[0]
            )
            return BinaryInfo("PE", frozenset({architecture}))

    return None


def validate_release_directory(
    directory: Path, executable_name: str, expected_os: str, expected_arch: str
) -> int:
    expected_format = EXPECTED_FORMAT[expected_os]
    executable = directory / executable_name
    if not executable.is_file():
        raise ValueError(f"release executable is missing: {executable}")

    native_files: list[tuple[Path, BinaryInfo]] = []
    errors: list[str] = []
    for path in sorted(
        candidate for candidate in directory.rglob("*") if candidate.is_file()
    ):
        try:
            info = inspect_binary(path)
        except (OSError, struct.error, ValueError) as error:
            errors.append(f"{path.relative_to(directory)}: malformed binary: {error}")
            continue
        if info is None:
            continue
        native_files.append((path, info))
        relative_path = path.relative_to(directory)
        if info.format != expected_format:
            errors.append(
                f"{relative_path}: expected {expected_format}, found {info.format}"
            )
        if expected_arch not in info.architectures:
            actual = ", ".join(sorted(info.architectures))
            errors.append(f"{relative_path}: expected {expected_arch}, found {actual}")

    executable_info = next(
        (info for path, info in native_files if path == executable), None
    )
    if executable_info is None:
        errors.append(f"{executable_name}: executable has no recognized binary header")

    if errors:
        raise ValueError(
            "release architecture validation failed:\n  " + "\n  ".join(errors)
        )

    print(
        f"Validated {len(native_files)} native files as "
        f"{expected_os}/{expected_arch} ({expected_format})"
    )
    return len(native_files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--os", choices=sorted(EXPECTED_FORMAT), required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()

    try:
        validate_release_directory(args.directory, args.executable, args.os, args.arch)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
