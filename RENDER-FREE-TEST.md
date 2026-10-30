# Test the platform on Render's FREE plan (demo only)

IMPORTANT: Render's free plan has no permanent storage and puts an idle service to sleep.
Everything saved (facilities, patients, payments, settings) is ERASED whenever the service restarts,
redeploys, or wakes from sleep. Use it ONLY to try the screens with fake data.
Real facilities need the paid disk setup (render.yaml in the lims-cloud package).

## 1. Put the files on GitHub (files must be at the TOP of the repository)
1. Create a free account at github.com, then New repository (private is fine), for example: mylab-lims.
2. Press "uploading an existing file" and drag in everything from THIS folder:
   server.py, requirements.txt, Procfile, and the public folder.
   Do not drag in a folder that contains them: server.py and requirements.txt must sit directly in the repository root.
3. Press Commit changes.

## 2. Create the service on Render
1. render.com, sign in with GitHub, New +, Web Service, choose your repository.
2. Fill in:
   - Name: mylab-lims (becomes part of your web address)
   - Region: the closest to you (for example Frankfurt)
   - Branch: main
   - Runtime / Language: Python 3
   - Build Command:  pip install -r requirements.txt
   - Start Command:  python server.py
   - Instance Type: Free
3. Environment Variables (Add):
   - LIMS_OWNER_PASSWORD = a password you choose for yourself (long, not used elsewhere)
   - LIMS_DEMO = 1        (shows a "demo, data may be erased" warning on every screen)
4. Under Advanced, set Health Check Path to:  /health
5. Press Create Web Service and wait until the log says the service is live.

## 3. Try it
- Your owner console:  https://YOUR-NAME.onrender.com/owner   (username: owner)
- Facilities register at:  https://YOUR-NAME.onrender.com
- In the owner console set the prices and bank details first, then register a test facility and walk through:
  create staff, enter a patient and order on two devices, open Subscription, request a bank-transfer payment,
  and confirm it from the owner console.

## 4. Sleeping and data loss
- After about 15 minutes with no visitors the free service sleeps. The next visit takes up to a minute to wake it,
  and the data is gone, so facilities must register again.
- A free monitor such as UptimeRobot that opens https://YOUR-NAME.onrender.com/health every 5 minutes reduces sleeping.
  It does NOT make the data permanent: any restart or redeploy still erases it. Check Render's current free-plan terms.

## 5. When the test looks good
Move to the paid plan with a disk: use the lims-cloud package (render.yaml) or add a Disk at /data to this service,
set LIMS_DATA=/data, and delete LIMS_DEMO. Only then should real facilities use it.
