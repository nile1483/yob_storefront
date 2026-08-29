// Copyright (c) 2026, YOB and Shayona
//
// Hides the column-label fields and grid cells beyond `column_count`, so a
// two-column table is edited as a two-column grid rather than six with four
// blank ones.
//
// Presentation only. The controller re-checks the bound, requires a label for
// every active column and CLEARS the cells past it on save, so a table narrowed
// from six to three carries no stale text -- and Data Import never loads this
// file. No custom grid framework: this only toggles `hidden` on fields Frappe
// already renders.

frappe.ui.form.on('YOB Storefront Product Table', {
	refresh: apply_column_count,
	column_count: apply_column_count,
});

function apply_column_count(frm) {
	const active = parseInt(frm.doc.column_count, 10) || 2;

	for (let n = 1; n <= 6; n++) {
		// Columns 1 and 2 always exist -- two is the minimum.
		frm.set_df_property(`column_${n}_label`, 'hidden', n > active);
		frm.set_df_property(`column_${n}_label`, 'reqd', n <= active);

		const cell = frappe.meta.get_docfield(
			'YOB Storefront Product Table Row', `col_${n}`, frm.doc.name);

		if (cell) {
			cell.hidden = n > active;
			// The grid shows `in_list_view` columns; drop the inactive ones out
			// of it so the row editor is exactly as wide as the table.
			cell.in_list_view = n <= active ? 1 : 0;
			cell.label = frm.doc[`column_${n}_label`] || `Column ${n}`;
		}
	}

	frm.refresh_field('rows');
}
