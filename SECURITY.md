# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**
([direct link](https://github.com/Tevaalgorithms/human-resilience-map/security/advisories/new)).

Include what you found, the steps to reproduce it, the impact, and the commit or version. This is a
single-maintainer pilot, so there is no guaranteed response time, but reports are read and
answered as soon as possible.

## Supported versions

Only the `main` branch is supported. This project is a planning pilot and has not had an
independent security review.

## Security design

| Area | What the project does |
| --- | --- |
| Authentication | Amazon Cognito authorization-code flow. Sign-in is invite-only, with optional MFA. |
| Sessions | Tokens stay on the server in a PostgreSQL session store. The browser holds an opaque session ID in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie. |
| Authorization | API routes require an authenticated user, and job results are owner-restricted. |
| Secrets | AWS Systems Manager Parameter Store (SecureString) in staging and production. No secrets in the repository or container images. Local `.env` files are gitignored. |
| Input handling | Bounded planning boxes, upload size limits, schema-validated requests, and parameterized SQL. |
| Language models | Models receive tool-derived evidence only. Plans are schema-validated, calls and tokens are bounded, prompts are versioned, and generated text is checked against tool output. User text is treated as untrusted. |
| Network | HTTPS through Caddy. Only ports 80 and 443 are public; SSH is limited to an administrator address. |
| Cloud access | Least-privilege IAM. The runtime identity can only read its own SSM parameters. A budget alert watches spend. |

## Data

The application uses public datasets (US Census, Landsat surface temperature, NLCD tree canopy).
It does not collect personal health information. Planning scores are indices, not medical
predictions, and are not a substitute for emergency or clinical guidance.

## If a secret is exposed

1. Rotate it immediately: the Cognito app client secret, the database password, the session
   secret, the OpenAI API key, or the IAM access key, as applicable.
2. Update the matching SSM parameter and restart the affected services.
3. Review recent logs for misuse and remove the secret from Git history if it was committed.

## For contributors

- Never commit secrets, tokens or private keys. Run `git status` and review the diff first.
- Do not weaken authentication, ownership checks, input limits or the grounding checks without a
  documented reason.
- Add a test with every security-relevant fix.
