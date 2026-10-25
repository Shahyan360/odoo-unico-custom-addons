import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useState, onWillStart, onMounted, onPatched } from "@odoo/owl";
import { StockKanbanRenderer, StockKanbanView } from "@stock/components/stock_overview/stock_overview";

// All RMA-linked pickings reuse existing operation types; we only ever filter on
// the rma_id set by the unico_rma module. No new operation type is created.
const RMA_BASE_DOMAIN = [["rma_id", "!=", false]];

/**
 * Renderer for the Inventory Overview dashboard that appends a single,
 * virtual "RMA" card (no stock.picking.type record backs it). The card's
 * counts and navigation are computed/handled entirely client-side from the
 * filtered set of RMA pickings.
 */
export class RmaStockKanbanRenderer extends StockKanbanRenderer {
    setup() {
        super.setup();
        this.orm = useService("orm");
        this.action = useService("action");
        this.rmaCard = useState({ total: 0, ready: 0, waiting: 0, late: 0 });
        onWillStart(() => this.loadRmaCounts());
        // Position the RMA card right after the Delivery Orders (outgoing) card.
        onMounted(() => this._placeRmaCard());
        onPatched(() => this._placeRmaCard());
    }

    /**
     * Place the virtual RMA card immediately after the (first) outgoing
     * operation-type card. The dashboard is a flex-wrap grid, so we reorder
     * with the CSS `order` property instead of moving Owl-managed DOM nodes.
     */
    _placeRmaCard() {
        const root = this.rootRef && this.rootRef.el;
        if (!root) {
            return;
        }
        const rmaCard = root.querySelector(".o_unico_rma_card");
        if (!rmaCard) {
            return;
        }
        const records = (this.props.list && this.props.list.records) || [];
        let targetIdx = -1;
        for (let i = 0; i < records.length; i++) {
            if (records[i].data.code === "outgoing") {
                targetIdx = i;
                break;
            }
        }
        if (targetIdx < 0) {
            // No delivery card on screen: leave the RMA card at its default spot.
            rmaCard.style.order = "";
            return;
        }
        // Real cards render in the same order as the records list (1:1).
        const cards = [...root.querySelectorAll(".o_kanban_record")].filter(
            (el) =>
                !el.classList.contains("o_kanban_ghost") &&
                !el.classList.contains("o_unico_rma_card")
        );
        cards.forEach((el, i) => {
            el.style.order = String(i * 2);
        });
        rmaCard.style.order = String(targetIdx * 2 + 1);
        // Keep the spacer ghost cards at the very end.
        root.querySelectorAll(".o_kanban_ghost").forEach((el) => {
            el.style.order = "100000";
        });
    }

    _today() {
        // Local midnight, formatted for an Odoo datetime comparison.
        return new Date().toISOString().slice(0, 10) + " 00:00:00";
    }

    async loadRmaCounts() {
        const active = ["state", "not in", ["done", "cancel"]];
        const [total, ready, waiting, late] = await Promise.all([
            this.orm.searchCount("stock.picking", [...RMA_BASE_DOMAIN, active]),
            this.orm.searchCount("stock.picking", [...RMA_BASE_DOMAIN, ["state", "=", "assigned"]]),
            this.orm.searchCount("stock.picking", [
                ...RMA_BASE_DOMAIN,
                ["state", "in", ["confirmed", "waiting"]],
            ]),
            this.orm.searchCount("stock.picking", [
                ...RMA_BASE_DOMAIN,
                active,
                ["scheduled_date", "<", this._today()],
            ]),
        ]);
        Object.assign(this.rmaCard, { total, ready, waiting, late });
    }

    openRma(filter) {
        const domain = [...RMA_BASE_DOMAIN];
        if (filter === "ready") {
            domain.push(["state", "=", "assigned"]);
        } else if (filter === "waiting") {
            domain.push(["state", "in", ["confirmed", "waiting"]]);
        } else if (filter === "late") {
            domain.push(["state", "not in", ["done", "cancel"]]);
            domain.push(["scheduled_date", "<", this._today()]);
        } else {
            domain.push(["state", "not in", ["done", "cancel"]]);
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "RMA Operations",
            res_model: "stock.picking",
            domain: domain,
            views: [
                [false, "list"],
                [false, "kanban"],
                [false, "form"],
            ],
            target: "current",
            context: { create: false },
        });
    }
}
RmaStockKanbanRenderer.template = "unico_delivery.RmaStockKanbanRenderer";

export const RmaStockKanbanView = {
    ...StockKanbanView,
    Renderer: RmaStockKanbanRenderer,
};

// Override the Inventory Overview dashboard view with our RMA-aware renderer.
registry.category("views").add("stock_dashboard_kanban", RmaStockKanbanView, { force: true });
