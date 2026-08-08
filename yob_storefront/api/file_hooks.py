import frappe

def make_public_file(doc, method):
    """
    Automatically make Item & Category thumbnails public
    """

    # Only for these doctypes
    allowed_doctypes = ["Item", "Category"]

    if doc.attached_to_doctype not in allowed_doctypes:
        return

    # No file URL
    if not doc.file_url:
        return

    # Already public
    if not doc.is_private:
        return

    # Make file public
    doc.is_private = 0
    doc.save(ignore_permissions=True) 
