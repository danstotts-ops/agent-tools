"""Google service: GA4, Search Console, Tag Manager, YouTube over REST.

Auth resolves in this order (see client.py):
  1. GOOGLE_OAUTH_REFRESH_TOKEN + GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET
     (used on Railway / prod — a stored user OAuth grant).
  2. Application Default Credentials via google.auth.default (local dev with
     `gcloud auth application-default login`).

Transport is a google-auth AuthorizedSession (requests), never httplib2.
"""
