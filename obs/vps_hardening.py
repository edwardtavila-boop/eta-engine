"""VPS hardening config generator — P8_COMPLY security.

Emits copy-paste-ready config blobs for a Linux VPS that will run the bot:

* **UFW firewall** rules (ingress + egress whitelist)
* **SSHD config** (keys only, no root, PAM off, no X11, port knock)
* **Fail2ban** jail snippet (SSH)
* **Systemd** service unit for the bot + hardening flags (NoNewPrivileges,
  PrivateTmp, ProtectSystem=strict, etc.)
* **Runbook** checklist (as a string) the operator walks through after
  provisioning a fresh VPS.

No shell execution — this module produces *text*. The operator SCPs the
files up, reviews, and applies. Safer than remote-applying hardening
from the bot's event loop.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Scope = Literal["ingress", "egress"]


class UFWRule(BaseModel):
    """One UFW allow/deny rule."""

    scope: Scope
    action: Literal["allow", "deny"]
    port: int
    protocol: Literal["tcp", "udp", "any"] = "tcp"
    source: str | None = None  # e.g. "1.2.3.4/32" — None = anywhere
    comment: str = ""


class HardeningConfig(BaseModel):
    """Pydantic bundle of the generated artifacts — serializable for docs/."""

    ufw_rules: list[UFWRule] = Field(default_factory=list)
    sshd_config: str = ""
    fail2ban_config: str = ""
    systemd_unit: str = ""
    runbook: str = ""


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def _default_ufw_rules(
    *,
    ssh_port: int = 22,
    prometheus_port: int = 9115,
    operator_ip: str | None = None,
) -> list[UFWRule]:
    """Conservative defaults: everything denied except SSH + metrics (+ egress to known APIs)."""
    rules: list[UFWRule] = [
        # Ingress: SSH from operator only
        UFWRule(
            scope="ingress",
            action="allow",
            port=ssh_port,
            protocol="tcp",
            source=operator_ip,
            comment="ssh from operator ip only",
        ),
        # Ingress: Prometheus scrape, localhost only (reverse proxy terminates external)
        UFWRule(
            scope="ingress",
            action="allow",
            port=prometheus_port,
            protocol="tcp",
            source="127.0.0.1",
            comment="prometheus scrape — localhost only",
        ),
        # Egress: HTTPS (exchange APIs, DefiLlama, Tradovate, etc.)
        UFWRule(scope="egress", action="allow", port=443, protocol="tcp", comment="https egress"),
        # Egress: NTP
        UFWRule(scope="egress", action="allow", port=123, protocol="udp", comment="ntp time sync"),
        # Egress: DNS
        UFWRule(scope="egress", action="allow", port=53, protocol="any", comment="dns"),
    ]
    return rules


def build_sshd_config(*, ssh_port: int = 22, allow_users: list[str] | None = None) -> str:
    """Harden sshd: keys only, no root, no password, no X11."""
    users = " ".join(allow_users or ["apex"])
    return (
        "\n".join(
            [
                "# /etc/ssh/sshd_config.d/10-apex-hardening.conf",
                f"Port {ssh_port}",
                "PermitRootLogin no",
                "PasswordAuthentication no",
                "PermitEmptyPasswords no",
                "ChallengeResponseAuthentication no",
                "KbdInteractiveAuthentication no",
                "UsePAM yes",
                "X11Forwarding no",
                "AllowTcpForwarding no",
                "ClientAliveInterval 300",
                "ClientAliveCountMax 2",
                "MaxAuthTries 3",
                "MaxSessions 2",
                f"AllowUsers {users}",
                "Protocol 2",
                "Compression no",
                "LogLevel VERBOSE",
            ]
        )
        + "\n"
    )


def build_fail2ban_config() -> str:
    """Minimal SSH jail — 4 failures → 1h ban."""
    return (
        "\n".join(
            [
                "[sshd]",
                "enabled = true",
                "port = 22",
                "filter = sshd",
                "logpath = /var/log/auth.log",
                "maxretry = 4",
                "findtime = 600",
                "bantime = 3600",
            ]
        )
        + "\n"
    )


def build_systemd_unit(
    *,
    bot_user: str = "apex",
    work_dir: str = "/home/apex/eta_engine",
    entrypoint: str = "python -m eta_engine.main",
) -> str:
    """Hardened systemd unit: sandbox + restart policy."""
    return (
        "\n".join(
            [
                "[Unit]",
                "Description=EVOLUTIONARY TRADING ALGO trading bot",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"User={bot_user}",
                f"Group={bot_user}",
                f"WorkingDirectory={work_dir}",
                f"ExecStart={entrypoint}",
                "Restart=on-failure",
                "RestartSec=5s",
                "StartLimitBurst=3",
                "StartLimitIntervalSec=60s",
                # sandboxing
                "NoNewPrivileges=true",
                "PrivateTmp=true",
                "ProtectSystem=strict",
                "ProtectHome=read-only",
                "ProtectKernelTunables=true",
                "ProtectKernelModules=true",
                "ProtectControlGroups=true",
                "RestrictNamespaces=true",
                "RestrictRealtime=true",
                "LockPersonality=true",
                "MemoryDenyWriteExecute=true",
                "SystemCallArchitectures=native",
                f"ReadWritePaths={work_dir}/data {work_dir}/logs",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
            ]
        )
        + "\n"
    )


def build_runbook(*, operator_ip: str | None = None) -> str:
    """Operator checklist — run once per fresh VPS."""
    lines = [
        "# EVOLUTIONARY TRADING ALGO — VPS HARDENING RUNBOOK",
        "",
        "## 1. User setup",
        "- [ ] Create non-root 'apex' user: `useradd -m -s /bin/bash apex`",
        "- [ ] Add SSH key to /home/apex/.ssh/authorized_keys (0600 perms)",
        "- [ ] Add apex to sudoers (NOPASSWD only for systemctl restart apex)",
        "- [ ] Disable root password: `passwd -l root`",
        "",
        "## 2. Firewall",
        "- [ ] `ufw default deny incoming` / `ufw default deny outgoing`",
        "- [ ] Apply rules from generated ufw_rules list",
        "- [ ] `ufw enable` — verify `ufw status` matches plan",
        "",
        "## 3. SSH",
        "- [ ] Drop /etc/ssh/sshd_config.d/10-apex-hardening.conf from generator",
        "- [ ] Reload: `systemctl reload sshd`",
        "- [ ] Test SSH from another terminal BEFORE closing current session",
        "",
        "## 4. Fail2ban",
        "- [ ] `apt install fail2ban`",
        "- [ ] Drop jail.local from generator",
        "- [ ] `systemctl enable --now fail2ban` — verify `fail2ban-client status sshd`",
        "",
        "## 5. Systemd service",
        "- [ ] Drop apex.service from generator to /etc/systemd/system/",
        "- [ ] `systemctl daemon-reload`",
        "- [ ] `systemctl enable apex`",
        "- [ ] Do NOT start yet — first populate .env with secrets",
        "",
        "## 6. Kernel + package hardening",
        "- [ ] Enable unattended-upgrades for security patches only",
        "- [ ] `apt install libpam-google-authenticator` for SSH 2FA (optional)",
        "- [ ] `sysctl` tweaks: disable IPv6 if not in use, enable tcp_syncookies",
        "",
        "## 7. Verification",
        "- [ ] Run `ss -tlnp` — only SSH + Prometheus loopback should be listening",
        "- [ ] `lynis audit system` — target 85+ hardening score",
        "- [ ] `systemctl status apex` — logs clean, no errors",
    ]
    if operator_ip:
        lines.insert(4, f"- Operator IP on file: {operator_ip}")
    return "\n".join(lines) + "\n"


def build_config(
    *,
    ssh_port: int = 22,
    prometheus_port: int = 9115,
    operator_ip: str | None = None,
    bot_user: str = "apex",
    work_dir: str = "/home/apex/eta_engine",
) -> HardeningConfig:
    """Compose the full hardening bundle for a fresh VPS."""
    return HardeningConfig(
        ufw_rules=_default_ufw_rules(
            ssh_port=ssh_port,
            prometheus_port=prometheus_port,
            operator_ip=operator_ip,
        ),
        sshd_config=build_sshd_config(ssh_port=ssh_port, allow_users=[bot_user]),
        fail2ban_config=build_fail2ban_config(),
        systemd_unit=build_systemd_unit(bot_user=bot_user, work_dir=work_dir),
        runbook=build_runbook(operator_ip=operator_ip),
    )


def ufw_commands(rules: list[UFWRule]) -> list[str]:
    """Render a list of `ufw` CLI commands for the supplied rules."""
    cmds: list[str] = []
    for r in rules:
        verb = "allow" if r.action == "allow" else "deny"
        direction = "in" if r.scope == "ingress" else "out"
        src = f"from {r.source} " if r.source else ""
        proto = r.protocol if r.protocol != "any" else ""
        cmd_parts = ["ufw", verb, direction]
        if src:
            cmd_parts.append(src.strip())
        port_spec = f"port {r.port}"
        if proto:
            port_spec += f"/{proto}"
        cmd_parts.append(port_spec)
        if r.comment:
            cmd_parts.append(f"comment '{r.comment}'")
        cmds.append(" ".join(cmd_parts))
    return cmds
