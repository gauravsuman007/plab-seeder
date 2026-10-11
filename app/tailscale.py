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
    # (--accept-dns=false, --netfilter-mode=off) rather than just
    # --accept-routes, so this works regardless of what's already set.
    proc = await asyncio.create_subprocess_exec(
        TS_BIN, f"--socket={TS_SOCKET}", "up",
        "--accept-routes", "--accept-dns=false", "--netfilter-mode=off",
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


async def logout() -> None:
    if not available():
        return
    await _run("logout")
