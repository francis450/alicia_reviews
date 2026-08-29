import frappe


def execute():
	"""Default existing installs to reviewing the SMS before it's sent.

	A Check field that was never saved reads back as 0, not None, so we look for
	an actual stored value in `tabSingles` instead.
	"""
	already_set = frappe.db.exists(
		"Singles",
		{"doctype": "Alicia Booking Settings", "field": "review_sms_before_send"},
	)
	if already_set:
		return

	settings = frappe.get_single("Alicia Booking Settings")
	settings.review_sms_before_send = 1
	settings.save(ignore_permissions=True)
