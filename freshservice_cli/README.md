# Freshservice CLI

This package provides the `freshservice` command from the parent
[`freshworks-cli`](../) project. It uses the Freshservice v2 API with the
command set documented in the parent README.

The API key is loaded from the macOS Keychain entry with account `api-key` and
service `freshservice.api-key`. Set the API base URL, for example
`https://<your-domain>.freshservice.com/api/v2`, in the
`FRESHSERVICE_BASE_URL` environment variable or pass `--base-url`.
