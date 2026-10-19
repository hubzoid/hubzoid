"""Company directories: SAML sign-in and SCIM provisioning (Okta, Microsoft Entra ID).

Native, on maintained libraries: pysaml2 (Apache-2.0, with the ``xmlsec1``
program for signatures) and scim2-models (Apache-2.0). No outside sign-in
service is involved: the identity provider talks to this deployment directly.

The rules:

  * **The directory decides who exists.** People are created, changed and
    turned off through SCIM (``/scim/v2/<provider>/Users``). A provisioned
    account is *managed*: it signs in only through its provider and has no
    local password.
  * **SAML finds the provisioned account.** A signed assertion's NameID is
    matched to the SCIM ``userName`` (case-insensitive) of that provider. An
    unprovisioned or ambiguous person is refused; no account is ever matched
    or merged by email alone, and nobody is created at sign-in.
  * **Turning someone off turns off everything.** ``active: false`` or a DELETE
    disables the account and moves its credential counter in one transaction:
    browser sessions, personal API keys, MCP grants and work that runs as them
    stop at their next step. Only the same provider reporting the person active
    again brings the account back; an administrator can't override it. The
    account and its conversations are kept.
  * **Users only.** Groups are not part of this release; providers are set up
    to push users, not groups (a group request answers "not supported").
"""
