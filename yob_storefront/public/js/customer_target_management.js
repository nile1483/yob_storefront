// Copyright (c) 2026, YOB and Shayona
//
// A Target Management entry point on Customer.
//
// WHY A TOOLBAR BUTTON RATHER THAN A DASHBOARD LINK
// -------------------------------------------------
// Frappe's `override_doctype_dashboards` hook REPLACES a DocType's whole
// dashboard, so adding one YOB link would mean taking ownership of ERPNext's
// entire Customer connections panel and re-declaring every link it ships. That
// breaks the moment ERPNext adds one, and `AGENTS.md` forbids editing dependency
// apps to achieve YOB behaviour.
//
// `doctype_js` is the convention this app already uses for Item, is additive,
// and cannot be invalidated by an upstream change. The button simply routes to
// the report with the customer pre-filled, so the report stays the single
// implementation and no data is read here.
frappe.ui.form.on('Customer', {
  refresh(frm) {
    if (frm.is_new()) {
      return;
    }

    // UX HARDENING, NOT A SECURITY CONTROL.
    //
    // The report enforces access server-side through its own `roles` and
    // `ref_doctype`, so a user without them cannot open it however they arrive.
    // What this avoids is offering a button that would then be refused.
    //
    // Two conditions, because either one alone is insufficient: the role is what
    // the report requires, and `frappe.perm` is what Frappe itself would consult
    // if the report's roles were edited on a site. Asking both means the button
    // tracks the real permission rather than a hard-coded assumption about it.
    const hasRole = (frappe.user_roles || []).includes('System Manager');

    const canRead = frappe.perm
      ? frappe.perm.has_perm('YOB Customer Target', 0, 'read')
      : hasRole;

    if (!hasRole || !canRead) {
      return;
    }

    frm.add_custom_button(
      __('Target Management'),
      () => {
        frappe.set_route('query-report', 'YOB Customer Target Report', {
          customer: frm.doc.name,
        });
      },
      __('View'),
    );
  },
});
