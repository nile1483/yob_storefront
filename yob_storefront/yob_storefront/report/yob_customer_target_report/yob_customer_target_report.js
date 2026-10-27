// Copyright (c) 2026, YOB and Shayona
//
// One customer's targets. `customer` is the only required filter: the report is
// customer-scoped by definition, and the cross-customer view is the Target
// Management Dashboard.
frappe.query_reports['YOB Customer Target Report'] = {
  filters: [
    {
      fieldname: 'customer',
      label: __('Customer'),
      fieldtype: 'Link',
      options: 'Customer',
      reqd: 1,
    },
    {
      fieldname: 'company',
      label: __('Company'),
      fieldtype: 'Link',
      options: 'Company',
    },
    {
      fieldname: 'target_period',
      label: __('Target Period'),
      fieldtype: 'Link',
      options: 'YOB Target Period',
    },
    {
      fieldname: 'target_rule',
      label: __('Target Rule'),
      fieldtype: 'Link',
      options: 'YOB Target Rule',
    },
    {
      fieldname: 'status',
      label: __('Status'),
      fieldtype: 'Select',
      options: ['', 'Not Started', 'In Progress', 'Achieved', 'Expired', 'Completed'],
    },
    {
      fieldname: 'target_type',
      label: __('Target Type'),
      fieldtype: 'Select',
      options: ['', 'Amount', 'Quantity'],
    },
    {
      fieldname: 'target_scope',
      label: __('Target Scope'),
      fieldtype: 'Select',
      options: ['', 'Item', 'Item Group', 'Brand', 'Total Sales Order'],
    },
  ],

  // Achievement is NOT capped by the backend, so the colouring must not imply a
  // ceiling either: anything at or above 100% is simply green.
  formatter(value, row, column, data, default_formatter) {
    const formatted = default_formatter(value, row, column, data);

    // A stale figure must look stale. The row stays visible because the
    // administrator is the person who has to act on it.
    if (column.fieldname === 'calculation_state' && data) {
      if (data.calculation_state !== 'OK') {
        return `<span style="color:var(--red-600);font-weight:600">${formatted}</span>`;
      }
      return `<span style="color:var(--gray-500)">${formatted}</span>`;
    }

    // Any number derived from a failed calculation is shown struck through, so
    // it cannot be read as current even at a glance.
    if (
      data &&
      data.calculation_state &&
      data.calculation_state !== 'OK' &&
      ['achievement_percentage', 'achieved_amount', 'achieved_quantity',
       'remaining_amount', 'remaining_quantity', 'final_incentive',
       'current_slab'].includes(column.fieldname)
    ) {
      return `<span style="color:var(--gray-600);text-decoration:line-through">${formatted}</span>`;
    }

    if (column.fieldname === 'achievement_percentage' && data) {
      const pct = flt(data.achievement_percentage);
      const colour = pct >= 100 ? 'green' : pct >= 80 ? 'orange' : 'red';
      return `<span style="color:var(--text-on-light);font-weight:500">
                <span style="color:var(--${colour}-600)">${formatted}</span></span>`;
    }

    if (column.fieldname === 'status' && data) {
      const tone = {
        Achieved: 'green',
        Completed: 'blue',
        'In Progress': 'orange',
        Expired: 'red',
        'Not Started': 'gray',
      }[data.status];
      if (tone) {
        return `<span style="color:var(--${tone}-600)">${formatted}</span>`;
      }
    }

    return formatted;
  },
};
