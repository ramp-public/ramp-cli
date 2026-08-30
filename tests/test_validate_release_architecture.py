"""Tests for release binary header validation."""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-release-architecture.py"


def _binary_header(binary_format: str, arch: str) -> bytes:
    if binary_format == "darwin":
        cpu_type = {"amd64": 0x01000007, "arm64": 0x0100000C}[arch]
        return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu_type) + bytes(64)
    if binary_format == "linux":
        machine = {"amd64": 0x003E, "arm64": 0x00B7}[arch]
        return b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", machine) + bytes(64)
    if binary_format == "windows":
        machine = {"amd64": 0x8664, "arm64": 0xAA64}[arch]
        return (
            b"MZ"
            + bytes(58)
            + struct.pack("<I", 64)
            + b"PE\0\0"
            + struct.pack("<H", machine)
            + bytes(64)
        )
    raise AssertionError(f"unsupported test format: {binary_format}")


def _run_validator(directory: Path, binary_format: str, arch: str):
    suffix = ".exe" if binary_format == "windows" else ""
    executable_name = f"ramp-{binary_format}-{arch}{suffix}"
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--directory",
            str(directory),
            "--executable",
            executable_name,
            "--os",
            binary_format,
            "--arch",
            arch,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("binary_format", "arch"),
    [
        pytest.param("darwin", "arm64", id="macho-arm64"),
        pytest.param("darwin", "amd64", id="macho-amd64"),
        pytest.param("linux", "arm64", id="elf-arm64"),
        pytest.param("linux", "amd64", id="elf-amd64"),
        pytest.param("windows", "amd64", id="pe-amd64"),
    ],
)
def test_validator_accepts_matching_executable_and_library(
    tmp_path: Path, binary_format: str, arch: str
):
    suffix = ".exe" if binary_format == "windows" else ""
    (tmp_path / f"ramp-{binary_format}-{arch}{suffix}").write_bytes(
        _binary_header(binary_format, arch)
    )
    (tmp_path / "native-library.bin").write_bytes(_binary_header(binary_format, arch))
    (tmp_path / "data.json").write_text("{}")

    result = _run_validator(tmp_path, binary_format, arch)

    assert result.returncode == 0, result.stderr
    assert "Validated 2 native files" in result.stdout


def test_validator_rejects_mislabeled_main_executable(tmp_path: Path):
    (tmp_path / "ramp-darwin-amd64").write_bytes(_binary_header("darwin", "arm64"))

    result = _run_validator(tmp_path, "darwin", "amd64")

    assert result.returncode == 1
    assert "ramp-darwin-amd64: expected amd64, found arm64" in result.stderr


def test_validator_rejects_mismatched_bundled_library(tmp_path: Path):
    (tmp_path / "ramp-linux-amd64").write_bytes(_binary_header("linux", "amd64"))
    (tmp_path / "bad-library.so").write_bytes(_binary_header("linux", "arm64"))

    result = _run_validator(tmp_path, "linux", "amd64")

    assert result.returncode == 1
    assert "bad-library.so: expected amd64, found arm64" in result.stderr
