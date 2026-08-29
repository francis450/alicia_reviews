// Website Booking — let staff review/edit the client SMS before it goes out.
const SMS_KINDS = { Confirmed: "confirmed", Cancelled: "cancelled" };

frappe.ui.form.on("Website Booking", {
	onload(frm) {
		frm._sms_last_status = frm.doc.status;
	},

	refresh(frm) {
		if (!frm.is_new() && SMS_KINDS[frm.doc.status]) {
			frm.add_custom_button(__("Send status SMS"), () =>
				open_sms_dialog(frm, SMS_KINDS[frm.doc.status])
			);
		}
	},

	after_save(frm) {
		const previous = frm._sms_last_status;
		const current = frm.doc.status;
		frm._sms_last_status = current;

		if (previous === current || !SMS_KINDS[current]) {
			return;
		}
		// Only auto-prompt when the server is holding back for review; otherwise it
		// has already sent the SMS and we'd be sending a second one.
		maybe_prompt_sms(frm, SMS_KINDS[current]);
	},
});

function maybe_prompt_sms(frm, kind) {
	frappe.call({
		method:
			"alicia_reviews.alicia_reviews.doctype.website_booking.website_booking.get_status_sms_preview",
		args: { name: frm.doc.name, kind },
		callback(r) {
			const preview = r.message;
			if (preview && preview.enabled && preview.review) {
				show_dialog(frm, kind, preview);
			}
		},
	});
}

function open_sms_dialog(frm, kind) {
	frappe.call({
		method:
			"alicia_reviews.alicia_reviews.doctype.website_booking.website_booking.get_status_sms_preview",
		args: { name: frm.doc.name, kind },
		callback(r) {
			const preview = r.message;
			if (!preview) {
				return;
			}
			if (!preview.enabled) {
				frappe.msgprint(
					__("Client SMS is turned off in Alicia Booking Settings.")
				);
				return;
			}
			show_dialog(frm, kind, preview);
		},
	});
}

function show_dialog(frm, kind, preview) {
	const d = new frappe.ui.Dialog({
		title: __("Send {0} SMS", [__(kind)]),
		fields: [
			{
				fieldname: "number",
				fieldtype: "Data",
				label: __("To"),
				default: preview.number,
				reqd: 1,
			},
			{
				fieldname: "message",
				fieldtype: "Small Text",
				label: __("Message"),
				default: preview.message,
				reqd: 1,
			},
			{
				fieldname: "hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Edit the text if you need to, then send. Leave this without sending to skip the SMS."
				)}</p>`,
			},
		],
		primary_action_label: __("Send SMS"),
		primary_action(values) {
			frappe.call({
				method:
					"alicia_reviews.alicia_reviews.doctype.website_booking.website_booking.send_status_sms",
				args: { name: frm.doc.name, kind, number: values.number, message: values.message },
				freeze: true,
				freeze_message: __("Sending SMS…"),
				callback(r) {
					d.hide();
					const status = (r.message || {}).status;
					if (status === "sent") {
						frappe.show_alert({ message: __("SMS sent"), indicator: "green" });
					} else if (status === "logged") {
						frappe.show_alert({
							message: __("Saved — no SMS gateway configured yet"),
							indicator: "orange",
						});
					} else {
						frappe.show_alert({
							message: __("SMS failed — see Error Log"),
							indicator: "red",
						});
					}
					frm.reload_doc();
				},
			});
		},
		secondary_action_label: __("Skip"),
		secondary_action() {
			d.hide();
		},
	});
	d.show();
}
