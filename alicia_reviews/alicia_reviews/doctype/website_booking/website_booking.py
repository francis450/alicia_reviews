import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import format_date, getdate, nowdate

# Python weekday(): Monday=0 ... Friday=4, Saturday=5, Sunday=6
WEEKEND_WEEKDAYS = (4, 5)

SETTINGS_DOCTYPE = "Alicia Booking Settings"


def get_settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def is_weekend(date_value) -> bool:
	"""True if the given date falls on a Friday or Saturday."""
	if not date_value:
		return False
	return getdate(date_value).weekday() in WEEKEND_WEEKDAYS


class WebsiteBooking(Document):
	def validate(self):
		self._block_weekend_on_create()

	def after_insert(self):
		notify_staff_of_new_booking(self)
		send_booking_sms(self, "new_booking")

	def on_update(self):
		previous = self.get_doc_before_save()
		if not previous or previous.status == self.status:
			return

		kind = {"Confirmed": "confirmed", "Cancelled": "cancelled"}.get(self.status)
		if not kind:
			return

		settings = get_settings()
		# In "review" mode the desk form pops the message up for staff to edit and
		# send by hand — so don't fire automatically. Background callers (the
		# weekend-cancel job, imports, the API) set this flag to send anyway.
		if (
			settings.sms_enabled
			and settings.review_sms_before_send
			and not frappe.flags.get("alicia_send_sms_now")
		):
			return

		send_booking_sms(self, kind)

	def _block_weekend_on_create(self):
		# Only guard new bookings — never get in the way of staff editing history.
		if not self.is_new():
			return
		settings = get_settings()
		if settings.block_weekend_bookings and is_weekend(self.preferred_date):
			frappe.throw(
				settings.weekend_message
				or _("Fridays and Saturdays are walk-in only — please choose another day.")
			)


# ---------------------------------------------------------------------------
# Staff notification (the desk "bell")
# ---------------------------------------------------------------------------
def get_notify_recipients(settings=None) -> list[str]:
	settings = settings or get_settings()
	raw = settings.notify_recipients or ""
	return [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]


def notify_staff_of_new_booking(doc):
	recipients = get_notify_recipients()
	if not recipients:
		return

	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	subject = _("New website booking: {0} — {1} on {2}").format(
		doc.customer_name,
		doc.service,
		format_date(doc.preferred_date) if doc.preferred_date else "",
	)
	enqueue_create_notification(
		recipients,
		{
			"type": "Alert",
			"subject": subject,
			"email_content": doc.notes or "",
			"document_type": doc.doctype,
			"document_name": doc.name,
			"from_user": frappe.session.user,
		},
	)


# ---------------------------------------------------------------------------
# Client / salon SMS
# ---------------------------------------------------------------------------
def _sms_context(doc) -> dict:
	return {
		"customer_name": doc.customer_name or "",
		"service": doc.service or "",
		"preferred_date": format_date(doc.preferred_date) if doc.preferred_date else "",
		"preferred_time": doc.preferred_time or "",
		"status": doc.status or "",
	}


def _render_template(template: str, doc) -> str:
	try:
		return (template or "").format(**_sms_context(doc))
	except (KeyError, IndexError, ValueError):
		# A stray brace in the template shouldn't lose the message entirely.
		return template or ""


def normalize_msisdn(number) -> str | None:
	"""Best-effort Kenyan MSISDN normalisation to +2547XXXXXXXX."""
	if not number:
		return None
	digits = re.sub(r"[^\d+]", "", str(number))
	if digits.startswith("+"):
		return digits
	if digits.startswith("0") and len(digits) == 10:
		return "+254" + digits[1:]
	if digits.startswith("254"):
		return "+" + digits
	if len(digits) == 9:
		return "+254" + digits
	return digits or None


def resolve_booking_sms(doc, kind: str, settings=None):
	"""Work out the recipient number + rendered message for a booking event.

	kind: 'confirmed' | 'cancelled' | 'new_booking'.
	Returns (number, message) or (None, None) when it shouldn't be sent.
	"""
	settings = settings or get_settings()

	if kind in ("confirmed", "cancelled"):
		if not settings.sms_enabled:
			return None, None
		template = settings.sms_confirmed if kind == "confirmed" else settings.sms_cancelled
		number = normalize_msisdn(doc.phone)
	elif kind == "new_booking":
		if not settings.sms_new_booking_enabled:
			return None, None
		template = settings.sms_new_booking
		number = normalize_msisdn(settings.sms_new_booking_number)
	else:
		return None, None

	if not template or not number:
		return None, None

	return number, _render_template(template, doc)


def deliver_sms(number: str, message: str, *, context: str = "") -> str:
	"""Send one SMS through Frappe's gateway. Returns 'logged' | 'sent' | 'error'."""
	if not frappe.db.get_single_value("SMS Settings", "sms_gateway_url"):
		frappe.logger("alicia_reviews").info(
			f"SMS gateway not configured — would send to {number}: {message} ({context})"
		)
		return "logged"

	try:
		from frappe.core.doctype.sms_settings.sms_settings import send_sms

		send_sms([number], message, success_msg=False)
		return "sent"
	except Exception:
		frappe.log_error(
			title="Alicia booking SMS failed",
			message=f"{context}\nnumber={number}\n\n{frappe.get_traceback()}",
		)
		return "error"


def send_booking_sms(doc, kind: str) -> str:
	"""Resolve + send an SMS for a booking event (the automatic path).

	Returns 'disabled' | 'sent' | 'logged' | 'error'.
	"""
	number, message = resolve_booking_sms(doc, kind)
	if not number:
		return "disabled"
	return deliver_sms(number, message, context=f"kind={kind} booking={doc.name}")


@frappe.whitelist()
def get_status_sms_preview(name: str, kind: str) -> dict:
	"""For the desk form: the message that would be sent for this status change."""
	if kind not in ("confirmed", "cancelled"):
		frappe.throw(_("Unknown SMS type."))
	frappe.has_permission("Website Booking", "read", doc=name, throw=True)

	settings = get_settings()
	doc = frappe.get_doc("Website Booking", name)
	number, message = resolve_booking_sms(doc, kind, settings)
	return {
		"enabled": bool(settings.sms_enabled),
		"review": bool(settings.review_sms_before_send),
		"number": number or normalize_msisdn(doc.phone) or "",
		"message": message
		or _render_template(
			settings.sms_confirmed if kind == "confirmed" else settings.sms_cancelled, doc
		),
	}


@frappe.whitelist()
def send_status_sms(name: str, kind: str, number: str, message: str) -> dict:
	"""For the desk form: send the (possibly edited) status SMS by hand."""
	if kind not in ("confirmed", "cancelled"):
		frappe.throw(_("Unknown SMS type."))
	frappe.has_permission("Website Booking", "write", doc=name, throw=True)

	number = normalize_msisdn(number)
	message = (message or "").strip()
	if not number or not message:
		frappe.throw(_("A phone number and a message are both required."))

	status = deliver_sms(number, message, context=f"kind={kind} booking={name} (manual)")
	frappe.get_doc("Website Booking", name).add_comment(
		"Comment",
		_("{0} SMS {1}: {2}").format(kind.title(), status, message),
	)
	return {"status": status}


# ---------------------------------------------------------------------------
# Scheduled cleanup — Fri/Sat is walk-in only
# ---------------------------------------------------------------------------
def cancel_weekend_bookings():
	"""Daily: cancel any still-open, upcoming booking that lands on a Fri or Sat.

	Past bookings are left untouched — there's nothing to walk into anymore, and
	rewriting history just spams clients with cancellation texts.
	"""
	settings = get_settings()
	if not settings.block_weekend_bookings:
		return

	rows = frappe.get_all(
		"Website Booking",
		filters={
			"status": ("in", ("Pending", "Confirmed")),
			"preferred_date": (">=", nowdate()),
		},
		fields=["name", "preferred_date"],
	)
	# No human is watching this job, so send the cancellation SMS straight away
	# even when "review before sending" is on.
	frappe.flags.alicia_send_sms_now = True
	try:
		for row in rows:
			if not is_weekend(row.preferred_date):
				continue
			doc = frappe.get_doc("Website Booking", row.name)
			doc.status = "Cancelled"
			doc.save(ignore_permissions=True)
			doc.add_comment(
				"Comment",
				_("Auto-cancelled: Fridays & Saturdays are walk-in only."),
			)
	finally:
		frappe.flags.alicia_send_sms_now = False
