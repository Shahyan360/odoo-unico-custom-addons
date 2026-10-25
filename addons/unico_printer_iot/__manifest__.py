{
    "name": "Unico Printer IoT (SNMP Tray Monitoring)",
    "version": "19.0.1.0.0",
    "category": "Inventory",
    "summary": "Monitor printer paper-tray levels over SNMP (e.g. Kyocera TASKalfa) "
               "from Odoo, via direct polling or a Windows IoT-box push agent.",
    "description": """
Unico Printer IoT
=================
Configure your network printers **once** and have Odoo keep their paper-tray
levels up to date automatically.

Two ways to get tray data into Odoo (per-printer setting):

* **Direct poll** - the Odoo server walks the printer's SNMP OIDs on a schedule.
  Use this when the Odoo server can reach the printer's LAN IP (on-prem Odoo or
  Odoo running on the IoT box itself).
* **Agent push** - a small scheduled script on your Windows Virtual IoT box does
  the SNMP walk and POSTs the values to Odoo. Use this when Odoo is cloud-hosted
  and cannot reach the printer LAN.

Standard Printer-MIB OIDs (RFC 3805) are pre-filled, so a typical Kyocera
TASKalfa works with zero OID tuning.
    """,
    "author": "Klouded Inc",

    # Note: DIRECT polling uses the optional 'puresnmp' lib (falls back to the
    # net-snmp 'snmpwalk' binary). It is intentionally NOT declared a hard
    # external dependency so the module installs cleanly in Agent-push mode,
    # which needs nothing extra on the Odoo side.
    "depends": ["iot"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/iot_device_views.xml",
        "views/printer_tray_views.xml",
        "views/printer_menus.xml",
    ],
    "application": False,
    "installable": True,
    "auto_install": False,
    "license": "LGPL-3",
}
