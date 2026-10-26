# Copyright (c) 2026, YOB and Shayona
"""Target Period: WHICH TRANSACTION DATES COUNT. That is its whole job.

It does not decide when a calculation runs -- `YOB Target Rule.report_start_date`
and `report_end_date` do that, and they are a different question with different
dates. Conflating the two is the defect this model exists to keep apart.

A period may deliberately stop before a calendar month ends: "January target"
counting only 1 - 25 January is a normal configuration, not a mistake.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class YOBTargetPeriod(Document):
    def validate(self):
        self.validate_dates()

    def validate_dates(self):
        """Both dates are required and ordered.

        Server-side, not `depends_on`: a period with no end date would make the
        qualifying window unbounded, and every achievement query built from it
        would silently count the wrong transactions.
        """

        if not self.start_date:
            frappe.throw(_("Start Date is required."), frappe.ValidationError)

        if not self.end_date:
            frappe.throw(_("End Date is required."), frappe.ValidationError)

        if getdate(self.start_date) > getdate(self.end_date):
            frappe.throw(
                _("Start Date {0} cannot be after End Date {1}.").format(
                    self.start_date, self.end_date),
                frappe.ValidationError,
            )
