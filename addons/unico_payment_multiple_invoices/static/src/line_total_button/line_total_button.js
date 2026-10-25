import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

/**
 * Open the Set Line Total popup from an order line row.
 *
 * A plain `<button type="object">` cannot do this. Odoo refuses to run an
 * object button on an x2many row that has never been saved — see
 * `ListRenderer.displaySaveNotification` — because the line has no id yet to
 * pass to the server, so a rep who adds a line and reaches straight for the
 * calculator is told to save first. Saving the quotation is not a decision
 * worth asking about, so this widget just does it, then calls the method with
 * the id that now exists.
 */
export class LineTotalButton extends Component {
    static template = "unico_payment_multiple_invoices.LineTotalButton";
    static props = { ...standardWidgetProps };

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
    }

    get isAvailable() {
        return Boolean(this.props.record.data.unico_can_set_line_total);
    }

    async onClick() {
        const record = this.props.record;
        const root = record.model.root;
        if (!(await root.save())) {
            return;
        }
        if (!record.resId) {
            return;
        }
        const action = await this.orm.call(
            "sale.order.line",
            "action_open_line_total_wizard",
            [record.resId],
        );
        this.action.doAction(action, {
            // The wizard writes price_unit straight to the line, so the form
            // in the background is stale until it is reloaded.
            onClose: () => root.load(),
        });
    }
}

export const lineTotalButton = {
    component: LineTotalButton,
    fieldDependencies: [{ name: "unico_can_set_line_total", type: "boolean" }],
};

registry.category("view_widgets").add("unico_line_total_button", lineTotalButton);
