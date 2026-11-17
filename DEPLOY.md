# Putting the Lab LIMS on the internet

Both options run the same files. Pick ONE.

- **Option A - rent a cloud server (VPS).** Most control, one-time setup, cheapest.
- **Option B - hosting platform (Render or Fly.io).** No machine to look after, HTTPS included.

Rules for both:
1. Run exactly ONE copy of the app. The database is a single file and live sync lives in that one process. Never scale to 2+ instances.
2. Always use HTTPS. Never put the plain HTTP version on the internet.
3. The database must live on a persistent disk or volume. Without one, every restart erases your data.
4. Choose a region near you (London, Paris or Johannesburg are closest to Nigeria) and read the hosting company's data-protection terms. Patient data is sensitive; confirm Nigeria Data Protection Act requirements with a qualified adviser.
5. This package has not been run on a live host yet. Do the first deployment with test data, check everything in the "Test it" list at the bottom, and only then use real patients.

---

## Option A - Cloud server (Ubuntu VPS)

You need: a VPS running Ubuntu 22.04 or 24.04 (any small plan), and a domain name pointing at it. No domain? A free name from duckdns.org also works.

**1. Point the domain.** In your domain's DNS, add an `A` record: `lims.yourlab.com` to the server's IP address.

**2. Log in and prepare the server** (replace `youruser` where shown):

```
ssh root@SERVER_IP
apt update && apt install -y python3 ufw
adduser --disabled-password --gecos "" lims
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

**3. Copy the files.** From your own computer, in the folder that contains this package:

```
scp -r server.py public root@SERVER_IP:/home/lims/
ssh root@SERVER_IP "chown -R lims:lims /home/lims"
```

**4. Create the service.** On the server, create `/etc/systemd/system/lims.service`:

```
[Unit]
Description=Lab LIMS
After=network.target

[Service]
User=lims
WorkingDirectory=/home/lims
Environment=PORT=8080
Environment=LIMS_DATA=/home/lims/data
Environment=LIMS_OWNER_PASSWORD=PUT-A-LONG-RANDOM-PASSWORD-HERE
ExecStart=/usr/bin/python3 /home/lims/server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Then: `systemctl daemon-reload && systemctl enable --now lims`

Check it: `curl localhost:8080/health` should answer `{"ok": true}`.

**5. Add HTTPS with Caddy** (gets and renews certificates by itself):

```
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy.list
apt update && apt install -y caddy
```

Put this in `/etc/caddy/Caddyfile` (replace the name):

```
lims.yourlab.com {
    reverse_proxy localhost:8080 {
        flush_interval -1
    }
}
```

Then: `systemctl reload caddy`. Open `https://lims.yourlab.com`.

(`flush_interval -1` keeps the live-update connection open. If you use nginx instead, add `proxy_buffering off;` and `proxy_read_timeout 1h;`.)

**6. Sign in to the owner console** at `https://lims.yourlab.com/owner` with username `owner` and the password from step 4, then change it. Facilities register themselves on the main page (see "Running it as a multi-facility service" below).

**7. Get backups OFF the server.** The app backs itself up every 6 hours into `/home/lims/data/backups`, but that is the same disk. Copy them elsewhere, for example nightly from your own computer or another machine:

```
scp -r root@SERVER_IP:/home/lims/data/backups ./lims-backups-$(date +%F)
```

Also turn on your VPS provider's snapshot or backup option if it has one.

**Updating later:** copy the new `server.py` and `public/` over the old ones, then `systemctl restart lims`. Your data in `data/` is untouched.

**Restoring:** `systemctl stop lims`, replace `/home/lims/data/lims.db` with a file from `data/backups`, `chown lims:lims` it, `systemctl start lims`.

---

## Option B - Hosting platform

Both need a persistent disk or volume, which usually means a paid plan. Check each company's current plans and pricing before choosing.

### B0. Try it first on Render's FREE plan (demo only, data is erased)
See `RENDER-FREE-TEST.md` (included in this package). Never use the free plan for real patient data.

### B1. Render

1. Put this whole folder in a private repository on GitHub (Dockerfile, render.yaml, server.py, public/).
2. On render.com choose New, then Blueprint, pick the repository. It reads `render.yaml`, builds the Dockerfile and attaches a 1 GB disk at `/data`.
3. Open the service, Environment tab, and read the generated `LIMS_OWNER_PASSWORD`.
4. Open `https://....onrender.com/owner` (HTTPS is automatic), sign in as `owner` with that password and change it. Facilities register on the main address.
5. Optional: add your own domain under Settings, Custom Domains.

### B2. Fly.io

1. Install the `flyctl` tool and run `fly auth login`.
2. In this folder, edit `fly.toml` and change `app` to a unique name.
3. Run:
   ```
   fly launch --no-deploy --copy-config
   fly volumes create lims_data --size 1 --region lhr
   fly secrets set LIMS_OWNER_PASSWORD=PUT-A-LONG-RANDOM-PASSWORD-HERE
   fly deploy
   ```
4. Keep it at one machine: `fly scale count 1`.
5. Open `https://your-app-name.fly.dev/owner`, sign in as `owner`, and change the password. Facilities register on the main address.


### If your host asks for requirements.txt or a "Python" setup (instead of Docker)
Some platforms (Render "Python 3" service, Railway, Heroku-style hosts) run `pip install -r requirements.txt` first. The package now includes that file (it is empty on purpose, because the server needs nothing installed) and a `Procfile`. Use these settings:
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python server.py`
- **Environment variables:** `LIMS_OWNER_PASSWORD` (your chosen owner password) and `LIMS_DATA=/data`
- **Persistent disk:** add a disk mounted at `/data`. Without it, every restart or redeploy erases all facilities' data.
- Make sure `requirements.txt` sits in the same folder as `server.py` in the repository you connect (not inside an extra parent folder).

### Backups on a platform
The app still makes its own verified backups inside the disk. Also, every week, sign in as admin, open Backup & Security and press "Download latest backup", and keep that file somewhere else (flash drive, another computer). Treat these files as confidential: they contain all patient data.

To restore, you need shell access to the disk: on Render use the service Shell, on Fly use `fly ssh console`. Stop writing first (suspend the service), replace `/data/lims.db` with the backup file, then restart.

---

## Test it (do this once before real patients)

1. Open the address on your phone and on a computer, signed in as two different users.
2. Register a patient on one; it should appear on the other within a couple of seconds.
3. Create orders from both at the same time; the order numbers must be different.
4. Restart the service (or redeploy). Sign in again; all data must still be there. If it is gone, the disk is not attached correctly. Stop and fix that first.
5. In Backup & Security press "Back up now" and confirm it says verified.
6. Visit the address with `http://` instead of `https://` and confirm it redirects to HTTPS or refuses.

## Things to know
- If the internet drops, a device keeps edits to existing records and sends them when it reconnects. Creating new patients, orders or receipts needs the connection, because the server hands out the numbers.
- Lockout after repeated wrong passwords counts per username when a proxy sits in front, so a determined attacker can still lock a real user out for 10 minutes. Strong passwords matter.
- Whoever controls the server or hosting account can read all data. Protect that account with a strong password and two-factor sign-in.

---

## Running it as a multi-facility service (subscriptions)

**How it works**
- A facility opens your main address, presses "Register here", and creates its own account. That person becomes the facility administrator (there is no default admin). They then add their own staff and choose each person's role.
- Staff sign in with the **facility code**, their username and password.
- Every facility gets its own private database file in `data/tenants/<code>/`, with its own live sync, users, audit log and verified backups. Facilities cannot see each other's data.
- You sign in at `/owner` (username `owner`). You see each facility's status, plan, expiry, and counts of patients, orders and users. You do not see patient records. You can extend, suspend or reactivate a facility, record a payment, confirm bank transfers, reset a facility admin's password, and set prices.

**First setup, in the owner console**
1. Set the prices for Daily, Monthly and Annual. A plan with price 0 cannot be bought.
2. Type your bank details (shown to facilities when they choose "Pay by bank transfer").
3. Set the free-trial length for new facilities (default 7 days, 0 = none).

**What happens when a facility's time runs out**
- Everyone there can still sign in and read their data, but nothing new can be saved, and a red banner explains why. The administrator renews from the Subscription page, and the new time is added to whatever remains.
- Suspending a facility signs everyone out and blocks sign-in until you activate it.

**Paying**
- Bank transfer works out of the box: the facility administrator picks a plan, gets a reference, pays, and you press "Confirm received" in the owner console. Cash or transfers you receive directly can be entered with "Record payment".
- Online card payment (Paystack) is optional. Create a Paystack account, then set `PAYSTACK_SECRET` (your secret key) and `LIMS_PUBLIC_URL` (your https address) on the server, and in the Paystack dashboard set the webhook URL to `https://YOUR-ADDRESS/api/paystack/webhook`. The webhook check (signature, amount, no double-credit) is tested here; the call that starts a payment could not be tested without a live Paystack account, so run one small real test payment before you tell facilities to use it.

**Before you take real facilities' money and data**
- You become the operator of other organisations' patient data. Get written terms of service, a privacy policy and a data-processing agreement with each facility, and check the Nigeria Data Protection Act registration and security requirements with a qualified adviser. I am not a lawyer.
- Backups of every facility stay on the same host. Copy `data/tenants` to separate storage often.
- This version starts empty. Data saved by the earlier single-facility build is not carried over.

---

## Facility codes, roles and monitoring

**Facility codes.** A facility never types its own code. When it registers, the server builds one from the initials of the facility name plus a random four-digit number that no other facility has (for example "Darul Hikmah Lab" becomes DHL-4821), and shows it on a confirmation screen with a Copy button. Staff sign in with that code, their username and their password. If an administrator forgets the code, find it in the owner console (Facilities table, "Facility code" column) and press "Copy message" to send it to them.

**Roles inside a facility.** Administrator, Reception, Cashier, Phlebotomist, Medical Laboratory Technician (MLT), Medical Laboratory Scientist (MLS) and Auditor. An MLT can register patients, log samples, create orders, enter raw results and reject samples. Only an MLS (or the administrator) can type the result remarks and press "Validate & Release". Validated results are locked, and reports (print, PDF, JPG, send) are only available after validation. The server enforces this, so it cannot be bypassed from the browser.

**Passwords.** Staff who forget a password use "Forgot password?" on the sign-in page. Their administrator is notified on the dashboard and sets a temporary password under Users. A facility administrator who is locked out contacts you, and you reset it from the owner console ("Reset admin password").

**Monitoring.** The owner console has a Monitoring section: server uptime, requests and server errors in the last hour, failed sign-ins and lock-outs in the last 24 hours, people signed in now, a per-facility table and a list of recent security events. It shows activity only; there is no way to view a facility's patient records or to sign in as one of its users. Event addresses are deleted after 30 days. For raw traffic logs use your host's Logs page, and consider putting Cloudflare in front of the site for attack filtering. Sign-in tokens are stored on the server only as hashes.

---

## Support and legal pages (set up from the owner console)

**Support.** In the owner console, open "Support contacts shown to facilities" and fill in your WhatsApp number (with country code, for example 2348012345678), phone, email, support hours and any extra note, then press Save. They appear on the sign-in page ("Need help? Contact support", so people who cannot sign in can still reach you) and on every facility's Help & Support page, with one-tap WhatsApp, call and email buttons. Facilities can also send a support request from that page. You see every request in the owner console under "Support requests" with the facility, the person and their role; type a reply and press "Reply & resolve", and the facility sees your reply on its Help & Support page. Support requests still work when a facility's subscription has expired. The console does not email or message you when a request arrives, so check it daily or ask people to use WhatsApp for urgent problems.

**Privacy Policy and Terms of Service.** When your documents are ready, paste each one into the two boxes under "Privacy Policy and Terms of Service" in the owner console and press Publish. Formatting: a line starting with "# " is a heading, "## " a sub-heading, "- " a bullet, and a blank line starts a new paragraph. Publishing creates the public pages /privacy and /terms, adds links on the sign-in page and inside the app, and makes new facilities tick a box to accept them when they register (the acceptance and the document date are recorded in that facility's audit log). To change a document, edit the text and Publish again. To remove one, empty its box and Publish. These documents should be written or checked by a lawyer who knows the Nigeria Data Protection Act.

---

## Alerts to the owner (email and WhatsApp)

The platform can message you when something needs you: new support requests, bank-transfer payments waiting for confirmation (and online payments received), new facilities registering, failed backups, and security events (sign-in lock-outs, server errors, fake payment notices). Messages carry the facility name and a short subject only, never patient data or the text of a support request, and include a link to your owner console when `LIMS_PUBLIC_URL` is set. Security alerts are sent at most once every 15 minutes and failed-backup alerts at most once an hour, so you are not flooded.

**1. Turn on sending (server settings).** Keys and passwords are set as environment variables on your host, never in the browser.

*Email, option A (recommended; works on hosts that block mail ports, such as many free plans):* create an account at resend.com, make an API key, and add and verify your domain. Then set
- `RESEND_API_KEY` = your key
- `LIMS_MAIL_FROM` = `myLab LIMS <alerts@yourdomain.com>`

*Email, option B (your own mail account over SMTP):* set `LIMS_SMTP_HOST`, `LIMS_SMTP_PORT` (default 587), `LIMS_SMTP_USER`, `LIMS_SMTP_PASS` and `LIMS_MAIL_FROM`. Use `LIMS_SMTP_TLS=ssl` for port 465 (the default `starttls` suits port 587). If your host blocks outgoing mail ports, the test will time out; use option A instead.

*WhatsApp:* the built-in option uses CallMeBot, a free third-party service that sends a WhatsApp message to your own number. Follow the activation steps on callmebot.com (you message their bot from your WhatsApp and receive an API key), then set `CALLMEBOT_APIKEY`. It is not an official WhatsApp product and could change or stop working, and the alert text passes through their servers. The official route is Meta's WhatsApp Business Cloud API, which needs a verified business and pre-approved message templates; it can be added later if you outgrow this.

**2. Choose what you hear about (owner console).** Open the "Alerts to you" card, type your email address (up to three, separated by commas) and your WhatsApp number with country code, tick the events you want, press **Save alert settings**, then **Send a test alert**. The test reports for each channel whether it was sent or why it failed. Later failures also appear in the Monitoring list as ALERT_FAILED. The card shows "ready" or "not set up" for each channel so you can see what the server has.

**Please run the test yourself before relying on this.** The sending code was tested here against mock email and WhatsApp servers, not against the real Resend, SMTP or CallMeBot services, so only your own test message proves your accounts are set up correctly.

---

## Departments, partial release and editing orders

**Departments.** Every test belongs to a department: Haematology, Chemical Pathology, Serology & Immunology, Microbiology & Parasitology, Blood Bank or Other. A default is chosen from the test name, and the administrator can change it in the Test Catalogue (Department column). In Users, a Medical Laboratory Technician or Scientist can be tied to one or more departments; leave all unticked for access to every department. Staff with departments only see, accept, reject, enter and validate their own department's tests, and the server enforces it.

**Samples.** The phlebotomist records the sample for each department on an order (for example whole blood for Haematology, serum for Chemical Pathology). Each department's own staff then accepts or rejects it from the Samples button. A rejection affects only that department's tests; a scientist can undo it.

**Validation.** A scientist validates one test at a time ("Validate this test") or all ready tests in their department. Other tests on the same order stay pending and can be entered and validated later. Released tests are locked until a scientist revokes them. The order shows "Partly released" until every test is released.

**Printing.** "Print / export" lets you tick which released tests to include and choose one page per department (default), one page per test, or all together. Each page repeats the patient details and carries the signature of the scientist(s) who validated the tests on it. PDF, JPG and Send follow the same selection; with several pages, one JPG per page is saved.

**Editing an order.** "Edit order" (reception, technicians, scientists, administrator) adds tests to the same order after it has been created or paid for. The total updates and the cashier sees the new balance. Tests that already have results cannot be removed.
