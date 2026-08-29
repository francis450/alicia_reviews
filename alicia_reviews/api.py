import frappe

def validate_review(data):
    name = (data.get("name") or "").strip()
    comment = (data.get("comment") or "").strip()
    rating = int(data.get("rating") or 0)

    if not name or len(name) > 60:
        frappe.throw("Please provide a valid name.")
    if len(comment) < 5 or len(comment) > 500:
        frappe.throw("Please share a review between 5 and 500 characters.")
    if rating not in range(1, 6):
        frappe.throw("Please select a rating from 1 to 5.")
    return name, rating, comment

def get_reviews():
    return frappe.get_all(
        "Website Review",
        filters={"published": 1},
        fields=["reviewer_name as name", "rating", "comment", "creation as created_at"],
        order_by="creation desc",
        limit_page_length=200,
    )

def submit_review(data):
    name, rating, comment = validate_review(data)
    review = frappe.get_doc({
        "doctype": "Website Review",
        "reviewer_name": name,
        "rating": rating,
        "comment": comment,
        "published": 1,
    })
    review.insert(ignore_permissions=True)
    return {
        "name": review.reviewer_name,
        "rating": review.rating,
        "comment": review.comment,
        "created_at": review.creation,
    }

@frappe.whitelist(allow_guest=True)
def website_reviews():
    if frappe.request.method == "GET":
        return get_reviews()
    if frappe.request.method == "POST":
        return submit_review(frappe.request.get_json(silent=True) or {})
    frappe.throw("Method not allowed", frappe.ValidationError)


def validate_booking(data):
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()
    service = (data.get("service") or "").strip()
    date = (data.get("date") or "").strip()
    time = (data.get("time") or "").strip()
    notes = (data.get("notes") or "").strip()

    if not name or len(name) > 100:
        frappe.throw("Please provide a valid name.")
    if not phone or len(phone) > 20:
        frappe.throw("Please provide a valid phone number.")
    if not service:
        frappe.throw("Please select a service.")
    if not date:
        frappe.throw("Please select a preferred date.")
    if not time:
        frappe.throw("Please select a preferred time.")
    if len(notes) > 500:
        frappe.throw("Notes must be under 500 characters.")

    from alicia_reviews.alicia_reviews.doctype.website_booking.website_booking import (
        get_settings,
        is_weekend,
    )

    settings = get_settings()
    if settings.block_weekend_bookings and is_weekend(date):
        frappe.throw(
            settings.weekend_message
            or "Fridays and Saturdays are walk-in only — please choose another day."
        )

    return name, phone, email, service, date, time, notes

def submit_booking(data):
    name, phone, email, service, date, time, notes = validate_booking(data)
    booking = frappe.get_doc({
        "doctype": "Website Booking",
        "customer_name": name,
        "phone": phone,
        "email": email,
        "service": service,
        "preferred_date": date,
        "preferred_time": time,
        "notes": notes,
        "status": "Pending",
    })
    booking.insert(ignore_permissions=True)
    return {
        "name": booking.name,
        "customer_name": booking.customer_name,
        "phone": booking.phone,
        "email": booking.email,
        "service": booking.service,
        "preferred_date": booking.preferred_date,
        "preferred_time": booking.preferred_time,
        "notes": booking.notes,
        "status": booking.status,
        "created_at": booking.creation,
    }

@frappe.whitelist(allow_guest=True)
def website_bookings():
    if frappe.request.method == "POST":
        return submit_booking(frappe.request.get_json(silent=True) or {})
    frappe.throw("Method not allowed", frappe.ValidationError)
