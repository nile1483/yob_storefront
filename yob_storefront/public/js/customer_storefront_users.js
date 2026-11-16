// Copyright (c) 2026, YOB and Shayona
//
// Customer > Portal Users > YOB Storefront Users.
//
// WHY AN HTML FIELD RATHER THAN A CHILD TABLE
// -------------------------------------------
// A child table on Customer would be a SECOND COPY of `YOB User Application
// Access`, saved by Customer and reconciled on every Customer save -- including
// the overwhelming majority that never open this tab. Two copies of a security
// grant that can disagree is a security bug, not a display bug.
//
// So this section owns no data. It reads the real access records on every
// render and writes through `yob_storefront.desk.storefront_users`, whose
// methods go through the real DocType -- which is what keeps the existing
// `YOB Storefront Buyer` role-sync hooks in charge of the role.
//
// Nothing here is a security control. Every method re-checks permissions
// server-side; the gating below only avoids offering an action that would then
// be refused.
//
// Follows the same `doctype_js` + HTML-field pattern as
// `item_storefront_content.js`.

const API = 'yob_storefront.desk.storefront_users';

const STATUS_STYLE = {
	Active: 'green',
	Disabled: 'gray',
	Scheduled: 'blue',
	Expired: 'orange',
	'User Disabled': 'red',
};

frappe.ui.form.on('Customer', {
	refresh(frm) {
		render_storefront_users(frm);
	},
});

function can_manage() {
	// Mirrors the server guard exactly: create on the grant (System Manager
	// only) plus READ on the Customer. Not Customer write -- nothing here edits
	// the customer master, and requiring it would hide the panel from the very
	// role that owns storefront access.
	if (!frappe.perm) {
		return false;
	}

	return (
		frappe.perm.has_perm('YOB User Application Access', 0, 'create') &&
		frappe.perm.has_perm('Customer', 0, 'read')
	);
}

function can_view() {
	return frappe.perm && frappe.perm.has_perm('YOB User Application Access', 0, 'read');
}

function render_storefront_users(frm) {
	const field = frm.get_field('custom_yob_storefront_users_html');

	if (!field || !field.$wrapper) {
		return;
	}

	const $w = field.$wrapper;
	$w.empty();

	// A Customer in `Not Saved` state has a provisional name. Pointing a grant
	// at it would create access to a Customer that may never exist, so the
	// controls are withheld and the reason is stated. The server refuses it too.
	if (frm.is_new()) {
		$w.html(
			`<p class="text-muted">${__('Save the Customer before adding storefront users.')}</p>`,
		);
		return;
	}

	if (!can_view()) {
		$w.html(
			`<p class="text-muted">${__('You do not have permission to view storefront access.')}</p>`,
		);
		return;
	}

	frappe
		.call({ method: `${API}.list_storefront_users`, args: { customer: frm.doc.name } })
		.then((r) => draw(frm, $w, r.message || []))
		.catch(() => {
			$w.html(
				`<p class="text-muted">${__('Storefront access could not be loaded.')}</p>`,
			);
		});
}

function draw(frm, $w, rows) {
	const manage = can_manage();

	const body = rows.length
		? rows.map((row) => row_html(row, manage)).join('')
		: `<tr><td colspan="${manage ? 6 : 5}" class="text-muted text-center" style="padding:12px">
				${__('No storefront users yet.')}
			</td></tr>`;

	$w.html(`
		<div class="yob-storefront-users">
			<table class="table table-bordered" style="margin-bottom:8px">
				<thead>
					<tr>
						<th style="width:30%">${__('User')}</th>
						<th style="width:10%">${__('Enabled')}</th>
						<th style="width:18%">${__('Valid From')}</th>
						<th style="width:18%">${__('Valid Until')}</th>
						<th style="width:14%">${__('Status')}</th>
						${manage ? `<th style="width:10%"></th>` : ''}
					</tr>
				</thead>
				<tbody>${body}</tbody>
			</table>
			${
				manage
					? `<button class="btn btn-default btn-sm" data-action="add">${__('Add Existing User')}</button>
					   <button class="btn btn-default btn-sm" data-action="create">${__('Create Website User')}</button>`
					: ''
			}
			<p class="text-muted small" style="margin-top:8px">
				${__('Storefront access is stored in YOB User Application Access. The YOB Storefront Buyer role is granted and revoked automatically.')}
			</p>
		</div>
	`);

	if (!manage) {
		return;
	}

	$w.find('[data-action="add"]').on('click', () => add_existing_dialog(frm));
	$w.find('[data-action="create"]').on('click', () => create_user_dialog(frm));
	$w.find('[data-action="edit"]').on('click', (e) =>
		edit_dialog(frm, rows.find((x) => x.name === $(e.currentTarget).data('name'))),
	);
	$w.find('[data-action="remove"]').on('click', (e) =>
		remove(frm, rows.find((x) => x.name === $(e.currentTarget).data('name'))),
	);
}

function row_html(row, manage) {
	const esc = frappe.utils.escape_html;
	const name_line = row.full_name
		? `<div class="text-muted small">${esc(row.full_name)}</div>`
		: '';

	// Reported, never edited here: the role follows the grant through the
	// existing hooks. Showing a grant that is enabled while the role is missing
	// is how an administrator notices a hook that did not run.
	const role_warning =
		row.enabled && !row.has_buyer_role
			? `<div class="text-danger small">${__('Buyer role missing')}</div>`
			: '';

	return `
		<tr>
			<td>${esc(row.user)}${name_line}${role_warning}</td>
			<td>${row.enabled ? __('Yes') : __('No')}</td>
			<td>${row.valid_from ? frappe.datetime.str_to_user(row.valid_from) : '<span class="text-muted">—</span>'}</td>
			<td>${row.valid_until ? frappe.datetime.str_to_user(row.valid_until) : '<span class="text-muted">—</span>'}</td>
			<td><span class="indicator-pill ${STATUS_STYLE[row.status] || 'gray'}">${__(row.status)}</span></td>
			${
				manage
					? `<td style="white-space:nowrap">
							<button class="btn btn-xs btn-default" data-action="edit" data-name="${esc(row.name)}">${__('Edit')}</button>
							<button class="btn btn-xs btn-default" data-action="remove" data-name="${esc(row.name)}">${__('Remove')}</button>
						</td>`
					: ''
			}
		</tr>
	`;
}

function done(frm, message) {
	frappe.show_alert({ message, indicator: 'green' });
	// Only this section is redrawn -- the rest of the Customer form, and any
	// unsaved edit on it, is left exactly as it was.
	render_storefront_users(frm);
}

function add_existing_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Add Storefront User'),
		fields: [
			{
				fieldname: 'user',
				fieldtype: 'Link',
				label: __('User'),
				options: 'User',
				reqd: 1,
				// Website Users only, matching what the server enforces. A
				// System User is refused server-side with an explanation; not
				// offering it here saves the administrator the round trip.
				get_query: () => ({ filters: { user_type: 'Website User', enabled: 1 } }),
			},
			{ fieldname: 'cb', fieldtype: 'Column Break' },
			{ fieldname: 'enabled', fieldtype: 'Check', label: __('Enabled'), default: 1 },
			{ fieldname: 'sb', fieldtype: 'Section Break' },
			{ fieldname: 'valid_from', fieldtype: 'Datetime', label: __('Valid From') },
			{ fieldname: 'cb2', fieldtype: 'Column Break' },
			{ fieldname: 'valid_until', fieldtype: 'Datetime', label: __('Valid Until') },
		],
		primary_action_label: __('Add'),
		primary_action(values) {
			frappe
				.call({
					method: `${API}.add_storefront_user`,
					args: { customer: frm.doc.name, ...values },
				})
				.then((r) => {
					if (!r.message) return;
					d.hide();
					done(frm, __('Storefront access granted to {0}.', [r.message.user]));
				});
		},
	});

	d.show();
}

function create_user_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Create Storefront User'),
		fields: [
			{ fieldname: 'first_name', fieldtype: 'Data', label: __('First Name'), reqd: 1 },
			{ fieldname: 'cb', fieldtype: 'Column Break' },
			{ fieldname: 'email', fieldtype: 'Data', label: __('Email'), options: 'Email', reqd: 1 },
			{ fieldname: 'sb', fieldtype: 'Section Break' },
			{
				fieldname: 'send_welcome_email',
				fieldtype: 'Check',
				label: __('Send Welcome Email'),
				default: 1,
				description: __('Emails the buyer a link to set their own password.'),
			},
			{
				fieldname: 'help',
				fieldtype: 'HTML',
				options: `<p class="text-muted small">${__(
					'Creates a Website User and grants it storefront access to this Customer. No Customer is created.',
				)}</p>`,
			},
		],
		primary_action_label: __('Create'),
		primary_action(values) {
			frappe
				.call({
					method: `${API}.create_storefront_user`,
					args: { customer: frm.doc.name, ...values },
				})
				.then((r) => {
					if (!r.message) return;
					d.hide();
					done(frm, __('Website User {0} created with storefront access.', [r.message.user]));
				});
		},
	});

	d.show();
}

function edit_dialog(frm, row) {
	if (!row) {
		return;
	}

	const d = new frappe.ui.Dialog({
		title: __('Edit Storefront Access'),
		fields: [
			{
				fieldname: 'user_display',
				fieldtype: 'Data',
				label: __('User'),
				default: row.user,
				read_only: 1,
				// Changing the user is a different grant, so it is a remove plus
				// an add -- deliberately visible as two acts, not one silent edit.
				description: __('Remove this access and add another to change the user.'),
			},
			{ fieldname: 'enabled', fieldtype: 'Check', label: __('Enabled'), default: row.enabled },
			{ fieldname: 'sb', fieldtype: 'Section Break' },
			{ fieldname: 'valid_from', fieldtype: 'Datetime', label: __('Valid From'), default: row.valid_from },
			{ fieldname: 'cb', fieldtype: 'Column Break' },
			{ fieldname: 'valid_until', fieldtype: 'Datetime', label: __('Valid Until'), default: row.valid_until },
		],
		primary_action_label: __('Update'),
		primary_action(values) {
			frappe
				.call({
					method: `${API}.update_storefront_user`,
					args: {
						name: row.name,
						enabled: values.enabled,
						// Always sent, as "" when cleared, so the server can tell
						// "cleared" from "not touched".
						valid_from: values.valid_from || '',
						valid_until: values.valid_until || '',
					},
				})
				.then((r) => {
					if (!r.message) return;
					d.hide();
					done(frm, __('Storefront access updated.'));
				});
		},
	});

	d.show();
}

function remove(frm, row) {
	if (!row) {
		return;
	}

	frappe.confirm(
		__(
			'Remove storefront access for {0}?<br><br>This removes storefront access only. The User, Contact and Address are kept.',
			[frappe.utils.escape_html(row.user)],
		),
		() => {
			frappe
				.call({ method: `${API}.remove_storefront_user`, args: { name: row.name } })
				.then((r) => {
					if (!r.message) return;
					done(frm, __('Storefront access removed for {0}.', [r.message.user]));
				});
		},
	);
}
