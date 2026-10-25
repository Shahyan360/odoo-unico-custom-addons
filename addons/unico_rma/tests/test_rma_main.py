from datetime import datetime

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestRmaMain(RmaTestCommon):

    def test_create_default_name_when_no_rma_type(self):
        rma = self._make_rma()
        self.assertEqual(rma.name, "New")

    def test_create_with_rma_with_do_assigns_sequence(self):
        rma = self._make_rma(rma_type="rma_with_do")
        # Sequence is defined in views/rma_order_sequence.xml with prefix RMA-
        self.assertTrue(rma.name)
        self.assertNotEqual(rma.name, "New")
        self.assertTrue(rma.name.startswith("RMA-"))

    def test_company_record_rule_is_loaded(self):
        rule = self.env.ref("unico_rma.rma_order_comp_rule", raise_if_not_found=False)
        self.assertTrue(rule)
        self.assertEqual(rule.model_id.model, "rma.main")

    def test_reject_wizard_sets_state_and_reason(self):
        rma = self._make_rma()
        wizard = self.env["rma.reject.wizard"].create({
            "rma_id": rma.id,
            "reject_reason": "Out of warranty",
        })
        wizard.action_confirm()
        self.assertEqual(rma.state, "reject")
        self.assertEqual(rma.reject_reason, "Out of warranty")

    def test_unlink_in_draft_succeeds(self):
        rma = self._make_rma()
        self.assertEqual(rma.state, "draft")
        rma.unlink()

    def test_unlink_outside_draft_raises(self):
        rma = self._make_rma()
        rma.write({"state": "submit"})
        with self.assertRaises(ValidationError):
            rma.unlink()

    def test_onchange_deadline_raises_when_date_after_deadline(self):
        rma = self._make_rma()
        rma.date = datetime(2026, 2, 1, 10, 0, 0)
        rma.deadline = datetime(2026, 1, 1, 10, 0, 0)
        with self.assertRaises(UserError):
            rma._onchange_deadline()

    def test_onchange_deadline_ok_when_deadline_after_date(self):
        rma = self._make_rma()
        rma.date = datetime(2026, 1, 1, 10, 0, 0)
        rma.deadline = datetime(2026, 2, 1, 10, 0, 0)
        # Should not raise
        rma._onchange_deadline()

    def test_compute_return_total(self):
        rma = self._make_rma(lines=[
            {"product_id": self.product_a.id, "return_qty": 2, "price_unit": 100.0, "delivery_qty": 5.0},
            {"product_id": self.product_b.id, "return_qty": 3, "price_unit": 50.0, "delivery_qty": 5.0},
        ])
        self.assertEqual(rma.total_return, 2 * 100.0 + 3 * 50.0)

    def test_compute_replace_total(self):
        rma = self._make_rma(replacements=[
            {"product_id": self.product_a.id, "qty": 1},
            {"product_id": self.product_b.id, "qty": 4},
        ])
        # list_price-based: product_a 100, product_b 50
        self.assertEqual(rma.total_replace, 1 * 100.0 + 4 * 50.0)

    def test_compute_difference_total(self):
        rma = self._make_rma(
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 1,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
            }],
            replacements=[{"product_id": self.product_b.id, "qty": 4}],
        )
        # return=100, replace=200 → diff=100
        self.assertEqual(rma.total_return, 100.0)
        self.assertEqual(rma.total_replace, 200.0)
        self.assertEqual(rma.total_difference, 100.0)
        self.assertEqual(rma.difference_amount, 100.0)

    def test_compute_difference_total_when_return_greater(self):
        rma = self._make_rma(
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 3,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
            }],
            replacements=[{"product_id": self.product_b.id, "qty": 1}],
        )
        # return=300, replace=50 → diff=-250, amount=250
        self.assertEqual(rma.total_difference, -250.0)
        self.assertEqual(rma.difference_amount, 250.0)

    def test_action_submit_raises_when_no_return_qty(self):
        rma = self._make_rma(lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        with self.assertRaises(ValidationError):
            rma.action_submit()

    def test_action_submit_raises_when_replacement_qty_zero(self):
        rma = self._make_rma(
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 1,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
            }],
            replacements=[{"product_id": self.product_b.id, "qty": 0}],
        )
        with self.assertRaises(ValidationError):
            rma.action_submit()

    def test_action_submit_success(self):
        rma = self._make_rma(lines=[{
            "product_id": self.product_a.id,
            "return_qty": 2,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        rma.action_submit()
        self.assertEqual(rma.state, "submit")

    def test_action_submit_ignores_service_lines_for_required_return_qty(self):
        service_product = self.env["product.product"].create({
            "name": "RMA Test Service",
            "type": "service",
            "list_price": 25.0,
        })
        rma = self._make_rma(lines=[{
            "product_id": service_product.id,
            "return_qty": 1,
            "price_unit": 25.0,
            "delivery_qty": 1.0,
        }])
        with self.assertRaises(ValidationError):
            rma.action_submit()

    def test_onchange_sale_order_builds_goods_lines_and_skips_services(self):
        service_product = self.env["product.product"].create({
            "name": "RMA Test Service Line",
            "type": "service",
            "list_price": 25.0,
        })
        sale_order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [
                (0, 0, {
                    "product_id": self.product_a.id,
                    "product_uom_qty": 2.0,
                    "price_unit": 100.0,
                }),
                (0, 0, {
                    "product_id": service_product.id,
                    "product_uom_qty": 1.0,
                    "price_unit": 25.0,
                }),
            ],
        })
        rma = self._make_rma(sale_order=sale_order.id, rma_type="rma_with_do")

        rma.onchange_sale_order_id()

        self.assertEqual(len(rma.rma_line_ids), 1)
        self.assertEqual(rma.rma_line_ids.product_id, self.product_a)

    def test_onchange_sale_order_populates_delivery_and_invoice_snapshots(self):
        shipping_partner = self.env["res.partner"].create({
            "name": "RMA Ship To",
            "email": "ship@example.com",
            "phone": "555-0101",
            "street": "100 Warehouse Way",
            "city": "Miami",
            "zip": "33101",
        })
        invoice_partner = self.env["res.partner"].create({
            "name": "RMA Bill To",
            "email": "bill@example.com",
            "phone": "555-0202",
            "street": "200 Accounting Ave",
            "city": "Orlando",
            "zip": "32801",
        })
        sale_order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "partner_shipping_id": shipping_partner.id,
            "partner_invoice_id": invoice_partner.id,
            "order_line": [
                (0, 0, {
                    "product_id": self.product_a.id,
                    "product_uom_qty": 2.0,
                    "price_unit": 100.0,
                }),
            ],
        })
        rma = self._make_rma(sale_order=sale_order.id, rma_type="rma_with_do")

        rma.onchange_sale_order_id()
        rma.set_sale_details()

        self.assertEqual(rma.del_partner, shipping_partner)
        self.assertEqual(rma.del_email, "ship@example.com")
        self.assertEqual(rma.del_phone, "555-0101")
        self.assertEqual(rma.del_street, "100 Warehouse Way")
        self.assertEqual(rma.inv_partner, invoice_partner)
        self.assertEqual(rma.inv_email, "bill@example.com")
        self.assertEqual(rma.inv_phone, "555-0202")
        self.assertEqual(rma.inv_street, "200 Accounting Ave")

    def test_action_approve_raises_when_already_approved(self):
        rma = self._make_rma(rma_type="rma_with_do")
        rma.write({"state": "approved"})
        with self.assertRaises(UserError):
            rma.action_approve()

    def test_action_approve_noop_when_not_rma_with_do(self):
        rma = self._make_rma()  # no rma_type
        rma.action_approve()
        # state unchanged since rma_type guard fails
        self.assertEqual(rma.state, "draft")

    def test_compute_is_editable_is_true(self):
        rma = self._make_rma()
        self.assertTrue(rma.is_editable)

    def test_compute_validated_false_when_not_processing(self):
        rma = self._make_rma()
        self.assertFalse(rma.validated)

    def test_compute_validated_true_when_processing_no_pickings(self):
        rma = self._make_rma()
        rma.write({"state": "processing"})
        self.assertTrue(rma.validated)

    def test_compute_counters_default_zero(self):
        rma = self._make_rma()
        self.assertEqual(rma.in_delivery_count, 0)
        self.assertEqual(rma.out_delivery_count, 0)
        self.assertEqual(rma.refund_inv_count, 0)
