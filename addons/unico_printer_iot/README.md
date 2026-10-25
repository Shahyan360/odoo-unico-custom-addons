# Unico Printer IoT — SNMP Tray Monitoring

Adds **paper-tray level monitoring** (sheets remaining / empty / low) to the
printers you already manage through Odoo's **IoT** app.

It does **not** reinvent printing. Printing stays 100% native IoT — you route
reports to a printer with the standard *Reports to Auto-Print* (`report_ids`) on
the IoT device. This module only adds, for printers you flag, a periodic SNMP
read of their tray levels.

Built/tested against a **Kyocera TASKalfa 6003i**; works with any printer that
exposes the standard **Printer-MIB (RFC 3805)** OIDs (most Kyocera, HP, Canon,
Brother, Ricoh).

---

## How it fits together

```
IoT app  →  iot.device (your printer, auto-discovered by the IoT box)
                 │
                 ├── report_ids        ← native printing (unchanged)
                 └── is_tray_printer ✔ ← THIS module: SNMP tray levels
                          │
                          └── printer.tray rows (level %, Empty/Low/OK)
```

There is **no separate printer model** — a "tray printer" is just an `iot.device`
with the **Tray Printer** box ticked.

### Getting tray data in — two modes (per device)

| Your setup | Mode | Who does the SNMP walk |
|------------|------|------------------------|
| Odoo is **cloud-hosted** (odoo.sh) and can't reach the printer LAN | **Agent push** | A tiny app on the Windows IoT box walks SNMP and POSTs to Odoo |
| Odoo runs **on-prem** and can reach the printer IP | **Direct poll** | Odoo itself walks SNMP every 15 min |

Cloud Odoo → use **Agent push** (the `.exe` below).

---

## Install

1. Make sure the **IoT** app is installed (this module depends on it).
2. Put `unico_printer_iot/` in your addons path (on Odoo.sh: commit + push).
3. Apps → Update Apps List → install **Unico Printer IoT**.

Nothing extra to install on the Odoo side for Agent-push mode.

---

## Use it with the Windows app (.exe) — recommended

### Step 1 — Flag the printer in Odoo (once)
1. **IoT → Devices** → open your printer (e.g. *TASKalfa 6003i*).
2. Tick **Tray Printer**. A **Tray Monitoring** tab appears.
3. On that tab: leave **Tray Data Source = Agent push**, set **Printer IP**
   (`192.0.2.10`) and **Community** (`public`), then **Save**.
4. Copy the **IoT Agent Config** block shown there (URL + token are pre-filled
   from this Odoo instance, so they're always correct for staging vs prod).
   *(The token is admin-only.)*

### Step 2 — Get the .exe
The optional Windows push agent and its build pipeline are not included in this source export. Obtain a compatible agent separately, or use the direct-polling method described below. The agent steps that follow describe the integration protocol for an independently supplied agent.

### Step 3 — Run it on the Windows IoT box
1. Copy the single `KloudedPrinterAgent.exe` to the IoT box and double-click it.
   *(First run: Windows SmartScreen → More info → Run anyway. It's unsigned.)*
2. In the app: paste **Odoo URL**, **Push Token**, **Printer IP**, set the
   interval (default 15 min), tick **Launch automatically when Windows starts**.
3. Click **Test Now** → you should see *Pushed N trays … ok*. The trays appear on
   the printer's **Tray Monitoring** tab in Odoo.
4. Click **Start**. The light turns green and it keeps pushing on the interval.

Config is saved to `%APPDATA%\KloudedPrinterAgent\config.json` — enter it once;
with autostart on it relaunches every login. That's the whole hand-off for
non-technical staff: **copy one .exe → fill the form → Start.**

> If a token is ever leaked, open the device in Odoo → **Tray Monitoring →
> Regenerate Token**, then update the .exe config with the new token.

---

## Use it without the .exe (on-prem / direct poll)
1. On the device's **Tray Monitoring** tab set **Tray Data Source = Direct poll**
   and fill in **Printer IP** / **Community**.
2. On the Odoo host: `pip install puresnmp` (or have net-snmp's `snmpwalk` on PATH).
3. **Test Connection** on the tab; the cron then refreshes every 15 min
   (**Settings → Technical → Scheduled Actions → "Printer IoT: Poll tray levels"**).

---

## What you see
- **IoT → Devices** list shows a **Tray Printer** column.
- **IoT → Printer Trays** lists every tray with a progress bar, `current/max`
  sheets, and a status badge: **Empty** (0), **Low** (≤ 20% by default,
  per-tray adjustable), **OK**, or **Unknown**.

Your printer renders as: Cassette 1 → **Empty (0/500)**, Cassettes 2–5 → **OK (500/500)**.
"Unknown" just means the printer reported the Printer-MIB special values `-2`
(unknown) / `-3` (full but unmeasured) — normal for some MP trays, not an error.

---

## Security notes
- The **push token is admin-only** (`iot.group_iot_admin`) and rotatable via
  **Regenerate Token** — rank-and-file users can't read it.
- The agent **refuses plain HTTP** (won't send the token cleartext) unless you
  set `"allow_insecure_http": true` for a trusted on-prem LAN.
- The push endpoint **caps payloads** (max 64 trays) and returns generic errors.
- SNMP **v2c** community strings travel cleartext on the LAN — keep it
  **read-only** and ensure the printer's SNMP **write** community is disabled.
- The `.exe` is **unsigned** (SmartScreen prompt). For wide rollout, add code
  signing to the build workflow (needs a certificate).
