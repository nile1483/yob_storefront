// Copyright (c) 2026, YOB and Shayona
//
// Desk convenience for maintaining an Item's merchandising filters.
//
// THIS IS NOT VALIDATION. Every rule it helps a user follow is enforced again on
// the server (`utils/item_storefront_filters.py`), because Data Import, the REST
// API and a grid paste never run this file. What it does is stop an
// administrator from being offered a choice the server would then refuse.

frappe.ui.form.on("Item", {
	setup(frm) {
		// Filters offered = those in the Item's own Filter Set (the admin scope).
		frm.set_query("filter", "custom_storefront_filters", (doc) => {
			if (!doc.custom_storefront_filter_set) {
				// No scope chosen yet: offer nothing rather than all hundred.
				return { filters: { name: ["in", []] } };
			}

			return {
				query: "yob_storefront.desk.filter_queries.filters_in_set",
				filters: { filter_set: doc.custom_storefront_filter_set },
			};
		});

		// Values offered = those of the row's own Filter.
		frm.set_query("filter_value", "custom_storefront_filters", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];

			if (!row || !row.filter) {
				return { filters: { name: ["in", []] } };
			}

			return { filters: { filter: row.filter, enabled: 1 } };
		});
	},

	custom_storefront_filter_set(frm) {
		// Rows are NOT deleted. Changing the scope may orphan existing rows, and
		// silently discarding merchant data is worse than showing the problem:
		// the server will name any row that no longer belongs, and the operator
		// decides what to keep.
		const rows = frm.doc.custom_storefront_filters || [];

		if (!rows.length) return;

		frappe.show_alert({
			message: __(
				"Filter Set changed. Existing filter rows are kept — remove any that no longer belong before saving."
			),
			indicator: "orange",
		});
	},
});

frappe.ui.form.on("YOB Storefront Item Filter", {
	filter(frm, cdt, cdn) {
		// A value from the previous Filter cannot belong to the new one.
		const row = locals[cdt][cdn];

		if (row && row.filter_value) {
			frappe.model.set_value(cdt, cdn, "filter_value", null);
		}
	},
});
