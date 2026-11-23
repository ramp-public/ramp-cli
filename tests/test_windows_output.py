import io
import sys

from ramp_cli import main as main_module


def _stream(encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding)


def test_windows_output_is_switched_to_utf8(monkeypatch):
    stdout, stderr = _stream("cp1252"), _stream("cp1252")
    monkeypatch.setattr(main_module.sys, "platform", "win32")
    monkeypatch.setattr(main_module.sys, "stdout", stdout)
    monkeypatch.setattr(main_module.sys, "stderr", stderr)

    main_module._use_utf8_output()

    assert stdout.encoding == "utf-8"
    assert stderr.encoding == "utf-8"
    stdout.write("Routed → ✔ ─")
    stdout.flush()


def test_other_platforms_keep_their_output_encoding(monkeypatch):
    stdout = _stream("latin-1")
    monkeypatch.setattr(main_module.sys, "platform", "darwin")
    monkeypatch.setattr(main_module.sys, "stdout", stdout)

    main_module._use_utf8_output()

    assert stdout.encoding == "latin-1"


def test_usage_names_the_command_ramp_whatever_binary_ran(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["C:/ramp/ramp-windows-amd64.exe", "router", "configure", "--help"]
    )
    monkeypatch.setattr(main_module, "record_installed_version", lambda: None)
    monkeypatch.setattr(main_module, "check_for_update", lambda: None)
    monkeypatch.setattr(main_module, "emit_update_notice", lambda agent: None)

    try:
        main_module.main()
    except SystemExit:
        pass

    output = capsys.readouterr().out
    assert "ramp router configure" in output
    assert "ramp-windows-amd64" not in output
