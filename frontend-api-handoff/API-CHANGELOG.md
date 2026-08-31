# API Changelog — what changed vs earlier frontend assumptions

Not a git log. This lists **contract differences** between what the frontend
previously assumed (the earlier `reference/api` package and pre-payment specs)
and what the backend does today.

Each entry: **OLD** → **CURRENT** → **FRONTEND ACTION**.

Where nothing changed, nothing is listed.

---

## 0. MRP and quantity guidance on the resolved SKU (OpenAPI 3.12.0)

**Additive.** Two new fields on `ProductDetail`, so they appear in
`catalog.get_item` (simple product) and `catalog.resolve_variant`. No existing
field changed meaning or value.

```jsonc
{"mrp": 1000,
 "quantity_control": {"moq": 10, "quantity_multiplier": 6, "allowed": true}}
```

Both come from the **same Item Price row the rate came from** — the
customer-specific row when one applies, the price-list row otherwise. Selecting
another variant re-resolves them along with the price, so a variant switch needs
no extra request.

### `mrp` — informational only

**OLD** — no MRP anywhere.

**CURRENT** — `mrp` is the Maximum Retail Price on the resolved Item Price, in
that Item Price's own currency. **Display only.**

It is **not** a base price, rate, discount or total; no other field is derived
from it; and **the backend computes no saving and no percentage** against it. If
you want "You save ₹300", that is your calculation to make and own — the API
deliberately does not supply one. It is not validated against the selling rate,
so `mrp` **may be lower** than `rate`; render accordingly rather than assuming a
discount. `null` when not configured.

### `quantity_control` — guidance, never enforcement

**This is the part to read carefully.** The backend applies none of it. Cart,
checkout and Sales Order accept exactly the quantities they accepted before, and
there is **no** "below MOQ" or "invalid step" error code — none was added, and
a regression test exists specifically to keep it that way.

* `moq` — the quantity your input **starts** at.
* `quantity_multiplier` — the **step counted from that start**.

```
moq 10 + multiplier 6  ->  10, 16, 22, 28
                       NOT 12, 18, 24
                       NOT "must be divisible by 6"
```

* only `moq` → start there, step by your existing default.
* only `quantity_multiplier` → start at your existing default, step by it.
* neither → unchanged behaviour.

**Apply either value only when `allowed` is `true`.**

### `allowed` — and what `false` does NOT mean

`false` means the authoritative pricing preview applied at least one Pricing Rule
(rate/discount rule, promotional scheme, Product Discount, free-item rule). Such
a rule can change behaviour at a quantity threshold, so "start at 10, step by 6"
stops being a promise the backend can keep.

**`allowed: false` does NOT mean the product cannot be purchased.** Fall back to
your ordinary quantity input and let the buyer enter any quantity. Do not try to
reverse-engineer the rule — the backend deliberately does not predict prices at
hypothetical quantities.

`moq` and `quantity_multiplier` are a **pair** under this one flag; there is no
per-field state. Both stay populated when `allowed` is `false`, for transparency
— they must simply not be applied.

**`mrp` is independent of `allowed`** and is displayed whenever non-null, because
it has no quantity behaviour and therefore no conflict:

```jsonc
{"rate": 700, "mrp": 1000,
 "quantity_control": {"moq": 10, "quantity_multiplier": 5, "allowed": false}}
```

### Normalisation

Blank, `0` and negative all normalise to `null` server-side for all three values.
`null` is the only "not configured" state you need to handle.

**FRONTEND ACTION** — none is forced; both fields can be ignored. To use them:
render `mrp` when non-null, and drive the quantity stepper from `moq` /
`quantity_multiplier` **only** when `allowed` is `true`.

**No listing changes.** `get_items`, `ListingCard`, `ProductSuggestion` and
`get_browse_categories` are untouched — these are buying-area facts and the
catalogue payload stays light. Phase 28 contracts are unaffected.

## 0. An `all_products` destination type (OpenAPI 3.11.0)

**Additive.** One new value in the `Destination.type` enum. No existing
destination changed shape, meaning or behaviour.

**OLD** — a merchant who wanted a menu item pointing at `/products` had to choose
`External URL` and type the route by hand. It worked (see 3.10.0), but the admin
label said "External URL" for a page that is not external, and the route was
merchant input rather than a contract.

**CURRENT** — `All Products` is a first-class menu destination type, alongside
`Home` and `Catalog`:

```
{"type":"all_products","target":null,"href":"/products",
 "external":false,"open_in_new_tab":false}
```

Like `home` and `catalog` it is a **fixed route**: `target` is null, `href` is
backend-owned and always exactly `/products`, and `external` is always `false`.
There is no field for a merchant to type a route into, so there is nothing to
mistype and nothing for the client to validate.

**The button's wording stays the merchant's.** `All Products` is the admin-facing
TYPE; the visible `label` is whatever they entered — "Products", "Shop All",
anything. Never render the type name.

**FRONTEND ACTION** — handle `all_products` exactly like `catalog`: route to
`href` with the SPA router. If your link component switches exhaustively on
`type`, add the case; if it already uses `href` + `external`, it needs no change
at all.

**`External URL` holding `/products` still works and is not deprecated in the
wire contract.** A menu configured before 3.11.0 keeps arriving as
`{type:"external_url", href:"/products", external:false}`. Both types land on the
same page with the same `href` and the same `external` flag, so a buyer cannot
tell which one a merchant used. Generic internal routes remain available for
destinations that have no type of their own, such as `/account`.

Unchanged in 3.11.0: every catalog contract — `ProductDetail`,
`ProductMerchandising`, `ProductPageDetail`, `VariantFamily`, `resolve_variant`,
`ProductSuggestion`, `BrowseCategory`, the ListingCard shape, `get_items` and
every CMS block schema.

## 0. Catalogue-wide browsing, and a smaller page (OpenAPI 3.10.0)

Three changes to `catalog.get_items`, plus one new endpoint. Additive except the
page-size ceiling, which is called out below.

### `scope_value` is now OPTIONAL — additive

**OLD** — a listing always named a category. Omitting `scope_value` answered
`validation_failed`.

**CURRENT** — omit it and `get_items` browses the whole public catalogue. This is
the scope behind the `/products` page.

```
GET …catalog.get_items                          -> the whole catalogue
GET …catalog.get_items?scope_value=power-tools  -> that category only
```

Only the SCOPE changes. Same card shape, same eligibility, same pricing, same
sort, same cursor, same `pagination` block, one card per family and never one per
variant. `query.scope_value` echoes `null`.

`scope_type` is untouched: `all` and `collection` remain reserved and still answer
`unsupported_scope`. Catalogue-wide is the ABSENCE of `scope_value`, not a scope
type.

**FRONTEND ACTION** — none for existing category pages. For `/products`, call
`get_items` with no `scope_value`, and note two rules:

* `storefront_filters` without `scope_value` answers
  `storefront_filter_context_required` — the selection is refused, not ignored,
  because which facets exist is a property of a category. Hide the facet UI when
  no category is selected.
* the scope is part of the cursor binding, so moving between the catalogue and a
  category means re-fetching from page one; a crossed cursor answers
  `cursor_invalid`.

### `page_size` maximum is 24 — **BREAKING for a client that sent 25..48**

**OLD** — `page_size` accepted 1..48, default 24.

**CURRENT** — `page_size` accepts 1..24, default 24. Out of range is still
**refused, not clamped**: 48 now answers `page_size_invalid` (422) rather than
returning 48 rows.

A page is a fixed unit of work; more products come from the cursor, not from a
bigger page. Every value the frontend actually sends today is 24 or less.

**FRONTEND ACTION** — if any call hard-codes a `page_size` above 24, lower it.
Nothing that omits `page_size` is affected.

### Search semantics documented accurately — no behaviour change

`search` has matched `item_name` **OR** `item_code` since 3.8.x (Phase 26A-1),
with words ANDed and each word satisfiable by either column. The `get_items`
parameter description had not said so. It does now. **No frontend action.**

### `catalog.get_browse_categories()` — new endpoint

Every enabled Storefront Category, **flat, at every depth**, for the `/products`
chip row:

```jsonc
{"categories":[
  {"name":"Power Tools","category_name":"Power Tools","slug":"power-tools",
   "parent_category":null,"is_group":1,"display_order":1,"level":0},
  {"name":"Drills","category_name":"Drills","slug":"drills",
   "parent_category":"Power Tools","is_group":0,"display_order":1,"level":1}]}
```

`get_categories` still answers ONE level at a time and is unchanged; it serves
tree navigation, where a buyer descends. Chips need the opposite, so this is a
second shape rather than a change to the first.

Metadata only — no items, prices, stock or listing work. Ordered by `level`, then
`display_order`, then label.

**Every row is a valid `get_items?scope_value=` target**, so a chip can be
rendered without checking anything first. Three kinds of category are withheld:
disabled, no slug, and **group** categories (`is_group = 1`), which hold
sub-categories rather than products and which `get_items` refuses with
`category_not_listable`.

There is no `is_group` field in the payload — every row is listable by
construction, so it could only ever be `0`.

**FRONTEND ACTION** — four rules:

* render any returned row as a chip; none of them can fail with
  `category_not_listable`.
* excluding groups does **not** flatten the tree to one level. A listable
  category at depth 1, 2 or 3 is still returned whatever its ancestors are, and
  `level` counts the full taxonomy including the groups that are not published.
  No aggregation is implied: a group is never returned as a chip listing its
  subtree, because `get_items` has no descendant recursion.
* `parent_category` may name a category that is **not** in this list — a listable
  child of a group keeps its real parent. It is a grouping key, not a chip
  reference.
* there is **no `All` category**. "All" is your own UI state, and selecting it
  means calling `get_items` with no `scope_value`. A disabled category never
  appears; an enabled child of a disabled parent still does.

### An `external_url` destination may be an INTERNAL route — bug fix

**No contract field changed.** `Destination` has always carried `external`; what
changed is that it is now correct for a case that previously produced `null`.

**OLD** — the merchant Menu accepted a single-leading-slash route (`/products`,
`/account`) in an `External URL` destination and saved it happily, but the runtime
projector demanded a scheme **and** a host. A saved route therefore projected as
`null` and **the menu item silently disappeared** from `cms.get_menu`.

**CURRENT** — a route projects properly:

```
/products              -> {type:"external_url", href:"/products", external:false}
https://example.com/x  -> {type:"external_url", href:"https://…",  external:true}
//example.com          -> null   (scheme-relative; unchanged)
javascript:…, data:…   -> null   (unchanged)
```

**FRONTEND ACTION** — **switch on `external`, not on `type`.** If your link
renderer treats `type === "external_url"` as "leaves the site" and builds a plain
anchor, an in-app route will do a full page load instead of a router navigation.
Use the SPA router when `external` is `false`.

This applies to **every destination surface** — menu items, image banners,
carousel slides and promo cards — because they share one projector. Nothing else
about them changed, and no other destination type is affected.

Unchanged in 3.10.0: `ProductDetail`, `ProductMerchandising`, `ProductPageDetail`,
`VariantFamily`, `resolve_variant`, `ProductSuggestion`, the ListingCard shape and
every CMS schema.

## 0. Product page and resolved SKU are separate schemas (OpenAPI 3.9.1)

**Contract correction. No runtime behaviour changed** — `get_item` and
`resolve_variant` return exactly what they returned in 3.9.0.

**THE DEFECT** — 3.9.0 added `gallery` and `sections` as **required** to
`ProductDetail`, which is also the schema `resolve_variant` returns. One schema
was doing two jobs, so a client generated strictly from that document expected
merchandising back from a variant selection that deliberately never sends it.

**CORRECTED**

```
ProductDetail          resolved SKU detail        -- NO merchandising
ProductMerchandising   { gallery, sections }      -- both required
ProductPageDetail      allOf: ProductDetail + ProductMerchandising
VariantFamily          allOf: family fields + ProductMerchandising
```

```
get_item         -> oneOf [ ProductPageDetail, VariantFamily ]
resolve_variant  -> ProductDetail
```

**FRONTEND ACTION** — regenerate from 3.9.1. If you hand-wrote a type for
`resolve_variant` that expects `gallery`/`sections`, remove them; the runtime
never sent them. Nothing about the product page changes: both `get_item` branches
still require both arrays.

The merchandising schemas themselves — `ProductGalleryImage`,
`ProductContentSection`, `ProductContentBlock` and the six block variants — are
untouched, as is `ProductSuggestion`.

## 0. Product Detail carries gallery and content (OpenAPI 3.9.0)

**Additive.** No existing Product Detail field changed meaning; two required
arrays were added to both branches.

**OLD** — a product page had one `image` and no merchant-authored content.

**CURRENT** — `catalog.get_item` also returns:

```
gallery    ProductGalleryImage[]      ordered; is_primary marks the opening image
sections   ProductContentSection[]    ordered; each has >= 1 block
```

with six block types: `rich_text` · `key_value` · `table` · `image` ·
`download` · `video`.

**FRONTEND ACTION** — render both from the existing `get_item` call; do **not**
add a second request. Treat array order as authoritative. Fall back to `image`
when `gallery` is empty (it is never synthesised into a row). Use the **new**
`ProductContentBlock` union — it is *not* the Phase 25 `ContentBlock`, and
`rich_text`/`image` are different shapes in the two unions.

`resolve_variant` is **unchanged** and deliberately carries neither key: selecting
a variant changes the SKU, price and stock, never the gallery or content, because
merchandising belongs to the template for a whole family.

`get_product_suggestions` is unchanged and stays its five lightweight fields.

## 0. Product search now matches the item code too (no version change)

**Behaviour change to an existing endpoint. No request or response field
changed, so the contract version stays 3.8.0.**

**OLD** — `search` matched the product **name** only, on both
`catalog.get_items` and `catalog.get_product_suggestions`. A buyer typing a code
fragment from a quote or a past order found nothing.

**CURRENT** — each search word matches the product **name OR its item code**.
AND across words is unchanged, and one word may be satisfied by either column:

```
"hex 10"          -> "hex" from the name, "10" from the code
"STO-ITEM-2026"   -> finds products by code fragment
```

**FRONTEND ACTION** — none required. Both endpoints changed together, on purpose:
the predicate is shared, so the header typeahead and the category listing still
describe exactly the same product universe. You may want to mention codes in
placeholder text now that they work.

Still not searchable: description, category, Item Group, Brand. No fuzzy
matching, no relevance ranking, and family collapse is unchanged — a variant
family is still returned once as the family, and a generated variant never
appears on its own even when its code contains the search text.

## 0. Header product suggestions (OpenAPI 3.8.0)

**New endpoint. Nothing existing changed.**

**OLD** — there was no typeahead; the only product query was the bounded
category listing.

**CURRENT**

```
catalog.get_product_suggestions(search)   up to 8 public products, no money
```

```jsonc
{"items":[{"item_code":"DRILL-001","item_name":"Cordless Drill",
           "slug":"cordless-drill","image":"/files/drill.jpg",
           "is_template":false}]}
```

**FRONTEND ACTION** — call it from the header only after **3 typed characters**;
below that it answers an empty list and does no work, and that is not an error.
Render `item_name` with `image`, and navigate by `slug`. Treat `is_template:
true` as a variant family — open the family page, do not offer add-to-cart.

**Do not show a price.** The payload deliberately carries no rate, discount, tax,
UOM, stock or warehouse, and it is not a `ListingCard` — do not type it as one.
There is no results page, no pagination and no `limit`; the 8-result cap is
server-owned. Enter is a no-op for this feature.

Suggestions are the same public products `get_items` would list — families
collapse to one row, generated variants never appear alone, and unpriced or
unrouted products are excluded — so a click always lands on a real product page.

## 0. Every content block carries a `content_width` (OpenAPI 3.7.0)

**Additive.** One more common field; no block payload lost or changed anything.

**OLD** — a block was always rendered inside the fixed content container, so a
hero banner or carousel could not span the full main width.

**CURRENT** — every projected `ContentBlock`, from both `cms.get_page` and
`cms.get_route_content`, carries:

```jsonc
{"type":"banner_carousel","block_name":"Homepage Hero",
 "section_style":"default","content_width":"full_width", …}
```

`content_width` is `contained` or `full_width`, published as the reusable
`ContentWidth` schema.

**FRONTEND ACTION** — inside the section you already render for `section_style`,
use `content_width` to decide whether to wrap the block in the fixed container or
render it directly:

```html
<section class="section-{{section_style}}">
  <div class="yob-content-container" *ngIf="contained">…block…</div>
  <ng-container *ngIf="!contained">…block…</ng-container>
</section>
```

**The two keys are INDEPENDENT** — all ten combinations are valid, and neither is
derived from the other. `full_width` is valid for **all five block types**, not
just banners. It controls horizontal containment only: background, spacing and
heights remain yours.

Like `section_style` it belongs to the **placement**, so the same Block may be
`full_width` in one location and `contained` in another — don't key layout off
`block_name`. Always present; rows predating the field project as `contained`, so
existing content renders exactly as it does today.

## 0. Every content block carries a `section_style` (OpenAPI 3.6.0)

**Additive.** No block payload lost or changed a field; one common field was
added.

**OLD** — a block was rendered inside whatever container surrounded it, so a
merchant could not give one block a distinct background band.

**CURRENT** — every projected `ContentBlock`, from both `cms.get_page` and
`cms.get_route_content`, carries:

```jsonc
{"type":"rich_text","block_name":"Welcome","section_style":"muted", …}
```

`section_style` is one of `default` · `muted` · `brand_soft` · `accent` · `dark`,
published as the reusable `SectionStyle` schema.

**FRONTEND ACTION** — wrap the existing block renderer in a full-width section
that maps the key to source-controlled CSS, with the existing fixed-width
container inside it. The block renderer itself needs no change.

**It belongs to the PLACEMENT, not the Block.** The same Block may return
`muted` on a page and `dark` on a route, so do not key styling off `block_name`
or cache a style per block. The value is always present — rows predating the
field project as `default` — so no null check is needed. The backend sends no
class names, colours or CSS.

## 0. Content Blocks can now appear inside application pages (OpenAPI 3.5.0)

**Additive.** `cms.get_page` and `/pages/:slug` are unchanged; no existing Page
Block was migrated.

**OLD** — a Content Block could only be reached through a merchant-authored
Storefront Page. An application page such as the cart could show no merchant
content at all.

**CURRENT** — the SAME blocks can also be placed into application-owned slots:

```
cms.get_route_content(route_key)   every slot of one route, in one request
```

```jsonc
{"route_key":"cart","slots":[
  {"key":"above_cart","blocks":[ …ContentBlock… ]},
  {"key":"below_cart","blocks":[]}]}
```

**FRONTEND ACTION** — call it **once per route**, not once per slot, and hand
each slot to its own content-slot component. `blocks` is the identical
`ContentBlock` union `get_page` already returns, so reuse the existing renderer
verbatim; a slot component must not know which block types exist. Render an empty
`blocks` as nothing. Treat any response containing a `product_grid` as
customer-specific and never cache it across users.

Angular owns where slots exist, the merchant owns what goes in them — adding a
slot is a code change in both repositories. `openapi.json` publishes the
route→slots table as `x-route-slots` so the constants can be generated. Login,
checkout, payment and the payment callback have no slots by decision and answer
the new `content_route_unknown`.

## 0. Content block schemas are now typed (OpenAPI 3.4.1)

**Documentation only — no runtime change.** `cms.get_page` returns exactly what
it returned in 3.4.0.

**OLD** — `slides` and `cards` were published as `array<object>`: an array of
something. `desktop_height_px` / `mobile_height_px` were listed once, under the
image-banner group, without saying which other block types return them.

**CURRENT** — two real schemas, `BannerCarouselSlide` and `PromoCard`, both
`{desktop_image, mobile_image, title, alt_text, destination}` with every key
always present, referenced from `slides` and `cards`. Every `ContentBlock`
property now names the block types that carry it, and the height fields are
documented as belonging to `image_banner`, `banner_carousel` and `promo_grid`
only — never `rich_text` or `product_grid`. `MenuItem.children` is likewise a
real self-reference instead of `array<object>`.

**FRONTEND ACTION** — sync the 3.4.1 reference and confirm your existing DTOs
match; there is nothing to re-implement. If you hand-wrote a slide or card type,
regenerate it from the schema now that one exists. A guard test asserts the
published schemas against blocks the runtime actually projected, so this cannot
drift again.

## 0. Storefront navigation, filters and content pages (Phase 25C)

**OLD** — navigation was hard-coded in the SPA, there were no merchandising
filters, and there was no dynamic content page.

**CURRENT** — three new read endpoints plus one additive parameter:

```
cms.get_menu(menu_key)                     published navigation tree
cms.get_page(slug)                         ordered, discriminated content blocks
catalog.get_category_filters(scope_value)  facets for a category (no counts)
catalog.get_items(..., storefront_filters) OR within a filter, AND across filters
```

**FRONTEND ACTION** — drive header and drawer from `get_menu` instead of a
hard-coded tree; build facet UI from `get_category_filters` and send the keys back
in `storefront_filters`; **restart pagination whenever the selection changes**
(the cursor is bound to it). Render blocks by `type`. Treat any response
containing a `product_grid` as customer-specific and never cache it across users.
`storefront_page` destinations carry a `null` href by design — build `/pages/${target}` on the client.

## 0a. Variant families: one page, one card, server-side resolution

**OLD** — every ERPNext variant was listed as its own product card, and every
variant carried its TEMPLATE's slug, so a product URL resolved to an arbitrary
sibling. There was no attribute data anywhere.

**CURRENT** —

```
catalog.get_items          one card per simple Item and one per FAMILY,
                           never one per variant. New on every card:
                           has_variants, price_state ("priced" | "select_options").
                           A family card's money fields are all null.
catalog.get_item           on a family slug returns is_template/is_purchasable,
                           attributes[] and variants[] and NO price.
catalog.resolve_variant    NEW. (template, attributes, qty) -> the full resolved
                           product payload, same shape as a simple product page.
cart.add_to_cart           unchanged signature; a template answers item_is_template
                           (422) and an unsalable SKU item_not_purchasable (422).
```

**FRONTEND ACTION** — render a family page from `attributes[]`, disable any pair
missing from `variants[]`, call `resolve_variant` for the chosen combination, then
`add_to_cart(data.name, qty)`. Never build an item code, never cross attribute
values, never sort `values` (they arrive in the merchant's order), and never show a
price on a family card — use `price_state`.

## 0. Prices and quantities are in the item's SELLING UOM

**OLD** — the product page priced in the item's selling UOM while the Cart and
the Sales Order used the stock UOM. For an item sold in Boxes of 10 at ₹100/Nos
the page said **₹1000 per Box** and the cart charged **₹100 per Nos** for the same
input. Frontends that read `stock_uom` as "the unit" were reading the cart's side
of that disagreement.

**CURRENT** — one unit end to end, resolved by ERPNext (`sales_uom`, else
`stock_uom`) and recorded on the cart line. `quantity` is counted in the line's
`uom`; `rate` is per that unit. New response fields, all additive:

```
catalog.get_item     conversion_factor, stock_qty      (uom, stock_uom already existed)
catalog.get_items    uom, conversion_factor            (stock_uom already existed)
cart.get_cart        uom_changed_items[]               (reconciliation list)
```

**FRONTEND ACTION** — display `uom` beside every quantity ("2 Strips"), treat
`rate`/`base_price` as per-`uom`, and never convert units yourself: use
`stock_qty` when you need stock units and `actual_qty` (already in `stock_uom`)
for availability. Surface `uom_changed_items[]` like `removed_items[]` — it means
a stored quantity is now worth something different because the merchant changed
the item's conversion factor.

## 0b. `add_to_cart` can answer `cart_item_uom_changed` (409)

**OLD** — `add_to_cart` always merged a repeat add into the existing line for that
SKU.

**CURRENT** — it still does, unless the merchant changed the item's selling unit
after that line was priced. Then the quantity being sent and the quantity already
stored are counted in different units, so the call is refused with
`cart_item_uom_changed` and the cart is left exactly as it was. `details` carries
`item_code`, `existing_uom`, `current_uom`.

**FRONTEND ACTION** — surface it as "this item is now sold in <current_uom>",
offer `remove_from_cart` followed by a fresh `add_to_cart`, and do not convert
quantities or retry blindly.

## 1. Cart setters return an acknowledgement, not a Cart

**OLD** — earlier docs did not pin the setter response shape; it was natural to
assume a setter returned the updated Cart.

**CURRENT** — verified by execution:

```
set_cart_contact          → { "contact_person": "…" }
set_cart_billing_address  → { "billing_address": "…", "shipping_address": "…" }
set_cart_shipping_address → { "shipping_address": "…" }
```

No totals, no `is_shippable`, no items, no reconciliation flags.

**FRONTEND ACTION** — after every setter, call `get_cart` and ingest the
canonical Cart. Do not merge the setter response into cart state as if it were
one.

## 2. Billing auto-fills shipping

**OLD** — not documented.

**CURRENT** — `set_cart_billing_address` returns **both** fields; when shipping
is unset, setting billing also sets shipping.

**FRONTEND ACTION** — reflect both values from that response, then refresh with
`get_cart`. Do not assume shipping is untouched.

## 3. `is_shippable` is server-owned and volatile

**OLD** — treated as a stable cart property.

**CURRENT** — derived on **every** reprice from whether any line is a stock
item. It can flip on any Cart response.

**FRONTEND ACTION** — re-read it from each Cart response. Never cache across a
mutation; never compute it client-side. `shipping_not_applicable` is the error
when you set a shipping address on a non-shippable cart.

## 4. Cart mutation responses differ in shape from `get_cart`

**OLD** — assumed one Cart shape.

**CURRENT** — `add_to_cart` and `remove_from_cart` return the **flat Cart
document**; `get_cart` returns an **envelope** (`cart`, `contact`,
`billing_address`, `shipping_address`, `cart_updated`, `removed_items`,
`price_updated_items`).

**FRONTEND ACTION** — two parsers, or normalise deliberately in one place. Treat
`get_cart` as canonical.

## 5. Public payment is token-authorized, not session-authorized

**OLD** — the payment page was reachable only after checkout, so a session was
implicitly assumed.

**CURRENT** — `/payment/<token>` is `allow_guest`. Verified in a real incognito
browser with no `sid`: full Razorpay payment completed.

**FRONTEND ACTION** — put the payment route **outside** the session guard. Do
not redirect to login. See `AUTHORIZATION-MATRIX.md`.

## 6. `get_checkout_data` now serves TWO source shapes

**OLD** — a single Cart-shaped payload.

**CURRENT** — branch on `data.source_doctype`:
`"Cart"` (before initiation) or `"Sales Order"` (after commitment). In the Sales
Order shape, `billing_address`/`shipping_address` are **name strings**, not
objects, and `items[]` is flat.

**FRONTEND ACTION** — handle both. A browser refresh after paying lands on the
Sales Order shape; assuming Cart-backed will crash the page.

## 7. `process_payment` request fields

**OLD** — assumed a richer payload might be needed.

**CURRENT** — the signature is exactly `token` and `payment_method`. Nothing
else is accepted.

**FRONTEND ACTION** — send only those two. Remove any customer/cart/order/
amount/currency/address/contact fields.

## 8. `payment_method` is `name`, not `method_code`

**OLD** — `method_code` (`"razorpay"`, `"paylater"`) looked like the identifier.

**CURRENT** — the backend expects the Payment Method record **`name`**
(e.g. `"Razorpay"`, `"Pay Later"`). Sending `method_code` answers
`payment_method_unsupported`.

**FRONTEND ACTION** — send `method.name` from `payment_methods[]`. `method_code`
remains available for display/branding only.

> Note: the **response** still echoes `payment_method` as the lowercase code
> (`"razorpay"`, `"paylater"`). Request and response use different values —
> intentional, for backward compatibility.

## 9. `proceed_to_payment` returns 200 **or** 201

**OLD** — a single success status assumed.

**CURRENT** — **201** when a Payment Request was created; **200** when an
existing open obligation was reused (same token).

**FRONTEND ACTION** — treat both as success. Do not branch on 201 alone.

## 10. Token is revoked after settlement

**OLD** — not specified.

**CURRENT** — on successful payment the token is cleared. Reusing that URL
answers `checkout_token_invalid` (404).

**FRONTEND ACTION** — treat it as a **terminal** state (show "payment
complete"), not a retryable error.

## 11. Razorpay `amount` is in paise

**OLD** — ambiguous.

**CURRENT** — `process_payment` (Razorpay) returns `amount` in **provider minor
units**: `13500` = ₹135.00. Everywhere else `amount` is the business amount.

**FRONTEND ACTION** — pass it to Razorpay Checkout **unchanged**. Do not
multiply or divide. Do not display it as a currency figure without converting.

## 12. New payment error codes

**OLD** — the earlier `checkout_payment` code list predated the payment work.

**CURRENT** — added: `payment_request_stale` (409),
`payment_provider_error` (500 **or 422**).

Also: `payment_provider_error` and `payment_provider_not_configured` now carry a
`details` object with `retryable` (bool) and, when relevant, `sales_order`.

**FRONTEND ACTION** — add both codes. **Branch on `details.retryable`** for
provider errors: `true` means the order exists and a retry is correct; `false`
means offer another method. See `ERROR-CODES.md`.

## 13. Provider failure after commitment does not mean "order failed"

**OLD** — a provider error would naturally be shown as a failed order.

**CURRENT** — `payment_provider_error` with `details.retryable: true` and
`details.sales_order` means the Sales Order **was committed and still exists**.

**FRONTEND ACTION** — offer a retry against the same payment link. Do not tell
the user their order failed.

## 14. `get_payment_methods` needs `company`

**OLD** — treated as optional.

**CURRENT** — Payment Method Assignments are typically Company-scoped; omitting
`company` returns an **empty list** with no error.

**FRONTEND ACTION** — always pass `company` (from the cart or `cms.get_config`).
On the public payment page, prefer `payment_methods[]` from `get_checkout_data`.

## 15. `address.add_contact` exists

**OLD** — missing from the earlier OpenAPI (only `add_address` was listed).

**CURRENT** — `POST address.add_contact`, mirroring `add_address`.

**FRONTEND ACTION** — you may create contacts inline at checkout.

## 16. `add_to_cart` `qty` is a DELTA, not an absolute quantity ⚠️

**This is the highest-risk item in this changelog.** It was raised as a
suspected error in the handoff and re-verified by execution; the handoff was
right and the earlier assumption was wrong.

**OLD / EARLIER ASSUMPTION** — `qty` **sets** the line quantity:

```
line has qty 2  →  add_to_cart(item, qty=5)  →  line becomes 5
```

**CURRENT BACKEND TRUTH** — `qty` is **added** to the existing line:

```
line has qty 2  →  add_to_cart(item, qty=5)  →  line becomes 7   (one row)
```

Proven by execution against the running backend, and stated explicitly in
`api/cart.py`:

```python
existing.quantity = (existing.quantity or 0) + qty
```

**FRONTEND ACTION — two things, both important:**

1. **A quantity stepper must send the delta, not the new total.** Moving 2 → 5
   means sending `qty=3`. Sending `qty=5` produces 7.
2. **`add_to_cart` is NOT idempotent.** A retried, double-submitted or
   double-clicked request **adds twice**. Guard the button, and do not retry
   this call automatically on network failure — a timeout may already have
   applied.

There is **no absolute "set quantity" endpoint** today. To set an absolute
value you must either compute the delta client-side, or
`remove_from_cart` followed by `add_to_cart` with the target quantity.

> The backend comment explains why one row is used rather than appending a
> second: ERPNext evaluates a Pricing Rule's `min_qty`/`max_qty` against the
> **row** quantity, so two rows of 5 would silently miss a `min_qty=10` rule
> that one row of 10 satisfies.

## 17. Other Cart semantics unchanged — and the redesign is still deferred

**OLD/CURRENT (no change)** — one row per `item_code`, fractional quantities,
whole-line removal, whole-cart expiry.

**Still DEFERRED, do not implement:** independent duplicate rows, stable line
identity, immutable line quantity, per-line expiry.

**FRONTEND ACTION** — none. Listed so the deferred design is not mistaken for
current contract.

## 18. Order detail: live Address objects replaced by immutable snapshots ⚠️

**OLD** — `get_order_details` returned resolved objects built by reading the
linked Address **master** live:

```json
{ "billing_address":  { "address_title": "...", "address_line1": "...", ... },
  "shipping_address": { ... } }
```

That made order history **mutable**: editing an address rewrote the address on
every past order. For an invoice-grade record that is silent data corruption —
nothing errors and the totals still agree.

**CURRENT** — explicit historical display fields, from the order's own
order-time snapshot (`Sales Order.address_display` and
`Sales Order.shipping_address`):

```json
{ "billing_address_name":     "Example Billing-Billing",
  "shipping_address_name":    "Example Shipping-Shipping",
  "billing_address_display":  "A401 Example House\nExampleton\n...",
  "shipping_address_display": null }
```

`billing_address` and `shipping_address` **are removed**. This is a deliberate
pre-deployment contract correction, not a compatibility break to work around: we
are not carrying historically mutable data forward for a frontend we control.

**FRONTEND ACTION:**

1. Stop binding `billing_address.*` / `shipping_address.*` object fields.
2. Render `*_address_display` as **plain text** — it contains real newlines
   (`white-space: pre-line`, or split on `\n`). It is **not** HTML; do not use
   `[innerHTML]`.
3. Keep using `*_address_name` only as an identifier (e.g. "reuse this
   address"), never as the order's rendered address.
4. Both display fields are `string | null` in **every** case — normal, legacy
   fallback, and missing. They never become objects, so no union type is needed.

> Order-time snapshots exist on all current real orders, so normal orders need
> no Address read at all. A legacy order with a blank snapshot falls back to the
> current master as best effort — still as the same string type. If that Address
> is gone too, the field is `null` and the rest of the order still renders.

## 19. `get_orders` rows now carry `currency`

**OLD** — order-list rows had no currency, so the client had to infer it from
environment configuration.

**CURRENT** — every row includes the Sales Order's own stored `currency`.

**FRONTEND ACTION** — format each row with `row.currency`. Remove any
environment-derived currency fallback in the order list: an order placed in a
different currency would otherwise render as the wrong money.

## 20. `get_orders` ordering is server-owned: `creation` desc

**OLD** — the ordering was never documented, so a client could reasonably sort
the array itself, most plausibly by `transaction_date`.

**CURRENT** — the server returns rows **newest first, by `creation`**. Confirmed
by observation against real orders.

The distinction matters: `creation` is a timestamp, `transaction_date` is a
date. Orders placed on the same day share a `transaction_date` but still have a
correct newest-first order — and **`creation` is not in the response**, so a
client sorting by `transaction_date` cannot reproduce it and will scramble
same-day orders.

**FRONTEND ACTION** — render the array in the order received. Remove any
client-side sort of the order list. There is still no paging, filter or search
parameter; `get_orders` takes none.

## 21. `update_address` is a partial update — stop padding the payload ⚠️

**OLD** — `update_address` assigned **every** field unconditionally from the
request. A field you did not send was written as blank, so an edit form that
posted only the inputs it rendered destroyed `address_line2`, `phone`,
`email_id` and the `is_primary_address` / `is_shipping_address` flags. The call
returned **success**, so nothing indicated data had been lost.

The only defence was to resend a complete Address object on every edit.

**CURRENT** — a genuine partial update, keyed on request **presence**:

| You send | Result |
|---|---|
| field omitted | unchanged |
| field with a value | validated and applied |
| explicit `""` (optional field) | cleared |
| invalid value | `validation_failed`, record untouched |

**FRONTEND ACTION — and this reverses previous advice:**

1. **Send only the fields your form edits.** Do not resend untouched optional
   fields to preserve them; that workaround is obsolete.
2. **Do not pad the payload with `""`.** An explicit empty value is now a
   deliberate *clear*. Padding would destroy exactly the data the old workaround
   existed to protect.
3. A partial payload no longer fails: `{name, address_line1}` used to answer
   `internal_server_error` (blanking the India-Compliance-mandatory
   `gst_category`), and now succeeds.

`update_contact` already behaved this way and still does — the two are now
consistent, and both are idempotent.

## 22. Deleting an address or contact can be refused — 409, not 500 ⚠️

**OLD** — a delete blocked by link integrity produced:

- `delete_address` → generic **500 `internal_server_error`** — indistinguishable
  from a backend crash;
- `delete_contact` → **no envelope at all**: a raw Frappe `LinkExistsError` at
  **HTTP 417**, with `_server_messages` carrying HTML, the referring document's
  name, and an absolute Desk URL (`http://<host>/desk/cart/CART-…`).

**CURRENT** — one business error on both:

```json
{ "message": { "errors": [
  { "code": "address_in_use", "field": "name",
    "detail": "This address is currently in use and can't be deleted." } ] } }
```

**409**, `address_in_use` / `contact_in_use`. Refused when a Cart has the record
selected, a historical Sales Order references it, or it is the Customer default.
No Desk HTML, no `_server_messages`, no referring docname.

**FRONTEND ACTION** — handle 409 as its own outcome:

1. It is **not** 404. The record still exists and stays in the list.
2. It is **not** retryable — nothing is detached automatically. The user must
   change the Cart selection first.
3. Render your own copy from the code; the body has nothing to parse.

## 23. Do not auto-retry a delete

**OLD** — not specified, so a generic mutation-retry layer would cover deletes.

**CURRENT** — unchanged behaviour, now stated: these endpoints have no
request-deduplication.

```
delete succeeds -> response lost -> retry -> 404 not_found
```

A retry turns a **success** into what looks like an error.

**FRONTEND ACTION** — on an uncertain delete, **re-read the list** and check
whether the record is gone. Updates are safe to repeat; deletes are not. Exclude
these endpoints from any blanket retry policy.

## 24. Account list caches now invalidate on write

**OLD** — `get_addresses` / `get_contacts` are cached for 30 minutes, and the
invalidation was broken: the cache-clear helper received the Customer *document*
where the key is built from the customer *name*, so the key never matched. A
list read after a write returned **stale data until the TTL expired**.

**CURRENT** — every mutation invalidates the list. A read immediately after a
write returns the new state.

**FRONTEND ACTION** — re-read the list after a successful mutation and trust the
result. Remove any cache-busting parameter, forced delay or optimistic-state
workaround added to compensate.

---

## Explicitly unchanged

- Frappe outer `message` envelope, and the YOB `data`/`notice`/`meta` vs
  `errors[]` inner model.
- CSRF on non-GET authenticated calls.
- HttpOnly `sid`, browser-managed.
- Catalog and auth request/response shapes.
- All pre-existing error codes keep their published values.

> **Orders are NOT in this list.** An earlier revision of this file listed
> "catalog, auth, and orders" as unchanged, which contradicted items 18–19 on
> the same page. Order detail and the order list both changed — see those two
> entries.
