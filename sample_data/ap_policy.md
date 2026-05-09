# Cashflo Accounts Payable Policy
## Invoice Booking & Three-Way Match Rules

### Section 1: Invoice Receipt & Basic Validation

1.1 Every invoice received must contain the following mandatory fields: Invoice Number, Invoice Date, Vendor GSTIN, Purchase Order Number, and Grand Total Amount. If any mandatory field is missing, the invoice shall be flagged as "Incomplete" and routed to the AP Clerk for manual review.

1.2 If the invoice date is in the future (i.e., later than the current processing date), the invoice must be rejected with reason "Future-Dated Invoice Not Permitted."

1.3 Handwritten invoices above INR 50,000 require additional approval from the AP Manager before booking.

1.4 Duplicate invoice detection: If an invoice with the same Invoice Number and Vendor GSTIN already exists in the system, the new invoice must be flagged as "Potential Duplicate" and held for review.

### Section 2: Purchase Order Matching

2.1 Every invoice must reference a valid Purchase Order (PO). If the PO Number on the invoice does not match any active PO in the system, the invoice shall be rejected with reason "Invalid PO Reference."

2.2 Invoice Amount vs PO Amount:
   a. If the Invoice Total Amount is within +/- 1% of the PO Amount (tolerance), the invoice is auto-approved for booking.
   b. If the Invoice Total Amount exceeds the PO Amount by more than 1% but less than 10%, the invoice is routed to the Department Head for approval.
   c. If the Invoice Total Amount exceeds the PO Amount by 10% or more, the invoice is escalated to the Finance Controller with a mandatory justification note.
   d. If the Invoice Total Amount is less than the PO Amount by more than 5%, a flag "Under-Invoiced — Verify Partial Delivery" must be raised.

2.3 Line-Item Matching:
   a. Each invoice line item quantity must be compared against the corresponding PO line item quantity.
   b. If Invoice Quantity > PO Quantity for any line item, the invoice is held with flag "Quantity Exceeds PO."
   c. If Invoice Unit Rate differs from the PO Unit Rate by more than 2%, the line item is flagged "Rate Mismatch" and routed to Procurement for verification.

### Section 3: GRN (Goods Receipt Note) Matching

3.1 For goods-based POs, a valid GRN must exist before the invoice can be booked. If no GRN is found for the referenced PO, the invoice is held with status "Awaiting GRN."

3.2 Invoice Quantity vs GRN Quantity:
   a. If Invoice Quantity <= GRN Quantity, the match is accepted.
   b. If Invoice Quantity > GRN Quantity, the invoice is rejected with reason "Invoice Qty Exceeds Received Qty. Refer Section 2.3(b) for PO-level escalation."

3.3 GRN Date must be on or before the Invoice Date. If the GRN Date is after the Invoice Date, the invoice is flagged with "GRN Post-Dated — Verify Receipt Timeline."

### Section 4: Tax & Compliance Validation

4.1 Vendor GSTIN Validation: The vendor's GSTIN on the invoice must match the GSTIN registered in the vendor master. If there is a mismatch, the invoice is rejected with reason "GSTIN Mismatch — Update Vendor Master or Verify Invoice."

4.2 PAN-GSTIN Cross Check: The PAN embedded in the vendor's GSTIN (characters 3–12) must match the PAN on file. Failure triggers a compliance hold.

4.3 Tax Calculation Verification:
   a. Total Taxable Amount + Total Tax Amount must equal the Grand Total (within INR 1 tolerance for rounding).
   b. If the supply is intra-state, CGST and SGST must each be present and equal.
   c. If the supply is inter-state, IGST must be present and CGST/SGST must be zero.
   d. If tax components do not match the above rules, the invoice is flagged "Tax Calculation Error."

4.4 Place of Supply must match the state code in the buyer's GSTIN. If mismatched, flag "Place of Supply Mismatch."

### Section 5: Amount Thresholds & Approval Matrix

5.1 Invoices up to INR 1,00,000: Auto-approved if all validations in Sections 1–4 pass.

5.2 Invoices between INR 1,00,001 and INR 10,00,000: Require approval from the Department Head.

5.3 Invoices between INR 10,00,001 and INR 50,00,000: Require approval from the Finance Controller.

5.4 Invoices above INR 50,00,000: Require approval from the CFO. Subject to additional audit trail documentation per Section 6.

5.5 Exception to Section 5.1: If the vendor is on the "Watch List" (compliance or payment history issues), all invoices regardless of amount require Department Head approval.

### Section 6: Deviation Notifications

6.1 Any deviation detected during the three-way match (Sections 2–3) must trigger an email notification to the relevant stakeholder within 15 minutes of detection.

6.2 Email notification must include: Invoice Number, Vendor Name, PO Number, Deviation Type (Amount Mismatch / Quantity Mismatch / Rate Mismatch / Missing GRN), Deviation Details (expected vs actual values), and Recommended Action.

6.3 If a deviation is not resolved within 48 hours, an automatic escalation email must be sent to the next level approver as defined in Section 5.

6.4 Critical deviations (amount variance > 10% or compliance failures from Section 4) must trigger an immediate email to the Finance Controller and the Internal Audit team simultaneously.

### Section 7: QR Code & Digital Signature Validation

7.1 For invoices above INR 10,00,000, a valid QR code must be present on the invoice as per GST e-invoicing norms. If the QR code is missing, the invoice is held with status "QR Code Missing — E-Invoice Compliance Required."

7.2 If a QR code is present, the Invoice Number and Vendor GSTIN extracted from the QR code must match the corresponding fields on the invoice face. Any mismatch results in a "QR Validation Failed" flag.

7.3 Digital signature is recommended but not mandatory. If present and invalid, flag "Signature Verification Failed" for manual review.
