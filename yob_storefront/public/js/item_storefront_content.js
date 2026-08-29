// Copyright (c) 2026, YOB and Shayona
//
// Item > Storefront > Product Content.
//
// Sections are standalone documents because Frappe supports only one level of
// child table, so this panel is the bridge back to the Item workflow: a merchant
// merchandising a product should never have to go hunting through a generic
// list to find its content.
//
// It shows how many sections this product has and gives two buttons scoped to
// THIS Item -- open them, or add one with `item` already filled in. Editing
// happens in the Section document itself, which is where the ordered block grid
// lives.
//
// Convenience only. The Section controller re-validates ownership on every save,
// and Data Import and the REST API never load this file.

frappe.ui.form.on('Item', {
	refresh(frm) {
		render_product_content(frm);
	},
});

function render_product_content(frm) {
	const wrapper = frm.get_field('custom_storefront_content_html');

	if (!wrapper || !wrapper.$wrapper) {
		return;
	}

	if (frm.is_new()) {
		wrapper.$wrapper.html(
			`<p class="text-muted">${__('Save this product before adding content sections.')}</p>`,
		);
		return;
	}

	// A generated variant owns no merchandising at all, so offering the buttons
	// would invite a save the server is going to refuse. Say why, and point at
	// the template that does own it.
	if (frm.doc.variant_of) {
		wrapper.$wrapper.html(`
			<p class="text-muted">
				${__('This is a variant of {0}. Product content belongs to the family template, which owns the whole family\'s page.',
					[frappe.utils.escape_html(frm.doc.variant_of)])}
			</p>
			<button class="btn btn-default btn-sm">${__('Open the template')}</button>
		`);
		wrapper.$wrapper.find('button').on('click', () =>
			frappe.set_route('Form', 'Item', frm.doc.variant_of));
		return;
	}

	frappe.db
		.count('YOB Storefront Product Content Section', { filters: { item: frm.doc.name } })
		.then((count) => {
			wrapper.$wrapper.html(`
				<p class="text-muted">${
					count
						? __('This product has {0} content section(s).', [count])
						: __('This product has no content sections yet.')
				}</p>
				<button class="btn btn-default btn-sm" data-action="open">${__('Manage sections')}</button>
				<button class="btn btn-primary btn-sm" data-action="add">${__('Add section')}</button>
			`);

			wrapper.$wrapper.find('[data-action="open"]').on('click', () =>
				frappe.set_route('List', 'YOB Storefront Product Content Section',
					{ item: frm.doc.name }));

			// `route_options` pre-fills the link, so a merchant never types the
			// product code again and cannot attach the section to the wrong one.
			wrapper.$wrapper.find('[data-action="add"]').on('click', () => {
				frappe.route_options = { item: frm.doc.name };
				frappe.new_doc('YOB Storefront Product Content Section');
			});
		});
}
