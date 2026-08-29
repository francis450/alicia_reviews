import frappe
from frappe.model.document import Document


class AliciaBookingSettings(Document):
	def validate(self):
		# Keep the recipient list tidy: strip blanks, dedupe, keep order.
		if self.notify_recipients:
			seen = set()
			cleaned = []
			for line in self.notify_recipients.replace(",", "\n").splitlines():
				email = line.strip()
				if email and email.lower() not in seen:
					seen.add(email.lower())
					cleaned.append(email)
			self.notify_recipients = "\n".join(cleaned)
