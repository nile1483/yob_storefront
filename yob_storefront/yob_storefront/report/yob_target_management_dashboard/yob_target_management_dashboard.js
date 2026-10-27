// Copyright (c) 2026, YOB and Shayona
//
// Cross-customer target management. ONE report with a `view` switch rather than
// five reports: the KPI cards and the achievement distribution are the same
// question in every view, and only the table body changes.
frappe.query_reports['YOB Target Management Dashboard'] = {
  filters: [
    {
      fieldname: 'view',
      label: __('View'),
      fieldtype: 'Select',
      default: 'Overview',
      reqd: 1,
      options: ['Overview', 'Near Target', 'At Risk', 'Ending Soon', 'Rule Performance'],
    },
    {
      fieldname: 'company',
      label: __('Company'),
      fieldtype: 'Link',
      options: 'Company',
      // Not required, but selecting it is what collapses the monetary KPIs into a
      // single currency. Without it they are reported per currency.
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
      fieldname: 'customer',
      label: __('Customer'),
      fieldtype: 'Link',
      options: 'Customer',
    },
    {
      fieldname: 'customer_group',
      label: __('Customer Group'),
      fieldtype: 'Link',
      options: 'Customer Group',
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
    {
      fieldname: 'scope_value',
      label: __('Scope Value'),
      fieldtype: 'Data',
    },
    {
      fieldname: 'near_target_floor',
      label: __('Near Target From %'),
      fieldtype: 'Float',
      default: 80,
      // Configurable rather than hard-coded: where "close enough to chase"
      // starts is a sales decision, not a backend truth.
    },
    {
      fieldname: 'horizon_days',
      label: __('Ending Within (Days)'),
      fieldtype: 'Int',
      default: 7,
    },
    {
      fieldname: 'at_risk_ceiling',
      label: __('At Risk Below %'),
      fieldtype: 'Float',
      default: 75,
    },
  ],

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
      return `<span style="color:var(--${colour}-600)">${formatted}</span>`;
    }

    // Urgency, not achievement: a deadline inside three days is worth seeing
    // even on a healthy row.
    if (column.fieldname === 'days_remaining' && data && data.days_remaining !== null) {
      const days = cint(data.days_remaining);
      const colour = days <= 3 ? 'red' : days <= 7 ? 'orange' : 'gray';
      return `<span style="color:var(--${colour}-600)">${formatted}</span>`;
    }

    return formatted;
  },
};
