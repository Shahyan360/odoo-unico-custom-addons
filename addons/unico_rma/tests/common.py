from datetime import datetime, timedelta

from odoo.tests.common import TransactionCase


class RmaTestCommon(TransactionCase):
    """Shared fixtures for unico_rma tests.

    Keeps fixtures minimal and dependency-light: no invoices, no pickings.
    Tests that need those build them locally.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.partner = cls.env["res.partner"].create({"name": "RMA Test Customer"})

        cls.product_a = cls.env["product.product"].create({
            "name": "RMA Test Product A",
            "type": "consu",
            "list_price": 100.0,
            "standard_price": 60.0,
        })
        cls.product_b = cls.env["product.product"].create({
            "name": "RMA Test Product B",
            "type": "consu",
            "list_price": 50.0,
            "standard_price": 30.0,
        })

        cls.return_reason = cls.env["return.reason"].create({
            "name": "Damaged on arrival",
        })
        cls.resolution_refund = cls.env["rma.resolution"].create({
            "rma_resolution": "Refund Resolution",
            "rma_action": "refund",
        })
        cls.resolution_replacement = cls.env["rma.resolution"].create({
            "rma_resolution": "Replacement Resolution",
            "rma_action": "replacement",
        })

        cls.sale_order = cls.env["sale.order"].create({
            "partner_id": cls.partner.id,
            "order_line": [
                (0, 0, {
                    "product_id": cls.product_a.id,
                    "product_uom_qty": 5.0,
                    "price_unit": 100.0,
                }),
            ],
        })

    @classmethod
    def _make_rma(cls, lines=None, replacements=None, **overrides):
        """Create an rma.main with optional lines/replacements.

        `lines` is a list of dicts merged into rma.lines vals.
        `replacements` is a list of dicts merged into rma.replace.order vals.
        """
        vals = {
            "subject": "Test RMA",
            "sale_order": cls.sale_order.id,
            "date": datetime(2026, 1, 1, 10, 0, 0),
            "deadline": datetime(2026, 1, 15, 10, 0, 0),
        }
        vals.update(overrides)

        if lines:
            vals["rma_line_ids"] = [(0, 0, line) for line in lines]
        if replacements:
            vals["replace_prd_ids"] = [(0, 0, rep) for rep in replacements]

        return cls.env["rma.main"].create(vals)
