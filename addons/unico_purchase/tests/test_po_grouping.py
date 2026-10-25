from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoPoGrouping(TransactionCase):
    """Per-source RFQ grouping through the real replenishment path: one draft PO
    per reordering rule (auto or manual), and a company switch back to native
    vendor-level grouping."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.unico_group_rfq_per_source = True
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.buy_route = cls.warehouse.buy_pull_id.route_id
        cls.vendor = cls.env["res.partner"].create({
            "name": "PO Grouping Vendor",
            "supplier_rank": 1,
        })
        cls.customer = cls.env["res.partner"].create({"name": "PO Grouping Customer"})

    def _make_buyable(self, name):
        return self.env["product.template"].create({
            "name": name,
            "type": "consu",
            "is_storable": True,
            "purchase_ok": True,
            "seller_ids": [(0, 0, {"partner_id": self.vendor.id, "price": 5.0})],
            "route_ids": [(6, 0, [self.buy_route.id])],
        }).product_variant_id

    def _make_orderpoint(self, name, trigger="manual"):
        return self.env["stock.warehouse.orderpoint"].create({
            "product_id": self._make_buyable(name).id,
            "warehouse_id": self.warehouse.id,
            "location_id": self.stock_location.id,
            "product_min_qty": 0.0,
            "product_max_qty": 1.0,
            "trigger": trigger,
        })

    def _vendor_pos(self):
        return self.env["purchase.order"].search([
            ("partner_id", "=", self.vendor.id), ("state", "=", "draft"),
        ])

    def _make_sale_demand(self, op):
        sale = self.env["sale.order"].create({"partner_id": self.customer.id})
        sale_line_vals = {
            "order_id": sale.id,
            "product_id": op.product_id.id,
            "product_uom_qty": 1.0,
        }
        if "unico_purchase_note" in self.env["sale.order.line"]._fields:
            sale_line_vals["unico_purchase_note"] = "RFQ grouping regression test"
        sale_line = self.env["sale.order.line"].create(sale_line_vals)
        move = self.env["stock.move"].create({
            "product_id": op.product_id.id,
            "product_uom_qty": 1.0,
            "product_uom": op.product_id.uom_id.id,
            "location_id": op.location_id.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "sale_line_id": sale_line.id,
        })
        return sale, move

    # -- Behaviour ---------------------------------------------------------

    def test_manual_orderpoints_get_separate_pos(self):
        """The Replenishment 'Order' path (manual rules) must not pile unrelated
        rules onto one vendor PO — the case enterprise strips orderpoint_id."""
        op_a = self._make_orderpoint("Group Manual A", trigger="manual")
        op_b = self._make_orderpoint("Group Manual B", trigger="manual")

        (op_a + op_b).action_replenish(force_to_max=True)

        pos = self._vendor_pos()
        self.assertEqual(len(pos), 2, "Each manual rule should get its own PO.")
        self.assertEqual(
            set(pos.mapped("unico_po_group_key")),
            {"op:%s" % op_a.id, "op:%s" % op_b.id},
        )

    def test_toggle_off_restores_native_grouping(self):
        self.company.unico_group_rfq_per_source = False
        op_a = self._make_orderpoint("Native A", trigger="manual")
        op_b = self._make_orderpoint("Native B", trigger="manual")

        (op_a + op_b).action_replenish(force_to_max=True)

        pos = self._vendor_pos()
        self.assertEqual(len(pos), 1, "Native grouping merges unrelated rules by vendor.")
        self.assertFalse(pos.unico_po_group_key)

    # -- Key logic ---------------------------------------------------------

    def test_orderpoint_stamps_group_key_in_values(self):
        op = self._make_orderpoint("Stamp Key", trigger="manual")
        values = op._prepare_procurement_values()
        self.assertEqual(values.get("unico_source_group_key"), "op:%s" % op.id)

    def test_cancelled_sale_demand_does_not_stamp_group_key(self):
        op = self._make_orderpoint("Cancelled Sale Key", trigger="manual")
        sale, move = self._make_sale_demand(op)
        move._action_cancel()

        self.assertIn(sale, op._unico_replenishment_sale_orders()[op.id])
        self.assertEqual(op._unico_orderpoint_group_key(), "op:%s" % op.id)

    def test_multiple_sale_demands_keep_stable_orderpoint_key(self):
        op = self._make_orderpoint("Multiple Sale Key", trigger="manual")
        sale_a, move_a = self._make_sale_demand(op)
        sale_b, _move_b = self._make_sale_demand(op)

        self.assertEqual(
            op._unico_replenishment_sale_orders()[op.id],
            sale_a | sale_b,
        )
        self.assertEqual(op._unico_orderpoint_group_key(), "op:%s" % op.id)

        move_a._action_cancel()
        self.assertEqual(op._unico_orderpoint_group_key(), "op:%s" % op.id)

    def test_group_key_precedence(self):
        rule = self.env["stock.rule"]
        sale = self.env["sale.order"].create({"partner_id": self.customer.id})
        reference = self.env["stock.reference"].create({
            "name": sale.name, "sale_ids": [(6, 0, [sale.id])],
        })
        # Stamped key wins over everything.
        self.assertEqual(
            rule._unico_po_group_key({"unico_source_group_key": "op:9",
                                      "reference_ids": reference}),
            "op:9",
        )
        # MTO sale procurement (no stamp) keys per sale order.
        self.assertEqual(
            rule._unico_po_group_key({"reference_ids": reference}),
            "so:%s" % sale.id,
        )
        self.assertFalse(rule._unico_po_group_key({}))
