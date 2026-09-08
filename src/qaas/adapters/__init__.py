"""Backend adapters: the seams where a real service replaces a local stand-in.

Everything above an adapter is written against the abstract class, so swapping
`tracker: local` for `tracker: jira` in config is a one-line change and not a
rewrite of the agent-facing tool surface.

The one-line change is the config; the credentials are not. `tracker: jira`
reads them from the environment, and refuses to start without them:

    JIRA_BASE_URL=https://acme.atlassian.net
    JIRA_EMAIL=qa-bot@acme.example
    JIRA_API_TOKEN=...            # id.atlassian.com/manage-profile/security/api-tokens
    JIRA_PROJECT_KEY=CORVID
    JIRA_SECURITY_PROJECT_KEY=CORVIDSEC   # restricted; without it security
                                          # findings are refused, not filed
    JIRA_ISSUE_TYPE=Bug           # optional

They stay out of `config/` because `config/` is committed.
"""
