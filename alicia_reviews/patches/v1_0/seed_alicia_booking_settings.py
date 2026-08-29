import frappe

DEFAULT_RECIPIENTS = [
	"aliciahairlinebeauty@gmail.com",
	"maggie@alicia.boraerp.co.ke",
]


def execute():
	"""Create the Alicia Booking Settings single with sensible defaults."""
	settings = frappe.get_single("Alicia Booking Settings")

	if settings.block_weekend_bookings is None:
		settings.block_weekend_bookings = 1

	if not settings.notify_recipients:
		existing = set(
			frappe.get_all(
				"User",
				filters={"enabled": 1, "name": ("in", DEFAULT_RECIPIENTS)},
				pluck="name",
			)
		)
		recipients = [r for r in DEFAULT_RECIPIENTS if r in existing]
		if recipients:
			settings.notify_recipients = "\n".join(recipients)

	settings.save(ignore_permissions=True)
