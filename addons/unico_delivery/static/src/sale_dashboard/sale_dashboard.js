import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, onWillUpdateProps } from "@odoo/owl";

/**
 * Dashboard banner shown on top of the Sales Orders list view, mirroring the
 * Purchase RFQ dashboard. Each tile shows a count and, on click, clears the
 * current search and toggles the matching (invisible) search-view filter so the
 * list shows exactly that subset.
 */
export class SaleDashBoard extends Component {
    static template = "unico_delivery.SaleDashboard";
    static props = { list: { type: Object, optional: true } };

    setup() {
        this.orm = useService("orm");
        this.saleData = {};
        onWillStart(() => this.updateDashboardState());
        onWillUpdateProps(() => this.updateDashboardState());
    }

    async updateDashboardState() {
        this.saleData = await this.orm.call("sale.order", "retrieve_dashboard_unico");
    }

    setSearchContext(ev) {
        const filterName = ev.currentTarget.getAttribute("filter_name");
        const filters = filterName.split(",");
        const searchItems = this.env.searchModel.getSearchItems((item) =>
            filters.includes(item.name)
        );
        this.env.searchModel.query = [];
        for (const item of searchItems) {
            this.env.searchModel.toggleSearchItem(item.id);
        }
    }
}
