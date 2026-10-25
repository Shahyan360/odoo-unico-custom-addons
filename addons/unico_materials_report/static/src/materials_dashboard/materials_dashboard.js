import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { ControlPanel } from "@web/search/control_panel/control_panel";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { Component, onWillStart, useState } from "@odoo/owl";

export class UnicoMaterialsDashboard extends Component {
    static template = "unico_materials_report.MaterialsDashboard";
    static components = { ControlPanel };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, dashboard: null });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.dashboard = await this.orm.call(
            "sale.order",
            "retrieve_unico_materials_dashboard",
            []
        );
        this.state.loading = false;
    }

    async refresh() {
        await this.load();
    }

    openTile(tile) {
        if (!tile) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: tile.label,
            res_model: tile.res_model || "sale.order",
            views: [[false, "list"], [false, "form"]],
            domain: tile.domain || [],
            target: "current",
        });
    }

    openOrders() {
        this.action.doAction("unico_materials_report.action_unico_waiting_materials_orders");
    }

    openLines() {
        this.action.doAction("unico_materials_report.action_unico_waiting_materials_lines");
    }

    urgencyClass(key) {
        if (key === "overdue") {
            return "o_unico_materials_tile_danger";
        }
        if (key === "urgent") {
            return "o_unico_materials_tile_warning";
        }
        return "o_unico_materials_tile_info";
    }

    get dashboard() {
        return this.state.dashboard || {};
    }

    get title() {
        return _t("Waiting on Materials");
    }
}

registry.category("actions").add(
    "unico_materials_report.dashboard",
    UnicoMaterialsDashboard
);
