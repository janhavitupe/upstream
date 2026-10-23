# A permanent database (Neon, free)

Render's free plan wipes the app's disk on every deploy and restart. Locally Upstream keeps its
data in a SQLite file (`data/upstream.db`); online it should use PostgreSQL, so payroll schedules,
invoices being chased, retainers and campaigns survive. Upstream switches automatically when
`DATABASE_URL` is set.

## 1. Create the database (about 3 minutes, no card)

1. Go to **https://neon.tech** → **Sign up** (GitHub sign-in is fine).
2. Create a project: name `upstream`, any recent Postgres version, the region closest to your
   Render service (e.g. AWS US East if Render is in Ohio/Virginia, or Singapore for Asia).
3. On the project dashboard, click **Connect** and copy the **connection string**. It looks like:
   ```
   postgresql://neondb_owner:AbC123...@ep-cool-name-123456.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
   Keep `?sslmode=require` at the end.

## 2. Give it to Render

Render → your service → **Environment** → **Add Environment Variable**:

| Key | Value |
|---|---|
| `DATABASE_URL` | the connection string |

**Save, rebuild, and deploy.** Upstream creates its tables on the first start.

## 3. Check

Open `https://<your-render-url>/health`. It should show `"database": "postgres"`.
Create something (an invoice, a campaign), push any commit so Render redeploys, and check it's
still there.

## Notes

- **Locally**, leave `DATABASE_URL` empty in `.env` to keep using the SQLite file. If you set it,
  your local app and Render share the same data.
- Neon's free tier sleeps when idle; the first request after a while takes a second or two longer.
- Uploaded bill files (Verified causes) are still stored on Render's disk, which is wiped on
  deploy. Their verified results, checks and donations are in the database and stay; only the
  original image would need re-uploading to re-check a campaign.
- The tests never use `DATABASE_URL`. To run the suite on PostgreSQL, point `TEST_DATABASE_URL`
  at a separate database whose name contains `test` (every test wipes it).
