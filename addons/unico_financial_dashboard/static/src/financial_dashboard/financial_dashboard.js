import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { ControlPanel } from "@web/search/control_panel/control_panel";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";

const CHART_WIDTH = 640;
const CHART_HEIGHT = 180;
const CHART_PADDING = 28;
const FOCUS_CHART_LABEL_WIDTH = 62;
const TABLE_PAGE_SIZE = 10;
const INVENTORY_HISTORY_COLORS = [
    "#111827",
    "#2563eb",
    "#0f766e",
    "#c2410c",
    "#7c3aed",
    "#be123c",
    "#0369a1",
    "#4d7c0f",
    "#a16207",
    "#6d28d9",
];
const WEEKLY_COLUMN_STORAGE_KEY = "unico_financial_dashboard_weekly_columns";
const SUMMARY_CARD_SECTION_TARGETS = {
    open_ar: "unico_financial_ar_aging",
    ar_offsets: "unico_financial_ar_offsets",
    net_ar: "unico_financial_ar_details",
    open_ap: "unico_financial_ap_aging",
    inventory_value: "unico_financial_inventory",
};

export class UnicoFinancialDashboard extends Component {
    static template = "unico_financial_dashboard.FinancialDashboard";
    static components = { ControlPanel };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            rangePreset: "default",
            dateStart: "",
            dateEnd: "",
            comparisonMode: "prior_period",
            comparisonStart: "",
            comparisonEnd: "",
            dashboard: null,
            selectedTrendMetricKey: "revenue",
            hoveredTrendPoint: null,
            weeklySort: {
                key: "key",
                direction: "asc",
            },
            tableSorts: {},
            tablePages: {},
            visibleWeeklyColumns: this.loadWeeklyColumnPreferences(),
            weeklyColumnPickerOpen: false,
            inventoryHistoryLoading: false,
            inventoryHistoryLoaded: false,
            inventoryHistoryError: "",
            inventoryHistoryKey: "",
        });
        this.onDocumentClick = (ev) => this.closeWeeklyColumnPickerOnOutsideClick(ev);
        this.inventoryObserver = null;
        onWillStart(() => this.loadDashboard());
        onMounted(() => {
            document.addEventListener("click", this.onDocumentClick, true);
            this.observeInventorySection();
        });
        onWillUnmount(() => {
            document.removeEventListener("click", this.onDocumentClick, true);
            if (this.inventoryObserver) {
                this.inventoryObserver.disconnect();
            }
        });
    }

    dashboardKwargs() {
        const kwargs = {};
        kwargs.range_preset = this.state.rangePreset || "default";
        if (
            this.state.rangePreset === "custom"
            && this.state.dateStart
            && this.state.dateEnd
        ) {
            kwargs.date_start = this.state.dateStart;
            kwargs.date_end = this.state.dateEnd;
        }
        kwargs.comparison_mode = this.state.comparisonMode || "prior_period";
        if (
            this.state.comparisonMode === "custom"
            && this.state.comparisonStart
            && this.state.comparisonEnd
        ) {
            kwargs.comparison_date_start = this.state.comparisonStart;
            kwargs.comparison_date_end = this.state.comparisonEnd;
        }
        return kwargs;
    }

    appliedRangeKwargs() {
        const dashboard = this.dashboard;
        return {
            range_preset: "custom",
            date_start: dashboard.date_start || "",
            date_end: dashboard.date_end || "",
        };
    }

    async loadDashboard() {
        this.state.loading = true;
        this.state.dashboard = await this.orm.call(
            "unico.financial.dashboard",
            "retrieve_dashboard",
            [],
            this.dashboardKwargs()
        );
        this.resetInventoryHistoryState();
        // Reflect the server-resolved selected range back into the pickers.
        this.state.rangePreset = this.state.dashboard.range_preset || "default";
        this.state.dateStart = this.state.dashboard.date_start || "";
        this.state.dateEnd = this.state.dashboard.date_end || "";
        this.state.comparisonMode = this.state.dashboard.comparison_mode || "prior_period";
        if (this.state.comparisonMode === "custom") {
            this.state.comparisonStart = this.state.dashboard.comparison_date_start || "";
            this.state.comparisonEnd = this.state.dashboard.comparison_date_end || "";
        } else {
            this.state.comparisonStart = "";
            this.state.comparisonEnd = "";
        }
        this.state.hoveredTrendPoint = null;
        this.state.weeklyColumnPickerOpen = false;
        this.state.tablePages = {};
        this.state.loading = false;
        window.setTimeout(() => this.observeInventorySection(), 0);
    }

    async onRangePresetChange(ev) {
        this.state.rangePreset = ev.target.value || "default";
    }

    isDateRangeReady() {
        if (this.state.rangePreset !== "custom") {
            return true;
        }
        return Boolean(
            this.state.dateStart
            && this.state.dateEnd
            && this.state.dateStart <= this.state.dateEnd
        );
    }

    isComparisonRangeReady() {
        if (this.state.comparisonMode !== "custom") {
            return true;
        }
        return Boolean(
            this.state.comparisonStart
            && this.state.comparisonEnd
            && this.state.comparisonStart <= this.state.comparisonEnd
        );
    }

    onDateStartChange(ev) {
        this.state.rangePreset = "custom";
        this.state.dateStart = ev.target.value;
    }

    onDateEndChange(ev) {
        this.state.rangePreset = "custom";
        this.state.dateEnd = ev.target.value;
    }

    resetRange() {
        this.state.rangePreset = "default";
        this.state.dateStart = "";
        this.state.dateEnd = "";
    }

    onComparisonModeChange(ev) {
        this.state.comparisonMode = ev.target.value || "prior_period";
        if (this.state.comparisonMode === "custom") {
            this.state.comparisonStart = this.dashboard.comparison_date_start || "";
            this.state.comparisonEnd = this.dashboard.comparison_date_end || "";
        } else {
            this.state.comparisonStart = "";
            this.state.comparisonEnd = "";
        }
    }

    onComparisonStartChange(ev) {
        this.state.comparisonMode = "custom";
        this.state.comparisonStart = ev.target.value;
    }

    onComparisonEndChange(ev) {
        this.state.comparisonMode = "custom";
        this.state.comparisonEnd = ev.target.value;
    }

    resetComparison() {
        this.state.comparisonMode = "prior_period";
        this.state.comparisonStart = "";
        this.state.comparisonEnd = "";
    }

    async refresh() {
        if (!this.canRefreshReport) {
            return;
        }
        await this.loadDashboard();
    }

    resetInventoryHistoryState() {
        this.state.inventoryHistoryLoading = false;
        this.state.inventoryHistoryLoaded = false;
        this.state.inventoryHistoryError = "";
        this.state.inventoryHistoryKey = "";
        if (this.state.dashboard?.inventory_value) {
            this.state.dashboard.inventory_value.history = {
                points: [],
                series: [],
                deferred: true,
            };
        }
    }

    observeInventorySection() {
        if (this.inventoryObserver) {
            this.inventoryObserver.disconnect();
            this.inventoryObserver = null;
        }
        const section = document.getElementById("unico_financial_inventory");
        if (!section || !("IntersectionObserver" in window)) {
            return;
        }
        const root = document.querySelector(".o_unico_financial_content");
        this.inventoryObserver = new IntersectionObserver((entries) => {
            if (entries.some((entry) => entry.isIntersecting)) {
                this.ensureInventoryHistoryLoaded();
            }
        }, {
            root,
            rootMargin: "240px 0px",
            threshold: 0.01,
        });
        this.inventoryObserver.observe(section);
    }

    async ensureInventoryHistoryLoaded() {
        if (
            !this.state.dashboard
            || this.state.inventoryHistoryLoading
            || this.state.inventoryHistoryLoaded
        ) {
            return;
        }
        const key = this.appliedReportFilterKey();
        this.state.inventoryHistoryLoading = true;
        this.state.inventoryHistoryError = "";
        try {
            const payload = await this.orm.call(
                "unico.financial.dashboard",
                "retrieve_inventory_history",
                [],
                this.appliedRangeKwargs()
            );
            if (key !== this.appliedReportFilterKey()) {
                return;
            }
            if (this.state.dashboard?.inventory_value) {
                this.state.dashboard.inventory_value.history = (
                    payload.history || { points: [], series: [] }
                );
            }
            this.state.inventoryHistoryKey = key;
            this.state.inventoryHistoryLoaded = true;
        } catch {
            if (key === this.appliedReportFilterKey()) {
                this.state.inventoryHistoryError = _t("Inventory history could not be loaded.");
            }
        } finally {
            if (key === this.appliedReportFilterKey()) {
                this.state.inventoryHistoryLoading = false;
            }
        }
    }

    closeWeeklyColumnPickerOnOutsideClick(ev) {
        const target = ev.target;
        if (!this.state.weeklyColumnPickerOpen || !(target instanceof Element)) {
            return;
        }
        if (!target.closest(".o_unico_column_picker")) {
            this.state.weeklyColumnPickerOpen = false;
        }
    }

    toggleWeeklyColumnPicker() {
        this.state.weeklyColumnPickerOpen = !this.state.weeklyColumnPickerOpen;
    }

    loadWeeklyColumnPreferences() {
        try {
            const value = window.localStorage.getItem(WEEKLY_COLUMN_STORAGE_KEY);
            const parsed = value ? JSON.parse(value) : {};
            return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch {
            return {};
        }
    }

    saveWeeklyColumnPreferences(preferences) {
        try {
            window.localStorage.setItem(WEEKLY_COLUMN_STORAGE_KEY, JSON.stringify(preferences));
        } catch {
            // Local storage can be unavailable in private browsing or restricted webviews.
        }
    }

    isWeeklyColumnVisible(columnKey) {
        const column = this.weeklySortColumns.find((item) => item.key === columnKey);
        return Boolean(column?.required) || this.state.visibleWeeklyColumns[columnKey] !== false;
    }

    toggleWeeklyColumn(columnKey) {
        const column = this.weeklySortColumns.find((item) => item.key === columnKey);
        if (!column || column.required) {
            return;
        }
        const preferences = { ...this.state.visibleWeeklyColumns };
        if (this.isWeeklyColumnVisible(columnKey)) {
            preferences[columnKey] = false;
            if (this.state.weeklySort.key === columnKey) {
                this.state.weeklySort = { key: "key", direction: "asc" };
            }
        } else {
            delete preferences[columnKey];
        }
        this.state.visibleWeeklyColumns = preferences;
        this.saveWeeklyColumnPreferences(preferences);
    }

    showAllWeeklyColumns() {
        this.state.visibleWeeklyColumns = {};
        this.saveWeeklyColumnPreferences({});
    }

    summaryCardTarget(card) {
        return SUMMARY_CARD_SECTION_TARGETS[card?.metric_key] || "";
    }

    isSummaryCardActionable(card) {
        return Boolean(this.summaryCardTarget(card) || card?.drilldown?.model);
    }

    openSummaryCard(card) {
        const target = this.summaryCardTarget(card);
        if (target) {
            this.scrollToSection(target);
            return;
        }
        this.openDrilldown(card?.drilldown, card?.label);
    }

    openApAgingVendor(row) {
        if (!row?.id) {
            return;
        }
        this.openRecord("res.partner", row.id);
    }

    scrollToSection(sectionId) {
        const section = document.getElementById(sectionId);
        if (section) {
            section.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        if (sectionId === "unico_financial_inventory") {
            this.ensureInventoryHistoryLoaded();
        }
    }

    openDrilldown(drilldown, title) {
        if (!drilldown || !drilldown.model) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: title,
            res_model: drilldown.model,
            views: [[false, "list"], [false, "form"]],
            domain: drilldown.domain || [],
            target: "current",
        });
    }

    openRecord(model, id) {
        if (!model || !id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            views: [[false, "form"]],
            res_id: id,
            target: "current",
        });
    }

    formatMoney(value) {
        const dashboard = this.state.dashboard || {};
        const currency = dashboard.currency || {};
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

    formatPercent(value) {
        return `${Number(value || 0).toFixed(1)}%`;
    }

    formatPercentagePoints(value) {
        const points = Number(value || 0);
        const sign = points > 0 ? "+" : "";
        return `${sign}${points.toFixed(1)} pp`;
    }

    paymentTermShareTotal(total) {
        return Number(total?.confirmed_sales || 0) ? 100 : 0;
    }

    formatNumber(value) {
        return new Intl.NumberFormat().format(Number(value || 0));
    }

    formatValue(value, type) {
        if (type === "money") {
            return this.formatMoney(value);
        }
        if (type === "percent") {
            return this.formatPercent(value);
        }
        return this.formatNumber(value);
    }

    inventoryHistoryScale() {
        const values = this.inventoryHistorySeries.flatMap(
            (series) => (series.values || []).map((value) => Number(value || 0))
        );
        if (!values.length) {
            return { min: 0, max: 1 };
        }
        let min = Math.min(...values);
        let max = Math.max(...values);
        if (min === max) {
            const offset = Math.abs(max || 1) * 0.1;
            min -= offset;
            max += offset;
        } else {
            const offset = (max - min) * 0.08;
            min -= offset;
            max += offset;
        }
        return { min, max };
    }

    inventoryHistoryPoints(series) {
        const values = (series?.values || []).map((value) => Number(value || 0));
        const scale = this.inventoryHistoryScale();
        return values.map((value, index) => this.chartPoint(value, index, values.length, scale));
    }

    inventoryHistoryPath(series) {
        const points = this.inventoryHistoryPoints(series);
        if (!points.length) {
            return "";
        }
        return points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ");
    }

    inventoryHistoryBoundLabel(bound) {
        return this.formatMoney(this.inventoryHistoryScale()[bound]);
    }

    inventoryHistorySeriesColor(index) {
        return INVENTORY_HISTORY_COLORS[index % INVENTORY_HISTORY_COLORS.length];
    }

    inventoryHistoryLastValue(series) {
        const values = series?.values || [];
        return values.length ? values[values.length - 1] : 0;
    }

    normalizeReportFilters(filters) {
        const rangePreset = filters.rangePreset || "default";
        const comparisonMode = filters.comparisonMode || "prior_period";
        return JSON.stringify({
            rangePreset,
            dateStart: rangePreset === "custom" ? filters.dateStart || "" : "",
            dateEnd: rangePreset === "custom" ? filters.dateEnd || "" : "",
            comparisonMode,
            comparisonStart: comparisonMode === "custom" ? filters.comparisonStart || "" : "",
            comparisonEnd: comparisonMode === "custom" ? filters.comparisonEnd || "" : "",
        });
    }

    currentReportFilterKey() {
        return this.normalizeReportFilters(this.state);
    }

    appliedReportFilterKey() {
        const dashboard = this.dashboard;
        return this.normalizeReportFilters({
            rangePreset: dashboard.range_preset || "default",
            dateStart: dashboard.date_start || "",
            dateEnd: dashboard.date_end || "",
            comparisonMode: dashboard.comparison_mode || "prior_period",
            comparisonStart: dashboard.comparison_date_start || "",
            comparisonEnd: dashboard.comparison_date_end || "",
        });
    }

    get hasInvalidReportFilters() {
        return !this.isDateRangeReady() || !this.isComparisonRangeReady();
    }

    get hasPendingReportChanges() {
        return Boolean(
            this.state.dashboard
            && this.currentReportFilterKey() !== this.appliedReportFilterKey()
        );
    }

    get canRefreshReport() {
        return !this.state.loading && !this.hasInvalidReportFilters;
    }

    get refreshButtonTitle() {
        if (!this.isDateRangeReady()) {
            return _t("Choose a valid report start and end date.");
        }
        if (!this.isComparisonRangeReady()) {
            return _t("Choose a valid comparison start and end date.");
        }
        return _t("Refresh report");
    }

    get inventoryHistory() {
        return this.dashboard.inventory_value?.history || { points: [], series: [] };
    }

    get inventoryHistoryPointsList() {
        return this.inventoryHistory.points || [];
    }

    get inventoryHistorySeries() {
        return this.inventoryHistory.series || [];
    }

    get inventoryHistoryStartLabel() {
        const first = this.inventoryHistoryPointsList[0];
        return first ? first.range_label || first.label || "" : "";
    }

    get inventoryHistoryEndLabel() {
        const points = this.inventoryHistoryPointsList;
        const last = points[points.length - 1];
        if (!last) {
            return "";
        }
        const label = last.range_label || last.label || "";
        return last.partial ? `${label} ${_t("(partial)")}` : label;
    }

    metricDefinition(key) {
        const definitions = this.dashboard.metric_definitions || {};
        return (key && definitions[key]) || "";
    }

    metricTitle(key) {
        return this.metricDefinition(key) || _t("No calculation note is available for this metric.");
    }

    cardTitle(card) {
        const definition = this.metricDefinition(card.metric_key);
        return [card.context_label, card.period_label, definition]
            .filter(Boolean)
            .join(" | ");
    }

    formatDelta(card) {
        if (card.value_type === "percent") {
            return `${card.delta >= 0 ? "+" : ""}${Number(card.delta || 0).toFixed(1)} pts`;
        }
        return `${card.delta >= 0 ? "+" : ""}${this.formatValue(card.delta, card.value_type)}`;
    }

    cardDeltaText(card) {
        if (card.show_delta === false) {
            return card.delta_label || "";
        }
        return `${this.formatDelta(card)} ${card.delta_label || _t("WoW")}`;
    }

    deltaClass(card) {
        const delta = Number(card.delta || 0);
        if (card.show_delta === false) {
            return "o_unico_finance_neutral";
        }
        if (!delta) {
            return "o_unico_finance_neutral";
        }
        const good = card.direction === "good_down" ? delta < 0 : delta > 0;
        return good ? "o_unico_finance_good" : "o_unico_finance_bad";
    }

    barWidth(row, metric) {
        const values = (this.state.dashboard?.weekly || []).map((week) => Math.abs(Number(week[metric] || 0)));
        const max = Math.max(...values, 1);
        return `${Math.max(3, Math.round((Math.abs(Number(row[metric] || 0)) / max) * 100))}%`;
    }

    sortWeekly(columnKey) {
        const current = this.state.weeklySort;
        let direction = columnKey === "key" ? "asc" : "desc";
        if (current.key === columnKey) {
            direction = current.direction === "asc" ? "desc" : "asc";
        }
        this.state.weeklySort = { key: columnKey, direction };
        this.setTablePage("weekly", 1);
    }

    weeklySortValue(row, columnKey) {
        if (columnKey === "key") {
            return row.key || "";
        }
        return Number(row[columnKey] || 0);
    }

    compareWeeklyRows(left, right) {
        const columnKey = this.state.weeklySort.key;
        const leftValue = this.weeklySortValue(left, columnKey);
        const rightValue = this.weeklySortValue(right, columnKey);
        let result = 0;
        if (typeof leftValue === "number" && typeof rightValue === "number") {
            result = leftValue - rightValue;
        } else {
            result = String(leftValue).localeCompare(String(rightValue));
        }
        if (!result && columnKey !== "key") {
            result = String(left.key || "").localeCompare(String(right.key || ""));
        }
        return this.state.weeklySort.direction === "desc" ? -result : result;
    }

    weeklyAriaSort(columnKey) {
        if (this.state.weeklySort.key !== columnKey) {
            return "none";
        }
        return this.state.weeklySort.direction === "desc" ? "descending" : "ascending";
    }

    weeklySortIndicatorClass(columnKey) {
        if (this.state.weeklySort.key !== columnKey) {
            return "is-none";
        }
        return this.state.weeklySort.direction === "desc" ? "is-desc" : "is-asc";
    }

    formatWeeklyCell(row, column) {
        return this.formatValue(row[column.key], column.type || "number");
    }

    sortTable(tableKey, columnKey, type = "text") {
        const current = this.state.tableSorts[tableKey] || {};
        let direction = "asc";
        if (current.key === columnKey) {
            direction = current.direction === "asc" ? "desc" : "asc";
        }
        this.state.tableSorts = {
            ...this.state.tableSorts,
            [tableKey]: { key: columnKey, direction, type },
        };
        this.setTablePage(tableKey, 1);
    }

    tableSortState(tableKey) {
        return this.state.tableSorts[tableKey] || {};
    }

    tableAriaSort(tableKey, columnKey) {
        const sort = this.tableSortState(tableKey);
        if (sort.key !== columnKey) {
            return "none";
        }
        return sort.direction === "desc" ? "descending" : "ascending";
    }

    tableSortIndicatorClass(tableKey, columnKey) {
        const sort = this.tableSortState(tableKey);
        if (sort.key !== columnKey) {
            return "is-none";
        }
        return sort.direction === "desc" ? "is-desc" : "is-asc";
    }

    tableSortValue(row, columnKey, type) {
        const value = row?.[columnKey];
        if (type === "number" || type === "money" || type === "percent") {
            return Number(value || 0);
        }
        return String(value || "").toLocaleLowerCase();
    }

    sortedTableRows(tableKey, rows) {
        const data = [...(rows || [])];
        const sort = this.tableSortState(tableKey);
        if (!sort.key) {
            return data;
        }
        return data.sort((left, right) => {
            const leftValue = this.tableSortValue(left, sort.key, sort.type);
            const rightValue = this.tableSortValue(right, sort.key, sort.type);
            let result = 0;
            if (typeof leftValue === "number" && typeof rightValue === "number") {
                result = leftValue - rightValue;
            } else {
                result = String(leftValue).localeCompare(String(rightValue));
            }
            return sort.direction === "desc" ? -result : result;
        });
    }

    tableRows(tableKey, rows) {
        return this.paginatedRows(tableKey, this.sortedTableRows(tableKey, rows));
    }

    paginatedRows(tableKey, rows) {
        const data = rows || [];
        const page = this.currentTablePage(tableKey, data);
        const start = (page - 1) * TABLE_PAGE_SIZE;
        return data.slice(start, start + TABLE_PAGE_SIZE);
    }

    currentTablePage(tableKey, rows = []) {
        const pageCount = this.tablePageCount(rows);
        const page = Number(this.state.tablePages[tableKey] || 1);
        return Math.min(Math.max(page, 1), pageCount);
    }

    tablePageCount(rows = []) {
        return Math.max(1, Math.ceil((rows || []).length / TABLE_PAGE_SIZE));
    }

    isTablePaginationVisible(rows = []) {
        return (rows || []).length > TABLE_PAGE_SIZE;
    }

    setTablePage(tableKey, page) {
        this.state.tablePages = {
            ...this.state.tablePages,
            [tableKey]: Math.max(1, Number(page || 1)),
        };
    }

    previousTablePage(tableKey) {
        this.setTablePage(tableKey, Number(this.state.tablePages[tableKey] || 1) - 1);
    }

    nextTablePage(tableKey, rows = []) {
        this.setTablePage(
            tableKey,
            Math.min(this.currentTablePage(tableKey, rows) + 1, this.tablePageCount(rows))
        );
    }

    tablePageSummary(tableKey, rows = []) {
        const data = rows || [];
        if (!data.length) {
            return _t("No rows");
        }
        const page = this.currentTablePage(tableKey, data);
        const start = (page - 1) * TABLE_PAGE_SIZE + 1;
        const end = Math.min(start + TABLE_PAGE_SIZE - 1, data.length);
        return `Showing ${start}-${end} of ${data.length}`;
    }

    selectTrendMetric(metricKey) {
        this.state.selectedTrendMetricKey = metricKey;
        this.state.hoveredTrendPoint = null;
    }

    setTrendHover(metric, point, index) {
        const week = this.weekly[index] || {};
        this.state.hoveredTrendPoint = {
            metric,
            point,
            index,
            label: this.weekDisplayLabel(week),
        };
    }

    selectTrendMetricPoint(ev, metric, point, index) {
        ev.stopPropagation();
        this.state.selectedTrendMetricKey = metric.key;
        this.setTrendHover(metric, point, index);
    }

    clearTrendHover() {
        this.state.hoveredTrendPoint = null;
    }

    weekDisplayLabel(week) {
        const label = week.range_label || week.label || "";
        return week.partial ? `${label} ${_t("(partial)")}` : label;
    }

    weekAxisLabel(week) {
        const label = week.label || week.range_label || "";
        return week.partial ? `${label} ${_t("(partial)")}` : label;
    }

    isSelectedTrendMetric(metricKey) {
        return this.selectedTrendMetric.key === metricKey;
    }

    chartValues(metricKey) {
        return this.weekly.map((week) => Number(week[metricKey] || 0));
    }

    trendEndpoints(values) {
        if (values.length <= 1) {
            const value = values[0] || 0;
            return [value, value];
        }
        const count = values.length;
        const sumX = values.reduce((total, _value, index) => total + index, 0);
        const sumY = values.reduce((total, value) => total + value, 0);
        const sumXY = values.reduce((total, value, index) => total + index * value, 0);
        const sumXX = values.reduce((total, _value, index) => total + index * index, 0);
        const denominator = count * sumXX - sumX * sumX;
        if (!denominator) {
            return [values[0] || 0, values[values.length - 1] || 0];
        }
        const slope = (count * sumXY - sumX * sumY) / denominator;
        const intercept = (sumY - slope * sumX) / count;
        return [intercept, intercept + slope * (count - 1)];
    }

    chartScale(metricKey) {
        const values = this.chartValues(metricKey);
        const trend = this.trendEndpoints(values);
        const scaleValues = values.concat(trend);
        let min = Math.min(...scaleValues, 0);
        let max = Math.max(...scaleValues, 0);
        if (min === max) {
            const offset = Math.abs(max || 1) * 0.1;
            min -= offset;
            max += offset;
        } else {
            const offset = (max - min) * 0.08;
            min -= offset;
            max += offset;
        }
        return { min, max, trend };
    }

    chartPoint(value, index, count, scale) {
        const drawableWidth = CHART_WIDTH - CHART_PADDING * 2;
        const drawableHeight = CHART_HEIGHT - CHART_PADDING * 2;
        const x = count <= 1
            ? CHART_WIDTH / 2
            : CHART_PADDING + (index * drawableWidth) / (count - 1);
        const y = CHART_HEIGHT - CHART_PADDING
            - ((value - scale.min) / (scale.max - scale.min)) * drawableHeight;
        return {
            x: Number(x.toFixed(2)),
            y: Number(y.toFixed(2)),
            value,
        };
    }

    pointKey(metricKey, point, index) {
        return `${metricKey}-${index}-${point.x}-${point.y}`;
    }

    chartPoints(metricKey) {
        const values = this.chartValues(metricKey);
        const scale = this.chartScale(metricKey);
        return values.map((value, index) => this.chartPoint(value, index, values.length, scale));
    }

    chartPath(metricKey) {
        const points = this.chartPoints(metricKey);
        if (!points.length) {
            return "";
        }
        return points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ");
    }

    trendPath(metricKey) {
        const values = this.chartValues(metricKey);
        const scale = this.chartScale(metricKey);
        const start = this.chartPoint(scale.trend[0], 0, values.length, scale);
        const end = this.chartPoint(scale.trend[1], Math.max(values.length - 1, 0), values.length, scale);
        return `M ${start.x} ${start.y} L ${end.x} ${end.y}`;
    }

    trendDelta(metricKey) {
        const values = this.chartValues(metricKey);
        if (!values.length) {
            return 0;
        }
        const [first, last] = this.trendEndpoints(values);
        return last - first;
    }

    trendClass(metric) {
        const delta = this.trendDelta(metric.key);
        if (!delta) {
            return "o_unico_finance_neutral";
        }
        const good = metric.direction === "good_down" ? delta < 0 : delta > 0;
        return good ? "o_unico_finance_good" : "o_unico_finance_bad";
    }

    trendLabel(metric) {
        const delta = this.trendDelta(metric.key);
        const prefix = delta > 0 ? "+" : "";
        if (metric.type === "percent") {
            return `${prefix}${delta.toFixed(1)} pts trend`;
        }
        return `${prefix}${this.formatValue(delta, metric.type)} trend`;
    }

    trendMetricValue(metric) {
        if (!this.weekly.length) {
            return 0;
        }
        if (metric.key === "margin_percent") {
            const sales = this.weekly.reduce((total, week) => total + Number(week.sales || 0), 0);
            const grossProfit = this.weekly.reduce(
                (total, week) => total + Number(week.gross_profit || 0),
                0
            );
            return sales ? (grossProfit / sales) * 100 : 0;
        }
        return this.weekly.reduce((total, week) => total + Number(week[metric.key] || 0), 0);
    }

    trendMetricValueLabel(metric) {
        return metric.key === "margin_percent" ? _t("Period Margin") : _t("Period Total");
    }

    metricTimeframeLabel(metric) {
        if (metric.snapshot) {
            return this.summaryContext.snapshot_label || _t("Current snapshot");
        }
        return this.summaryContext.range_dates || this.trendRangeLabel;
    }

    hoverMatches(metric) {
        return this.state.hoveredTrendPoint?.metric?.key === metric.key;
    }

    trendTooltipStyle() {
        const point = this.state.hoveredTrendPoint?.point;
        if (!point) {
            return "";
        }
        const xRatio = point.x / CHART_WIDTH;
        const xPercent = xRatio * 100;
        const xOffset = FOCUS_CHART_LABEL_WIDTH * (1 - xRatio);
        return `left: calc(${xPercent}% + ${xOffset}px); top: ${(point.y / CHART_HEIGHT) * 100}%;`;
    }

    chartBoundLabel(metric, bound) {
        const scale = this.chartScale(metric.key);
        return this.formatValue(scale[bound], metric.type);
    }

    get chartViewBox() {
        return `0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`;
    }

    get chartAxis() {
        return {
            x1: CHART_PADDING,
            x2: CHART_WIDTH - CHART_PADDING,
            y: CHART_HEIGHT - CHART_PADDING,
        };
    }

    get chartGridLines() {
        return [
            CHART_PADDING,
            CHART_PADDING + (CHART_HEIGHT - CHART_PADDING * 2) / 2,
            CHART_HEIGHT - CHART_PADDING,
        ];
    }

    hoverValue() {
        const hover = this.state.hoveredTrendPoint;
        if (!hover) {
            return "";
        }
        return this.formatValue(hover.point.value, hover.metric.type);
    }

    weekCountLabel(count) {
        return count === 1 ? _t("1 week") : `${count} ${_t("weeks")}`;
    }

    get chartStartLabel() {
        const first = this.weekly[0];
        return first ? this.weekAxisLabel(first) : "";
    }

    get chartEndLabel() {
        const last = this.weekly[this.weekly.length - 1];
        return last ? this.weekAxisLabel(last) : "";
    }

    get trendRangeLabel() {
        if (!this.weekly.length) {
            return "";
        }
        if (this.weekly.length === 1) {
            return this.chartStartLabel;
        }
        return `${this.chartStartLabel} through ${this.chartEndLabel}`;
    }

    get trendRangeSummary() {
        const context = this.summaryContext;
        if (context.range_dates && context.comparison_dates) {
            return `${context.week_count_label || this.weekCountLabel(this.weekly.length)} selected: ${context.range_dates}. Compared with ${context.comparison_dates}.`;
        }
        if (!this.trendRangeLabel) {
            return "";
        }
        return `${this.weekCountLabel(this.weekly.length)}: ${this.trendRangeLabel}`;
    }

    get selectedTrendMetric() {
        return (
            this.trendMetrics.find((metric) => metric.key === this.state.selectedTrendMetricKey)
            || this.trendMetrics[0]
        );
    }

    get trendMetrics() {
        return [
            { key: "revenue", label: _t("Invoiced Revenue"), type: "money", direction: "good_up", className: "o_unico_trend_revenue" },
            { key: "confirmed_sales", label: _t("Booked Sales"), type: "money", direction: "good_up", className: "o_unico_trend_confirmed" },
            { key: "gross_profit", label: _t("Estimated Gross Profit"), type: "money", direction: "good_up", className: "o_unico_trend_profit" },
            { key: "margin_percent", label: _t("Estimated Margin %"), type: "percent", direction: "good_up", className: "o_unico_trend_margin" },
            { key: "cogs", label: _t("Posted COGS"), type: "money", direction: "good_down", className: "o_unico_trend_cogs" },
            { key: "credit_notes", label: _t("Credit Memos"), type: "money", direction: "good_down", className: "o_unico_trend_credits" },
            { key: "unpaid_invoices", label: _t("Open AR by Invoice Week"), type: "money", direction: "good_down", className: "o_unico_trend_ar" },
            { key: "cash_collected", label: _t("Cash Collected"), type: "money", direction: "good_up", className: "o_unico_trend_cash" },
            { key: "collected_pre_invoice", label: _t("Pre-Invoice Collections"), type: "money", direction: "good_up", className: "o_unico_trend_preinvoice" },
            { key: "rma_credit_exposure", label: _t("Open RMA Exposure"), type: "money", direction: "good_down", className: "o_unico_trend_returns" },
            { key: "payment_exceptions", label: _t("Payment Exceptions"), type: "number", direction: "good_down", className: "o_unico_trend_exceptions" },
        ];
    }

    get weeklySortColumns() {
        return [
            { key: "key", label: _t("Reporting Week"), className: "", required: true },
            { key: "revenue", label: _t("Invoiced Revenue"), type: "money", className: "text-end", bar: true },
            { key: "confirmed_sales", label: _t("Booked Sales"), type: "money", className: "text-end", bar: true },
            { key: "gross_profit", label: _t("Estimated Gross Profit"), type: "money", className: "text-end" },
            { key: "margin_percent", label: _t("Estimated Margin %"), type: "percent", className: "text-end" },
            { key: "cogs", label: _t("Posted COGS"), type: "money", className: "text-end" },
            { key: "credit_notes", label: _t("Credit Memos"), type: "money", className: "text-end" },
            { key: "refunds", label: _t("Customer Refunds"), type: "money", className: "text-end" },
            { key: "unpaid_invoices", label: _t("Open AR by Invoice Week"), type: "money", className: "text-end" },
            { key: "collected_pre_invoice", label: _t("Pre-Invoice Collections"), type: "money", className: "text-end" },
            { key: "collected_invoiced", label: _t("Invoice Collections"), type: "money", className: "text-end" },
            { key: "rma_credit_exposure", label: _t("Open RMA Exposure"), type: "money", className: "text-end" },
            { key: "unpaid_delivery_orders", label: _t("Unpaid Delivery Orders"), type: "money", className: "text-end" },
            { key: "payment_exceptions", label: _t("Payment Exceptions"), type: "number", className: "text-end" },
        ];
    }

    get visibleWeeklySortColumns() {
        return this.weeklySortColumns.filter((column) => this.isWeeklyColumnVisible(column.key));
    }

    get sidebarSections() {
        return [
            { id: "unico_financial_overview", label: _t("Overview") },
            { id: "unico_financial_open_sales_orders", label: _t("Open Sales Orders") },
            { id: "unico_financial_sales_conversion", label: _t("Sales Conversion") },
            { id: "unico_financial_trends", label: _t("Trend Explorer") },
            { id: "unico_financial_weekly", label: _t("Week Over Week") },
            { id: "unico_financial_cogs", label: _t("Posted COGS") },
            { id: "unico_financial_inventory", label: _t("Inventory") },
            { id: "unico_financial_ar_aging", label: _t("AR Aging") },
            { id: "unico_financial_ar_details", label: _t("Customer Balances") },
            { id: "unico_financial_ap_aging", label: _t("AP Aging") },
            { id: "unico_financial_ap_details", label: _t("Vendor Payables") },
            { id: "unico_financial_operations", label: _t("Returns & Payments") },
            { id: "unico_financial_delivery", label: _t("Unpaid Delivery") },
        ];
    }

    get sortedWeekly() {
        return [...this.weekly].sort((left, right) => this.compareWeeklyRows(left, right));
    }

    get dashboard() {
        return this.state.dashboard || {};
    }

    get summaryContext() {
        return this.dashboard.summary_context || {};
    }

    get weekly() {
        return this.dashboard.weekly || [];
    }

    get title() {
        return _t("Financial KPI Dashboard");
    }
}

registry.category("actions").add(
    "unico_financial_dashboard.dashboard",
    UnicoFinancialDashboard
);
