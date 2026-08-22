// Copyright (c) 2026, YOB and Shayona
//
// Desk convenience for Content Placements: show a merchant only the positions
// the chosen route actually renders, and show them by their friendly label while
// the field stores the machine key.
//
// Convenience ONLY. `YOB Storefront Content Placement.validate` re-checks the
// (route, slot) pair against the same registry on every save, and Data Import,
// the REST API and `bench execute` never run this file. Nothing here is a rule;
// the server owns every rule.
//
// The options come from the server rather than being repeated here, so the
// registry has exactly one definition. A second copy in JavaScript would drift
// the first time a slot was added, and the drift would look like a working UI.

frappe.ui.form.on('YOB Storefront Content Placement', {
	refresh(frm) {
		load_slot_options(frm);
	},

	route_key(frm) {
		// The route changed, so the stored position probably belongs to the page
		// the merchant just navigated away from. Clear it rather than leave a
		// value that will be refused on save -- `cart` + `hero` looks filled in
		// and is not valid.
		if (frm.doc.slot_key) {
			frm.set_value('slot_key', '');
		}

		load_slot_options(frm);
	},
});

function load_slot_options(frm) {
	if (!frm.doc.route_key) {
		set_options(frm, []);
		return;
	}

	frappe.call({
		method: 'yob_storefront.desk.content_slots.get_slot_options',
		args: { route_key: frm.doc.route_key },
		callback(response) {
			set_options(frm, response.message || []);
		},
	});
}

function set_options(frm, options) {
	// `\n` first so the field can legitimately be empty until a choice is made.
	frm.set_df_property(
		'slot_key',
		'options',
		[''].concat(options.map((option) => option.value)).join('\n'),
	);

	// Frappe renders a Select by its stored value, so the friendly labels are
	// carried as the option text through the underlying control where available.
	const field = frm.get_field('slot_key');

	if (field && field.$input) {
		options.forEach((option) => {
			field.$input
				.find(`option[value="${option.value}"]`)
				.text(option.label);
		});
	}

	frm.refresh_field('slot_key');
}
