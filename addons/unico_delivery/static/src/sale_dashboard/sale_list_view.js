import { registry } from "@web/core/registry";
import { saleFileUploadListView } from "@sale/views/sale_file_upload_list/sale_file_upload_list_view";
import { SaleFileUploadListRenderer } from "@sale/views/sale_file_upload_list/sale_file_upload_list_renderer";
import { SaleListView } from "@sale/views/sale_onboarding_list/sale_onboarding_list_view";
import { SaleListRenderer } from "@sale/views/sale_onboarding_list/sale_onboarding_list_renderer";
import { SaleDashBoard } from "./sale_dashboard";

// Sales Orders list (file-upload variant).
export class SaleDashBoardListRenderer extends SaleFileUploadListRenderer {
    static template = "unico_delivery.SaleListRenderer";
    static components = Object.assign({}, SaleFileUploadListRenderer.components, {
        SaleDashBoard,
    });
}

export const SaleDashBoardListView = {
    ...saleFileUploadListView,
    Renderer: SaleDashBoardListRenderer,
};

registry.category("views").add("sale_dashboard_list", SaleDashBoardListView);

// Quotations list (onboarding variant) — keep the onboarding action helper.
export class SaleOnboardingDashBoardListRenderer extends SaleListRenderer {
    static template = "unico_delivery.SaleOnboardingListRenderer";
    static components = Object.assign({}, SaleListRenderer.components, {
        SaleDashBoard,
    });
}

export const SaleOnboardingDashBoardListView = {
    ...SaleListView,
    Renderer: SaleOnboardingDashBoardListRenderer,
};

registry.category("views").add("sale_onboarding_dashboard_list", SaleOnboardingDashBoardListView);
