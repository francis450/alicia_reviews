"""Pesapal API 3.0 integration for the Alicia storefront cart checkout.

Flow:
  1. Frontend POSTs cart -> `create_cart_payment` builds a (draft) Sales Order,
     a Pesapal Transaction, calls SubmitOrderRequest and returns `redirect_url`.
  2. Customer pays on Pesapal's hosted page, browser returns to `callback_url`.
  3. Pesapal pings `pesapal_ipn`; we call GetTransactionStatus and, on success,
     submit the Sales Order + create a Sales Invoice + a Payment Entry.
  4. Frontend success page polls `payment_status` for the final state.

Credentials / defaults live in the `Pesapal Settings` single doctype.
"""

import contextlib

import frappe
from frappe.integrations.utils import make_get_request, make_post_request
from frappe.utils import flt, get_url, nowdate

TOKEN_CACHE_KEY = "pesapal_access_token"
TOKEN_TTL_SEC = 240  # Pesapal tokens last 5 min; refresh a little early.

# GetTransactionStatus status_code -> our Status
STATUS_CODE_MAP = {0: "Invalid", 1: "Completed", 2: "Failed", 3: "Reversed"}


@contextlib.contextmanager
def _privileged():
	"""Run ERPNext document work as Administrator.

	The public endpoints are hit by Guest (the storefront) and by Pesapal's
	servers (the IPN). Creating a Customer / Sales Order / Sales Invoice pulls in
	ERPNext code that does explicit `has_permission(..., throw=True)` checks which
	`ignore_permissions` does not bypass. Inputs are validated before we get here
	(items must be published Website Items, the amount is computed server-side).
	"""
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		yield
	finally:
		frappe.set_user(user)


# ---------------------------------------------------------------------------
# settings / low-level API
# ---------------------------------------------------------------------------


def _settings():
	s = frappe.get_cached_doc("Pesapal Settings")
	if not s.enabled:
		frappe.throw("Online payments are not available right now.", title="Pesapal disabled")
	if not (s.consumer_key and s.get_password("consumer_secret", raise_exception=False)):
		frappe.throw("Pesapal credentials are not configured.", title="Pesapal misconfigured")
	return s


def _token(force=False):
	if not force:
		cached = frappe.cache().get_value(TOKEN_CACHE_KEY)
		if cached:
			return cached

	s = _settings()
	resp = make_post_request(
		f"{s.base_url}/api/Auth/RequestToken",
		headers={"Accept": "application/json", "Content-Type": "application/json"},
		json={
			"consumer_key": s.consumer_key,
			"consumer_secret": s.get_password("consumer_secret"),
		},
	)
	if not resp or not resp.get("token"):
		frappe.throw(f"Pesapal auth failed: {(resp or {}).get('error') or resp}")

	frappe.cache().set_value(TOKEN_CACHE_KEY, resp["token"], expires_in_sec=TOKEN_TTL_SEC)
	return resp["token"]


def _headers():
	return {
		"Accept": "application/json",
		"Content-Type": "application/json",
		"Authorization": f"Bearer {_token()}",
	}


def _api_post(path, body):
	s = frappe.get_cached_doc("Pesapal Settings")
	return make_post_request(f"{s.base_url}{path}", headers=_headers(), json=body)


def _api_get(path, params=None):
	s = frappe.get_cached_doc("Pesapal Settings")
	return make_get_request(f"{s.base_url}{path}", headers=_headers(), params=params or {})


# ---------------------------------------------------------------------------
# IPN registration (run once per environment / URL change)
# ---------------------------------------------------------------------------


def _ipn_url():
	return get_url("/api/method/alicia_reviews.pesapal.pesapal_ipn")


@frappe.whitelist()
def register_ipn():
	"""Register our IPN URL with Pesapal and store the returned ipn_id.

	Call from bench:  bench --site <site> execute alicia_reviews.pesapal.register_ipn
	or from the Pesapal Settings form.
	"""
	s = _settings()
	url = _ipn_url()
	resp = _api_post("/api/URLSetup/RegisterIPN", {"url": url, "ipn_notification_type": "POST"})
	if not resp or not resp.get("ipn_id"):
		frappe.throw(f"IPN registration failed: {(resp or {}).get('error') or resp}")

	frappe.db.set_single_value("Pesapal Settings", {"ipn_id": resp["ipn_id"], "ipn_url": url})
	frappe.clear_document_cache("Pesapal Settings", "Pesapal Settings")
	frappe.db.commit()
	return {"ipn_id": resp["ipn_id"], "url": url}


def _ensure_ipn(s):
	if s.ipn_id and s.ipn_url == _ipn_url():
		return s.ipn_id
	return register_ipn()["ipn_id"]


# ---------------------------------------------------------------------------
# ERPNext document helpers
# ---------------------------------------------------------------------------


def _split_name(full_name):
	parts = (full_name or "").strip().split()
	if not parts:
		return "Customer", ""
	if len(parts) == 1:
		return parts[0], ""
	return parts[0], " ".join(parts[1:])


def _find_customer_by_email(email):
	if not email:
		return None
	rows = frappe.db.sql(
		"""
		select dl.link_name
		from `tabDynamic Link` dl
		join `tabContact Email` ce on ce.parent = dl.parent
		where dl.parenttype = 'Contact'
			and dl.link_doctype = 'Customer'
			and ce.email_id = %s
		limit 1
		""",
		email,
	)
	return rows[0][0] if rows else None


def _get_or_create_customer(s, cust):
	name = (cust.get("name") or "").strip()
	email = (cust.get("email") or "").strip()
	phone = (cust.get("phone") or "").strip()

	existing = _find_customer_by_email(email)
	if existing:
		return existing

	territory = (
		s.territory
		or frappe.db.get_value("Territory", {"is_group": 0}, "name")
		or "All Territories"
	)
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name or email or f"Web Customer {phone}",
			"customer_type": "Individual",
			"customer_group": s.customer_group or "Individual",
			"territory": territory,
		}
	)
	customer.insert(ignore_permissions=True)

	if email or phone:
		first, last = _split_name(name)
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": first,
				"last_name": last,
				"links": [{"link_doctype": "Customer", "link_name": customer.name}],
			}
		)
		if email:
			contact.append("email_ids", {"email_id": email, "is_primary": 1})
		if phone:
			contact.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
		contact.insert(ignore_permissions=True)
		frappe.db.set_value("Customer", customer.name, "customer_primary_contact", contact.name)

	address = (cust.get("address") or "").strip()
	city = (cust.get("city") or "").strip()
	if address:
		addr = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": name or customer.name,
				"address_type": "Billing",
				"address_line1": address,
				"city": city or "Nairobi",
				"country": "Kenya",
				"email_id": email,
				"phone": phone,
				"links": [{"link_doctype": "Customer", "link_name": customer.name}],
			}
		)
		addr.insert(ignore_permissions=True)

	return customer.name


def _validate_items(items):
	"""Return a clean list of {item_code, qty}; reject anything not published."""
	if not items:
		frappe.throw("Your cart is empty.")

	clean = []
	for row in items:
		code = (row.get("item_code") or "").strip()
		qty = flt(row.get("qty") or 0)
		if not code or qty <= 0:
			continue
		if not frappe.db.get_value("Website Item", {"item_code": code, "published": 1}):
			frappe.throw(f"'{code}' is not available for online purchase.")
		clean.append({"item_code": code, "qty": qty})

	if not clean:
		frappe.throw("Your cart is empty.")
	return clean


def _create_sales_order(s, customer, items, notes):
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"customer": customer,
			"order_type": "Sales",
			"company": s.company,
			"currency": s.currency or "KES",
			"selling_price_list": s.selling_price_list or "Selling Price",
			"transaction_date": nowdate(),
			"delivery_date": nowdate(),
			"po_no": None,
			"items": [
				{
					"item_code": i["item_code"],
					"qty": i["qty"],
					"warehouse": s.warehouse,
					"delivery_date": nowdate(),
				}
				for i in items
			],
		}
	)
	if notes:
		so.tc_name = None
		so.terms = notes
	so.set_missing_values()
	so.insert(ignore_permissions=True)
	return so


def _process_successful_payment(txn, s):
	"""Submit the SO, raise a Sales Invoice and a Payment Entry. Idempotent."""
	if txn.payment_entry:
		return

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	with _privileged():
		so = frappe.get_doc("Sales Order", txn.sales_order)
		if so.docstatus == 0:
			so.submit()

		si = txn.sales_invoice and frappe.get_doc("Sales Invoice", txn.sales_invoice)
		if not si:
			si = make_sales_invoice(so.name)
			si.set_missing_values()
			si.update_stock = 0
			si.insert(ignore_permissions=True)
			si.submit()
			txn.db_set("sales_invoice", si.name)

		pe = get_payment_entry("Sales Invoice", si.name)
		if s.mode_of_payment:
			pe.mode_of_payment = s.mode_of_payment
		if s.deposit_account:
			pe.paid_to = s.deposit_account
		pe.reference_no = txn.confirmation_code or txn.order_tracking_id or txn.merchant_reference
		pe.reference_date = nowdate()
		pe.insert(ignore_permissions=True)
		pe.submit()
		txn.db_set("payment_entry", pe.name)


# ---------------------------------------------------------------------------
# public endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def create_cart_payment(customer=None, items=None, notes=None):
	"""Create a Sales Order + Pesapal payment for a guest cart.

	Body (JSON): {customer: {name,email,phone,address,city}, items: [{item_code,qty}], notes}
	Returns: {redirect_url, order_tracking_id, merchant_reference, sales_order, amount, currency}
	"""
	if frappe.request and frappe.request.method != "POST":
		frappe.throw("Method not allowed", frappe.ValidationError)

	customer = frappe.parse_json(customer) if isinstance(customer, str) else (customer or {})
	items = frappe.parse_json(items) if isinstance(items, str) else (items or [])
	if not customer.get("email") and not customer.get("phone"):
		frappe.throw("An email or phone number is required.")

	s = _settings()
	if not s.company:
		frappe.throw("Pesapal Settings: set the Company before taking orders.")

	clean_items = _validate_items(items)
	notification_id = _ensure_ipn(s)

	with _privileged():
		customer_name = _get_or_create_customer(s, customer)
		so = _create_sales_order(s, customer_name, clean_items, notes)

	amount = flt(so.rounded_total or so.grand_total, 2)
	if amount <= 0:
		frappe.throw("Order total came out as zero — check item prices.")

	merchant_reference = f"{so.name}-{frappe.generate_hash(length=6)}"
	txn = frappe.get_doc(
		{
			"doctype": "Pesapal Transaction",
			"customer": customer_name,
			"customer_name": customer.get("name"),
			"email": customer.get("email"),
			"phone": customer.get("phone"),
			"sales_order": so.name,
			"amount": amount,
			"currency": so.currency,
			"merchant_reference": merchant_reference,
			"status": "Pending",
			"notes": notes,
		}
	)
	txn.insert(ignore_permissions=True)

	first, last = _split_name(customer.get("name"))
	order = {
		"id": merchant_reference,
		"currency": so.currency,
		"amount": amount,
		"description": f"Alicia Hairline & Beauty order {so.name}"[:100],
		"callback_url": s.callback_url,
		"notification_id": notification_id,
		"billing_address": {
			"email_address": customer.get("email") or "",
			"phone_number": customer.get("phone") or "",
			"country_code": "KE",
			"first_name": first,
			"last_name": last,
			"line_1": customer.get("address") or "",
			"city": customer.get("city") or "",
		},
	}

	resp = _api_post("/api/Transactions/SubmitOrderRequest", order)
	txn.append_log({"event": "SubmitOrderRequest", "response": resp})

	if not resp or resp.get("error") or not resp.get("redirect_url"):
		txn.status = "Failed"
		txn.status_description = str((resp or {}).get("error") or "No redirect_url returned")
		txn.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.throw("Could not start the payment. Please try again.")

	txn.order_tracking_id = resp.get("order_tracking_id")
	txn.redirect_url = resp.get("redirect_url")
	txn.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"redirect_url": resp["redirect_url"],
		"order_tracking_id": resp.get("order_tracking_id"),
		"merchant_reference": merchant_reference,
		"sales_order": so.name,
		"amount": amount,
		"currency": so.currency,
	}


def _sync_status(txn, s):
	"""Query Pesapal for the definitive status and reconcile our records."""
	resp = _api_get(
		"/api/Transactions/GetTransactionStatus",
		params={"orderTrackingId": txn.order_tracking_id},
	)
	txn.append_log({"event": "GetTransactionStatus", "response": resp})

	code = (resp or {}).get("status_code")
	txn.payment_method = (resp or {}).get("payment_method")
	txn.confirmation_code = (resp or {}).get("confirmation_code")
	txn.status_description = (resp or {}).get("payment_status_description") or (resp or {}).get("description")

	new_status = STATUS_CODE_MAP.get(code)
	if new_status:
		txn.status = new_status
	txn.save(ignore_permissions=True)
	frappe.db.commit()

	if txn.status == "Completed":
		try:
			_process_successful_payment(txn, s)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Pesapal: post-payment ERP steps failed for {txn.name}",
				message=frappe.get_traceback(),
			)
			txn.reload()
			txn.append_log({"event": "erp_error", "traceback": frappe.get_traceback()})
			txn.save(ignore_permissions=True)
			frappe.db.commit()

	return txn.status


@frappe.whitelist(allow_guest=True)
def pesapal_ipn(**kwargs):
	"""Instant Payment Notification endpoint that Pesapal calls."""
	data = {**(frappe.form_dict or {}), **kwargs}
	tracking_id = data.get("OrderTrackingId") or data.get("orderTrackingId")
	merchant_ref = data.get("OrderMerchantReference") or data.get("orderMerchantReference")
	notification_type = data.get("OrderNotificationType") or data.get("orderNotificationType")

	name = None
	if merchant_ref:
		name = frappe.db.get_value("Pesapal Transaction", {"merchant_reference": merchant_ref})
	if not name and tracking_id:
		name = frappe.db.get_value("Pesapal Transaction", {"order_tracking_id": tracking_id})

	status = 500
	if name:
		try:
			txn = frappe.get_doc("Pesapal Transaction", name)
			if not txn.order_tracking_id and tracking_id:
				txn.db_set("order_tracking_id", tracking_id)
				txn.reload()
			_sync_status(txn, _settings())
			status = 200
		except Exception:
			frappe.log_error(title="Pesapal IPN handler failed", message=frappe.get_traceback())
	else:
		frappe.log_error(
			title="Pesapal IPN: unknown transaction",
			message=frappe.as_json(data),
		)

	return {
		"orderNotificationType": notification_type,
		"orderTrackingId": tracking_id,
		"orderMerchantReference": merchant_ref,
		"status": status,
	}


@frappe.whitelist(allow_guest=True)
def payment_status(merchant_reference=None, order_tracking_id=None):
	"""Frontend success page polls this. Does a live re-check while Pending."""
	filters = None
	if merchant_reference:
		filters = {"merchant_reference": merchant_reference}
	elif order_tracking_id:
		filters = {"order_tracking_id": order_tracking_id}
	if not filters:
		frappe.throw("merchant_reference is required.")

	name = frappe.db.get_value("Pesapal Transaction", filters)
	if not name:
		frappe.throw("Unknown transaction.", frappe.DoesNotExistError)

	txn = frappe.get_doc("Pesapal Transaction", name)
	if txn.status == "Pending" and txn.order_tracking_id:
		try:
			_sync_status(txn, _settings())
		except Exception:
			frappe.log_error(title="Pesapal payment_status sync failed", message=frappe.get_traceback())

	return {
		"status": txn.status,
		"status_description": txn.status_description,
		"merchant_reference": txn.merchant_reference,
		"order_tracking_id": txn.order_tracking_id,
		"sales_order": txn.sales_order,
		"sales_invoice": txn.sales_invoice,
		"amount": txn.amount,
		"currency": txn.currency,
		"payment_method": txn.payment_method,
		"confirmation_code": txn.confirmation_code,
	}
