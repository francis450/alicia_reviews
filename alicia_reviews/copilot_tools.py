# Copyright (c) 2026, Alicia Hairline and Beauty and contributors
# For license information, please see license.txt

"""AI Copilot tools tailored to Alicia's data.

The generic ai_copilot tools (search_documents / get_document / create_document /
update_document) only do single-record CRUD -- they cannot group, sum, or run a
date range, and search_documents caps at 100 rows. Every question a salon owner
actually asks ("sales today", "top services this month", "who owes us money",
"which stylist earned the most") is an aggregation, so these tools fill that gap.

Each function is registered as a `Copilot Tool` row by
alicia_reviews.patches.v1_0.seed_copilot_tools. The agent loop calls them as the
logged-in user, so every tool re-checks `frappe.has_permission` up front -- these
run raw SQL for speed and SQL bypasses Frappe's permission engine.

Read tools return plain dicts. Mutating tools (log_expense, manage_booking,
set_review_published) are marked is_mutating=1 in the registry, so the agent
loop pauses for the user's confirmation before they run.
"""

import frappe
from frappe.utils import add_days, cint, flt, get_first_day, getdate, today

# Alicia runs a single stock location; keeping it here avoids a warehouse arg on
# every stock question.
DEFAULT_WAREHOUSE = "STORE - AH&B"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require(doctype: str, ptype: str = "read"):
	if not frappe.has_permission(doctype, ptype):
		frappe.throw(
			f"You do not have permission to {ptype} {doctype}.", frappe.PermissionError
		)


def _date_range(from_date=None, to_date=None):
	"""Default to the current calendar month when the caller gives nothing."""
	to_d = getdate(to_date) if to_date else getdate(today())
	from_d = getdate(from_date) if from_date else get_first_day(to_d)
	if from_d > to_d:
		from_d, to_d = to_d, from_d
	return str(from_d), str(to_d)


def _date_bounds(from_date=None, to_date=None):
	"""Like _date_range but the upper bound is exclusive (to_date + 1 day) so the
	filter is correct for Datetime columns too -- `BETWEEN from AND to` on a
	Datetime silently drops rows timestamped after midnight on the last day."""
	from_d, to_d = _date_range(from_date, to_date)
	return from_d, to_d, str(add_days(getdate(to_d), 1))


def _money(value) -> float:
	return round(flt(value), 2)


# ---------------------------------------------------------------------------
# sales & items
# ---------------------------------------------------------------------------

_SALES_GROUPERS = {
	"day": "si.posting_date",
	"week": "YEARWEEK(si.posting_date, 3)",
	"month": "DATE_FORMAT(si.posting_date, '%Y-%m')",
	"pos_profile": "si.pos_profile",
	"cashier": "si.owner",
}


@frappe.whitelist()
def sales_summary(from_date=None, to_date=None, group_by=None):
	"""Revenue, invoice count and average ticket over a date range.

	group_by: one of day, week, month, pos_profile, cashier, item_group,
	payment_mode -- or omit for a single total.
	"""
	_require("Sales Invoice")
	from_d, to_d = _date_range(from_date, to_date)
	params = {"from_d": from_d, "to_d": to_d}

	base = (
		"FROM `tabSales Invoice` si "
		"WHERE si.docstatus = 1 AND si.posting_date BETWEEN %(from_d)s AND %(to_d)s"
	)
	totals = frappe.db.sql(
		f"SELECT COUNT(*) c, COALESCE(SUM(si.base_grand_total), 0) t {base}",
		params,
		as_dict=True,
	)[0]
	result = {
		"from_date": from_d,
		"to_date": to_d,
		"invoice_count": cint(totals.c),
		"total_sales": _money(totals.t),
		"average_ticket": _money(totals.t / totals.c) if totals.c else 0.0,
	}
	if not group_by:
		return result

	if group_by in _SALES_GROUPERS:
		expr = _SALES_GROUPERS[group_by]
		rows = frappe.db.sql(
			f"SELECT {expr} AS label, COUNT(*) c, COALESCE(SUM(si.base_grand_total), 0) t "
			f"{base} GROUP BY label ORDER BY t DESC",
			params,
			as_dict=True,
		)
	elif group_by == "item_group":
		rows = frappe.db.sql(
			"SELECT sii.item_group AS label, COUNT(DISTINCT si.name) c, "
			"COALESCE(SUM(sii.base_net_amount), 0) t "
			"FROM `tabSales Invoice` si JOIN `tabSales Invoice Item` sii ON sii.parent = si.name "
			"WHERE si.docstatus = 1 AND si.posting_date BETWEEN %(from_d)s AND %(to_d)s "
			"GROUP BY sii.item_group ORDER BY t DESC",
			params,
			as_dict=True,
		)
	elif group_by == "payment_mode":
		rows = frappe.db.sql(
			"SELECT sip.mode_of_payment AS label, COUNT(DISTINCT si.name) c, "
			"COALESCE(SUM(sip.base_amount), 0) t "
			"FROM `tabSales Invoice` si JOIN `tabSales Invoice Payment` sip ON sip.parent = si.name "
			"WHERE si.docstatus = 1 AND si.posting_date BETWEEN %(from_d)s AND %(to_d)s "
			"GROUP BY sip.mode_of_payment ORDER BY t DESC",
			params,
			as_dict=True,
		)
	else:
		frappe.throw(
			"group_by must be one of: day, week, month, pos_profile, cashier, "
			"item_group, payment_mode"
		)

	result["group_by"] = group_by
	result["breakdown"] = [
		{"label": str(r.label), "invoice_count": cint(r.c), "total": _money(r.t)} for r in rows
	]
	return result


_TOP_ITEMS_ORDER = {
	"revenue": "revenue DESC, qty DESC",
	"quantity": "qty DESC, revenue DESC",
	"invoices": "invoices DESC, revenue DESC",
}


@frappe.whitelist()
def top_items(
	from_date=None,
	to_date=None,
	item_group=None,
	services_only=False,
	products_only=False,
	order_by="revenue",
	limit=10,
):
	"""Best-selling items/services over a date range.

	order_by: "revenue" (default -- highest money), "quantity" (most units), or
	"invoices" (most transactions). "Best selling" is ambiguous: a single wig
	outranks a KES 300 service on revenue but not on volume, so say which the
	user means. Set services_only for labour only (non-stock items),
	products_only for retail stock only; item_group filters to one category.
	"""
	_require("Sales Invoice")
	from_d, to_d = _date_range(from_date, to_date)
	params = {"from_d": from_d, "to_d": to_d, "limit": min(cint(limit) or 10, 50)}
	order_sql = _TOP_ITEMS_ORDER.get(order_by or "revenue")
	if not order_sql:
		frappe.throw("order_by must be one of: revenue, quantity, invoices")

	conds = ["si.docstatus = 1", "si.posting_date BETWEEN %(from_d)s AND %(to_d)s"]
	if item_group:
		conds.append("sii.item_group = %(item_group)s")
		params["item_group"] = item_group
	if services_only:
		conds.append("it.is_stock_item = 0")
	if products_only:
		conds.append("it.is_stock_item = 1")

	rows = frappe.db.sql(
		"SELECT sii.item_code, sii.item_name, sii.item_group, "
		"SUM(sii.qty) qty, COALESCE(SUM(sii.base_net_amount), 0) revenue, "
		"COUNT(DISTINCT si.name) invoices "
		"FROM `tabSales Invoice Item` sii "
		"JOIN `tabSales Invoice` si ON si.name = sii.parent "
		"JOIN `tabItem` it ON it.name = sii.item_code "
		f"WHERE {' AND '.join(conds)} "
		"GROUP BY sii.item_code, sii.item_name, sii.item_group "
		f"ORDER BY {order_sql} LIMIT %(limit)s",
		params,
		as_dict=True,
	)
	return {
		"from_date": from_d,
		"to_date": to_d,
		"ordered_by": order_by or "revenue",
		"items": [
			{
				"item_code": r.item_code,
				"item_name": r.item_name,
				"item_group": r.item_group,
				"qty_sold": _money(r.qty),
				"revenue": _money(r.revenue),
				"invoices": cint(r.invoices),
			}
			for r in rows
		],
	}


# ---------------------------------------------------------------------------
# technicians / stylists
# ---------------------------------------------------------------------------


@frappe.whitelist()
def technician_performance(from_date=None, to_date=None, technician=None):
	"""Per-stylist workload and revenue over a date range, ranked by service
	(labour) revenue.

	How revenue is attributed: a `Technician` row names a stylist and a service;
	it is matched to the invoice line with the same name. Staff often type a wig
	SKU into the service field, so matched lines are split into `service_revenue`
	(labour -- non-stock items) and `product_revenue` (wigs/hair/products sold on
	the same ticket). Rank stylists on `service_revenue` for a fair "who did the
	most work" answer; `product_revenue` is co-credited, not necessarily sold by
	that person. Figures are approximate (~3% high) where an invoice repeats a
	service name. Pass a technician for their per-service breakdown.
	"""
	_require("Sales Invoice")
	from_d, to_d = _date_range(from_date, to_date)
	params = {"from_d": from_d, "to_d": to_d}

	tech_cond = ""
	if technician:
		tech_cond = "AND t.technician = %(technician)s"
		params["technician"] = technician

	rows = frappe.db.sql(
		f"""
		SELECT t.technician,
			COUNT(DISTINCT t.name) services,
			COUNT(DISTINCT t.parent) invoices,
			COUNT(DISTINCT si.posting_date) days_worked,
			COALESCE(SUM(CASE WHEN it.is_stock_item = 0 THEN sii.base_net_amount END), 0) service_revenue,
			COALESCE(SUM(CASE WHEN it.is_stock_item = 1 THEN sii.base_net_amount END), 0) product_revenue
		FROM `tabTechnician` t
		JOIN `tabSales Invoice` si ON si.name = t.parent
			AND si.docstatus = 1
			AND si.posting_date BETWEEN %(from_d)s AND %(to_d)s
		LEFT JOIN `tabSales Invoice Item` sii ON sii.parent = t.parent AND sii.item_name = t.service
		LEFT JOIN `tabItem` it ON it.name = sii.item_code
		WHERE 1 = 1 {tech_cond}
		GROUP BY t.technician
		ORDER BY service_revenue DESC
		""",
		params,
		as_dict=True,
	)
	out = {
		"from_date": from_d,
		"to_date": to_d,
		"ranked_by": "service_revenue",
		"technicians": [
			{
				"technician": r.technician,
				"services": cint(r.services),
				"invoices": cint(r.invoices),
				"days_worked": cint(r.days_worked),
				"service_revenue": _money(r.service_revenue),
				"product_revenue": _money(r.product_revenue),
			}
			for r in rows
		],
	}

	if technician:
		svc = frappe.db.sql(
			"""
			SELECT t.service,
				COALESCE(it.is_stock_item, 0) is_product,
				COUNT(DISTINCT t.name) times,
				COALESCE(SUM(sii.base_net_amount), 0) revenue
			FROM `tabTechnician` t
			JOIN `tabSales Invoice` si ON si.name = t.parent
				AND si.docstatus = 1
				AND si.posting_date BETWEEN %(from_d)s AND %(to_d)s
			LEFT JOIN `tabSales Invoice Item` sii ON sii.parent = t.parent AND sii.item_name = t.service
			LEFT JOIN `tabItem` it ON it.name = sii.item_code
			WHERE t.technician = %(technician)s
			GROUP BY t.service, is_product
			ORDER BY revenue DESC
			""",
			params,
			as_dict=True,
		)
		out["service_breakdown"] = [
			{
				"service": r.service,
				"kind": "product" if cint(r.is_product) else "service",
				"times": cint(r.times),
				"revenue": _money(r.revenue),
			}
			for r in svc
		]
	return out


# ---------------------------------------------------------------------------
# POS cash control
# ---------------------------------------------------------------------------


@frappe.whitelist()
def pos_shift_report(from_date=None, to_date=None, only_variances=False):
	"""POS closing shifts over a date range with expected-vs-counted cash per
	payment mode.

	A payment line only counts toward the variance if a closing amount was
	actually entered (closing_amount != 0). Lines where cash was expected but no
	count was keyed are reported as `uncounted`, NOT as a shortage -- staff here
	often skip the count. `cash_variance` and `total_cash_variance` sum only real
	(counted) differences. Set only_variances to list just shifts with a real
	counted variance.
	"""
	_require("POS Closing Shift")
	from_d, to_d = _date_range(from_date, to_date)
	params = {"from_d": from_d, "to_d": to_d}

	shifts = frappe.db.sql(
		"""
		SELECT name, pos_profile, user, posting_date, period_start_date, period_end_date,
			grand_total, total_quantity
		FROM `tabPOS Closing Shift`
		WHERE docstatus = 1 AND posting_date BETWEEN %(from_d)s AND %(to_d)s
		ORDER BY period_start_date
		""",
		params,
		as_dict=True,
	)
	if not shifts:
		return {"from_date": from_d, "to_date": to_d, "shift_count": 0, "shifts": []}

	names = [s.name for s in shifts]
	details = frappe.db.sql(
		"""
		SELECT parent, mode_of_payment, opening_amount, closing_amount, expected_amount, difference
		FROM `tabPOS Closing Shift Detail`
		WHERE parent IN %(names)s
		""",
		{"names": names},
		as_dict=True,
	)
	by_shift = {}
	for d in details:
		by_shift.setdefault(d.parent, []).append(d)

	out_shifts = []
	total_diff = 0.0
	shifts_with_counts = 0
	for s in shifts:
		lines = by_shift.get(s.name, [])
		payments = []
		shift_diff = 0.0
		shift_counted = False
		for line in lines:
			counted = flt(line.closing_amount) != 0
			expected = flt(line.expected_amount)
			if counted:
				shift_counted = True
				shift_diff += flt(line.difference)
			payments.append(
				{
					"mode": line.mode_of_payment,
					"expected": _money(expected),
					"counted": _money(line.closing_amount),
					"difference": _money(line.difference) if counted else None,
					"status": "counted" if counted else ("uncounted" if expected else "empty"),
				}
			)
		if shift_counted:
			shifts_with_counts += 1
		total_diff += shift_diff
		if only_variances and not (shift_counted and round(shift_diff, 2) != 0):
			continue
		out_shifts.append(
			{
				"shift": s.name,
				"pos_profile": s.pos_profile,
				"cashier": s.user,
				"date": str(s.posting_date),
				"sales_total": _money(s.grand_total),
				"counted": shift_counted,
				"cash_variance": _money(shift_diff) if shift_counted else None,
				"payments": payments,
			}
		)
	result = {
		"from_date": from_d,
		"to_date": to_d,
		"shift_count": len(shifts),
		"shifts_with_cash_count": shifts_with_counts,
		"total_cash_variance": _money(total_diff),
		"shifts": out_shifts,
	}
	if shifts_with_counts == 0:
		result["note"] = (
			"No shift in this range has a closing cash count entered, so cash variance "
			"cannot be assessed -- only expected amounts are on record."
		)
	return result


# ---------------------------------------------------------------------------
# stock
# ---------------------------------------------------------------------------


@frappe.whitelist()
def stock_status(item_code=None, item_group=None, low_stock_threshold=5, slow_mover_days=0, limit=40):
	"""Stock on hand for one item, or the low/at-risk items in a category.

	With item_code: that item's live quantities. Without: items at or below
	low_stock_threshold (or below their safety stock), lowest first. Set
	slow_mover_days > 0 to instead list stock items with no sale in that many days.
	"""
	_require("Item")
	cap = min(cint(limit) or 40, 100)

	if item_code:
		bin_row = frappe.db.sql(
			"SELECT actual_qty, projected_qty, reserved_qty, valuation_rate, stock_value "
			"FROM `tabBin` WHERE item_code = %(i)s AND warehouse = %(w)s",
			{"i": item_code, "w": DEFAULT_WAREHOUSE},
			as_dict=True,
		)
		item = frappe.db.get_value(
			"Item", item_code, ["item_name", "item_group", "stock_uom", "safety_stock", "disabled"], as_dict=True
		)
		if not item:
			frappe.throw(f"No such Item: {item_code}")
		b = bin_row[0] if bin_row else {}
		return {
			"item_code": item_code,
			"item_name": item.item_name,
			"item_group": item.item_group,
			"uom": item.stock_uom,
			"actual_qty": _money(b.get("actual_qty")),
			"projected_qty": _money(b.get("projected_qty")),
			"reserved_qty": _money(b.get("reserved_qty")),
			"safety_stock": _money(item.safety_stock),
			"stock_value": _money(b.get("stock_value")),
			"disabled": bool(item.disabled),
		}

	params = {"w": DEFAULT_WAREHOUSE, "limit": cap}
	group_cond = ""
	if item_group:
		group_cond = "AND it.item_group = %(g)s"
		params["g"] = item_group

	if cint(slow_mover_days) > 0:
		params["since"] = str(add_days(today(), -cint(slow_mover_days)))
		rows = frappe.db.sql(
			f"""
			SELECT it.name item_code, it.item_name, it.item_group,
				COALESCE(b.actual_qty, 0) actual_qty
			FROM `tabItem` it
			LEFT JOIN `tabBin` b ON b.item_code = it.name AND b.warehouse = %(w)s
			WHERE it.is_stock_item = 1 AND it.disabled = 0 {group_cond}
				AND COALESCE(b.actual_qty, 0) > 0
				AND it.name NOT IN (
					SELECT sii.item_code FROM `tabSales Invoice Item` sii
					JOIN `tabSales Invoice` si ON si.name = sii.parent
					WHERE si.docstatus = 1 AND si.posting_date >= %(since)s
				)
			ORDER BY actual_qty DESC
			LIMIT %(limit)s
			""",
			params,
			as_dict=True,
		)
		return {
			"mode": "slow_movers",
			"no_sale_since": params["since"],
			"items": [
				{
					"item_code": r.item_code,
					"item_name": r.item_name,
					"item_group": r.item_group,
					"actual_qty": _money(r.actual_qty),
				}
				for r in rows
			],
		}

	params["threshold"] = flt(low_stock_threshold)
	match_where = (
		f"it.is_stock_item = 1 AND it.disabled = 0 {group_cond} "
		"AND COALESCE(b.actual_qty, 0) <= GREATEST(%(threshold)s, COALESCE(it.safety_stock, 0))"
	)
	counts = frappe.db.sql(
		f"""
		SELECT COUNT(*) matching,
			SUM(CASE WHEN COALESCE(b.actual_qty, 0) <= 0 THEN 1 ELSE 0 END) out_of_stock
		FROM `tabItem` it
		LEFT JOIN `tabBin` b ON b.item_code = it.name AND b.warehouse = %(w)s
		WHERE {match_where}
		""",
		params,
		as_dict=True,
	)[0]
	rows = frappe.db.sql(
		f"""
		SELECT it.name item_code, it.item_name, it.item_group, it.safety_stock,
			COALESCE(b.actual_qty, 0) actual_qty, COALESCE(b.projected_qty, 0) projected_qty
		FROM `tabItem` it
		LEFT JOIN `tabBin` b ON b.item_code = it.name AND b.warehouse = %(w)s
		WHERE {match_where}
		ORDER BY actual_qty ASC
		LIMIT %(limit)s
		""",
		params,
		as_dict=True,
	)
	return {
		"mode": "low_stock",
		"threshold": flt(low_stock_threshold),
		"total_matching": cint(counts.matching),
		"out_of_stock_count": cint(counts.out_of_stock),
		"returned": len(rows),
		"items": [
			{
				"item_code": r.item_code,
				"item_name": r.item_name,
				"item_group": r.item_group,
				"actual_qty": _money(r.actual_qty),
				"projected_qty": _money(r.projected_qty),
				"safety_stock": _money(r.safety_stock),
			}
			for r in rows
		],
	}


# ---------------------------------------------------------------------------
# customers & receivables
# ---------------------------------------------------------------------------


@frappe.whitelist()
def customer_overview(customer):
	"""One customer's full picture: spend, visits, balance, favourite services
	and stylist, and their last few invoices. Accepts the customer id or a name
	fragment.
	"""
	_require("Customer")
	_require("Sales Invoice")

	name = frappe.db.get_value("Customer", customer, "name")
	if not name:
		matches = frappe.get_all(
			"Customer",
			or_filters={"name": ["like", f"%{customer}%"], "customer_name": ["like", f"%{customer}%"]},
			fields=["name", "customer_name"],
			limit=6,
		)
		if not matches:
			frappe.throw(f"No customer matches {customer!r}.")
		if len(matches) > 1:
			return {"ambiguous": True, "matches": matches}
		name = matches[0].name

	info = frappe.db.get_value(
		"Customer", name, ["customer_name", "customer_group", "territory", "mobile_no"], as_dict=True
	)
	agg = frappe.db.sql(
		"""
		SELECT COUNT(*) invoices, MIN(posting_date) first_visit, MAX(posting_date) last_visit,
			COALESCE(SUM(base_grand_total), 0) lifetime_spend,
			COALESCE(SUM(outstanding_amount), 0) outstanding
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND customer = %(c)s
		""",
		{"c": name},
		as_dict=True,
	)[0]
	recent = frappe.db.sql(
		"""
		SELECT name, posting_date, base_grand_total, status, outstanding_amount
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND customer = %(c)s
		ORDER BY posting_date DESC, creation DESC LIMIT 5
		""",
		{"c": name},
		as_dict=True,
	)
	top_services = frappe.db.sql(
		"""
		SELECT sii.item_name, COUNT(*) times, COALESCE(SUM(sii.base_net_amount), 0) spend
		FROM `tabSales Invoice Item` sii
		JOIN `tabSales Invoice` si ON si.name = sii.parent
		WHERE si.docstatus = 1 AND si.customer = %(c)s
		GROUP BY sii.item_name ORDER BY times DESC, spend DESC LIMIT 5
		""",
		{"c": name},
		as_dict=True,
	)
	fav_tech = frappe.db.sql(
		"""
		SELECT t.technician, COUNT(*) visits
		FROM `tabTechnician` t
		JOIN `tabSales Invoice` si ON si.name = t.parent
		WHERE si.docstatus = 1 AND si.customer = %(c)s
		GROUP BY t.technician ORDER BY visits DESC LIMIT 1
		""",
		{"c": name},
		as_dict=True,
	)
	return {
		"customer": name,
		"customer_name": info.customer_name,
		"customer_group": info.customer_group,
		"territory": info.territory,
		"mobile_no": info.mobile_no,
		"invoices": cint(agg.invoices),
		"first_visit": str(agg.first_visit) if agg.first_visit else None,
		"last_visit": str(agg.last_visit) if agg.last_visit else None,
		"lifetime_spend": _money(agg.lifetime_spend),
		"outstanding": _money(agg.outstanding),
		"favourite_stylist": fav_tech[0].technician if fav_tech else None,
		"top_services": [
			{"service": r.item_name, "times": cint(r.times), "spend": _money(r.spend)} for r in top_services
		],
		"recent_invoices": [
			{
				"invoice": r.name,
				"date": str(r.posting_date),
				"total": _money(r.base_grand_total),
				"status": r.status,
				"outstanding": _money(r.outstanding_amount),
			}
			for r in recent
		],
	}


_AGING_CASE = """
	CASE
		WHEN si.due_date IS NULL OR DATEDIFF(%(as_of)s, si.due_date) <= 0 THEN 'current'
		WHEN DATEDIFF(%(as_of)s, si.due_date) <= 30 THEN '1_30'
		WHEN DATEDIFF(%(as_of)s, si.due_date) <= 60 THEN '31_60'
		WHEN DATEDIFF(%(as_of)s, si.due_date) <= 90 THEN '61_90'
		ELSE 'over_90'
	END
"""


@frappe.whitelist()
def accounts_receivable(customer=None, min_outstanding=0, limit=50):
	"""Open (unpaid / partly-paid) invoices: total owed, aging buckets and
	per-customer totals computed over EVERY open invoice, plus the largest
	individual invoices (capped by limit). Pass a customer to scope to theirs.
	Excludes credit balances (negative outstanding).
	"""
	_require("Sales Invoice")
	as_of = str(getdate(today()))
	where = "si.docstatus = 1 AND si.outstanding_amount > %(min)s"
	params = {"min": flt(min_outstanding), "as_of": as_of}
	if customer:
		where += " AND si.customer = %(c)s"
		params["c"] = customer

	# Totals + aging over the whole set -- never the limited page.
	summary = frappe.db.sql(
		f"SELECT COUNT(*) n, COALESCE(SUM(si.outstanding_amount), 0) total FROM `tabSales Invoice` si WHERE {where}",
		params,
		as_dict=True,
	)[0]
	aging_rows = frappe.db.sql(
		f"SELECT {_AGING_CASE} bucket, COALESCE(SUM(si.outstanding_amount), 0) amt "
		f"FROM `tabSales Invoice` si WHERE {where} GROUP BY bucket",
		params,
		as_dict=True,
	)
	aging = {"current": 0.0, "1_30": 0.0, "31_60": 0.0, "61_90": 0.0, "over_90": 0.0}
	for r in aging_rows:
		aging[r.bucket] = _money(r.amt)

	by_customer = frappe.db.sql(
		f"""
		SELECT si.customer, si.customer_name,
			COUNT(*) invoices, COALESCE(SUM(si.outstanding_amount), 0) outstanding
		FROM `tabSales Invoice` si WHERE {where}
		GROUP BY si.customer, si.customer_name
		ORDER BY outstanding DESC LIMIT 50
		""",
		params,
		as_dict=True,
	)

	invoices = frappe.db.sql(
		f"""
		SELECT si.name, si.customer_name, si.posting_date, si.due_date,
			si.base_grand_total, si.outstanding_amount, si.status,
			GREATEST(DATEDIFF(%(as_of)s, si.due_date), 0) AS days_overdue
		FROM `tabSales Invoice` si WHERE {where}
		ORDER BY si.outstanding_amount DESC LIMIT %(limit)s
		""",
		{**params, "limit": min(cint(limit) or 50, 200)},
		as_dict=True,
	)

	return {
		"as_of": as_of,
		"total_outstanding": _money(summary.total),
		"total_open_invoices": cint(summary.n),
		"aging": aging,
		"by_customer": [
			{
				"customer": r.customer,
				"customer_name": r.customer_name,
				"invoices": cint(r.invoices),
				"outstanding": _money(r.outstanding),
			}
			for r in by_customer
		],
		"largest_invoices": [
			{
				"invoice": r.name,
				"customer_name": r.customer_name,
				"date": str(r.posting_date),
				"due_date": str(r.due_date) if r.due_date else None,
				"total": _money(r.base_grand_total),
				"outstanding": _money(r.outstanding_amount),
				"days_overdue": cint(r.days_overdue),
				"status": r.status,
			}
			for r in invoices
		],
	}


# ---------------------------------------------------------------------------
# expenses
# ---------------------------------------------------------------------------

_EXPENSE_GROUPERS = {
	"category": "expense_category",
	"mode_of_payment": "mode_of_payment",
	"month": "DATE_FORMAT(posting_date, '%Y-%m')",
}


@frappe.whitelist()
def expense_summary(from_date=None, to_date=None, group_by="category"):
	"""Submitted expenses over a date range. group_by: category, mode_of_payment,
	month, or none for a single total.
	"""
	_require("Expense Entry")
	from_d, to_d, to_excl = _date_bounds(from_date, to_date)
	params = {"from_d": from_d, "to_excl": to_excl}

	# posting_date is a Datetime -- half-open range so entries later on the last
	# day are not dropped.
	base = (
		"FROM `tabExpense Entry` "
		"WHERE docstatus = 1 AND posting_date >= %(from_d)s AND posting_date < %(to_excl)s"
	)
	total = frappe.db.sql(
		f"SELECT COUNT(*) c, COALESCE(SUM(amount), 0) t {base}", params, as_dict=True
	)[0]
	out = {
		"from_date": from_d,
		"to_date": to_d,
		"entry_count": cint(total.c),
		"total_expense": _money(total.t),
	}
	if group_by and group_by in _EXPENSE_GROUPERS:
		expr = _EXPENSE_GROUPERS[group_by]
		rows = frappe.db.sql(
			f"SELECT {expr} label, COUNT(*) c, COALESCE(SUM(amount), 0) t {base} "
			f"GROUP BY label ORDER BY t DESC",
			params,
			as_dict=True,
		)
		out["group_by"] = group_by
		out["breakdown"] = [
			{"label": str(r.label), "entry_count": cint(r.c), "total": _money(r.t)} for r in rows
		]
	elif group_by:
		frappe.throw("group_by must be one of: category, mode_of_payment, month")
	return out


@frappe.whitelist()
def log_expense(amount, expense_category, mode_of_payment, posting_date=None, remarks=None, submit=False):
	"""Record a petty-cash / operating expense (Expense Entry). Created as a draft
	for review unless submit is true. MUTATING -- needs confirmation.
	"""
	_require("Expense Entry", "create")
	doc = frappe.get_doc(
		{
			"doctype": "Expense Entry",
			"amount": flt(amount),
			"expense_category": expense_category,
			"mode_of_payment": mode_of_payment,
			"posting_date": posting_date or frappe.utils.now_datetime(),
			"remarks": remarks,
		}
	)
	doc.insert()
	if cint(submit):
		doc.submit()
	return {
		"doctype": "Expense Entry",
		"name": doc.name,
		"docstatus": doc.docstatus,
		"submitted": bool(cint(submit)),
		"amount": _money(doc.amount),
	}


# ---------------------------------------------------------------------------
# website bookings & reviews  (alicia_reviews)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_bookings(status=None, from_date=None, to_date=None, limit=50):
	"""Online booking requests. Filter by status (Pending, Confirmed, Cancelled,
	Completed) and/or a preferred_date range.
	"""
	_require("Website Booking")
	filters = {}
	if status:
		filters["status"] = status
	if from_date or to_date:
		lo = str(getdate(from_date)) if from_date else "1900-01-01"
		hi = str(getdate(to_date)) if to_date else "3000-01-01"
		filters["preferred_date"] = ["between", [lo, hi]]

	rows = frappe.get_all(
		"Website Booking",
		filters=filters,
		fields=[
			"name", "customer_name", "phone", "email", "service",
			"preferred_date", "preferred_time", "status", "notes", "creation",
		],
		order_by="preferred_date asc",
		limit_page_length=min(cint(limit) or 50, 200),
	)
	return {"count": len(rows), "bookings": rows}


@frappe.whitelist()
def manage_booking(booking, new_status, note=None):
	"""Set an online booking's status to Pending / Confirmed / Cancelled /
	Completed, optionally appending a note. MUTATING -- needs confirmation.
	"""
	_require("Website Booking", "write")
	valid = {"Pending", "Confirmed", "Cancelled", "Completed"}
	if new_status not in valid:
		frappe.throw(f"new_status must be one of {', '.join(sorted(valid))}.")
	doc = frappe.get_doc("Website Booking", booking)
	before = doc.status
	doc.status = new_status
	if note:
		doc.notes = f"{(doc.notes or '').strip()}\n[{today()}] {note}".strip()
	doc.save()
	return {"doctype": "Website Booking", "name": doc.name, "status_before": before, "status_after": new_status}


@frappe.whitelist()
def list_reviews(published=None, max_rating=None, limit=50):
	"""Website reviews. published=0 for the moderation queue, max_rating=3 to
	surface unhappy customers.
	"""
	_require("Website Review")
	filters = {}
	if published is not None:
		filters["published"] = cint(published)
	if max_rating is not None:
		filters["rating"] = ["<=", cint(max_rating)]
	rows = frappe.get_all(
		"Website Review",
		filters=filters,
		fields=["name", "reviewer_name", "rating", "comment", "published", "creation"],
		order_by="creation desc",
		limit_page_length=min(cint(limit) or 50, 200),
	)
	return {"count": len(rows), "reviews": rows}


@frappe.whitelist()
def set_review_published(review, published):
	"""Publish (1) or hide (0) a website review. MUTATING -- needs confirmation."""
	_require("Website Review", "write")
	doc = frappe.get_doc("Website Review", review)
	doc.published = cint(published)
	doc.save()
	return {"doctype": "Website Review", "name": doc.name, "published": doc.published}
