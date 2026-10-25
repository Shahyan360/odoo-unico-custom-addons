from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoPurchaseNoteTransfer(TransactionCase):
    """The originating sale line's Purchase Note must surface on the purchase
    line raised to fulfil it, including on Min/Max *replenishment* POs whose
    origin carries orderpoint references rather than a sale order name."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env["res.partner"].create({"name": "Note Customer"})
        cls.vendor = cls.env["res.partner"].create({"name": "DW Distribution"})
        cls.salesperson = cls.env["res.users"].create({
            "name": "Sally Sales",
            "login": "sally_sales_note_test",
            # Needs the Salesperson group to create sale orders (the create chain
            # runs as this user via with_user, so without it create() is denied).
            # Odoo 19 renamed res.users.groups_id -> group_ids.
            "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_salesman").id])],
        })
        cls.product = cls.env["product.product"].create({
            "name": "Carrara Door",
            "type": "consu",
            "is_storable": True,
        })
        cls.note = "Confirm purchasing availability before confirmation."

    def _sale_with_note(self, note=None, state="draft"):
        order = self.env["sale.order"].with_user(self.salesperson).create({
            "partner_id": self.customer.id,
        })
        self.env["sale.order.line"].create({
            "order_id": order.id,
            "product_id": self.product.id,
            "product_uom_qty": 2.0,
            "unico_purchase_note": note if note is not None else self.note,
        })
        return order

    def _po(self, origin):
        po = self.env["purchase.order"].create({
            "partner_id": self.vendor.id,
            "origin": origin,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_qty": 5.0,
            })],
        })
        return po.order_line[0]

    def test_replenishment_po_surfaces_open_sale_notes(self):
        """A replenishment PO (orderpoint refs in origin) shows the open sale's
        note attributed to the order + salesperson."""
        sale = self._sale_with_note()
        line = self._po("Replenishment Report, OP/00088, OP/00055")
        self.assertIn(self.note, line.unico_purchase_note)
        self.assertIn(sale.name, line.unico_purchase_note)
        self.assertIn("Sally Sales", line.unico_purchase_note)

    def test_replenishment_dedupes_identical_notes(self):
        """Several open sales sharing the same note collapse to one block that
        lists every requesting order."""
        a = self._sale_with_note()
        b = self._sale_with_note()
        line = self._po("Replenishment Report, OP/00088")
        # One occurrence of the note text, both orders credited.
        self.assertEqual(line.unico_purchase_note.count(self.note), 1)
        self.assertIn(a.name, line.unico_purchase_note)
        self.assertIn(b.name, line.unico_purchase_note)

    def test_replenishment_ignores_closed_sales(self):
        """Notes from cancelled sales are not relevant to a buyer and must not
        appear on the replenishment line."""
        cancelled = self._sale_with_note()
        cancelled.state = "cancel"
        line = self._po("Replenishment Report, OP/00088")
        self.assertFalse(line.unico_purchase_note)

    def test_non_replenishment_po_without_sale_name_has_no_note(self):
        """A plain PO whose origin is neither a sale name nor a replenishment ref
        must not pull in unrelated open-sale notes (no cross-contamination)."""
        self._sale_with_note()
        line = self._po("Manual restock")
        self.assertFalse(line.unico_purchase_note)

    def test_plain_rfq_matches_only_its_own_sale(self):
        """The existing name-in-origin match still wins and is not polluted by
        other open sales for the same product."""
        own = self._sale_with_note(note="Own order instructions")
        self._sale_with_note(note="Some other order instructions")
        line = self._po(own.name)
        self.assertEqual(line.unico_purchase_note, "Own order instructions")
