# Pharmacy owner requirements: where each one lives

A pharmacy owner sent this list. Each row names the screen a pharmacist or
owner opens and the tests that prove it works. Everything below is in the
**Pharmacy** vertical; rows marked *shared* also apply to distributors (and
the money and staff rows to schools/estates), per `verticals/features.py`.

Roles: owner = `superuser`, `admin`, `pharmacist`, `cashier`,
`stock_clerk`, `accountant`.

## Point of sale

| Requirement | Screen | Tests |
|---|---|---|
| The basics: sell, search, scan, cash, credit, receipts, void, refund | Sell > Point of sale | `test_pos_billing.py`, `test_pos_services.py` |
| Percentage discounts (*shared*) | Point of sale: tag icon per line, "Discount on sale". Over the business cap (Settings, default 10%) a manager code is texted to admins | `test_pos_owner.py::test_line_discount_by_percent`, `test_cashier_over_the_cap_needs_a_manager_code`, `test_cap_counts_line_and_sale_discounts_together`, `test_removing_padding_lines_cannot_keep_a_big_discount`, `test_parallel_line_discounts_cannot_both_pass_the_cap` (MySQL) |
| Get payment from Safaricom or the bank before the receipt prints (*shared*) | Point of sale > M-Pesa (STK prompt; the sale completes and the receipt prints only when Daraja confirms) or Bank (bank name + reference recorded first). Money > Payments to verify for typed codes and bank refs | `test_pos_billing.py::test_mpesa_completion_refused_until_money_arrives`, `test_pos_owner.py::test_bank_sale_completes_only_after_the_reference_is_recorded`, `test_a_bank_reference_is_used_once`, `test_accountant_verifies_a_bank_payment`, `test_rejecting_a_bank_payment_reopens_the_invoice` |
| Batch and expiry tracking | Sale lines take the soonest-expiring batch that is not expired or recalled; the till shows the next expiry; the printed receipt shows batch and expiry per line. Stock > Batches & expiry | `test_stock.py`, `test_pos_owner.py::test_printed_receipt_shows_batch_expiry_and_who_served`, `test_writeoffs_recalls.py::test_batch_list_statuses_and_values` |
| Patient medication history, monthly | Patients > Patients: search, then a patient's month-by-month history (item, quantity, batch, expiry, prescriber, served by), current chronic medicines and next refill. Printable | `test_patients.py` (19 tests), e.g. `test_sales_group_into_nairobi_months`, `test_rows_carry_batch_expiry_and_who_served`, `test_prescription_and_refill_rows` |

## Inventory

| Requirement | Screen | Tests |
|---|---|---|
| Low stock alerts (*shared*) | Dashboard "Today" tile and attention list; Stock > Batches & expiry > Low stock; SMS to admins after a sale takes an item to its reorder level | `test_writeoffs_recalls.py::test_low_stock_endpoints_agree_on_stock_at_or_below_reorder_level`, `test_owner_dashboard.py::test_admin_sees_money_stock_and_attention` |
| Damaged and expired medicines | Stock > Batches & expiry: write off a chosen batch with a reason (damaged, expired, lost, returned to supplier, count correction). Stock > Write-offs: history and monthly loss at cost | `test_writeoffs_recalls.py::test_write_off_takes_from_the_chosen_batch_not_the_soonest`, `test_expired_reason_only_for_an_expired_batch`, `test_controlled_write_off_is_one_witnessed_destruction_entry`, `test_write_off_report_totals_equal_the_rows` |
| Recalled medicines | Stock > Recalls: recall a batch (held from sale for good), see who bought it, text them once, write off what is left | `test_recall_holds_the_batch_and_sales_skip_it`, `test_a_recalled_batch_is_never_released`, `test_buyers_are_net_of_returns_and_include_orders`, `test_notify_texts_each_buyer_once` |

## Procurement

| Requirement | Screen | Tests |
|---|---|---|
| Purchase order generation (*shared*) | Purchasing > Purchase orders: "Draft from low stock" (optionally per supplier), receive deliveries into batches, PDF to send the supplier | `test_pricing_purchasing.py::test_draft_from_low_stock_all_items`, `test_draft_from_low_stock_for_one_supplier`, `test_po_pdf` |
| Automatic purchase and selling prices (*shared*) | Purchasing > Pricing: markup per item or business default, rounding to 1/5/10 shillings, auto items re-priced on every delivery, manual items get a suggested price, below-cost warnings | `test_selling_price_table`, `test_po_receive_reprices_auto_item_and_audits`, `test_reprice_manual_item_keeps_price_but_suggests`, `test_pricing_table_flags_below_cost_and_low_margin` |

## Financial management (*shared*)

| Requirement | Screen | Tests |
|---|---|---|
| Daily sales and profit reports | Money > Daily report: per Nairobi day sales, discounts, refunds, VAT, cost of goods, gross and net profit, by payment method; CSV | `test_finance_reports.py::test_daily_totals_equal_the_sum_of_sales`, `test_days_are_nairobi_days`, `test_daily_csv` |
| Cashier reconciliation (cash counted vs system) | Money > Reconciliation: per shift and per cashier, expected vs counted cash, M-Pesa confirmed vs typed codes, bank refs; variance over the business limit texts the admins | `test_shift_expected_cash_and_variance`, `test_mpesa_and_bank_checks`, `test_variance_sms_goes_once_and_only_over_the_line` |
| Gross and net profit | Daily report tiles; cost from the batches each sale used; expenses (Money > Expenses) give net profit; items with no cost are flagged, never counted as pure profit | `test_cogs_comes_from_the_batches_a_sale_drew_from`, `test_a_line_without_any_cost_is_flagged_not_hidden`, `test_net_profit_is_gross_profit_less_expenses` |
| Financial performance graphs | Daily report charts (daily revenue and gross profit, payment split, top items, 12-month net profit); dashboard 7-day chart | `test_monthly_net_profit`, `test_top_items_and_categories_rank_by_gross_profit`, `test_owner_dashboard.py` |

## Employees (*shared*)

| Requirement | Screen | Tests |
|---|---|---|
| Multiple user accounts and roles | Staff > Users & roles (owner, admin, pharmacist, cashier, stock clerk, accountant) | `test_login_security.py::test_accountant_is_a_role_choice_and_unknown_roles_are_refused` |
| Role-based access permissions | Staff > Roles & permissions: matrix read from the code's role lists; every page and API checks role and business type | `test_staff_activity.py::test_permission_matrix_is_read_from_the_role_tuples`, `test_features.py::test_every_nav_link_opens` |
| Salesperson and cashier performance | "Served by" at the till; Staff > Performance: sales, average basket, discounts given, voids/refunds, cash variance per person | `test_staff_activity.py::test_performance_splits_cashier_and_salesperson`, `test_pos_owner.py::test_served_by_and_customer_on_a_sale` |
| Login security and activity monitoring | 5 wrong passwords lock the account for 15 minutes (admin can unlock), login rate limit, idle sign-out; Staff > Activity log of sign-ins and actions | `test_login_security.py` (24 tests), `test_staff_activity.py::test_activity_is_this_business_only`, `test_failed_logins_summary` |

## General

| Requirement | Screen / how | Tests |
|---|---|---|
| Backup and recovery | Settings > Backup: last backup, back up now, download (single-shop install). Nightly automatic backup, verify and rotation in the `backup` service; `flask backup restore` with confirmation and a pre-restore copy. Dashboard warns after 26 h | `test_backup.py` (41 tests), e.g. `test_restore_takes_a_pre_restore_backup_then_restores`, `test_verify_detects_a_corrupted_file` |
| One server shared by several computers | `deploy/compose.yml` on a shop PC: every till opens `http://<server-ip>`; see `docs/install-local.md` | `test_compose_file_defines_the_services_and_profiles` |
| Track and access the system online | Cloudflare Tunnel profile on the shop server, or the VPS profile with TLS (`docs/install-vps.md`) | `test_every_compose_variable_is_documented_in_env_example` |

## Not done yet

- Real Daraja and Africa's Talking sandbox runs (issue #3).
- A full compose run against MySQL 8.0 on a shop PC, including the MariaDB
  `mysqldump` client against MySQL 8 authentication.
- Bank API confirmation: bank payments are confirmed by an admin against
  the statement, not by the bank automatically.
- PPB register column layout still to be checked with a pilot pharmacist
  (issue #4).
