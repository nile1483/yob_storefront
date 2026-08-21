"""Desk-only helpers.

These are NOT storefront endpoints. They serve the Frappe Desk (link queries,
tree loaders) and are reachable only by an authenticated Desk user with the
relevant DocType permissions. Storefront endpoints live one level up, carry the
YOB API boundary, and answer the public envelope.
"""
