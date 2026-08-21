// Copyright (c) 2026, YOB and Shayona
//
// Desk tree for storefront menus. Ships with the app rather than as a Client
// Script record, so it arrives with a fresh install and is reviewable in a diff.

frappe.provide("frappe.treeview_settings");

frappe.treeview_settings["YOB Storefront Menu Item"] = {
	breadcrumb: "YOB Storefront",
	title: __("Storefront Menu Items"),
	get_tree_root: true,
	root_label: "All Menu Items",
	ignore_fields: ["parent_yob_storefront_menu_item"],
	get_tree_nodes:
		"yob_storefront.desk.menu_tree.get_children",
	add_tree_node: "yob_storefront.desk.menu_tree.add_node",
	filters: [
		{
			fieldname: "menu",
			fieldtype: "Link",
			options: "YOB Storefront Menu",
			label: __("Menu"),
		},
	],
	onrender(node) {
		// Only a Group may hold children, and only at the root: hiding the "+"
		// on anything else keeps the one-level rule visible in the UI instead of
		// letting a user discover it through a validation error.
		if (!node.is_root && !node.data.expandable) {
			node.hide_add = true;
		}
	},
	post_render(treeview) {
		frappe.treeview_settings["YOB Storefront Menu Item"].treeview = treeview;
	},
};
