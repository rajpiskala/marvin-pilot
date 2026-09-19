# Security policy

Marvin Pilot handles a credential that can modify an Amazing Marvin account. Please do not include
API tokens, full-access tokens, sync credentials, task content, plans, receipts, backup exports, or
other private account data in a public issue.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting feature for this repository. Include the affected
Marvin Pilot version, operating system, reproduction steps using sanitized data, and the security
impact. Do not test a report against an account you do not own.

If private vulnerability reporting is unavailable, open a public issue containing no exploit or
private data and ask the maintainer for a private contact channel.

## Supported versions

Security fixes are provided for the latest published major release.

## Credential boundary

The intended deployment gives a read/discovery integration only Marvin's limited `API_TOKEN`.
Marvin Pilot keeps `FULL_ACCESS_TOKEN` on the human-controlled side, preferably in the operating
system credential store. Never place the full-access token in a change plan, prompt, issue,
environment variable, shell history, or MCP configuration.
