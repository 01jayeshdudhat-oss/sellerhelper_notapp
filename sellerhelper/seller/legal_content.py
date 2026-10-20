import hashlib
import json


LEGAL_PAGES = {
    "about": {
        "title": "About SellerHelper",
        "intro": (
            "SellerHelper provides online tools for customers, independent shops, "
            "shop teams, and delivery partners."
        ),
        "sections": [
            {
                "heading": "What the platform does",
                "paragraphs": [
                    "Customers can discover participating shops, place orders, manage delivery addresses, and review order and payment information.",
                    "Shop owners and authorized staff can manage shop listings and order workflows. Delivery partners may receive and complete assigned deliveries.",
                ],
            },
            {
                "heading": "Marketplace participants",
                "paragraphs": [
                    "Unless an order page expressly says otherwise, the shop shown for an item is the seller responsible for that item, its description, availability, and fulfillment. SellerHelper provides the online marketplace and related tools; it is not automatically the seller or delivery provider for every transaction.",
                ],
            },
        ],
    },
    "terms": {
        "title": "Terms and Conditions",
        "intro": "These terms apply when you access SellerHelper or use its marketplace and account features.",
        "sections": [
            {
                "heading": "Accounts and use",
                "paragraphs": [
                    "Provide accurate account, contact, tax, and delivery information; keep your credentials private; and promptly update information that changes. You are responsible for activity performed using your account.",
                    "Use only the workspace and tools you are authorized to access. Do not misuse the service, interfere with its operation, submit unlawful or deceptive content, or attempt unauthorized access.",
                ],
            },
            {
                "heading": "Listings, orders, and availability",
                "paragraphs": [
                    "Product descriptions, prices, stock, units, and seller terms are supplied or managed by participating sellers and may change. An order is subject to seller confirmation and availability. Review the checkout and order details before submitting or paying.",
                    "For products sold by a shop, the shop is responsible for the product, its quality, applicable tax information, and fulfillment, subject to applicable law and any express platform commitment.",
                ],
            },
            {
                "heading": "Payments, wallet, cancellation, and refunds",
                "paragraphs": [
                    "Payment methods, fees, wallet use, cancellation eligibility, and refund handling are shown in the relevant checkout or order workflow. A displayed wallet balance is an account record and may be adjusted for valid transactions, refunds, reversals, or corrections.",
                    "Cancellation and refund eligibility depends on order status, the seller's stated terms, the payment method, and applicable law. Contact support promptly if a payment or refund appears incorrect.",
                ],
            },
            {
                "heading": "Delivery and verification",
                "paragraphs": [
                    "Delivery availability and timing depend on the shop, delivery coverage, item readiness, and delivery partner availability. Keep your delivery details accurate and accessible.",
                    "A delivery verification code is sensitive. Share it only with the assigned delivery partner when your order is actually being handed over. SellerHelper may use order and delivery status information to operate and support the service.",
                ],
            },
            {
                "heading": "Service availability and changes",
                "paragraphs": [
                    "Features may be updated, suspended, or discontinued for maintenance, safety, legal, or operational reasons. We will not exclude rights or remedies that cannot be excluded under applicable law.",
                ],
            },
            {
                "heading": "Contact and applicable law",
                "paragraphs": [
                    "For questions or complaints, use the Contact Us page and the support details published there. These terms are subject to applicable law. Any mandatory rights or consumer protections available to you remain unaffected.",
                ],
            },
        ],
    },
    "privacy": {
        "title": "Privacy Policy",
        "intro": "This policy describes the information processed when you use SellerHelper. The information actually collected depends on the features you use.",
        "sections": [
            {
                "heading": "Information used",
                "items": [
                    "Account and profile information such as name, username, contact details, and optional business or tax details.",
                    "Order, item, payment-status, wallet, cancellation, refund, and customer-service records.",
                    "Delivery address and location information when you provide it or use a location-enabled feature.",
                    "Shop, staff, rider, device, and notification information needed to operate the selected workspace and requested features.",
                    "Technical and security information generated when using the service, such as session and diagnostic records.",
                ],
            },
            {
                "heading": "How information is used and shared",
                "paragraphs": [
                    "Information is used to provide accounts, marketplace listings, order processing, payments, delivery coordination, customer support, fraud and security controls, and service improvements.",
                    "Relevant order and delivery details are shared with the shop and assigned delivery participants so they can fulfill an order. Payment processors and technical service providers may process information needed to provide their services. Information may also be disclosed when required by law or to protect users and the service.",
                ],
            },
            {
                "heading": "Location, retention, and security",
                "paragraphs": [
                    "Location access is used only for features that need it, such as selecting a delivery location or coordinating delivery. Device permissions can be managed in your device settings.",
                    "Records are retained for as long as reasonably needed for service, accounting, safety, dispute-resolution, and legal purposes. Appropriate safeguards are used, but no online service can guarantee absolute security.",
                ],
            },
            {
                "heading": "Your choices and requests",
                "paragraphs": [
                    "You can review and update profile and address information using the available account pages. To ask about access, correction, deletion, or another privacy concern, contact us using the details on the Contact Us page. Some records may need to be retained where required or permitted by law.",
                ],
            },
        ],
    },
    "disclaimer": {
        "title": "Disclaimer",
        "intro": "Please review this information together with the Terms and Conditions and the details shown for your order.",
        "sections": [
            {
                "heading": "Seller listings",
                "paragraphs": [
                    "Product information, images, prices, stock, tax details, and seller statements may be supplied by participating shops. The shop is responsible for the accuracy and fulfillment of its listing, subject to applicable law.",
                ],
            },
            {
                "heading": "Availability and delivery estimates",
                "paragraphs": [
                    "Availability, delivery coverage, fees, and estimated delivery times can change. An estimate is not a guarantee; delays may occur because of seller readiness, traffic, weather, location, or other operational conditions.",
                ],
            },
            {
                "heading": "Technical information",
                "paragraphs": [
                    "Maps, location estimates, notifications, and other third-party or device-provided information may be incomplete or temporarily unavailable. Check the final order information and use your judgment before relying on an estimate.",
                ],
            },
            {
                "heading": "Legal rights",
                "paragraphs": [
                    "Nothing on this page limits a consumer right or other protection that cannot legally be limited. This notice is not professional, tax, or legal advice.",
                ],
            },
        ],
    },
    "refunds": {
        "title": "Cancellation and Refund Policy",
        "intro": "Cancellation and refund availability depends on the order status, seller action, payment method, and applicable law.",
        "sections": [
            {
                "heading": "Customer cancellations",
                "paragraphs": [
                    "Where the order page offers a cancellation option, submit the request through that order as soon as possible. Orders may become ineligible for cancellation after the shop begins confirmation, preparation, or fulfillment.",
                ],
            },
            {
                "heading": "Shop cancellations and unavailable items",
                "paragraphs": [
                    "If a shop cancels an order or cannot fulfill an item, any applicable refund or balance adjustment is handled according to the order record, payment method, seller terms, and applicable law.",
                ],
            },
            {
                "heading": "Refund processing",
                "paragraphs": [
                    "Approved refunds are returned through the applicable payment method or credited to the wallet when the order workflow indicates that method. Processing and posting times can vary by payment provider and bank.",
                    "Review your order and wallet history first. If a payment or refund is missing or incorrect, contact support with the order reference and relevant payment details. Never send a password, full card number, or one-time password to support.",
                ],
            },
        ],
    },
    "delivery": {
        "title": "Delivery Policy",
        "intro": "Delivery is available only where a participating shop and delivery service can fulfill the selected order.",
        "sections": [
            {
                "heading": "Coverage and timing",
                "paragraphs": [
                    "Coverage, delivery fees, and estimated times are shown during the order flow when available. Estimates may change due to shop preparation, route conditions, weather, demand, or other operational factors.",
                ],
            },
            {
                "heading": "Address and contact details",
                "paragraphs": [
                    "Provide a complete address, accurate map pin, and reachable phone number. You may manage saved addresses from your account. A location correction made by a delivery partner for a completed order applies to that order and does not automatically change your saved address.",
                ],
            },
            {
                "heading": "Receiving an order",
                "paragraphs": [
                    "Be available at the delivery location and inspect the order as reasonably possible. When delivery verification is required, share your code only with the assigned delivery partner at handover. Do not share it in advance or with an unassigned person.",
                    "Report missing, damaged, or incorrect items through the order or support workflow as soon as possible, including the order reference and relevant details.",
                ],
            },
        ],
    },
    "grievance": {
        "title": "Grievance Redressal",
        "intro": "We aim to review service complaints promptly and route order issues to the shop or delivery participant best placed to resolve them.",
        "sections": [
            {
                "heading": "How to raise a complaint",
                "paragraphs": [
                    "For an order, payment, wallet, or delivery issue, contact us using the published support details on the Contact Us page. Include your account username, order reference, a clear description, and any relevant evidence. Do not include passwords, full payment-card details, or one-time passwords.",
                    "For a product or fulfillment issue, identify the shop and item. We may share relevant complaint information with that shop or delivery participant to investigate and respond.",
                ],
            },
            {
                "heading": "Grievance officer",
                "paragraphs": [
                    "The grievance officer's name and contact details should be published in the Contact Us section by the platform operator. If those details are not displayed, the operator has not configured them in this deployment; contact the site administrator.",
                ],
            },
        ],
    },
}


REQUIRED_LEGAL_DOCUMENTS = (
    "terms",
    "privacy",
    "disclaimer",
    "refunds",
    "delivery",
)


def legal_document_version(document_slug):
    document = LEGAL_PAGES[document_slug]
    canonical_content = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()
