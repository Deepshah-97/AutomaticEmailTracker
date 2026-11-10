# Put the Follow-Up Agent on a server (Render)

This puts the agent and the demo dashboard on Render, and the database on Supabase, so they keep running when
your Mac is off. Allow about 30 minutes. It costs **$0**: the database is on Supabase's free plan, and the agent and dashboard
are on Render's free plan.

The catch with the free plan: when nobody has used an app for 15 minutes, Render puts it to sleep. The next
visit wakes it, which takes up to a minute, and you log in to the dashboard again. While the agent is asleep it
does no hourly Gmail checks and sends nothing, so it only works while you are using it. That is fine for a live
demo. **Before a demo**, open **AGENT URL**`/v1/health` and your **DASHBOARD URL** a couple of minutes early so
both are awake. See "Later: real clients" for the one change to make before using it for real.

You never need to paste a key into a chat. Keys go only into Render, Supabase, Google Cloud and your own
Terminal.

## 0. Create the free database (Supabase)

1. Go to **supabase.com**, click **Start your project**, and sign up (GitHub sign-in is fine).
2. Click **New project**. Name it `followup`, click **Generate a password**, and copy that password somewhere
   safe on your Mac. Pick the region closest to **US West (Oregon)**, and click **Create new project**. Wait
   about a minute.
3. Click **Connect** at the top of the project page. Under **Connection string**, choose the **Session
   pooler** one (not "Direct connection"). Copy it.
4. In that copied text, replace `[YOUR-PASSWORD]` with the password from step 2. This full line is your
   **DATABASE URL**. Keep it handy for the next part.

## 1. Create a Render account

1. Go to **render.com** and click **Get Started**. Sign up with **GitHub**.
2. When GitHub asks which repositories Render can see, choose **Only select repositories** and pick both
   **AutomaticEmailTracker** and **DemoTracker**.
3. You don't need to add a card while everything is on the free plan. Render asks for one only when you
   switch the agent to a paid plan later.

## 2. Create everything from the Blueprint

1. In Render, click **New → Blueprint**.
2. Pick the **AutomaticEmailTracker** repository and click **Connect**. Render reads the `render.yaml` file
   and lists two things: `followup-agent` and `followup-dashboard`.
3. Give the Blueprint any name, such as `followup`.
4. Render asks for a few values. Fill them in like this:

   | Setting | What to type |
   |---|---|
   | `DATABASE_URL` | Your **DATABASE URL** from step 0 |
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
- **The dashboard shows an error about the agent right after waking up:** the agent was still asleep. Wait a
  minute and reload the page.
- **No draft appears:** check the agent's **Logs**; the usual cause is an unfunded Anthropic key.
- **The agent fails with "could not connect" or "password authentication failed":** check `DATABASE_URL`
  in `followup-agent → Environment`. It must be the **Session pooler** string with your real password in it.
- **Supabase says the project is paused:** Supabase pauses free projects after a week with no use. Open the
  project at supabase.com and click **Restore**, then wait a couple of minutes before opening the dashboard.
- **Gmail shows "reconnect" after a week:** normal while your Google app is in Testing mode. Connect again.

## Later: real clients

`TEST_ALLOWLIST` is only for the demo. You never add client addresses to it. While `TEST_MODE` is `true`, the
agent can only email the addresses on that list, which keeps the demo from reaching anyone real. When
`TEST_MODE` is `false`, the list is ignored and the agent follows up with whoever is on the thread you pick
when you create a task.

To switch to real clients:

1. In Render, open `followup-agent → Settings → Instance Type`, choose the smallest paid one (about $7 a
   month), and save. A free agent sleeps and would miss the hourly checks and scheduled sends.
2. In Render, open `followup-agent → Environment` and click **Edit**.
3. Change `TEST_MODE` from `true` to `false`. You can leave `TEST_ALLOWLIST` as it is.
4. Click **Save, rebuild, and deploy** and wait until it shows **Live**.

What changes once it is off:

- **Normal waits apply.** A task waits its scenario's business days before the first follow-up, so
  **Run check now** won't produce a draft straight away like it does in the demo.
- **Office hours apply.** Emails go out only between 08:30 and 17:30 on weekdays, in the salesperson's time
  zone, with a short random delay of up to 40 minutes so they don't look automated.
- **The daily cap applies:** at most 50 sends per salesperson per day.

The only people you add are **salespeople** (the people whose Gmail sends the follow-ups), using the
`/v1/admin/users` command from step 5. Each one then connects their Gmail from the dashboard's **Team** page.
Clients are never added anywhere.
