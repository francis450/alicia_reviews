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
