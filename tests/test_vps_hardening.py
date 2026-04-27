"""VPS hardening config generator tests — P8_COMPLY security."""

from __future__ import annotations

from eta_engine.obs.vps_hardening import (
    HardeningConfig,
    UFWRule,
    build_config,
    build_fail2ban_config,
    build_runbook,
    build_sshd_config,
    build_systemd_unit,
    ufw_commands,
)

# ---------------------------------------------------------------------------
# SSHD
# ---------------------------------------------------------------------------


def test_sshd_config_locks_down_root_and_passwords() -> None:
    cfg = build_sshd_config()
    assert "PermitRootLogin no" in cfg
    assert "PasswordAuthentication no" in cfg
    assert "PermitEmptyPasswords no" in cfg
    assert "X11Forwarding no" in cfg
    assert "Protocol 2" in cfg


def test_sshd_config_respects_custom_port_and_users() -> None:
    cfg = build_sshd_config(ssh_port=2222, allow_users=["alice", "bob"])
    assert "Port 2222" in cfg
    assert "AllowUsers alice bob" in cfg


def test_sshd_config_defaults_to_apex_user() -> None:
    cfg = build_sshd_config()
    assert "AllowUsers apex" in cfg


# ---------------------------------------------------------------------------
# Fail2ban
# ---------------------------------------------------------------------------


def test_fail2ban_config_sets_strict_ssh_jail() -> None:
    cfg = build_fail2ban_config()
    assert "[sshd]" in cfg
    assert "enabled = true" in cfg
    assert "maxretry = 4" in cfg
    assert "bantime = 3600" in cfg


# ---------------------------------------------------------------------------
# Systemd
# ---------------------------------------------------------------------------


def test_systemd_unit_has_sandbox_flags() -> None:
    unit = build_systemd_unit()
    for flag in [
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "RestrictNamespaces=true",
        "MemoryDenyWriteExecute=true",
    ]:
        assert flag in unit


def test_systemd_unit_respects_custom_user_and_paths() -> None:
    unit = build_systemd_unit(bot_user="sniper", work_dir="/opt/sniper")
    assert "User=sniper" in unit
    assert "Group=sniper" in unit
    assert "WorkingDirectory=/opt/sniper" in unit
    assert "ReadWritePaths=/opt/sniper/data /opt/sniper/logs" in unit


def test_systemd_unit_has_restart_policy() -> None:
    unit = build_systemd_unit()
    assert "Restart=on-failure" in unit
    assert "RestartSec=5s" in unit
    assert "StartLimitBurst=3" in unit


# ---------------------------------------------------------------------------
# UFW rules + CLI rendering
# ---------------------------------------------------------------------------


def test_build_config_returns_bundle_with_all_artifacts() -> None:
    cfg = build_config(operator_ip="1.2.3.4/32")
    assert isinstance(cfg, HardeningConfig)
    assert len(cfg.ufw_rules) >= 5
    assert cfg.sshd_config
    assert cfg.fail2ban_config
    assert cfg.systemd_unit
    assert cfg.runbook


def test_ufw_defaults_allow_ssh_from_operator_only() -> None:
    cfg = build_config(operator_ip="1.2.3.4/32")
    ssh_rule = next(r for r in cfg.ufw_rules if r.port == 22 and r.scope == "ingress")
    assert ssh_rule.action == "allow"
    assert ssh_rule.source == "1.2.3.4/32"


def test_ufw_defaults_allow_prometheus_localhost_only() -> None:
    cfg = build_config(prometheus_port=9115)
    prom_rule = next(r for r in cfg.ufw_rules if r.port == 9115)
    assert prom_rule.source == "127.0.0.1"
    assert prom_rule.scope == "ingress"


def test_ufw_defaults_egress_https_ntp_dns() -> None:
    cfg = build_config()
    egress_ports = {r.port for r in cfg.ufw_rules if r.scope == "egress"}
    assert 443 in egress_ports
    assert 123 in egress_ports  # NTP
    assert 53 in egress_ports  # DNS


def test_ufw_commands_render_allow_in_with_source() -> None:
    rules = [
        UFWRule(
            scope="ingress",
            action="allow",
            port=22,
            protocol="tcp",
            source="1.2.3.4/32",
            comment="operator ssh",
        ),
    ]
    cmds = ufw_commands(rules)
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd.startswith("ufw allow in")
    assert "from 1.2.3.4/32" in cmd
    assert "port 22/tcp" in cmd
    assert "comment 'operator ssh'" in cmd


def test_ufw_commands_render_egress_without_source() -> None:
    rules = [
        UFWRule(scope="egress", action="allow", port=443, protocol="tcp", comment="https"),
    ]
    cmds = ufw_commands(rules)
    assert cmds[0].startswith("ufw allow out")
    assert "from" not in cmds[0]
    assert "port 443/tcp" in cmds[0]


def test_ufw_commands_handle_any_protocol() -> None:
    rules = [
        UFWRule(scope="egress", action="allow", port=53, protocol="any", comment="dns"),
    ]
    cmds = ufw_commands(rules)
    # "any" protocol → emit port spec without the "/proto" suffix
    assert "port 53" in cmds[0]
    assert "/any" not in cmds[0]


# ---------------------------------------------------------------------------
# Runbook
# ---------------------------------------------------------------------------


def test_runbook_includes_all_sections() -> None:
    book = build_runbook()
    for heading in [
        "## 1. User setup",
        "## 2. Firewall",
        "## 3. SSH",
        "## 4. Fail2ban",
        "## 5. Systemd service",
        "## 6. Kernel + package hardening",
        "## 7. Verification",
    ]:
        assert heading in book


def test_runbook_surfaces_operator_ip_when_supplied() -> None:
    book = build_runbook(operator_ip="1.2.3.4")
    assert "1.2.3.4" in book


def test_runbook_omits_operator_ip_when_absent() -> None:
    book = build_runbook(operator_ip=None)
    assert "Operator IP on file" not in book
