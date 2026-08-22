# CHG-002 — Phase 25F Sign-off Report

Status: `PASS with live environment smoke outstanding`

Owner: `Nilesh`

Date: `2026-08-22`

Verification site: `test.localhost`. `yob.localhost` was not migrated,
reinstalled or subjected to write-path tests.

---

## 1. What Phase 25F was for

Not development. Phase 25F asks one question about work already signed off:

> Do the links FIT?

Every stage of the storefront chain already had tests. None of them could catch a
**seam** defect, because each test supplied its own inputs. A test that calls
`get_category_filters("power-tools")` cannot notice that navigation publishes a
docname where the filter endpoint expects a slug — the test typed the slug
itself.

So the proof is a chain in which **every step consumes the published output of
the step before it**, and no step is fed a constant a test author wrote:

```text
Frappe admin configuration
  -> Menu                      cms.get_menu
  -> category slug             destination.target -- the ONLY identity published
  -> Category Filter Set       catalog.get_category_filters
  -> filtered listing          catalog.get_items(storefront_filters=...)
  -> dynamic Storefront Page   cms.get_page
  -> all five Blocks
  -> Product Grid              the SAME catalogue service
  -> simple product / family
  -> variant resolution        catalog.resolve_variant
  -> quantity
  -> Cart                      cart.add_to_cart
```

`tests/test_storefront_chain.py` — **18 tests, all passing.**

## 2. Link-by-link evidence

| # | Link | Proved by | Result |
|---|---|---|---|
| 1 | Desk configuration → Menu | `test_a_merchants_menu_becomes_navigation` | Group + leaf published in the merchant's order |
| 2 | Menu → public identity only | `test_navigation_publishes_no_database_identity` | no `link_category` / `link_page` / `link_item`, no docname |
| 3 | Published slug → Category Filter Set | `test_the_slug_from_navigation_resolves_its_category_filters` | the slug navigation returned resolves its own facets |
| 4 | Item set ⊃ Category set | `test_item_metadata_wider_than_the_category_stays_hidden` | `material` held on the Item, never exposed by the category |
| 5 | Values limited to the category | `test_only_values_present_in_the_category_are_offered` | only 230V / 415V offered |
| 6 | Facet keys → filtered listing | `test_the_published_facet_keys_are_accepted_verbatim` | keys accepted as published; non-matching item excluded |
| 7 | Filtering actually narrows | `test_an_unfiltered_listing_is_wider_than_the_filtered_one` | filtered ⊂ unfiltered |
| 8 | Cursor bound to the selection | `test_a_cursor_cannot_cross_from_one_selection_to_another` | `cursor_invalid` |
| 9 | Page → five Blocks | `test_the_page_returns_all_five_blocks_in_the_merchants_order` | exact type sequence |
| 10 | Blocks carry renderable content | `test_every_block_carries_its_own_renderable_content` | images, prose, slides, cards, heights |
| 11 | Grid bound to the menu's category | `test_the_grid_is_bound_to_the_same_category_navigation_published` | grid `category` == `destination.target` |
| 12 | One destination contract | `test_a_slide_and_a_promo_card_share_one_destination_contract` | banner == slide == card |
| 13 | Grid cards ARE catalogue cards | `test_the_grid_serves_the_same_cards_as_the_catalogue` | byte-identical rows |
| 14 | Simple priced, family not | `test_a_family_card_carries_no_price_and_a_simple_one_does` | `priced` 150.00 vs `select_options`, `rate: null` |
| 15 | No variant merchandised | `test_no_generated_variant_is_ever_merchandised` | children absent from the grid |
| 16 | Family → resolve → qty → Cart | `test_the_grids_family_card_resolves_and_reaches_the_cart` | Cart holds the resolved SKU, qty 3, at the previewed rate |
| 17 | A family can never be bought | `test_the_family_the_grid_advertised_can_never_be_bought` | `item_is_template`, Cart untouched |
| 18 | Buyer authority | `test_the_buyer_controls_only_the_product_and_the_quantity` | no endpoint in this chain accepts UOM, warehouse, price list, rate |

Link 16 is the whole governing rule in one assertion: the family page was opened
by the slug **the grid card published**, the resolver was given the attribute
values **that page advertised**, and the Cart was sent the SKU **the resolver
returned** — with a quantity, and nothing else.

## 3. Backend test result

```
bench --site test.localhost run-tests --app yob_storefront
763 tests, 2 skipped, 0 failures, 0 errors
```

The two skips are the pre-existing GST CGST/SGST-vs-IGST split assertions: the
dev company is `gst_category: Unregistered` with no GSTIN, so India Compliance
declines to classify GST accounts. Numeric tax parity is still asserted. This is
unrelated to Phase 25 and is not a Phase 25F finding.

## 4. Cross-stack contract

The published reference is **OpenAPI 3.4.1**, 39 paths, 26 schemas.

Phase 25C-1 closed the one gap Angular had recorded independently: `slides` and
`cards` were published as `array<object>`, and the height fields were documented
under `image_banner` only although the runtime also returns them for
`banner_carousel` and `promo_grid`. Angular's Phase 25E had already typed both
correctly by reading `content_service._media_row` directly — so two independent
readings of the projector agreed, which is corroboration rather than luck.

Angular's DTOs were then checked field-by-field against 3.4.1:

| Contract | Angular type | Verdict |
|---|---|---|
| `x-block-fields.image_banner` | `ImageBannerBlockDto` | match |
| `x-block-fields.rich_text` | `RichTextBlockDto` | match |
| `x-block-fields.banner_carousel` | `BannerCarouselBlockDto` | match |
| `x-block-fields.product_grid` | `ProductGridBlockDto` | match |
| `x-block-fields.promo_grid` | `PromoGridBlockDto` | match |
| `BannerCarouselSlide` | `ContentMediaRowDto` | match |
| `PromoCard` | `ContentMediaRowDto` | match |
| `PageResponse.data` | `StorefrontPageDto` | match |

**No Angular application change was required**, exactly as predicted. Angular
models both media rows as one `ContentMediaRowDto` while the contract publishes
two schemas of identical shape; that is deliberate on both sides — the backend
keeps them separate because they are separate stored types that may diverge.

The mirrors in `docs/api-handoff/` and `reference/api/` were synced to 3.4.1
(`yob-frontend` commit `8fce36a`), and the Angular suite was re-run against them:

```
938 tests, 50 files, 0 failures
```

Unchanged before and after the sync — which is the result the sign-off wanted.

## 5. What now guards each contract

| Boundary | Guard |
|---|---|
| endpoint published at all | `TestPublishedApiReference` (Phase 24D-1) |
| error code published | `TestPublishedApiReference` |
| block SHAPE published | `PublishedBlockShapeCase` (Phase 25C-1) — asserts the schemas against blocks the runtime actually **projected** |
| the links fit | `ChainCase` (Phase 25F) |

The schema↔DTO comparison in §4 was performed as a one-off verification. A
permanent Angular-side guard reading the mirrored `openapi.json` would make it
self-enforcing; **recommended, not blocking** — the backend guard already binds
the schema to the runtime, so the DTOs can only drift by an Angular edit.

## 6. Live environment smoke — OUTSTANDING

No live credentials are available in this environment, so nothing below was
executed against a real deployment:

- a browser session against a real Frappe site with real merchant configuration;
- the Angular SPA rendering a real menu, facets, page and grid;
- a real buyer completing resolve → add to cart in the browser.

Per the owner's direction this does **not** block Phase 25F sign-off and does
**not** justify further architecture work. It moves to the
**pre-production/deployment checklist**:

1. configure one Menu, one Category Filter Set and one Storefront Page in Desk;
2. load the SPA and confirm the header renders that menu;
3. filter a category and confirm the URL, the facets and the narrowed grid;
4. open `/pages/<slug>` and confirm all five blocks render;
5. open a family from a Product Grid, resolve a variant, set a quantity, add to
   cart, and confirm the Cart line matches the previewed SKU and rate.

Each step above already has an automated equivalent listed in §2. The smoke test
confirms deployment and configuration, not architecture.

## 7. Verdict

**PASS with live environment smoke outstanding.**

The chain from Frappe admin configuration to Cart is proved end to end at the
backend, the published cross-stack contract is exact and machine-checked in both
directions, and Angular required no change to satisfy it. Phase 26 may begin.
