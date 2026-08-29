from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate

from alicia_reviews.alicia_reviews.doctype.website_booking.website_booking import (
	cancel_weekend_bookings,
	normalize_msisdn,
	send_booking_sms,
)

NOTIFY_USER = "maggie@alicia.boraerp.co.ke"


def _next_weekday(target: int) -> str:
	"""Return the next date (as YYYY-MM-DD) whose weekday() == target."""
	d = getdate(nowdate())
	for _ in range(1, 8):
		d = add_days(d, 1)
		if getdate(d).weekday() == target:
			return str(d)
	raise RuntimeError("unreachable")


NEXT_WEDNESDAY = _next_weekday(2)
NEXT_FRIDAY = _next_weekday(4)
NEXT_SATURDAY = _next_weekday(5)
# NEXT_SATURDAY is 1-7 days ahead, so -14 lands on a Saturday 7-13 days in the past.
PAST_SATURDAY = str(add_days(getdate(NEXT_SATURDAY), -14))


class TestWebsiteBooking(FrappeTestCase):
	def setUp(self):
		self.settings = frappe.get_single("Alicia Booking Settings")
		self.settings.block_weekend_bookings = 1
		self.settings.weekend_message = "Fridays and Saturdays are walk-in only — please choose another day."
		self.settings.sms_enabled = 0
		self.settings.sms_new_booking_enabled = 0
		self.settings.notify_recipients = ""
		self.settings.save(ignore_permissions=True)

	def _make(self, **overrides):
		values = {
			"doctype": "Website Booking",
			"customer_name": "Test Client",
			"phone": "0712345678",
			"service": "Wig Installation",
			"preferred_date": NEXT_WEDNESDAY,
			"preferred_time": "11:00 AM",
			"status": "Pending",
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	# ------------------------------------------------------------------ weekend
	def test_weekday_booking_allowed(self):
		doc = self._make(preferred_date=NEXT_WEDNESDAY)
		self.assertEqual(doc.status, "Pending")

	def test_friday_booking_blocked(self):
		with self.assertRaises(frappe.ValidationError):
			self._make(preferred_date=NEXT_FRIDAY)

	def test_saturday_booking_blocked(self):
		with self.assertRaises(frappe.ValidationError):
			self._make(preferred_date=NEXT_SATURDAY)

	def test_weekend_block_can_be_disabled(self):
		self.settings.block_weekend_bookings = 0
		self.settings.save(ignore_permissions=True)
		doc = self._make(preferred_date=NEXT_FRIDAY)
		self.assertEqual(doc.status, "Pending")

	# ------------------------------------------------------------- notification
	def test_new_booking_notifies_staff(self):
		self.settings.notify_recipients = NOTIFY_USER
		self.settings.save(ignore_permissions=True)

		doc = self._make()

		logs = frappe.get_all(
			"Notification Log",
			filters={
				"document_type": "Website Booking",
				"document_name": doc.name,
				"for_user": NOTIFY_USER,
			},
		)
		self.assertEqual(len(logs), 1)

	def test_no_recipients_no_notification(self):
		doc = self._make()
		logs = frappe.get_all(
			"Notification Log",
			filters={"document_type": "Website Booking", "document_name": doc.name},
		)
		self.assertEqual(logs, [])

	# --------------------------------------------------------------------- sms
	def test_status_change_to_confirmed_sends_sms(self):
		doc = self._make()
		with patch(
			"alicia_reviews.alicia_reviews.doctype.website_booking.website_booking.send_booking_sms"
		) as spy:
			doc.status = "Confirmed"
			doc.save(ignore_permissions=True)
		spy.assert_called_once()
		self.assertEqual(spy.call_args[0][1], "confirmed")

	def test_status_change_to_cancelled_sends_sms(self):
		doc = self._make()
		with patch(
			"alicia_reviews.alicia_reviews.doctype.website_booking.website_booking.send_booking_sms"
		) as spy:
			doc.status = "Cancelled"
			doc.save(ignore_permissions=True)
		spy.assert_called_once()
		self.assertEqual(spy.call_args[0][1], "cancelled")

	def test_same_status_save_does_not_send_sms(self):
		doc = self._make()
		with patch(
			"alicia_reviews.alicia_reviews.doctype.website_booking.website_booking.send_booking_sms"
		) as spy:
			doc.customer_name = "Test Client Renamed"
			doc.save(ignore_permissions=True)
		spy.assert_not_called()

	def test_send_booking_sms_disabled_by_default(self):
		doc = self._make()
		self.assertEqual(send_booking_sms(doc, "confirmed"), "disabled")

	def test_send_booking_sms_logs_when_no_gateway(self):
		self.settings.sms_enabled = 1
		self.settings.sms_confirmed = "Hi {customer_name}, confirmed for {preferred_date}."
		self.settings.save(ignore_permissions=True)
		doc = self._make()
		with patch.object(frappe.db, "get_single_value", return_value=None):
			self.assertEqual(send_booking_sms(doc, "confirmed"), "logged")

	def test_send_booking_sms_sent_with_gateway(self):
		self.settings.sms_enabled = 1
		self.settings.sms_confirmed = "Hi {customer_name}."
		self.settings.save(ignore_permissions=True)
		doc = self._make()
		with patch.object(frappe.db, "get_single_value", return_value="https://sms.example/api"), patch(
			"frappe.core.doctype.sms_settings.sms_settings.send_sms"
		) as send:
			self.assertEqual(send_booking_sms(doc, "confirmed"), "sent")
		send.assert_called_once()
		self.assertEqual(send.call_args[0][0], ["+254712345678"])

	# ------------------------------------------------------------- scheduler
	def test_cancel_weekend_bookings(self):
		# Seed with the weekend guard off so we can create the weekend rows.
		self.settings.block_weekend_bookings = 0
		self.settings.save(ignore_permissions=True)
		upcoming_weekend = self._make(preferred_date=NEXT_SATURDAY, status="Confirmed")
		upcoming_weekday = self._make(preferred_date=NEXT_WEDNESDAY, status="Pending")
		past_weekend = self._make(preferred_date=PAST_SATURDAY, status="Confirmed")

		self.settings.block_weekend_bookings = 1
		self.settings.save(ignore_permissions=True)
		cancel_weekend_bookings()

		self.assertEqual(
			frappe.db.get_value("Website Booking", upcoming_weekend.name, "status"), "Cancelled"
		)
		self.assertEqual(
			frappe.db.get_value("Website Booking", upcoming_weekday.name, "status"), "Pending"
		)
		# past bookings are left alone
		self.assertEqual(
			frappe.db.get_value("Website Booking", past_weekend.name, "status"), "Confirmed"
		)

	# ------------------------------------------------------------- msisdn util
	def test_normalize_msisdn(self):
		self.assertEqual(normalize_msisdn("0712345678"), "+254712345678")
		self.assertEqual(normalize_msisdn("254712345678"), "+254712345678")
		self.assertEqual(normalize_msisdn("+254712345678"), "+254712345678")
		self.assertEqual(normalize_msisdn("712345678"), "+254712345678")
		self.assertEqual(normalize_msisdn("+254 712 345 678"), "+254712345678")
		self.assertIsNone(normalize_msisdn(""))
