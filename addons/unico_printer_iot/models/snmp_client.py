"""Tiny SNMP-walk helper used for DIRECT polling.

It tries, in order:
  1. the pure-python ``puresnmp`` library (recommended: ``pip install puresnmp``)
  2. the net-snmp ``snmpwalk`` command line binary (if installed on the host)

Agent-push mode does NOT use this module at all - the Windows IoT box does the
walk instead and POSTs the values to Odoo.
"""
import logging
import re
import shutil
import subprocess

_logger = logging.getLogger(__name__)


class SnmpError(Exception):
    """Raised when an SNMP walk cannot be performed or fails."""


def _walk_puresnmp(host, port, community, oid, timeout):
    """Walk ``oid`` using the puresnmp library. Returns list of (suffix, value)."""
    from puresnmp import Client, V2C, PyWrapper  # imported lazily

    client = PyWrapper(Client(host, V2C(community), port=port))
    results = []
    base = oid.strip(".")
    # PyWrapper.walk is async in puresnmp >= 2.0; run it to completion.
    import asyncio

    async def _run():
        out = []
        async for varbind in client.walk(oid):
            full = str(varbind.oid).strip(".")
            suffix = full[len(base):].strip(".") if full.startswith(base) else full
            out.append((suffix, _coerce(varbind.value)))
        return out

    results = asyncio.run(_run())
    return results


def _walk_snmpwalk(host, port, community, version, oid, timeout):
    """Walk ``oid`` using the net-snmp ``snmpwalk`` binary."""
    binary = shutil.which("snmpwalk")
    if not binary:
        raise SnmpError(
            "Neither the 'puresnmp' python library nor the 'snmpwalk' binary is "
            "available on the Odoo host. Install one of them, or switch this "
            "printer to 'Agent push' mode."
        )
    cmd = [
        binary,
        "-v", "2c" if version == "v2c" else "1",
        "-c", community,
        "-O", "qn",          # quick output, numeric OIDs
        "-t", str(max(1, int(timeout))),
        "-r", "1",
        f"{host}:{port}",
        oid,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise SnmpError("SNMP walk timed out (printer not reachable?)") from exc
    if proc.returncode != 0:
        raise SnmpError((proc.stderr or proc.stdout or "snmpwalk failed").strip())

    base = oid.strip(".")
    results = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format with -O qn:  ".1.3.6.1.2.1.43.8.2.1.10.1.2 500"
        parts = line.split(" ", 1)
        full = parts[0].strip(".")
        value = parts[1].strip() if len(parts) > 1 else ""
        suffix = full[len(base):].strip(".") if full.startswith(base) else full
        results.append((suffix, _coerce(value)))
    return results


def _coerce(value):
    """Best-effort convert an SNMP value to int, else stripped string."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip().strip('"')
    # snmpwalk may prefix types, e.g. 'INTEGER: 500' or 'STRING: "Cassette 1"'
    text = re.sub(r"^[A-Za-z0-9-]+:\s*", "", text).strip().strip('"')
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def walk(host, port, community, version, oid, timeout=5):
    """Return a list of ``(suffix, value)`` tuples for the SNMP subtree ``oid``.

    ``suffix`` is the part of the responding OID *after* the requested base OID
    (e.g. ``"1.2"`` for tray index 1.2), used to line trays up across OIDs.
    """
    try:
        return _walk_puresnmp(host, port, community, oid, timeout)
    except ImportError:
        return _walk_snmpwalk(host, port, community, version, oid, timeout)
    except SnmpError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any backend error cleanly
        _logger.warning("puresnmp walk failed, trying snmpwalk binary: %s", exc)
        return _walk_snmpwalk(host, port, community, version, oid, timeout)
