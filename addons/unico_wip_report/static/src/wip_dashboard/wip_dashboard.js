import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { ControlPanel } from "@web/search/control_panel/control_panel";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { Component, onWillStart, useState } from "@odoo/owl";

export class UnicoWipDashboard extends Component {
    static template = "unico_wip_report.WipDashboard";
    static components = { ControlPanel };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            dashboard: null,
            // "all" by default on purpose: a WIP board that hides open work
            // would bury the very orders it exists to surface.
            filters: {
                period: "all",
                date_field: "expected_date",
                date_from: "",
                date_to: "",
            },
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        const { period, date_field, date_from, date_to } = this.state.filters;
        try {
            this.state.dashboard = await this.orm.call(
                "unico.wip.item",
                "retrieve_unico_wip_dashboard",
                [],
                {
                    period,
                    date_field,
                    date_from: date_from || null,
                    date_to: date_to || null,
                }
            );
        } finally {
            this.state.loading = false;
        }
    }

    async refresh() {
        await this.load();
    }

    get isCustomPeriod() {
        return this.state.filters.period === "custom";
    }

    async onPeriodChange(ev) {
        this.state.filters.period = ev.target.value;
        if (!this.isCustomPeriod) {
            // Drop any half-entered custom range so the header label and the
            // numbers cannot describe different windows.
            this.state.filters.date_from = "";
            this.state.filters.date_to = "";
        }
        await this.load();
    }

    async onDateFieldChange(ev) {
        this.state.filters.date_field = ev.target.value;
        await this.load();
    }

    async onCustomDateChange(bound, ev) {
        this.state.filters[bound] = ev.target.value;
        await this.load();
    }

    get dashboard() {
        return this.state.dashboard || {};
    }

    get title() {
        return _t("Work in Progress");
    }

    // ------------------------------------------------------------------
    // Drill-down. Every tile carries the domain that produced its count, so
    // opening a tile shows exactly the orders it counted.
    // ------------------------------------------------------------------
    openTile(tile) {
        if (!tile || !tile.count) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: tile.label,
            res_model: tile.res_model || "unico.wip.item",
            views: [[false, "list"], [false, "form"]],
            domain: tile.domain || [],
            target: "current",
        });
    }

    openWatchlist() {
        const watchlist = this.dashboard.watchlist;
        if (!watchlist || !watchlist.total) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Needs Attention"),
            res_model: "unico.wip.item",
            views: [[false, "list"], [false, "form"]],
            domain: watchlist.domain || [],
            target: "current",
        });
    }

    // Opens the tracker, not the sale order — the blocker note and stage live
    // there, and that is what someone clicking a watchlist row wants to edit.
    openOrder(row) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "unico.wip.item",
            res_id: row.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openBoard() {
        this.action.doAction("unico_wip_report.action_unico_wip_board");
    }

    // ------------------------------------------------------------------
    // Presentation
    // ------------------------------------------------------------------
    // Stages are records now, so the key is an id — colour by the automatic
    // rule the server sends along instead. A custom stage with no rule gets
    // the neutral treatment.
    stageClass(tile) {
        switch (tile && tile.token) {
            case "confirmed":
                return "o_unico_wip_tile_neutral";
            case "purchasing":
                return "o_unico_wip_tile_warning";
            case "shipping":
                return "o_unico_wip_tile_accent";
            case "receipt":
                return "o_unico_wip_tile_info";
            case "delivery":
                return "o_unico_wip_tile_success";
            default:
                return "o_unico_wip_tile_neutral";
        }
    }

    urgencyClass(key) {
        if (key === "overdue") {
            return "o_unico_wip_tile_danger";
        }
        if (key === "at_risk") {
            return "o_unico_wip_tile_warning";
        }
        return "o_unico_wip_tile_info";
    }

    paymentClass(key) {
        if (key === "not_paid") {
            return "o_unico_wip_tile_danger";
        }
        if (key === "paid") {
            return "o_unico_wip_tile_success";
        }
        return "o_unico_wip_tile_neutral";
    }

    // Currency payload + Intl formatting, matching unico_financial_dashboard.
    formatMoney(value) {
        const currency = this.dashboard.currency || {};
        const amount = Number(value || 0);
        const formatted = new Intl.NumberFormat(undefined, {
            minimumFractionDigits: currency.decimal_places ?? 2,
            maximumFractionDigits: currency.decimal_places ?? 2,
        }).format(amount);
        if (!currency.symbol) {
            return formatted;
        }
        return currency.position === "after"
            ? `${formatted} ${currency.symbol}`
            : `${currency.symbol} ${formatted}`;
    }

    formatNumber(value) {
        return new Intl.NumberFormat().format(Number(value || 0));
    }

    formatPercent(value) {
        return `${Number(value || 0).toFixed(1)}%`;
    }

    formatDate(value) {
        if (!value) {
            return "—";
        }
        // Server datetimes arrive as naive "YYYY-MM-DD HH:MM:SS" UTC strings.
        const parsed = new Date(`${value.replace(" ", "T")}Z`);
        return Number.isNaN(parsed.getTime())
            ? value
            : parsed.toLocaleDateString();
    }

    delayLabel(row) {
        if (row.delay_days > 0) {
            return _t("%s day(s) late", row.delay_days);
        }
        return row.blocked ? _t("Flagged") : _t("On time");
    }
}

registry.category("actions").add("unico_wip_report.dashboard", UnicoWipDashboard);
