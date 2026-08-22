# CHG-002 — Storefront Navigation, Catalog Filters and Content Blocks

Status: `Phase 25A design approved with corrections; Phase 25B (admin + data model), Phase 25C (runtime APIs) and Phase 25C-1 (contract precision) implemented; Phase 25F signed off — see §21 for the as-built names and the sign-off report for the chain verdict`

Owner: `Nilesh`

Date: `2026-08-21`

Source material: `filters-block-menu.zip` (47 files, md5 `7269df3c1f388b4bb406c98556e711f2`)

## 1. Task classification

- Type: `new capability, three related domains`
- Owning app: `yob_storefront`
- Business behavior change intended: `Yes`
- Public API breaking change intended: `No — all additive`
- Stored-data migration intended: `No on the two benches here (proved in §18)`

---

## 2. Prototype reconstruction

### 2.1 Navigation — 2 DocTypes

**`YOB Storefront Menu`** (master, `autoname: field:menu_name`, `track_changes`):
`menu_name` (Data, reqd) · `menu_key` (Data, reqd) · `enabled` (Check, default 1) ·
`description` (Small Text). Controller validates `menu_key` against
`^[a-z0-9-]+$` only.

**`YOB Storefront Menu Item`** (`is_tree: 1`, NestedSet, `autoname: UUID`):
`label` (reqd) · `menu` (Link → Menu, reqd) · `item_type` (Select: Group, Home,
Web Page, External URL, Catalog, Product Category, reqd) · `enabled` ·
`web_page` (Link → **Web Page**) · `external_url` (Data/URL) · `open_in_new_tab` ·
`product_category` (Link → **Category**) · tree fields `lft/rgt/is_group/
old_parent/parent_yob_storefront_menu_item`.

Server validation actually present:
- `item_type` required and within the allowed set;
- `is_group` derived from `item_type == "Group"` (not user-set);
- destination fields cleared when the type changes; required per type;
- a non-Group may not have children;
- parent must exist, must be a Group, must have no parent of its own
  (one level), may not be self;
- an item that has children may not be moved under a parent;
- child inherits the parent's `menu`; a conflicting `menu` is rejected;
- `menu` may not change while children exist.

Desk behaviour: a tree view (`yob_storefront_menu_item_tree.js`) with a Menu
filter, `get_children`/`add_node` whitelisted loaders, a root label
"All Menu Items", `hide_add` on child nodes, and a dialog that re-evaluates
destination-field visibility when Item Type changes. The form JS mirrors the
server rules and filters the parent picker to root Groups of the same Menu.

### 2.2 Filters — 5 DocTypes + 3 custom fields + 1 Client Script + 2 query helpers

| DocType | Type | Fields |
|---|---|---|
| `Filter` | master, `field:filter_name` | `filter_name` (reqd, **unique**), `frontend_label`, `enabled`, `sequence`, `values` (Table → Filter Value) |
| `Filter Value` | **child** (`istable: 1`), `field:value` | `value` (reqd, **unique**), `sequence` |
| `Filter Set` | master, `autoname: prompt` | `label`, `description`, `enabled`, `filters` (Table → Filter Set Filter) |
| `Filter Set Filter` | child | `filter` (Link → Filter), `sequence` |
| `Item Filters` | child | `filter` (Link → Filter), `filter_value` (**Link → Filter Value**), `sequence` |

All five controllers are empty `pass` classes — **zero server validation**.

Custom fields on Item (`filters/custom/item.json`): `custom_filters_tab`
(Tab Break) · `custom_filter_set` (Link → Filter Set) · `custom_item_filter`
(Table → Item Filters).

`filters/api/filter.py` — two `@frappe.whitelist()` link-query helpers,
**not** using the YOB API boundary: `get_filters_for_filter_set` returns the
Filters of a Filter Set; `get_filter_values` returns rows of the `Filter Value`
child table for a Filter.

`filters/fixtures/client_script.json` — one Client Script on Item that scopes the
two link queries, clears `custom_item_filter` when the Filter Set changes, clears
`filter_value` when `filter` changes, and rejects duplicate `filter::filter_value`
pairs **in the browser only**.

### 2.3 Content Blocks — 3 DocTypes

**`YOB Storefront Block`** (master, UUID): `block_name` (reqd), `enabled`,
`block_type` (Select: Image Banner, Rich Text, Banner Carousel, Product Grid,
Offer Grid, reqd), plus per-type field groups driven by `depends_on` /
`mandatory_depends_on`:

- Image Banner — `desktop_image` (Attach Image, mandatory), `mobile_image`,
  `alt_text`, `link` (Data)
- Rich Text — `content_title`, `text_alignment` (Left/Center), `content`
  (Text Editor, mandatory)
- Banner Carousel — `slides` (Table → Block Slide, mandatory), `auto_play`,
  `interval`
- Product Grid — `product_source` (Category/Item Group), `product_category`
  (Link → Category), `item_group` (Link → Item Group), `card_type`
  (Square/Portrait), `item_limit` (default 12), `sort_by` (Newest / Price Low to
  High / Price High to Low / Name A-Z / Name Z-A)
- Offer Grid — `offers` (Table → Block Offer, mandatory), `cards_per_row` (1/2/3)
- shared display — `desktop_height`, `mobile_height` (Int, unit unstated)

**`YOB Storefront Block Slide`** and **`YOB Storefront Block Offer`** are
identical child DocTypes: `desktop_image` (reqd), `mobile_image`, `title`,
`alt_text`, `link`.

Server validation present: required desktop image for Image Banner; content for
Rich Text; ≥1 slide and a desktop image per slide; product source consistency
(clears the unused link) and `item_limit <= 12`; ≥1 offer with desktop image and
`cards_per_row ∈ {1,2,3}`.

---

## 3. Documentation / code mismatches in the ZIP

| # | Documentation says | Code does |
|---|---|---|
| 1 | `Filter` uses the standard Frappe `name`, "no separate `filter_name` field is required" | `filter_name` exists, is unique, and is the autoname source |
| 2 | `Filter Value` has an `enabled` Check | **No `enabled` field** exists |
| 3 | `Filter` has a `filter_values` table | The field is named `values` |
| 4 | `Filter Set` uses the standard `name`, "no `filter_set_name` needed" | `autoname: prompt` **and** a separate `label` field — two competing identities |
| 5 | `Filter Set` has an `enabled` Check that is "required" | `enabled` exists but nothing enforces it |
| 6 | Child DocType is `Item Filter` (singular) | The DocType is `Item Filters` (plural) |
| 7 | `Item Filter.filter_value` is "Link → Filter Value" and required | Link exists but is **not** `reqd`, and the link query returns the *text* `value`, so what is stored is not a Filter Value record name |
| 8 | `Category` has a `filter_set` field | **No Category field exists anywhere in the ZIP** |
| 9 | Item fields are `filter_set` / `item_filters` | Actual fields are `custom_filter_set` / `custom_item_filter` |
| 10 | "When `filter_set` is selected, the system loads the Filters into Item Filters" | The Client Script **clears** the table and loads nothing |
| 11 | Filter Set "does not select Filter Values" | Correct in code |
| 12 | Blocks doc describes five block types | Correct, but `Offer Grid`'s `desktop_height`/`mobile_height` are exposed with no unit and no validation |

Additional code-level defects independent of the docs:

13. **`Filter Value.value` is globally `unique`** — `Red` could exist under
    exactly one Filter in the entire system.
14. **`Item Filters.filter_value` Links to a child DocType.** Frappe child rows
    are not addressable masters; the prototype's own link query sidesteps this by
    returning `value` text, so the "Link" is a string that no referential rule
    protects.
15. Module value is `yob_storefront` for menu and filters but **`YOB Storefront`**
    for the three block DocTypes.
16. `Filter`/`Filter Set` permissions grant System Manager only; child DocTypes
    carry no permission rows.
17. All five `test_*.py` files are empty stubs (9 lines of boilerplate).
18. The link-query helpers are bare `@frappe.whitelist()` functions — no
    `@yob_api`, no `require_application`, no envelope.

---

## 4. Conflicts with current YOB architecture

1. **Bare whitelisted endpoints** violate the Phase 23/24 API boundary
   (`@yob_api` + `require_application` + stable error codes + envelope).
2. **Client-Script-only validation** is not a security control. Data Import, the
   REST API and `bench execute` all bypass it.
3. **Client Script fixtures** are a mutable database record; the app already
   ships four and it is the wrong direction for new work (see §17).
4. **Product Grid `sort_by` includes Price Low/High** — price is customer- and
   price-list-specific and is produced by an ERPNext Sales Order projection, not
   stored on the Item. Sorting a bounded page by price would require pricing
   every candidate first. The Phase 22B listing deliberately supports
   `name_asc | name_desc | newest` only.
5. **Product Grid `product_source = Item Group`** has no counterpart in the
   current listing service, which supports `scope_type = category` only.
6. **Menu `item_type = Web Page`** points at Frappe's `Web Page`. Current
   evidence: **zero Web Page records on either bench**, and Angular has no route
   that renders one (`/terms` is a hard-coded component). Only
   `YOB Store Settings.default_terms_page` / `default_privacy_page` reference it.
7. **`Offer Grid`** collides conceptually with ERPNext Pricing Rules / Product
   Discounts, which YOB already treats as the only source of promotions.

---

## 5. Current YOB Category / taxonomy model

`Category` (module `yob_storefront`, `is_tree: 1`, `autoname: field:category_name`):
`category_name` (unique) · `slug` (unique) · `parent_category` / `lft` / `rgt` /
`is_group` / `old_parent` · `thumbnail` · `banner` · `display_order` ·
`is_active` · `meta_title` · `meta_description` · `description` (HTML Editor) ·
two HTML preview fields.

It is the authoritative storefront taxonomy: `Item.custom_category` links to it,
`catalog.get_items` filters on it, `get_category` serves it, and Angular routes
`/catalog/:categorySlug` by `slug`. **No second taxonomy may be introduced.**
A group Category holds sub-categories and is not listable
(`category_not_listable`).

## 6. Current YOB content / page model

**There is none.** The app owns no page DocType; `api/cms.py` exposes only
`get_config`. Frappe's `Web Page` is referenced by two Store Settings fields but
has zero rows on both benches and no Angular route. Angular's `content` domain
contains three hard-coded pages (`terms`, `session-unavailable`, `not-found`).

Incidental defect found: `cms.get_config` calls
`frappe.cache().delete_value(cache_key)` immediately before reading the cache, so
its one-hour cache never serves anything.

## 7. Current YOB sidebar / workspace mechanism

Three source-controlled JSON files, synced by `bench migrate`:

- `yob_storefront/desktop_icon/yob_storefront.json` — Apps Page icon → Workspace Sidebar
- `yob_storefront/workspace_sidebar/yob_storefront.json` — sidebar items
- `yob_storefront/yob_storefront/workspace/yob_storefront/yob_storefront.json` —
  the Workspace: Card Breaks (`Catalog`, `Orders`, `Payments`) with Links, plus
  four Shortcuts.

New entries are added to these files. **No second workspace.**

---

## 8. Recommended canonical DocType names

The app's existing names are mixed (`Category`, `Cart`, `Payment Method` vs
`YOB Store Settings`), so there is no stronger convention to defer to, and the
prototype is itself inconsistent (menu and block prefixed, filters not). Generic
names such as `Filter` are unacceptable in a shared Frappe namespace.

| Prototype | Canonical |
|---|---|
| `Filter` | `YOB Storefront Filter` |
| `Filter Value` | `YOB Storefront Filter Value` (**master**, §10) |
| `Filter Set` | `YOB Storefront Filter Set` |
| `Filter Set Filter` | `YOB Storefront Filter Set Filter` (child) |
| `Item Filters` | `YOB Storefront Item Filter` (child) |
| `YOB Storefront Menu` | unchanged |
| `YOB Storefront Menu Item` | unchanged |
| `YOB Storefront Block` | unchanged |
| `YOB Storefront Block Slide` | unchanged |
| `YOB Storefront Block Offer` | `YOB Storefront Block Promo Card` (§14) |
| — | `YOB Storefront Page`, `YOB Storefront Page Block` (§15) |

Module for every one: **`yob_storefront`** (the Module Def in `modules.txt`).

## 9. Final fields

### YOB Storefront Filter (master, `autoname: field:key`)
`key` (Data, reqd, unique, `set_only_once`) — lowercase `[a-z0-9_]`, the protocol
identity used in APIs and URLs · `label` (Data, reqd) — display only ·
`enabled` (Check, default 1) · `sequence` (Int).
No `values` child table: values are masters.

### YOB Storefront Filter Value (master, `autoname: hash`)
`filter` (Link → Filter, reqd) · `value` (Data, reqd) — display text ·
`value_key` (Data, reqd) — URL-safe token, unique **per filter** ·
`enabled` (Check, default 1) · `sequence` (Int).
Uniqueness: `(filter, value)` and `(filter, value_key)`, enforced in `validate()`
and by a composite DB unique index created in `after_migrate`. A random `hash`
name keeps the link target stable when a merchant renames the display text.

### YOB Storefront Filter Set (master, `autoname: field:key`)
`key` (Data, reqd, unique) · `label` (Data, reqd) · `enabled` · `description` ·
`filters` (Table → Filter Set Filter).

### YOB Storefront Filter Set Filter (child)
`filter` (Link → Filter, reqd) · `sequence` (Int).

### YOB Storefront Item Filter (child)
`filter` (Link → Filter, reqd) · `filter_value` (Link → Filter Value, reqd) ·
`sequence` (Int, only if merchants need to order facet chips on the PDP).

### Server validation (all of it server-side, none in JS)
- Filter Value: `filter` enabled; `(filter,value)` and `(filter,value_key)` unique.
- Filter Set: no Filter twice; every Filter enabled at save time.
- Item Filter rows: value belongs to the named filter; filter enabled; value
  enabled; no duplicate `(filter, filter_value)` pair on one Item; multiple
  different values under one Filter are allowed (Colour → Red **and** Blue).
- Item Filter rows rejected on a variant child (`variant_of` set) — see §11.

## 10. Filter Value: master, not child

Child rows are not addressable entities. Three concrete failures in the
prototype prove it: the global `unique` on `value` makes `Red` usable under one
Filter only; a Link field pointed at a child DocType has no referential
protection and no working link picker, which is why the prototype needed a custom
query; and that query returns the *text*, so the stored "link" is a display
string that breaks the moment a value is renamed.

A master with `filter` + `value` + `value_key` + `enabled` + `sequence`, named by
`hash`, gives Item Filter a stable target, allows `Colour/Red` and `Paint/Red` to
coexist, and lets uniqueness be scoped exactly where the business needs it.

## 11. Item / Category fields, and variant semantics

Installed by `install.ensure_custom_fields()` (`create_custom_fields`), called
from both `after_install` and `after_migrate`. **No Customize Form, no fixture.**

| DocType | Field | Type |
|---|---|---|
| Item | `custom_storefront_section` | Tab Break "Storefront" |
| Item | `custom_storefront_filters` | Table → YOB Storefront Item Filter |
| Category | `filter_set` | Link → YOB Storefront Filter Set (app-owned field in `category.json`, not a Custom Field — YOB owns Category) |

**Filters attach to the entity that appears in the listing:**

| Catalog entity | Filters live on | Reason |
|---|---|---|
| simple Item | itself | it is the card |
| variant family | the **template** (`has_variants = 1`) | Phase 24B lists one card per family |
| actual variant child | **rejected at save** | a variant is never listed, so rows there would silently do nothing |

Merchants therefore never duplicate filter rows onto generated variants. ERPNext
variant attributes (Colour/Size used to resolve a SKU) remain entirely separate
from merchandising filters; no second variant engine appears anywhere.

**Category ↔ Item — CORRECTED IN 25B.** The 25A recommendation (no Filter Set on
Item) was wrong. Both carry one, and they do different jobs:

> **Item Filter Set and Category Filter Set serve separate purposes. Item Filter
> Set limits and validates the merchandising filter metadata maintainable on an
> Item/template. Category Filter Set controls which filters are exposed on that
> storefront category. They are not required to be identical.**

| | `Item.custom_storefront_filter_set` | `Category.storefront_filter_set` |
| --- | --- | --- |
| Job | ADMIN SCOPE — which Filters an administrator may attach to this product | DISPLAY — which Filters that category's listing exposes to buyers |
| Enforced | server-side on `Item.validate`: every row's Filter must belong to it | read by the Phase 25C facet projection |
| Audience | merchandiser in Desk | buyer on the storefront |

They are independent by design. An `Industrial Switch` may carry Voltage, Colour,
Material, IP Rating and Mount Type from `Electrical Product Filters`, while its
category exposes only Voltage and Colour from `Electrical Customer Filters`.
`Item.filter_set == Category.filter_set` is **never** required, and a category's
narrower set never erases or restricts the richer item metadata. When a buyer
filters a category, only assignments for the category-visible Filters matter to
the frontend; the rest stay on the item as product data.

**No inheritance up the Category tree, confirmed in 25B-1.** Every Category
selects its own Filter Set explicitly; a child with none exposes no filters, and
the projection must not walk the tree looking for a parent's. Desk may later
default an Item's set from its Category as a convenience, never as a hard
coupling.

**Ordering of slides and promo cards is the Frappe child-row `idx`** — no second
sequence field exists, and the projection preserves it.

**`YOB Store Settings.default_terms_page` / `default_privacy_page` stay on Frappe
`Web Page` for now.** They are not blockers for the runtime APIs, and the
hard-coded Angular legal-page flow is untouched; moving those routes to dynamic
pages is separate work.

## 11a. ERPNext Item Group is not storefront taxonomy

> **ERPNext Item Group remains an internal ERP/pricing concept and is not used as
> YOB storefront taxonomy. Storefront Category is authoritative for frontend
> navigation, filtering and Product Grid selection.**

Item Group keeps doing its ERPNext work — Pricing Rules match on it exactly as
Phase 23 already supports, and it organises the item master for accounting and
reporting. It is never exposed to Angular as a category, a navigation
destination, a filter taxonomy or a Product Grid source. `YOB Storefront Block`
therefore has **no** `item_group` field, and `YOB Storefront Menu Item` has no
Item Group destination type; both are asserted by test.

Four rules that survive this phase unchanged:

* ERPNext variant attributes (Colour/Size resolving a SKU) are a separate system
  from merchandising filters, and neither reads the other.
* Generated variants never duplicate storefront filters; the template carries them.
* Pricing stays Phase 22–24 authoritative; nothing here prices anything.
* A Product Grid holds a bounded query, not a pricing engine — and YOB is not a
  generic runtime layout or theme builder.

## 12. Filter matching semantics

```
values within one filter  -> OR
different filters         -> AND

colour = red OR blue   AND   material = cotton
```

Implemented as one `EXISTS` per selected filter, appended to the **existing**
Stage-1 candidate SQL in `catalog_listing_service.fetch_candidates`:

```sql
AND EXISTS (
  SELECT 1 FROM `tabYOB Storefront Item Filter` f
  WHERE f.parent = i.name AND f.parenttype = 'Item'
    AND f.filter = %(filter_0)s
    AND f.filter_value IN %(values_0)s
)
```

Properties preserved: still a bounded superset, so Stage 2 (exact base price) and
Stage 3 (Sales Order pricing) are untouched; page size, keyset cursor,
`has_more` honesty, variant-family collapse and catalog eligibility all unchanged;
**no additional pricing work** — filtering happens before any Sales Order is built.

**Cursor binding.** `_binding_fingerprint` currently hashes
`[scope_type, scope_value, sorted(terms), sort, customer, price_list]`. The
normalised filter selection is appended to that list, so a cursor issued under one
selection cannot be replayed against another; a mismatch already answers
`cursor_invalid`.

**Facet counts are out of scope, confirmed in 25B-1.** Filter definitions and
values are returned; per-value counts are neither computed nor precomputed. A truthful count requires running the
full three-stage pipeline per value, which is exactly the unbounded work Phase 22B
removed. The first cut returns available values without counts.

## 13. Final navigation model

`YOB Storefront Menu` + tree `YOB Storefront Menu Item` as prototyped, with the
existing server rules kept, plus:

- destination types become **Home · Catalog · Product Category · Content Page ·
  External URL · Group** — `Web Page` is replaced by `Content Page`
  (→ `YOB Storefront Page`, §15);
- `product_category` must be `is_active = 1` and non-group, validated server-side;
- `external_url` must parse to scheme `http`/`https` only — `javascript:`,
  `data:`, `vbscript:` and schemeless control characters rejected in `validate()`;
- root-level destination items remain allowed; grandchildren stay forbidden;
- the storefront projection publishes a node only when the Menu is enabled, the
  node is enabled, its parent (if any) is enabled, and its destination still
  resolves (active Category, published Page). Ordering is `lft, name`.

The API returns **resolved routes**, never DocType names:

```jsonc
{"key":"main","items":[
  {"label":"Tools","type":"group","children":[
    {"label":"Hand Tools","type":"category","route":"/catalog/hand-tools"},
    {"label":"About","type":"page","route":"/p/about"},
    {"label":"Blog","type":"external","url":"https://…","new_tab":true}]}]}
```

## 14. Content block model and naming

`YOB Storefront Block` keeps its five concepts; `Offer Grid` is renamed
**`Promo Grid`** with child **`YOB Storefront Block Promo Card`**, because
"Offer" in YOB already means an ERPNext Pricing Rule. Machine types:
`image_banner`, `rich_text`, `banner_carousel`, `product_grid`, `promo_grid`.

**Destinations are TYPED, and shared with navigation (25B-1).** The 25A proposal
gave blocks a free-text `link_url`; that was replaced before any content existed.
A merchant picks a type and a record and never types an Angular route:

| Type | Target | Rule |
| --- | --- | --- |
| *(blank)* | — | not clickable |
| Catalog | — | implied route |
| Storefront Category | `link_category` | active, not a group |
| Storefront Page | `link_page` | must exist |
| Product | `link_item` | simple Item or variant FAMILY with a slug; a generated variant is refused — Phase 24 family routing is authoritative |
| External URL | `link_external_url` | http(s) only; `javascript:`, `data:`, `//host` refused |

`utils.storefront_content.apply_destination()` validates all of them and is used
by `YOB Storefront Menu Item` **and** by blocks, slides and promo cards, so
navigation and content cannot grow two incompatible routing systems. Route
construction happens in the Phase 25C projection — never in Desk JavaScript.
Hyperlinks typed inside Rich Text stay governed by HTML sanitisation, not modelled
as destinations.

Field corrections:
- heights become `desktop_height_px` / `mobile_height_px` with explicit units and
  a sane range (e.g. 80–1200), or are dropped in favour of an aspect-ratio Select;
- `link` fields become the typed destination above (25B-1), not free Data;
- `sort_by` restricted to the listing's real modes: `newest | name_asc | name_desc`
  — no price sorting (§4.4);
- `item_limit` 1–12 enforced server-side (`cint`, both bounds — the prototype
  checks only the upper);
- `cards_per_row` validated against the Select options;
- `interval` bounded (e.g. 2000–15000 ms) and required when `auto_play` is set;
- stale per-type fields cleared on save for **every** type, not only Product Grid;
- Rich Text `content` sanitised server-side on save (`frappe.utils.sanitize_html`)
  so Angular never needs to bypass its sanitizer;
- images returned exactly as the catalog returns them (see §16, media note).

## 15. Page / block placement

No current page model exists (§6), so this must be created:

**`YOB Storefront Page`** — `title` · `slug` (unique, URL-safe) · `enabled` ·
`meta_title` · `meta_description` · `blocks` (Table → Page Block).
**`YOB Storefront Page Block`** (child) — `block` (Link → Block, reqd) ·
`sequence` · `enabled`.

Blocks stay reusable masters; a Page is an ordered composition. Menu "Content
Page" destinations link here. Angular gains one route (`/p/:slug`) in the later
frontend phase. Home page composition is just a Page with a reserved slug.

Open question for approval: `YOB Store Settings.default_terms_page` /
`default_privacy_page` currently link to `Web Page`. Either re-point them at
`YOB Storefront Page` (a Custom-Field/DocType change plus a data patch — trivial
today, zero rows) or leave them and accept two page concepts. Recommendation:
re-point, and drop `Web Page` from the storefront entirely.

## 16. Storefront API design

All endpoints use `@frappe.whitelist(methods=["GET"])` + `@yob_api` +
`require_application(STOREFRONT_APP)`, stable snake_case error codes, and the YOB
envelope — no bare whitelisted functions.

| Endpoint | Purpose |
|---|---|
| `navigation.get_menu(menu_key)` | one enabled Menu as a resolved tree (§13) |
| `catalog.get_filters(scope_type, scope_value)` | facet definitions for a category: `[{key,label,values:[{key,label}]}]` |
| `catalog.get_items(..., filters=<JSON>)` | **existing** endpoint; `filters` changes from "must be empty" to a strict object `{"colour":["red","blue"]}` keyed by filter/value **keys**. Unknown keys still answer `unsupported_filters` |
| `content.get_page(slug)` | an enabled Page and its ordered, discriminated blocks |

Block payloads are discriminated by `type`, never by which nullable field is
populated:

```jsonc
{"type":"product_grid","title":"…","card_type":"square",
 "items":[ …ListingCard… ]}
```

**Product Grid reuses the catalog service**: `list_items()` with
`page_size = item_limit`, no cursor, the same `PricingContext`. It therefore
yields the existing `ListingCard` shape — `price_state: "priced"` for simple
items and `"select_options"` for variant families — with correct selling UOM,
media and customer price list, and it never picks a child variant to give a
family a price. Supporting `product_source = Item Group` means adding
`scope_type = "item_group"` to the listing service, not writing a second query.

Bounded work: a page's grids are capped (recommended: ≤3 Product Grid blocks per
page, ≤12 items each ⇒ ≤36 priced items), and that cap is enforced when the Page
is saved, not at render time.

**Media note:** the catalog currently returns **relative** image paths (every
`get_url()` call in `catalog.py` is deliberately commented out) while
`cart_service` returns absolute URLs via `get_url`. Blocks must follow the catalog
convention; the cart divergence is pre-existing and should be reconciled
separately.

**Caching:** menus and page/block *structure* are customer-independent and may be
cached by `(menu_key)` / `(page_slug)`. Product Grid output must **not** be cached
across customers — price depends on the customer's price list via
`SellingContext`. If grid caching is ever added, the key must include
`ctx.price_list` (and customer where a customer-specific Item Price exists). No
whole-page caching.

## 17. Automatic installation strategy

| Artifact | Mechanism |
|---|---|
| 11 new DocTypes (incl. children) | standard DocTypes inside the app, module `yob_storefront`, synced by `bench migrate` |
| `Category.filter_set` | app-owned field in `category.json` (YOB owns Category) |
| Item custom fields | `install.ensure_custom_fields()` via `create_custom_fields`, already called from `after_install` **and** `after_migrate` |
| Composite unique index on Filter Value | `after_migrate`, guarded and idempotent |
| Desk JS (tree view, link queries, per-type UI) | app-owned files wired through `doctype_js` / `doctype_tree_js` hooks — **not** Client Script fixtures |
| Link queries | `@frappe.whitelist()` Desk-only helpers under `yob_storefront/api/desk/`, clearly separated from storefront endpoints |
| Workspace / sidebar | new Card Break + Links added to the three existing JSON files |
| Patches | only if a rename is needed (§18) |
| Fixtures | none for this work |
| API reference | `frontend-api-handoff/` updated in the same phase; the Phase 24D-1 guard already fails the build if an endpoint or error code is unpublished |

**Blocking pre-existing defect found during this audit:** `Item.custom_slug` and
`Item.custom_category` are created by **nothing** — the Custom Field fixture entry
in `hooks.py` is commented out and `ensure_custom_fields()` only handles Payment
Request. They exist on these benches because someone created them by hand. A
fresh `yob_storefront` install therefore has no slug or category field and the
entire Phase 22–24 catalog cannot work. This must be fixed in 25B before anything
else, by moving both fields into `ensure_custom_fields()`.

## 18. Migration of anything already created manually

Proved by direct query against both benches:

| Object | yob.localhost | test.localhost |
|---|---|---|
| `Filter`, `Filter Value`, `Filter Set`, `Filter Set Filter`, `Item Filters` | **absent** | **absent** |
| `YOB Storefront Menu`, `Menu Item`, `Block`, `Block Slide`, `Block Offer` | **absent** | **absent** |
| `custom_filter_set`, `custom_item_filter` Custom Fields | **absent** | **absent** |
| `Web Page` rows | **0** | **0** |

There is nothing to migrate here, and no data can be destroyed. The prototype was
built on some other environment (its Custom Field JSON carries a `2026-08-12`
creation stamp). If that environment matters, the migration is a rename patch —
`Filter → YOB Storefront Filter` etc. via `frappe.rename_doc`, plus moving
`Filter Value` child rows into master records keyed by `(filter, value)`. That
patch is only worth writing if the merchant confirms real data exists there.

## 19. Implementation sequence

**Phase 25B — foundations (backend, no Angular)**
1. Fix the install gap: `custom_slug` / `custom_category` into `ensure_custom_fields()`, with a test proving a fresh install has them.
2. Filter DocTypes (5) + `Category.filter_set` + Item custom fields + full server validation + Desk JS via hooks + workspace links.
3. `catalog.get_filters` and the `filters` argument on `catalog.get_items`, including cursor-fingerprint binding.
4. Tests: uniqueness, enabled behaviour, set membership, duplicate pairs, variant-child rejection, OR/AND matching, cursor binding, and a proof that filtering adds no pricing calls.

**Phase 25C — navigation and content**
5. Menu DocTypes with hardened validation + `navigation.get_menu`.
6. `YOB Storefront Page` / `Page Block`, Block DocTypes with the §14 corrections, `content.get_page`, Product Grid through the existing listing service.
7. Tests per §15 of the brief; API reference republished.

**Phase 25D — Angular** (separate, later): menu-driven header and drawer, facet UI, `/p/:slug`, block renderers. Angular must not hard-code the merchant's navigation.

## 20. Decisions needing product approval

1. **Item Filter Set field** — recommended: none on Item; Category owns the set.
   Alternative: keep `custom_filter_set` on Item and validate membership.
2. **Variant-child filter rows** — recommended: reject at save. Alternative:
   allow and silently ignore.
3. **`Web Page` retirement** — recommended: introduce `YOB Storefront Page`,
   re-point Store Settings, drop Web Page from the storefront.
4. **`Offer Grid` → `Promo Grid`** rename (recommended, avoids collision with
   ERPNext promotions).
5. **Facet counts** — recommended: none in 25B; add later only with a bounded design.
6. **Product Grid `Item Group` source** — requires a new `scope_type` in the
   listing service; confirm merchants actually need it, otherwise Category-only.
7. **Price sorting in Product Grid** — recommended: drop it (§4.4).
8. **Filter/value URL keys** — recommended: explicit `key` / `value_key` rather
   than display text in URLs.
9. **Per-page grid cap** (recommended ≤3 grids × 12 items).
10. **Prototype-site migration** — only needed if real data exists on the
    environment where the prototype was built.

---

## 21. Phase 25C — as built

The runtime layer shipped in one phase rather than the §19 split, and three names
in §16/§19 were superseded during implementation. **§21 is authoritative where it
disagrees with §16.**

| §16 name | As built | Why |
|---|---|---|
| `navigation.get_menu` | `cms.get_menu(menu_key)` | no new API module: `cms` already owns storefront presentation config |
| `content.get_page` | `cms.get_page(slug)` | same module, same reason |
| `catalog.get_filters(scope_type, scope_value)` | `catalog.get_category_filters(scope_value)` | filters are a property of a Category; a `scope_type` that only ever takes one value is a parameter nobody can use correctly |
| `catalog.get_items(..., filters=<JSON>)` | `catalog.get_items(..., storefront_filters=<JSON>)` | `filters` stays reserved and still answers `unsupported_filters`, so the Phase 22B contract is untouched and no client that sends `filters` today changes meaning |
| Angular `/p/:slug` | `/pages/:slug` | product decision, locked in 25C |

Everything else in §16 shipped as designed: one destination projection shared by
menu items, banners, slides and promo cards; blocks discriminated by `type`;
Product Grid answered by `list_items()` with no pricing, UOM, warehouse or variant
logic of its own; no facet counts; no caching in this cut.

The dynamic page route is **not stored**. A `storefront_page` destination carries
`type` + `target` (the public slug) and a null `href`; Angular builds
`/pages/${target}`. A route change therefore stays an SPA change rather than a
data migration.

Filter matching is one correlated `EXISTS` per selected filter, appended to the
**Stage-1** candidate SQL — OR within a filter, AND across filters — so filtering
happens before pricing and a narrower selection costs fewer pricing calls, never
more. The normalised selection joins the cursor binding fingerprint.

New stable error codes: `menu_not_found`, `page_not_found`,
`storefront_filter_invalid`, `storefront_filter_unknown`,
`storefront_filter_value_unknown`, `storefront_filter_context_required`.

Phase 25C-1 typed the content-block schemas that 25C had published as
`array<object>` (`BannerCarouselSlide`, `PromoCard`, `x-block-fields`), taking
OpenAPI to **3.4.1**. Documentation only; no runtime change.

Phase 25F proved the chain end to end and signed the work off. See
`CHG-002-storefront-navigation-filters-blocks-report.md`.
