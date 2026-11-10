# Put the Follow-Up Agent on a server (Render)

This puts the agent, its database and the demo dashboard on Render, so they keep running when your Mac is
off. Allow about 30 minutes. It costs about **$20 a month**: the agent ($7), the dashboard ($7 plus $0.25 for
its small disk) and the database ($6). You can delete everything from Render at any time to stop paying.

You never need to paste a key into a chat. Keys go only into Render, Google Cloud and your own Terminal.

## 1. Create a Render account

1. Go to **render.com** and click **Get Started**. Sign up with **GitHub**.
2. When GitHub asks which repositories Render can see, choose **Only select repositories** and pick both
   **AutomaticEmailTracker** and **DemoTracker**.
3. In Render, open **Account Settings → Billing** and add a card.

## 2. Create everything from the Blueprint

1. In Render, click **New → Blueprint**.
2. Pick the **AutomaticEmailTracker** repository and click **Connect**. Render reads the `render.yaml` file
   and lists three things: `followup-db`, `followup-agent` and `followup-dashboard`.
3. Give the Blueprint any name, such as `followup`.
4. Render asks for a few values. Fill them in like this:

   | Setting | What to type |
   |---|---|
   | `ANTHROPIC_API_KEY` | Your Anthropic key (the same one in your Mac's `.env`) |
   | `GOOGLE_CLIENT_ID` | Same as your Mac's `.env` |
   | `GOOGLE_CLIENT_SECRET` | Same as your Mac's `.env` |
   | `TEST_ALLOWLIST` | Your two test Gmail addresses, separated by a comma |
   | `DEMO_PASSWORD` | Any password you like for the dashboard |
   | `AGENT_BASE_URL` | `https://followup-agent.onrender.com` for now (you fix it in step 5 if it differs) |
   | `AGENT_API_KEY` | `later` (you replace it in step 5) |
   | `AGENT_WEBHOOK_SECRET` | `later` (you replace it in step 5) |

5. Click **Apply** (or **Deploy Blueprint**). The first build takes about 5 to 10 minutes. Wait until
   `followup-agent` and `followup-dashboard` both show **Live** (green).

## 3. Write down the two web addresses

Open `followup-agent` in Render. Under its name is its address, for example
`https://followup-agent.onrender.com` or `https://followup-agent-abcd.onrender.com`. Do the same for
`followup-dashboard`. Below, these are **AGENT URL** and **DASHBOARD URL**.

Check the agent: open **AGENT URL**`/v1/health` in your browser. It should say `"status":"ok"`.

## 4. Tell Google about the new address

1. Go to **console.cloud.google.com → APIs & Services → Credentials** and click your OAuth client.
2. Under **Authorized redirect URIs**, click **Add URI** and enter **AGENT URL**`/oauth/google/callback`,
   for example `https://followup-agent.onrender.com/oauth/google/callback`. Keep the localhost one too.
3. Click **Save**. Google can take a few minutes to apply it.

## 5. Add the salesperson and connect the dashboard

1. In Render, open `followup-agent → Environment` and copy the value of `ADMIN_API_KEY` (click the eye icon).
2. On your Mac, open Terminal and run these lines one at a time. Replace the parts in capitals. Use the
   **sales Gmail** address both times.

   ```bash
   export AGENT=https://followup-agent.onrender.com        # your AGENT URL
   export DASH=https://followup-dashboard.onrender.com      # your DASHBOARD URL
   export ADMIN=PASTE_ADMIN_API_KEY_HERE

   curl -X POST $AGENT/v1/admin/users -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
     -d '{"email":"SALES_GMAIL_ADDRESS","name":"Your Name","timezone":"America/New_York"}'

   curl -X POST $AGENT/v1/admin/api-clients -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
     -d "{\"name\":\"Demo dashboard\",\"webhook_url\":\"$DASH/webhooks/agent\",\"user_emails\":[\"SALES_GMAIL_ADDRESS\"]}"
   ```

   The second command prints an `api_key` and a `webhook_secret`. Copy both now; they are only shown once.
3. In Render, open `followup-dashboard → Environment`, click **Edit**, and set:
   - `AGENT_BASE_URL` to your **AGENT URL** (no slash at the end)
   - `AGENT_API_KEY` to the `api_key`
   - `AGENT_WEBHOOK_SECRET` to the `webhook_secret`
4. Click **Save, rebuild, and deploy**. Wait until it shows **Live** again.

## 6. Run the demo on the server

1. Open your **DASHBOARD URL** and log in with your `DEMO_PASSWORD`.
2. Go to **Team → Connect Gmail** and sign in as the **sales Gmail** again. The server has its own database,
   so Gmail must be connected once here even though it was connected on your Mac.
3. From here it is the same as on your Mac: send an email from the sales Gmail, create a task, click
   **Run check now**, approve the draft, reply from the client Gmail, and click **Run check now** again.

From now on the agent checks every hour by itself, even with your Mac off. You can stop the Mac version.

## If something goes wrong

- **"redirect_uri_mismatch" from Google:** the URI in step 4 must exactly match **AGENT URL**`/oauth/google/callback`,
  starting with `https://`. Wait five minutes after saving and try again.
- **The dashboard doesn't update live after a send:** the `webhook_url` in step 5 must be your real
  **DASHBOARD URL** followed by `/webhooks/agent`. If it was wrong, run the `api-clients` command again with
  the right address and put the new `api_key` and `webhook_secret` into the dashboard (step 5.3).
- **A service shows "Failed":** open it, click **Logs**, and send the last red lines to Claude (they contain no
  keys).
- **No draft appears:** check the agent's **Logs**; the usual cause is an unfunded Anthropic key.
- **Gmail shows "reconnect" after a week:** normal while your Google app is in Testing mode. Connect again.

## Later: real clients

The server starts in demo mode (`TEST_MODE=true`), so it only emails the addresses on `TEST_ALLOWLIST`. To use
it with real clients, set `TEST_MODE` to `false` in `followup-agent → Environment`. Waiting days and office
hours then apply as normal.
