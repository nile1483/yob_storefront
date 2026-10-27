# Copyright (c) 2026, YOB and Shayona
"""One Target Rule a block exposes, and the dates it is displayed between.

These are STOREFRONT VISIBILITY dates and nothing else. Validated by the parent
block, which is where duplicate rows and customer-visibility are judged.
"""

from frappe.model.document import Document


class YOBStorefrontBlockTargetRow(Document):
    pass
