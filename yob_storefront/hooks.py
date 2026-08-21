app_name = "yob_storefront"
app_title = "YOB Storefront"
app_publisher = "shayona"
app_description = "YOB Storefront"
app_email = "anand@shayona.biz"
app_license = "mit"

# Apps
# ------------------

# yob_core and yob_auth MUST be installed first. Storefront authentication and
# application-access authorization live entirely in yob_auth; this app has
# no independent password/OTP/session implementation and must fail loudly
# rather than fall back to any legacy local auth.
#
# yob_core is listed explicitly rather than left implicit behind yob_auth,
# because yob_storefront imports it directly (see api/response.py).
required_apps = [
    "yob_core",
    "yob_auth",
    "erpnext",
    "payments",
    "india_compliance",
]

# Shown as a tile on the /apps screen. The Desk entry point itself is the
# Desktop Icon + Workspace Sidebar pair in desktop_icon/ and workspace_sidebar/,
# which Frappe imports from disk on `bench migrate` (frappe.model.sync).
#
# The logo lives at public/logo.png, which Frappe serves from /assets/<app>/ --
# there is no public/images/logo.png. The route is the v16 desk path; /app/*
# only redirects there.
add_to_apps_screen = [
	{
		"name": "yob_storefront",
		"logo": "/assets/yob_storefront/logo.png",
		"title": "YOB Storefront",
		"route": "/desk/yob_storefront"
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
app_include_css = "/assets/yob_storefront/css/yob.css"
app_include_js = "/assets/yob_storefront/js/yob.js"

# Desk behaviour ships as APP-OWNED FILES, not Client Script records. A Client
# Script is mutable site data: it can be edited or deleted in production, it does
# not arrive with a fresh install unless someone remembers the fixture, and it
# cannot be reviewed in a pull request. These files migrate with the app.
doctype_js = {
    "Item": "public/js/item_storefront_filters.js",
}

doctype_tree_js = {
    "YOB Storefront Menu Item": "public/js/yob_storefront_menu_item_tree.js",
}

# include js, css files in header of web template
# web_include_css = "/assets/yob_storefront/css/yob.css"
# web_include_js = "/assets/yob_storefront/js/yob.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "yob_storefront/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "yob_storefront/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "yob_storefront.utils.jinja_methods",
# 	"filters": "yob_storefront.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "yob_storefront.install.before_install"

# Registers the STOREFRONT application in yob_auth so a fresh install is
# functional. Grants no user access -- see yob_storefront/install.py.
after_install = "yob_storefront.install.after_install"
after_migrate = "yob_storefront.install.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "yob_storefront.uninstall.before_uninstall"
# after_uninstall = "yob_storefront.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "yob_storefront.utils.before_app_install"
# after_app_install = "yob_storefront.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "yob_storefront.utils.before_app_uninstall"
# after_app_uninstall = "yob_storefront.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "yob_storefront.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
    # Keeps the YOB-managed `YOB Storefront Buyer` role in step with STOREFRONT
    # grants. Registered here (not in yob_auth) because yob_auth must never know
    # about a solution app; the handlers no-op for other applications.
    "YOB User Application Access": {
        "after_insert": "yob_storefront.permissions.storefront_role.on_application_access_update",
        "on_update": "yob_storefront.permissions.storefront_role.on_application_access_update",
        "on_trash": "yob_storefront.permissions.storefront_role.on_application_access_trash",
    },

    "File": {
        "after_insert": "yob_storefront.api.file_hooks.make_public_file",
        "on_update": "yob_storefront.api.file_hooks.make_public_file"
    }, 
             
    "YOB Store Settings": {
        "on_update": "yob_storefront.utils.cache.clear_store_config_cache"
    },  
    # ---------------- CUSTOMER ----------------

    "Customer": {
        "on_update": "yob_storefront.utils.cache.clear_customer_cache",
        "after_insert": "yob_storefront.utils.cache.clear_customer_cache"
    },

    "Contact": {
        "on_update": "yob_storefront.utils.cache.clear_customer_cache",
        "after_insert": "yob_storefront.utils.cache.clear_customer_cache",
        "on_trash": "yob_storefront.utils.cache.clear_customer_cache"
    },

    # ---------------- ITEM ----------------

    "Item": {
        # A public slug addresses exactly one product. Enforced here rather than
        # by a unique index: unslugged Items all store the same empty string.
        # Storefront filter integrity is enforced in the same gate, because a
        # Client Script cannot see Data Import, the REST API or bench execute.
        "validate": [
            "yob_storefront.utils.item_slug.validate_unique_slug",
            "yob_storefront.utils.item_storefront_filters.validate_item_storefront_filters",
        ],
        "on_update": "yob_storefront.utils.cache.clear_item_cache",
        "after_insert": "yob_storefront.utils.cache.clear_item_cache",
        "on_trash": "yob_storefront.utils.cache.clear_item_cache"
    },

    # ---------------- PRICING ----------------

    "Pricing Rule": {
        "on_update": "yob_storefront.utils.cache.clear_pricing_cache",
        "after_insert": "yob_storefront.utils.cache.clear_pricing_cache",
        "on_trash": "yob_storefront.utils.cache.clear_pricing_cache"
    },
    
    "Price List": {
        "on_update": "yob_storefront.utils.cache.clear_pricing_cache",
        "after_insert": "yob_storefront.utils.cache.clear_pricing_cache",
        "on_trash": "yob_storefront.utils.cache.clear_pricing_cache"
    },
    
    "Item Group": {
        "on_update": "yob_storefront.utils.cache.clear_pricing_cache"
    },

    "Item Price": {
        "on_update": "yob_storefront.utils.cache.clear_pricing_cache",
        "after_insert": "yob_storefront.utils.cache.clear_pricing_cache",
        "on_trash": "yob_storefront.utils.cache.clear_pricing_cache"
    },
    
    "Customer Group": {
        "on_update": "yob_storefront.utils.cache.clear_pricing_cache"
    },

    "Territory": {
        "on_update": "yob_storefront.utils.cache.clear_pricing_cache"
    },

	# "*": {
	# 	"on_update": "method",
	# 	"on_cancel": "method",
	# 	"on_trash": "method"
	# }
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"yob_storefront.tasks.all"
# 	],
# 	"daily": [
# 		"yob_storefront.tasks.daily"
# 	],
# 	"hourly": [
# 		"yob_storefront.tasks.hourly"
# 	],
# 	"weekly": [
# 		"yob_storefront.tasks.weekly"
# 	],
# 	"monthly": [
# 		"yob_storefront.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "yob_storefront.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "yob_storefront.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
#     "yob_storefront.api.some.method": "yob_storefront.api.some.method"
# }

 

#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "yob_storefront.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["yob_storefront.utils.before_request"]
# after_request = ["yob_storefront.utils.after_request"]

# Job Events
# ----------
# before_job = ["yob_storefront.utils.before_job"]
# after_job = ["yob_storefront.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"yob_storefront.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

# Add this line to your hooks.py
 
fixtures = [
    # {
    #     "doctype": "Custom Field",
    #     "filters": [
    #          ["module", "=", "yob_storefront"]
    #     ]
    # },
    {
        "doctype": "Client Script",
        "filters": [
            ["module", "=", "yob_storefront"]
        ]
    }
]