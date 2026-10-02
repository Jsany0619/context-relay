"""Read-only private-network diagnostics. Never log identities, keys, or login URLs."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess


def summarize_status(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid network status")
    addresses = []
    for value_ip in value.get("TailscaleIPs", []):
        try:
            address = ipaddress.ip_address(value_ip)
            if address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"):
                addresses.append(str(address))
        except (ValueError, TypeError):
            continue
    connected = value.get("BackendState") == "Running" and bool(addresses)
    peers = value.get("Peer") or {}
    count = sum(peer.get("Online") is True for peer in peers.values() if isinstance(peer, dict)) if isinstance(peers, dict) else 0
    return {"kind": "network_diagnostics", "state": "connected" if connected else "not_connected",
            "local_ipv4": addresses if connected else [], "online_peers": count,
            "detail": ("电脑私有网络已连接。请使用下面的地址开启手机连接，再配对。手机也须登录同一私有网络；在线设备数不证明手机已能访问此端口。"
                       if connected else "Tailscale 尚未连接。请在电脑和手机登录自己的同一账号，并在手机确认 VPN 连接。")}


def diagnostics():
    executable = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale/tailscale.exe"
    if not executable.is_file():
        return {"kind": "network_diagnostics", "state": "not_installed", "local_ipv4": [], "online_peers": 0,
                "detail": "电脑尚未安装 Tailscale。同一 Wi-Fi 可直接使用；异地请先从 tailscale.com/download 安装两端客户端、登录同一账号。无需公网端口映射。"}
    try:
        result = subprocess.run([str(executable), "status", "--json"], capture_output=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError("Status unavailable")
        return summarize_status(json.loads(result.stdout.decode("utf-8")))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"kind": "network_diagnostics", "state": "unknown", "local_ipv4": [], "online_peers": 0,
                "detail": "暂时无法核实私有网络状态。请打开 Tailscale 检查登录与网络；未修改任何网络配置。"}


if __name__ == "__main__":
    print(json.dumps(diagnostics(), ensure_ascii=False, indent=2))
