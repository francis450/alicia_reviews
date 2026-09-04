import frappe
from frappe.model.document import Document


class PesapalTransaction(Document):
	def append_log(self, entry):
		"""Append a timestamped dict to the JSON log field."""
		import json

		log = []
		if self.ipn_log:
			try:
				log = json.loads(self.ipn_log)
			except (ValueError, TypeError):
				log = []
		log.append({"at": frappe.utils.now(), **entry})
		self.ipn_log = json.dumps(log, indent=2, default=str)
