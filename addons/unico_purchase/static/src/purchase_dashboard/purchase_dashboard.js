import { patch } from "@web/core/utils/patch";
import { PurchaseDashBoard } from "@purchase/views/purchase_dashboard";

/**
 * Keep the sale-status restriction when a dashboard tile is clicked.
 *
 * The RFQ and Purchase Order actions open on "Ready to Order (Confirmed Sale)"
 * so a buyer sees demand that is actually orderable. Every dashboard tile runs
 * through `setSearchContext`, which wipes the whole query before toggling its
 * own filter, so clicking New or Late RFQ silently dropped that restriction and
 * brought quotations and cancelled sales back into the list.
 *
 * The filter is re-applied only when it was active at click time. A buyer who
 * deliberately switched it off keeps their view; nothing is forced back on.
 */
const STICKY_FILTER_NAMES = ["filter_unico_sale_confirmed"];

patch(PurchaseDashBoard.prototype, {
    setSearchContext(ev) {
        const searchModel = this.env.searchModel;
        // Capture before super: it clears `query` as its first act.
        const stickyItems = searchModel
            .getSearchItems((item) => STICKY_FILTER_NAMES.includes(item.name))
            .filter((item) =>
                searchModel.query.some((queryElem) => queryElem.searchItemId === item.id)
            );

        super.setSearchContext(ev);

        for (const item of stickyItems) {
            searchModel.toggleSearchItem(item.id);
        }
    },
});
