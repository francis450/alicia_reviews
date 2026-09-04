# Copyright (c) 2026, Alicia Hairline and Beauty and contributors
# For license information, please see license.txt

"""Register Alicia's AI Copilot tools in the `Copilot Tool` registry.

Handlers live in alicia_reviews.copilot_tools. This patch is idempotent: it
inserts missing rows and refreshes the description / schema / mutating flag on
rows it already created, so editing a tool here and re-running `bench migrate`
is enough to roll the change out.

Safe to run without ai_copilot installed -- it no-ops if the doctype is absent.
"""

import frappe

HANDLER = "alicia_reviews.copilot_tools"

TOOLS = [
	{
		"tool_name": "sales_summary",
		"label": "Sales Summary",
		"is_mutating": 0,
		"description": (
			"Total sales revenue, invoice count and average ticket over a date range, with an "
			"optional breakdown. Use this for any 'how much did we sell', 'revenue this month', "
			"'sales today', 'this week vs last week', or 'sales by category / payment method / "
			"cashier' question -- do NOT use search_documents for these, it cannot sum. Dates are "
			"YYYY-MM-DD; if omitted it covers the current calendar month. group_by is optional: "
			"'day', 'week', 'month', 'pos_profile', 'cashier', 'item_group' (hair vs services vs "
			"products etc.), or 'payment_mode' (CASH / TILL / PAYBILL / VISA CARD ...)."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"from_date": {"type": "string", "description": "Start date YYYY-MM-DD (inclusive)."},
				"to_date": {"type": "string", "description": "End date YYYY-MM-DD (inclusive)."},
				"group_by": {
					"type": "string",
					"enum": ["day", "week", "month", "pos_profile", "cashier", "item_group", "payment_mode"],
					"description": "Optional breakdown dimension.",
				},
			},
		},
	},
	{
		"tool_name": "top_items",
		"label": "Top Items / Services",
		"is_mutating": 0,
		"description": (
			"Best-selling items or services over a date range, with quantity sold, revenue and "
			"number of invoices. Use for 'top 10 services this month', 'best selling wigs', "
			"'what sells most'. order_by picks the ranking: 'revenue' (default), 'quantity' "
			"(most units), or 'invoices' (most transactions) -- 'best selling' is ambiguous, so "
			"if the user means volume pass order_by='quantity'. Set services_only=true for "
			"labour only (installations, styling, nails), products_only=true for retail stock "
			"only (hair, products), or pass item_group to restrict to one exact category. Dates "
			"default to the current month."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"from_date": {"type": "string"},
				"to_date": {"type": "string"},
				"item_group": {"type": "string", "description": "Exact Item Group name, e.g. 'HUMAN HAIR' or 'SERVICE'."},
				"services_only": {"type": "boolean"},
				"products_only": {"type": "boolean"},
				"order_by": {"type": "string", "enum": ["revenue", "quantity", "invoices"]},
				"limit": {"type": "integer", "description": "Rows to return (default 10, max 50)."},
			},
		},
	},
	{
		"tool_name": "technician_performance",
		"label": "Stylist Performance",
		"is_mutating": 0,
		"description": (
			"Per-stylist (technician) productivity over a date range: services done, invoices "
			"worked, days worked, and revenue SPLIT into service_revenue (labour) and "
			"product_revenue (wigs/hair sold on the same ticket). The list is ranked by "
			"service_revenue -- use that for 'which stylist did the most / earned the most', "
			"commission and staff reviews. product_revenue is co-credited, not necessarily sold "
			"by that person, so do not add the two together as 'their earnings'. Figures are "
			"approximate (matched by service name). Pass a technician name for their per-service "
			"breakdown (each row tagged service vs product). Dates default to the current month."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"from_date": {"type": "string"},
				"to_date": {"type": "string"},
				"technician": {"type": "string", "description": "Optional stylist name for a detailed breakdown."},
			},
		},
	},
	{
		"tool_name": "pos_shift_report",
		"label": "POS Shift / Cash Report",
		"is_mutating": 0,
		"description": (
			"POS closing shifts over a date range: expected vs actually-counted cash per payment "
			"mode. A line counts toward variance only if a closing amount was keyed; lines where "
			"cash was expected but no count was entered are flagged 'uncounted', not a shortage. "
			"cash_variance / total_cash_variance sum only real counted differences, and "
			"shifts_with_cash_count tells you how many shifts were actually counted -- if it is 0 "
			"the response carries a 'note' saying variance cannot be assessed. Use for 'did the "
			"till balance', 'cash shortages this week', end-of-day reconciliation. only_variances=true "
			"lists just shifts with a real counted discrepancy. Dates default to the current month."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"from_date": {"type": "string"},
				"to_date": {"type": "string"},
				"only_variances": {"type": "boolean", "description": "Only return shifts with a non-zero cash variance."},
			},
		},
	},
	{
		"tool_name": "stock_status",
		"label": "Stock Status",
		"is_mutating": 0,
		"description": (
			"Inventory levels. With item_code: that one item's on-hand, projected and reserved "
			"quantity plus stock value. Without item_code: items at or below low_stock_threshold "
			"(or below their safety stock), lowest first -- use for 'what are we low on'. The "
			"response's total_matching / out_of_stock_count cover EVERY matching item; the "
			"'items' list is capped by limit (returned tells you how many came back), so read "
			"total_matching for 'how many are low', not len(items). Set slow_mover_days (e.g. 60) "
			"to instead list stock items that still have quantity but have not sold in that many "
			"days -- use for 'what's not moving'. item_group restricts to one category."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"item_code": {"type": "string", "description": "Exact item code for a single-item lookup."},
				"item_group": {"type": "string"},
				"low_stock_threshold": {"type": "number", "description": "Qty at/below which an item counts as low (default 5)."},
				"slow_mover_days": {"type": "integer", "description": "If > 0, list in-stock items with no sale in this many days instead."},
				"limit": {"type": "integer", "description": "Max rows (default 40, max 100)."},
			},
		},
	},
	{
		"tool_name": "customer_overview",
		"label": "Customer Overview",
		"is_mutating": 0,
		"description": (
			"A single customer's full history in one call: lifetime spend, visit count, first / "
			"last visit, current outstanding balance, favourite stylist, top services, and their "
			"last 5 invoices. Use for 'tell me about customer X', 'when was Jane last in', 'does "
			"she owe anything'. Accepts the customer id or a partial name; if the name is "
			"ambiguous it returns the candidate list instead."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"customer": {"type": "string", "description": "Customer id or name fragment."},
			},
			"required": ["customer"],
		},
	},
	{
		"tool_name": "accounts_receivable",
		"label": "Accounts Receivable",
		"is_mutating": 0,
		"description": (
			"Who owes the salon money. total_outstanding, total_open_invoices and the aging "
			"buckets (current, 1-30, 31-60, 61-90, over 90 days overdue) cover EVERY open "
			"invoice; by_customer is every customer with a balance (top 50), and "
			"largest_invoices is just the biggest individual invoices capped by limit -- use "
			"total_outstanding/aging for 'how much are we owed', not a sum over largest_invoices. "
			"Use for 'who owes us', 'accounts receivable', 'overdue invoices', collections "
			"follow-up. Pass a customer to scope to their open invoices."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"customer": {"type": "string", "description": "Optional customer id to filter to."},
				"min_outstanding": {"type": "number", "description": "Ignore invoices owing at/below this amount (default 0)."},
				"limit": {"type": "integer", "description": "Max invoices (default 50, max 200)."},
			},
		},
	},
	{
		"tool_name": "expense_summary",
		"label": "Expense Summary",
		"is_mutating": 0,
		"description": (
			"Submitted operating / petty-cash expenses (Expense Entry) over a date range, with "
			"an optional breakdown by 'category', 'mode_of_payment', or 'month'. Use for 'what "
			"did we spend this month', 'expenses by category', 'how much on X'. Dates default to "
			"the current month."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"from_date": {"type": "string"},
				"to_date": {"type": "string"},
				"group_by": {"type": "string", "enum": ["category", "mode_of_payment", "month"]},
			},
		},
	},
	{
		"tool_name": "log_expense",
		"label": "Log Expense",
		"is_mutating": 1,
		"description": (
			"Record a petty-cash / operating expense as an Expense Entry. Created as a DRAFT for "
			"review unless submit=true. Requires a real amount, an existing Expense Category, and "
			"an existing Mode of Payment -- ask the user for any you do not have, do not guess. "
			"Mutating: the user must confirm before it runs."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"amount": {"type": "number"},
				"expense_category": {"type": "string", "description": "Must match an existing Expense Category."},
				"mode_of_payment": {"type": "string", "description": "Must match an existing Mode of Payment, e.g. CASH, TILL, PAYBILL."},
				"posting_date": {"type": "string", "description": "YYYY-MM-DD; defaults to now."},
				"remarks": {"type": "string"},
				"submit": {"type": "boolean", "description": "Submit immediately instead of leaving a draft (default false)."},
			},
			"required": ["amount", "expense_category", "mode_of_payment"],
		},
	},
	{
		"tool_name": "list_bookings",
		"label": "List Website Bookings",
		"is_mutating": 0,
		"description": (
			"Online booking requests from the website. Filter by status (Pending, Confirmed, "
			"Cancelled, Completed) and/or a preferred_date range. Use for 'what bookings do we "
			"have', 'today's appointments', 'unconfirmed bookings'."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"status": {"type": "string", "enum": ["Pending", "Confirmed", "Cancelled", "Completed"]},
				"from_date": {"type": "string", "description": "Earliest preferred_date YYYY-MM-DD."},
				"to_date": {"type": "string", "description": "Latest preferred_date YYYY-MM-DD."},
				"limit": {"type": "integer", "description": "Max rows (default 50, max 200)."},
			},
		},
	},
	{
		"tool_name": "manage_booking",
		"label": "Update Booking Status",
		"is_mutating": 1,
		"description": (
			"Set an online booking's status to Pending / Confirmed / Cancelled / Completed, "
			"optionally appending a dated note. Look the booking up with list_bookings first to "
			"get its exact id. Mutating: the user must confirm before it runs."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"booking": {"type": "string", "description": "Website Booking id."},
				"new_status": {"type": "string", "enum": ["Pending", "Confirmed", "Cancelled", "Completed"]},
				"note": {"type": "string", "description": "Optional note to append."},
			},
			"required": ["booking", "new_status"],
		},
	},
	{
		"tool_name": "list_reviews",
		"label": "List Website Reviews",
		"is_mutating": 0,
		"description": (
			"Website reviews. Pass published=0 for the moderation queue (not yet shown on the "
			"site), or max_rating=3 to surface unhappy customers. Use for 'any new reviews', "
			"'show me bad reviews', 'what needs moderating'."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"published": {"type": "integer", "enum": [0, 1], "description": "0 = hidden/pending, 1 = live on site."},
				"max_rating": {"type": "integer", "description": "Only reviews with rating <= this (1-5)."},
				"limit": {"type": "integer", "description": "Max rows (default 50, max 200)."},
			},
		},
	},
	{
		"tool_name": "set_review_published",
		"label": "Publish / Hide Review",
		"is_mutating": 1,
		"description": (
			"Publish (published=1) or hide (published=0) a website review. Find the review id "
			"with list_reviews first. Mutating: the user must confirm before it runs."
		),
		"json_schema": {
			"type": "object",
			"properties": {
				"review": {"type": "string", "description": "Website Review id."},
				"published": {"type": "integer", "enum": [0, 1]},
			},
			"required": ["review", "published"],
		},
	},
]


def execute():
	if not frappe.db.exists("DocType", "Copilot Tool"):
		return

	for tool in TOOLS:
		handler_method = f"{HANDLER}.{tool['tool_name']}"
		values = {
			"label": tool["label"],
			"description": tool["description"],
			"json_schema": frappe.as_json(tool["json_schema"]),
			"handler_method": handler_method,
			"is_mutating": tool["is_mutating"],
			"owner_app": "alicia_reviews",
			"enabled": 1,
		}

		existing = frappe.db.exists("Copilot Tool", tool["tool_name"])
		if existing:
			doc = frappe.get_doc("Copilot Tool", tool["tool_name"])
			# Only refresh rows this patch owns -- never stomp a hand-tuned tool.
			if (doc.owner_app or "") not in ("", "alicia_reviews"):
				continue
			doc.update(values)
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc({"doctype": "Copilot Tool", "tool_name": tool["tool_name"], **values}).insert(
				ignore_permissions=True
			)
