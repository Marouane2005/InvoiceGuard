"""One catalog of customer-facing errors. Every error says what happened and what to do next."""
from __future__ import annotations

# code -> (http status, message template, hint template)
CATALOG: dict = {
    "missing_api_key": (401, "No API key was provided.", "Send your key in the header 'Authorization: Bearer <your key>'."),
    "invalid_api_key": (401, "That API key is not valid.", "Check the key for typos, or ask your administrator for a new one."),
    "invalid_admin_token": (401, "That admin token is not valid.", "Send the admin token in the 'X-Admin-Token' header."),
    "rate_limited": (429, "Too many requests.", "Wait a minute, then try again."),
    "empty_file": (400, "The uploaded file is empty.", "Choose a file that has content and upload it again."),
    "unsupported_file": (415, "This file type is not supported.", "Upload a PDF, PNG, JPEG, GIF, or WebP file."),
    "file_too_large": (413, "This file is too large.", "Upload a file smaller than {limit_mb} MB."),
    "quota_exceeded": (429, "Your monthly invoice limit has been reached.", "Ask your administrator to raise the limit, or wait until next month."),
    "internal_error": (500, "Something went wrong on our side.", "Try again in a few minutes. If it keeps happening, contact support and quote the X-Request-ID header."),
    "server_busy": (503, "The server is busy reading other invoices.", "Wait a few seconds, then upload again."),
    "extraction_not_configured": (503, "Invoice reading is not set up on this server.", "Ask the server administrator to set ANTHROPIC_API_KEY."),
    "extraction_failed": (502, "We could not read this invoice: {detail}.", "Try again in a few minutes. If it keeps failing, upload a clearer copy."),
    "not_found": (404, "We could not find that item.", "Check the ID and try again."),
    "tenant_not_found": (404, "We could not find that customer account.", "Check the account ID and try again."),
    "conflict": (409, "{detail}", "Refresh the review queue to see the item's current state."),
    "invalid_correction": (422, "The corrected invoice still has problems: {detail}.", "Fix those fields and submit again."),
    "invalid_request": (422, "The request is not valid: {detail}.", "Check the fields listed and try again."),
    "invalid_setting": (422, "That setting is not valid: {detail}.", "Correct the value and save again."),
    "notifications_not_configured": (409, "Slack notifications are not set up.", "Add a Slack webhook in your settings, then try again."),
}


class PipelineError(Exception):
    """An error that maps directly to an HTTP response."""

    def __init__(self, code: str, **fmt):
        status, message, hint = CATALOG[code]
        self.code = code
        self.status = status
        self.message = message.format(**fmt)
        self.hint = hint.format(**fmt)
        super().__init__(self.message)

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}
