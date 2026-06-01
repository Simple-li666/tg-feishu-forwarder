# External Cron Setup

GitHub's native `schedule` trigger can be delayed or skipped, so this project uses an external HTTP cron service to trigger the workflow through GitHub's `workflow_dispatch` API.

Recommended service: <https://cron-job.org>

## 1. Create a GitHub token

Create a fine-grained personal access token in GitHub:

```text
GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens -> Generate new token
```

Recommended settings:

```text
Repository access: Only select repositories
Repository: Simple-li666/tg-feishu-forwarder
Permissions:
  Actions: Read and write
Expiration: 30 or 90 days
```

Do not commit this token into the repository. It will only be pasted into cron-job.org as an HTTP header.

GitHub's workflow dispatch API requires `Actions` write permission for fine-grained tokens.

## 2. Create the cron-job.org job

Create a new cron job:

```text
Title: tg-feishu-forwarder
URL: https://api.github.com/repos/Simple-li666/tg-feishu-forwarder/actions/workflows/tg-to-feishu.yml/dispatches
Schedule: Every 5 minutes
Request method: POST
```

Add these request headers:

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_GITHUB_TOKEN
X-GitHub-Api-Version: 2026-03-10
Content-Type: application/json
```

Request body:

```json
{
  "ref": "main",
  "inputs": {
    "trigger_source": "cron-job.org"
  }
}
```

If cron-job.org lets you configure expected status codes, accept any `2xx` response. GitHub's current API returns `200` with workflow run metadata for a successful dispatch.

## 3. Test

After saving the cron job:

1. Click the cron-job.org test/run button once.
2. Open GitHub Actions for this repository.
3. Confirm a new `TG to Feishu` run appears.
4. Send a new Telegram group message.
5. Wait for the next cron-job.org run and check Feishu.

The first run after setup may only update `state/last_ids.json` if there are no new Telegram messages after the current cursor. Subsequent runs only forward messages newer than the saved cursor.

## Security notes

- Use a fine-grained token, not a classic token, unless you have a specific reason.
- Restrict the token to this repository only.
- Grant only `Actions: Read and write`.
- Set an expiration date and rotate it when it expires.
- If the token leaks, revoke it immediately in GitHub.

References:

- GitHub workflow dispatch API: <https://docs.github.com/en/rest/actions/workflows?apiVersion=2026-03-10#create-a-workflow-dispatch-event>
- cron-job.org custom HTTP jobs: <https://docs.cron-job.org/creating-cron-jobs.html>
