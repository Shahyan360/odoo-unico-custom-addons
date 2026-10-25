"""The PO Alert gate: a flagged customer's order cannot be confirmed without
their purchase order details.
"""
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoPoRequirement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env["product.product"].create({
            "name": "PO Requirement Test Product",
            "type": "consu",
            "list_price": 100.0,
        })
        cls.plain_customer = cls.env["res.partner"].create({
            "name": "No PO Needed Co",
        })
        cls.po_company = cls.env["res.partner"].create({
            "name": "PO Required Co",
            "is_company": True,
            "unico_po_alert": True,
        })
        cls.po_contact = cls.env["res.partner"].create({
            "name": "Buyer At PO Required Co",
            "parent_id": cls.po_company.id,
        })

    def _order(self, partner, po_detail=None):
        vals = {
            "partner_id": partner.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_uom_qty": 1.0,
            })],
        }
        if po_detail is not None:
            vals["unico_po_detail"] = po_detail
        return self.env["sale.order"].create(vals)

    # -- The gate ----------------------------------------------------------

    def test_flagged_customer_without_po_detail_is_blocked(self):
        order = self._order(self.po_company)
        with self.assertRaises(UserError):
            order.action_confirm()
        self.assertNotEqual(order.state, "sale")

    def test_flagged_customer_with_po_detail_confirms(self):
        order = self._order(self.po_company, po_detail="PO-12345")
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_unflagged_customer_needs_nothing(self):
        order = self._order(self.plain_customer)
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_whitespace_only_po_detail_does_not_count(self):
        """A space bar is not a purchase order number."""
        order = self._order(self.po_company, po_detail="   ")
        with self.assertRaises(UserError):
            order.action_confirm()

    # -- Where the flag is read from --------------------------------------

    def test_contact_inherits_the_flag_from_its_company(self):
        """Ticked once on the company, enforced for every contact under it."""
        self.assertFalse(self.po_contact.unico_po_alert)
        order = self._order(self.po_contact)
        self.assertTrue(order.unico_po_required)
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_po_required_flag_follows_the_customer(self):
        order = self._order(self.plain_customer)
        self.assertFalse(order.unico_po_required)
        order.partner_id = self.po_company
        self.assertTrue(order.unico_po_required)

    # -- Batch confirmation -----------------------------------------------

    def test_batch_confirm_names_every_offending_order(self):
        """Confirming from the list must say which orders are short, not just
        fail on the first one."""
        blocked_a = self._order(self.po_company)
        blocked_b = self._order(self.po_contact)
        allowed = self._order(self.plain_customer)
        orders = blocked_a | blocked_b | allowed

        with self.assertRaises(UserError) as caught:
            orders.action_confirm()
        message = str(caught.exception)
        self.assertIn(blocked_a.name, message)
        self.assertIn(blocked_b.name, message)
        self.assertNotIn(allowed.name, message)
        # Nothing is confirmed: the whole batch is refused together.
        self.assertNotEqual(allowed.state, "sale")
