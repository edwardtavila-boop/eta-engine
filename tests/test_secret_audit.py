from __future__ import annotations

from scripts import _secret_audit


def test_secret_audit_detects_common_token_shapes(tmp_path) -> None:
    token = "sk-" + "A" * 32
    path = tmp_path / "sample.txt"
    path.write_text(f"token = {token}\n", encoding="utf-8")

    findings = _secret_audit._scan_file(path)

    assert findings == [(1, "OpenAI/Anthropic key", "sk-AAA...AAAA")]


def test_secret_audit_allows_explicit_false_positive_marker(tmp_path) -> None:
    token = "ghp_" + "B" * 36
    path = tmp_path / "sample.txt"
    path.write_text(f"example = {token}  # noqa: secret\n", encoding="utf-8")

    assert _secret_audit._scan_file(path) == []


def test_secret_audit_skips_binary_files(tmp_path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"\x00" + b"sk-" + b"C" * 32)

    assert _secret_audit._is_binary(path)
    assert _secret_audit._should_skip_file(path)


def test_secret_audit_explicit_file_paths_respect_skip_extensions(tmp_path) -> None:
    path = tmp_path / "sample.png"
    path.write_text("sk-" + "D" * 32, encoding="utf-8")

    assert _secret_audit.main([str(path)]) == 0
