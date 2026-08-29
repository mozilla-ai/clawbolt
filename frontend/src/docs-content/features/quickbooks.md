# QuickBooks Online

> **Experimental**
>
> The QuickBooks integration is experimental. Do not connect it to a production QuickBooks account. Use a sandbox company only while this feature is being developed.

Clawbolt can connect to QuickBooks Online so you can manage invoices, estimates, and customers directly from chat. Ask what invoices exist for a test customer, or dictate a job description and Clawbolt will create a draft estimate in QuickBooks.

## What it can do

The integration provides four tools that the agent uses to interact with QuickBooks:

| Tool | Purpose |
|------|---------|
| `qb_query` | Look up invoices, estimates, customers, items, payments, and more |
| `qb_create` | Create customers, estimates, invoices, or items |
| `qb_update` | Update customers, estimates, invoices, or items |
| `qb_send` | Email an invoice or estimate to a customer |

The agent handles the queries and API calls itself, so you just talk in plain language.

### Voice-to-estimate workflow

The primary workflow for tradespeople in the field:

1. Dictate a job description into your phone (your phone's speech-to-text converts it to a message)
2. Clawbolt extracts the client, scope, labor, and materials from your description
3. Clawbolt creates a draft estimate in QuickBooks immediately
4. Come back later to review and refine it via chat
5. When it's ready, Clawbolt sends it to your client by email

No "computer time" required.

## Setup

### 1. Create an Intuit developer account

Go to [developer.intuit.com](https://developer.intuit.com) and sign up (or sign in with your existing Intuit account).

### 2. Create a QuickBooks app

1. From the dashboard, click **Create an app**
2. Select **QuickBooks Online and Payments** as the platform
3. Give it a name (e.g. "Clawbolt")
4. Select the scope **`com.intuit.quickbooks.accounting`**
5. Click **Create App**

### 3. Get your client ID and secret

1. In your app, go to the **Keys & credentials** tab
2. Under **Development** (for sandbox testing), copy:
   - **Client ID**
   - **Client Secret**
3. Add `https://<your-domain>/api/oauth/callback` as a redirect URI

### 4. Configure Clawbolt

Add these to your `.env` file:

```bash
QUICKBOOKS_CLIENT_ID=your_client_id
QUICKBOOKS_CLIENT_SECRET=your_client_secret
QUICKBOOKS_ENVIRONMENT=sandbox
APP_BASE_URL=https://your-clawbolt-domain.example
```

Restart Clawbolt. Users authorize their own company through Clawbolt's OAuth flow; tokens and company IDs are stored per user.

Once the admin credentials are configured, users can connect their QuickBooks account in two ways:

- **Over chat (preferred):** Ask the assistant "connect my QuickBooks" and it will generate an authorization link.
- **From the dashboard:** Open the **Tools** page and connect from there.

For user workflows and chat examples, see [Estimates and Invoicing](/docs/guide/estimates).

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Tools don't appear | Check that `QUICKBOOKS_CLIENT_ID` and `QUICKBOOKS_CLIENT_SECRET` are set. The integration is disabled when these are empty. |
| 401 Unauthorized | Reconnect QuickBooks from the Tools page to replace an expired or revoked grant. |
| 400 Bad Request | The query syntax may be invalid. Check the Clawbolt logs for the full error. QBO does not support subqueries. |
| Wrong company data | Disconnect and reconnect, then authorize the intended QuickBooks company. |

## Reference

See the [Configuration](https://github.com/mozilla-ai/clawbolt/blob/main/docs/self-host/configuration.md#quickbooks-online) page for the full list of environment variables.
