import frappe
from frappe.model.document import Document

SANDBOX_BASE = "https://cybqa.pesapal.com/pesapalv3"
LIVE_BASE = "https://pay.pesapal.com/v3"


class PesapalSettings(Document):
	@property
	def base_url(self):
		return SANDBOX_BASE if self.sandbox else LIVE_BASE

	def validate(self):
		if self.enabled and not (self.consumer_key and self.get_password("consumer_secret", raise_exception=False)):
			frappe.throw("Set the Consumer Key and Consumer Secret before enabling Pesapal.")
