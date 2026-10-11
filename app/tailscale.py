"""Thin wrapper around the `tailscale` CLI.

No tailscaled runs in this container -- NET_ADMIN and the actual VPN
interface live entirely in the `tailscale` sidecar service (see
docker-compose.tailscale.yml). This container only has the client binary,
talking to the sidecar's daemon over a unix socket shared via a volume
(TS_SOCKET, default /var/run/tailscale/tailscaled.sock). If that socket
isn't mounted -- the base compose without the tailscale overlay -- every
function here reports "not configured" and callers fall back to normal
network egress, per the project's requirement that tailscale is fully
optional.
"""
from __future__ import annotations

import asyncio
import json
import os

TS_SOCKET = os.environ.get("TS_SOCKET", "/var/run/tailscale/tailscaled.sock")
TS_BIN = os.environ.get("TS_BIN", "tailscale")
# The subnet the WebUI is actually accessed from (e.g. the home LAN). Set
# this so selecting an exit node doesn't take the WebUI down -- see
# _pin_lan_route() below.
LAN_CIDR = os.environ.get("LAN_CIDR", "")


class TailscaleError(Exception):
    pass


def available() -> bool:
    return os.path.exists(TS_SOCKET)


async def _run(*args: str, timeout: float = 15) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        TS_BIN, f"--socket={TS_SOCKET}", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TailscaleError(f"tailscale {' '.join(args)} timed out")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def status() -> dict:
    """{configured, logged_in, exit_node_id, exit_node_name, exit_nodes: [...]}."""
    empty = {"configured": False, "logged_in": False, "exit_node_id": None,
             "exit_node_name": None, "exit_nodes": []}
    if not available():
        return empty
    await _capture_default_gateway()  # learn the LAN gateway before an exit node can hide it
    try:
        rc, out, err = await _run("status", "--json")
    except TailscaleError as e:
        return {**empty, "configured": True, "error": str(e)}
    if rc != 0 or not out.strip():
        return {**empty, "configured": True, "error": err.strip() or "status failed"}
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return {**empty, "configured": True, "error": "could not parse tailscale status"}

    backend = d.get("BackendState", "")
    exit_node_id = None
    exit_node_name = None
    exit_nodes = []
    for peer_id, peer in (d.get("Peer") or {}).items():
        name = (peer.get("DNSName") or peer.get("HostName") or "").rstrip(".")
        if peer.get("ExitNodeOption"):
            exit_nodes.append({"id": peer_id, "name": name,
                                "hostname": peer.get("HostName", ""), "online": bool(peer.get("Online"))})
        if peer.get("ExitNode"):
            exit_node_id, exit_node_name = peer_id, name
    return {
        "configured": True, "logged_in": backend == "Running", "backend_state": backend,
        "exit_node_id": exit_node_id, "exit_node_name": exit_node_name, "exit_nodes": exit_nodes,
        "self_name": ((d.get("Self") or {}).get("DNSName") or "").rstrip("."),
    }


_LAN_GW: tuple[str, str] | None = None


async def _run_ip(*args: str, timeout: float = 10) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "ip", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", f"ip {' '.join(args)} timed out"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _capture_default_gateway() -> tuple[str, str] | None:
    """Record the non-tailscale default gateway/interface (the docker
    bridge) the first time it's seen, before any exit node has replaced it
    -- so a later exit-node selection can pin the LAN route back through
    it. Cached for the life of the process since this never changes."""
    global _LAN_GW
    if _LAN_GW is not None:
        return _LAN_GW
    rc, out, _ = await _run_ip("route", "show", "default")
    for line in out.splitlines():
        parts = line.split()
        if "tailscale0" in parts:
            continue  # an exit node is already active; nothing to learn
        if "via" in parts and "dev" in parts:
            gw = parts[parts.index("via") + 1]
            dev = parts[parts.index("dev") + 1]
            _LAN_GW = (gw, dev)
            return _LAN_GW
    return None


async def _pin_lan_route() -> None:
    """Selecting an exit node replaces the default route with one via
    tailscale0 -- which also swallows return traffic for inbound WebUI
    connections from the real LAN. `--exit-node-allow-lan-access` doesn't
    cover this: it only exempts subnets this netns is *directly attached*
    to (the docker bridge subnet), not the real LAN sitting beyond the
    host's NAT, which this container has no specific route for at all once
    the default route points at the exit node. Re-add an explicit route for
    LAN_CIDR via the original (pre-tailscale) gateway -- Linux always
    prefers the longer prefix match over the default route, so this holds
    regardless of what the exit node does. No-ops if LAN_CIDR isn't set.
    Confirmed live 2026-10-11: selecting an exit node on plab-register
    killed WebUI access from the LAN until this was added."""
    if not LAN_CIDR:
        return
    gw = await _capture_default_gateway()
    if not gw:
        return
    gateway, dev = gw
    await _run_ip("route", "replace", LAN_CIDR, "via", gateway, "dev", dev)


async def _drain(proc: asyncio.subprocess.Process) -> None:
    """Keep reading a backgrounded process's stdout so its pipe never fills
    and blocks it after we've stopped actively consuming it."""
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
    except Exception:                                                     # noqa: BLE001
        pass


async def login() -> dict:
    """Kick off interactive login; returns the auth URL for the owner to
    open in any browser. `tailscale up` keeps running in the background
    until that approval completes -- we only wait long enough to capture
    the printed URL, then let it finish on its own."""
    if not available():
        raise TailscaleError("tailscale socket not found -- is the tailscale sidecar running?")
    # `tailscale up` refuses to run unless every non-default pref already
    # set on the daemon (by containerboot's own startup `up` call, or a
    # prior one of ours) is restated -- otherwise it exits immediately with
    # "requires mentioning all non-default flags" instead of printing a
    # login URL. Restate the full set containerboot/the compose file apply
    # (--accept-dns=false, --netfilter-mode=off, --accept-routes=false).
    #
    # --accept-routes=false is deliberate, not just "restate the default":
    # this node only ever *consumes* an exit node (see the compose file),
    # it never needs subnet routes from other tailnet peers. An earlier
    # version of this call passed bare `--accept-routes` (true), which
    # pulled in whatever subnet route any other peer happens to advertise
    # (e.g. one overlapping the LAN this host is on). That installed a
    # competing route in this container's netns, so return traffic for
    # inbound WebUI connections got sent out via tailscale0 instead of back
    # out the LAN interface -- the WebUI went unreachable the moment login
    # completed, with no exit node ever selected (confirmed live 2026-10-11
    # on plab-register). Keep this false.
    proc = await asyncio.create_subprocess_exec(
        TS_BIN, f"--socket={TS_SOCKET}", "up",
        "--accept-routes=false", "--accept-dns=false", "--netfilter-mode=off",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    url = None
    while loop.time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                break
            continue
        if not line:
            break
        text = line.decode(errors="replace")
        if "https://login.tailscale.com" in text:
            url = text.strip()
            break

    asyncio.create_task(_drain(proc))   # keep the pipe from filling once we stop reading here
    if url:
        return {"url": url, "already_logged_in": False}
    st = await status()
    if st.get("logged_in"):
        return {"url": None, "already_logged_in": True}
    raise TailscaleError("did not receive a login URL from tailscale up")


async def set_exit_node(node_id: str | None) -> None:
    """`node_id` is the peer's nodekey (what `status()` and the UI dropdown
    use as a stable id), but `tailscale set --exit-node` only accepts an IP
    or a peer hostname, not a nodekey -- resolve it via `status()` first."""
    if not available():
        raise TailscaleError("tailscale socket not found -- is the tailscale sidecar running?")
    target = None
    if node_id:
        st = await status()
        target = next((p["name"] for p in st.get("exit_nodes", []) if p["id"] == node_id), None)
        if not target:
            raise TailscaleError(f"no known exit-node peer with id {node_id!r}")
    args = ["set", f"--exit-node={target}", "--exit-node-allow-lan-access"] if target else ["set", "--exit-node="]
    rc, out, err = await _run(*args)
    if rc != 0:
        raise TailscaleError(err.strip() or out.strip() or "tailscale set failed")
    if target:
        await _pin_lan_route()


async def logout() -> None:
    if not available():
        return
    await _run("logout")
